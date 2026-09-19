import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from datetime import datetime,timedelta,timezone

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
import study


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"study.db"
        study.build_schema(self.path)
        self.sid = study.upsert_source("test:one","Episode 1","Test","video",path=self.path)
        self.other = study.upsert_source("test:two","Episode 2","Test","audio",path=self.path)
        with study.connect(self.path) as db:
            for sid in (self.sid,self.other):
                for i,text in enumerate(["平和だなあ","普通の人生","大学を出て"]):
                    db.execute("INSERT INTO cues(source_id,ordinal,start,end,raw_text,text) VALUES(?,?,?,?,?,?)",
                               (sid,i,10+i*4,13+i*4,text,text))
            self.cues = [r[0] for r in db.execute("SELECT id FROM cues WHERE source_id=? ORDER BY ordinal",(self.sid,))]
            self.foreign = db.execute("SELECT id FROM cues WHERE source_id=?",(self.other,)).fetchone()[0]
        app = FastAPI();app.include_router(study.create_router(self.path))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close();self.tmp.cleanup()

    def test_explanations_use_source_text_and_reject_foreign_spans(self):
        with patch('explanations.start',return_value='a'*64) as start:
            response=self.client.post('/api/study/explanations',json={'cue_id':self.cues[0],'end_cue_id':self.cues[1]})
            self.assertEqual(response.status_code,202)
            start.assert_called_once_with('平和だなあ 普通の人生')
        response=self.client.post('/api/study/explanations',json={'cue_id':self.cues[0],'end_cue_id':self.foreign})
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.client.get('/api/study/explanations/not-a-key').status_code,404)

    def event(self, **kwargs):
        return study.Event(**dict(event_id=uuid.uuid4(),source_id=self.sid,action="watch",
                                  position=10,end_position=20,seconds=10,**kwargs))

    def test_captions_preserve_raw_and_clean_reading_annotations(self):
        cues=study.parse_subtitles("\ufeffWEBVTT\n\n00:01.000 --> 00:03.500 align:start\n（三上）防空壕(ぼうくうごう) <i>へ</i>\n\n00:04.000 --> 00:03.000\ninvalid")
        self.assertEqual(len(cues),1)
        self.assertEqual(cues[0]["text"],"防空壕 へ")
        self.assertIn("（三上）",cues[0]["raw_text"])
        self.assertEqual(cues[0]["end"],3.5)

    def test_srt_and_overlapping_cues(self):
        cues=study.parse_subtitles("1\n00:00:10,000 --> 00:00:12,000\n一\n\n2\n00:00:11,000 --> 00:00:13,000\n二")
        self.assertEqual([c['start'] for c in cues],[10,11])

    def test_capture_is_idempotent_and_preserves_source_span(self):
        req=study.CaptureRequest(cue_id=self.cues[0],end_cue_id=self.cues[1],kind="grammar",expression="だなあ")
        a=study.capture(req,self.path);b=study.capture(req,self.path)
        self.assertEqual(a["id"],b["id"])
        self.assertEqual(a["context"],"平和だなあ 普通の人生")
        self.assertEqual((a["start"],a["end"]),(10,17))

    def test_cross_source_capture_rejected(self):
        with self.assertRaises(HTTPException):
            study.capture(study.CaptureRequest(cue_id=self.cues[0],end_cue_id=self.foreign),self.path)

    def test_event_retry_does_not_double_count(self):
        event=self.event()
        self.assertEqual(study.record_events([event],self.path)["accepted"],1)
        self.assertEqual(study.record_events([event],self.path)["accepted"],0)
        with study.connect(self.path) as db:
            r=db.execute("SELECT * FROM progress").fetchone()
            self.assertEqual((r['position'],r['seconds']),(20,10))

    def test_late_offline_event_does_not_rewind_resume(self):
        fresh=self.event(occurred_at=datetime.now(timezone.utc))
        old=self.event(occurred_at=datetime.now(timezone.utc)-timedelta(days=1)).model_copy(update={'position':0,'end_position':10})
        study.record_events([fresh,old],self.path)
        with study.connect(self.path) as db:
            p=db.execute('SELECT * FROM progress').fetchone()
            self.assertEqual(p['position'],20);self.assertEqual(p['seconds'],20)

    def test_invalid_batch_rolls_back(self):
        invalid=self.event().model_copy(update={'source_id':9999})
        with self.assertRaises(HTTPException):study.record_events([self.event(),invalid],self.path)
        with study.connect(self.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM activity').fetchone()[0],0)

    def test_seek_cannot_inflate_watched_time(self):
        with self.assertRaises(ValidationError):
            study.Event(event_id=uuid.uuid4(),source_id=self.sid,action='watch',position=0,end_position=500,seconds=10)

    def test_shadow_requires_cue_and_does_not_count_as_playback(self):
        event=study.Event(event_id=uuid.uuid4(),source_id=self.sid,action='shadow',position=10,end_position=13,seconds=0,cue_id=self.cues[0])
        study.record_events([event],self.path)
        with study.connect(self.path) as db:self.assertIsNone(db.execute('SELECT * FROM progress').fetchone())
        with self.assertRaises(HTTPException):study.record_events([event.model_copy(update={'event_id':uuid.uuid4(),'cue_id':self.foreign})],self.path)

    def test_revisit_retry_and_intervals(self):
        capture=study.capture(study.CaptureRequest(cue_id=self.cues[0]),self.path)
        event_id=uuid.uuid4()
        a=study.complete_revisit(capture['id'],event_id,self.path)
        b=study.complete_revisit(capture['id'],event_id,self.path)
        self.assertEqual(a,b);self.assertEqual(a['revisit_count'],1)
        self.assertGreater(a['next_review_at'],capture['next_review_at'])

    def test_api_validation_and_source_cues(self):
        r=self.client.get(f'/api/study/sources/{self.sid}')
        self.assertEqual(r.status_code,200);self.assertEqual(len(r.json()['cues']),3)
        self.assertNotIn('media_path',r.json()['source'])
        r=self.client.post('/api/study/events',json={'events':[{'event_id':str(uuid.uuid4()),'source_id':self.sid,'action':'watch','position':0,'end_position':5,'seconds':-1}]})
        self.assertEqual(r.status_code,422)

    def test_remove_capture_keeps_context(self):
        c=study.capture(study.CaptureRequest(cue_id=self.cues[0]),self.path)
        self.assertEqual(self.client.delete(f"/api/study/captures/{c['id']}").status_code,200)
        with study.connect(self.path) as db:
            row=db.execute('SELECT * FROM captures WHERE id=?',(c['id'],)).fetchone()
            self.assertEqual(row['archived'],1);self.assertEqual(row['context'],'平和だなあ')
        restored=study.capture(study.CaptureRequest(cue_id=self.cues[0]),self.path)
        self.assertEqual(restored['id'],c['id']);self.assertEqual(restored['archived'],0)

    def test_youtube_catalog_rejects_other_hosts_and_keeps_identity(self):
        import media_ingest
        with self.assertRaises(ValueError):media_ingest.catalog_youtube('http://localhost/private',self.path)
        a=media_ingest.catalog_youtube('https://youtu.be/7pfQlHa0LWM',self.path)
        b=media_ingest.catalog_youtube('https://www.youtube.com/watch?v=7pfQlHa0LWM',self.path)
        self.assertEqual(a,b)

    def test_upload_rejects_untimed_transcript_before_creating_source(self):
        r=self.client.post('/api/study/upload',data={'title':'test','kind':'song'},files={
            'media':('test.mp3',b'not audio','audio/mpeg'),
            'transcript':('test.txt',b'untimed text','text/plain')})
        self.assertEqual(r.status_code,422)
        with study.connect(self.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM sources').fetchone()[0],2)


if __name__=='__main__':unittest.main()
