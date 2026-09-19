"""Source-anchored media, captures, and activity. Independent of Anki scheduling.

The source is stable; cues and captures retain the original text. Activity is
idempotent and replay suggestions are practice prompts, never memory grades.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data/study.db"
MEDIA_DIR = ROOT / "data/study_media"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path: Path = DB_PATH):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def build_schema(path: Path = DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
          id INTEGER PRIMARY KEY, external_key TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL, collection TEXT NOT NULL DEFAULT '',
          kind TEXT NOT NULL CHECK(kind IN ('video','audio','song','reading')),
          metadata TEXT NOT NULL DEFAULT '{}', media_path TEXT,
          status TEXT NOT NULL DEFAULT 'catalogued', error TEXT,
          duration REAL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cues (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_id INTEGER NOT NULL REFERENCES sources(id), ordinal INTEGER NOT NULL,
          start REAL NOT NULL, end REAL NOT NULL, raw_text TEXT NOT NULL,
          text TEXT NOT NULL, tokens TEXT NOT NULL DEFAULT '[]',
          UNIQUE(source_id,ordinal), CHECK(start>=0 AND end>start)
        );
        CREATE TABLE IF NOT EXISTS activity (
          event_id TEXT PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
          action TEXT NOT NULL, position REAL NOT NULL, end_position REAL NOT NULL,
          seconds REAL NOT NULL, cue_id INTEGER REFERENCES cues(id),
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS activity_source ON activity(source_id,created_at);
        CREATE TABLE IF NOT EXISTS progress (
          source_id INTEGER PRIMARY KEY REFERENCES sources(id),
          position REAL NOT NULL DEFAULT 0, furthest REAL NOT NULL DEFAULT 0,
          seconds REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS captures (
          id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
          cue_id INTEGER NOT NULL REFERENCES cues(id), end_cue_id INTEGER NOT NULL REFERENCES cues(id),
          kind TEXT NOT NULL, expression TEXT NOT NULL, context TEXT NOT NULL,
          start REAL NOT NULL, end REAL NOT NULL, lemma TEXT NOT NULL DEFAULT '',
          reading TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, next_review_at TEXT NOT NULL,
          revisit_count INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
          UNIQUE(source_id,cue_id,end_cue_id,kind,expression)
        );
        CREATE TABLE IF NOT EXISTS revisits (
          event_id TEXT PRIMARY KEY, capture_id INTEGER NOT NULL REFERENCES captures(id),
          created_at TEXT NOT NULL
        );
        """)


def clean_subtitle(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>|\{[^}]*\}", "", value))
    value = re.sub(r"(?<=[一-鿿々])\([ぁ-ゖァ-ヺー]+\)", "", value)
    value = re.sub(r"（[^）]*）", "", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_subtitles(value: str) -> list[dict]:
    """Read SRT/WebVTT. Keep annotations in raw_text, clean only the study view."""
    stamp = r"(?:(\d{1,3}):)?(\d{2}):(\d{2})[,.](\d{3})"
    timing = re.compile(stamp + r"\s*-->\s*" + stamp)
    def seconds(parts):
        h, m, s, ms = parts
        return int(h or 0)*3600 + int(m)*60 + int(s) + int(ms)/1000
    result = []
    for block in re.split(r"\n\s*\n", value.lstrip("\ufeff").replace("\r\n", "\n")):
        lines = block.splitlines()
        for i, line in enumerate(lines):
            match = timing.search(line)
            if not match:
                continue
            start, end = seconds(match.groups()[:4]), seconds(match.groups()[4:])
            raw = "\n".join(lines[i+1:]).strip()
            text = clean_subtitle(raw)
            if end > start and raw:
                result.append(dict(start=start, end=end, raw_text=raw, text=text))
            break
    return sorted(result, key=lambda c: (c["start"], c["end"]))


def upsert_source(key, title, collection, kind, metadata=None, path=DB_PATH):
    with connect(path) as db:
        db.execute("""INSERT INTO sources(external_key,title,collection,kind,metadata,created_at)
          VALUES(?,?,?,?,?,?) ON CONFLICT(external_key) DO UPDATE SET
          title=excluded.title,collection=excluded.collection,metadata=excluded.metadata""",
          (key,title,collection,kind,json.dumps(metadata or {}, ensure_ascii=False),now()))
        return db.execute("SELECT id FROM sources WHERE external_key=?", (key,)).fetchone()[0]


def install_media(source_id, media_path, subtitles, duration, path=DB_PATH):
    import fugashi
    import dictionary
    tagger = fugashi.Tagger()
    cues = parse_subtitles(subtitles) if subtitles else []
    if subtitles and not cues:
        raise ValueError("No timed cues found. Use an SRT or WebVTT transcript.")
    for cue in cues:
        cue["tokens"] = dictionary.tokenize(cue["text"], tagger) if cue["text"] else []
    with connect(path) as db:
        # Existing anchors must never be reassigned under saved captures.
        if not db.execute("SELECT 1 FROM cues WHERE source_id=?", (source_id,)).fetchone():
            db.executemany("""INSERT INTO cues(source_id,ordinal,start,end,raw_text,text,tokens)
                VALUES(?,?,?,?,?,?,?)""", [(source_id,i,c["start"],c["end"],c["raw_text"],c["text"],
                json.dumps(c["tokens"],ensure_ascii=False)) for i,c in enumerate(cues)])
        db.execute("UPDATE sources SET media_path=?,duration=?,status='ready',error=NULL WHERE id=?",
                   (str(media_path),duration,source_id))


class Event(BaseModel):
    event_id: uuid.UUID
    source_id: int = Field(gt=0)
    action: str = Field(pattern=r"^(watch|listen|read|replay|shadow|sing)$")
    position: float = Field(ge=0, allow_inf_nan=False)
    end_position: float = Field(ge=0, allow_inf_nan=False)
    seconds: float = Field(ge=0, le=30, allow_inf_nan=False)
    cue_id: int | None = None
    occurred_at: datetime | None = None

    @model_validator(mode="after")
    def valid_span(self):
        if self.action in ("watch", "listen", "replay"):
            if not 0 <= self.end_position-self.position <= self.seconds*4+1:
                raise ValueError("Playback events must describe played spans, not seeks")
        if self.action in ("shadow", "sing") and self.cue_id is None:
            raise ValueError("Mark a subtitle span for shadowing or singing")
        if self.action in ("shadow", "sing") and self.seconds != 0:
            raise ValueError("Practice marks do not measure playback time")
        if self.occurred_at is not None:
            if self.occurred_at.tzinfo is None:
                raise ValueError("Activity time needs a timezone")
            if self.occurred_at > datetime.now(timezone.utc)+timedelta(minutes=5):
                raise ValueError("Activity time is in the future")
        return self


class Events(BaseModel):
    events: list[Event] = Field(max_length=100)


def record_events(events: list[Event], path=DB_PATH):
    added = 0
    with connect(path) as db:
        for e in events:
            source = db.execute("SELECT kind,duration FROM sources WHERE id=?", (e.source_id,)).fetchone()
            if not source:
                raise HTTPException(404, "Source not found")
            if e.action == "read" and source["kind"] != "reading":
                raise HTTPException(422, "Reading activity requires a reading source")
            if source["kind"] == "reading" and e.action != "read":
                raise HTTPException(422, "Use reading activity for this source")
            if source["duration"] and max(e.position,e.end_position) > source["duration"]+2:
                raise HTTPException(422, "Position exceeds source duration")
            if e.cue_id is not None and not db.execute(
                "SELECT 1 FROM cues WHERE id=? AND source_id=?", (e.cue_id,e.source_id)).fetchone():
                raise HTTPException(422, "Cue does not belong to source")
            stamp = e.occurred_at.astimezone(timezone.utc).isoformat() if e.occurred_at else now()
            inserted = db.execute("""INSERT OR IGNORE INTO activity
                VALUES(?,?,?,?,?,?,?,?)""", (str(e.event_id),e.source_id,e.action,e.position,
                e.end_position,e.seconds,e.cue_id,stamp)).rowcount
            if not inserted:
                continue
            added += 1
            # Explicit practice marks do not inflate playback time or move resume.
            if e.action not in ("shadow","sing"):
                db.execute("""INSERT INTO progress VALUES(?,?,?,?,?) ON CONFLICT(source_id)
                  DO UPDATE SET position=CASE WHEN excluded.updated_at>=progress.updated_at
                  THEN excluded.position ELSE progress.position END,furthest=MAX(progress.furthest,excluded.furthest),
                  seconds=progress.seconds+excluded.seconds,updated_at=MAX(progress.updated_at,excluded.updated_at)""",
                  (e.source_id,e.end_position,max(e.position,e.end_position),e.seconds,stamp))
    return {"accepted": added}


class CaptureRequest(BaseModel):
    cue_id: int
    end_cue_id: int | None = None
    kind: str = Field(default="phrase", pattern=r"^(word|phrase|grammar|lyric)$")
    expression: str = Field(default="", max_length=2000)
    lemma: str = Field(default="", max_length=200)
    reading: str = Field(default="", max_length=200)


def capture(req: CaptureRequest, path=DB_PATH):
    with connect(path) as db:
        cue = db.execute("SELECT * FROM cues WHERE id=?", (req.cue_id,)).fetchone()
        end = db.execute("SELECT * FROM cues WHERE id=?", (req.end_cue_id or req.cue_id,)).fetchone()
        if not cue or not end:
            raise HTTPException(404, "Subtitle not found")
        if cue["source_id"] != end["source_id"] or not 0 <= end["ordinal"]-cue["ordinal"] <= 20:
            raise HTTPException(422, "Choose a continuous span of up to 21 subtitles")
        rows = db.execute("SELECT text FROM cues WHERE source_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
                          (cue["source_id"],cue["ordinal"],end["ordinal"])).fetchall()
        context = " ".join(r[0] for r in rows)
        expression = req.expression.strip() or context
        if not expression or len(expression)>2000:
            raise HTTPException(422, "Choose a shorter spoken expression")
        # Snapshot the real source context, not client-supplied text/location.
        due = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        db.execute("""INSERT INTO captures(source_id,cue_id,end_cue_id,kind,expression,
          context,start,end,lemma,reading,created_at,next_review_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(source_id,cue_id,end_cue_id,kind,expression) DO UPDATE SET archived=0""",
          (cue["source_id"],cue["id"],end["id"],req.kind,expression,context,cue["start"],end["end"],
           req.lemma,req.reading,now(),due))
        row = db.execute("""SELECT * FROM captures WHERE source_id=? AND cue_id=? AND end_cue_id=?
          AND kind=? AND expression=?""", (cue["source_id"],cue["id"],end["id"],req.kind,expression)).fetchone()
        return dict(row)


class RevisitRequest(BaseModel):
    event_id: uuid.UUID


def complete_revisit(capture_id, event_id, path=DB_PATH):
    with connect(path) as db:
        row = db.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Capture not found")
        if db.execute("INSERT OR IGNORE INTO revisits VALUES(?,?,?)", (str(event_id),capture_id,now())).rowcount:
            count = row["revisit_count"]+1
            delay = [1,3,7,14][min(count,3)]
            due = (datetime.now(timezone.utc)+timedelta(days=delay)).isoformat()
            db.execute("UPDATE captures SET revisit_count=?,next_review_at=? WHERE id=?", (count,due,capture_id))
        return dict(db.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone())


def reading_source(work_id, path=DB_PATH):
    old = ROOT / "data/wakatta.db"
    if not old.exists():
        raise HTTPException(404, "Reading library not found")
    db = sqlite3.connect(old.as_uri()+"?mode=ro", uri=True)
    try:
        work = db.execute("SELECT title FROM works WHERE id=?", (work_id,)).fetchone()
        pages = db.execute("SELECT count(*) FROM pages WHERE work_id=?", (work_id,)).fetchone()[0]
    finally:
        db.close()
    if not work:
        raise HTTPException(404, "Work not found")
    return upsert_source(f"reading:{work_id}",work[0].split(" -- ")[0],"Manga library","reading",{"work_id":work_id,"pages":pages},path)


def source_dict(row):
    d = dict(row)
    meta = json.loads(d.pop("metadata"))
    d.pop("media_path",None)
    d.pop("external_key",None)
    d["metadata"] = {k:v for k,v in meta.items() if k not in ("remote_path","subtitle_path","host")}
    d["metadata"]["has_transcript"] = bool(meta.get("subtitle_path"))
    return d


def create_router(path: Path = DB_PATH):
    router = APIRouter()

    @router.get("/library")
    def library_page():
        return FileResponse(ROOT / "static/library.html")

    @router.get("/watch/{source_id}")
    def watch_page(source_id: int):
        return FileResponse(ROOT / "static/watch.html")

    @router.get("/api/study/library")
    def library():
        # Include existing reading material in the same library without migrating it.
        old = ROOT / "data/wakatta.db"
        if old.exists():
            db = sqlite3.connect(old.as_uri()+"?mode=ro",uri=True)
            try:
                ids = db.execute("SELECT id FROM works").fetchall()
            except sqlite3.OperationalError:
                ids = []
            finally:
                db.close()
            for (wid,) in ids:
                reading_source(wid,path)
        with connect(path) as db:
            sources = [source_dict(r) for r in db.execute("""SELECT s.*,p.position,p.furthest,p.seconds,p.updated_at,
              (SELECT count(*) FROM captures c WHERE c.source_id=s.id AND archived=0) capture_count
              FROM sources s LEFT JOIN progress p ON p.source_id=s.id ORDER BY s.collection,s.id""")]
            captures = [dict(r) for r in db.execute("""SELECT c.*,s.title,s.collection,s.kind source_kind
                FROM captures c JOIN sources s ON s.id=c.source_id WHERE archived=0
                ORDER BY c.next_review_at,c.id DESC""")]
            activities = [dict(r) for r in db.execute("""SELECT a.*,s.title FROM activity a JOIN sources s
                ON s.id=a.source_id WHERE action IN ('shadow','sing') ORDER BY created_at DESC LIMIT 30""")]
        return {"sources":sources,"captures":captures,"practice":activities,"now":now()}

    @router.get("/api/study/sources/{source_id}")
    def source(source_id: int):
        with connect(path) as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not row:
                raise HTTPException(404,"Source not found")
            cues = []
            for r in db.execute("SELECT * FROM cues WHERE source_id=? ORDER BY ordinal", (source_id,)):
                d = dict(r); d["tokens"] = json.loads(d["tokens"]); cues.append(d)
            progress = db.execute("SELECT * FROM progress WHERE source_id=?", (source_id,)).fetchone()
            captures = [dict(r) for r in db.execute("SELECT * FROM captures WHERE source_id=? AND archived=0",(source_id,))]
            meta = json.loads(row['metadata'])
            next_source = db.execute("""SELECT id FROM sources WHERE collection=? AND
              json_extract(metadata,'$.season')=? AND json_extract(metadata,'$.episode')=? LIMIT 1""",
              (row['collection'],meta.get('season'),(meta.get('episode') or 0)+1)).fetchone()
            return {"source":source_dict(row),"cues":cues,"progress":dict(progress) if progress else None,
                    "captures":captures,"next_source_id":next_source[0] if next_source else None}

    @router.get("/api/study/sources/{source_id}/media")
    def media(source_id: int):
        with connect(path) as db:
            row = db.execute("SELECT media_path,status FROM sources WHERE id=?",(source_id,)).fetchone()
        if not row or row["status"]!="ready" or not row["media_path"]:
            raise HTTPException(404,"Media not prepared")
        file = Path(row["media_path"]).resolve()
        if not file.is_relative_to(MEDIA_DIR.resolve()) or not file.is_file():
            raise HTTPException(404,"Media file unavailable")
        return FileResponse(file)  # Starlette handles byte ranges for seeking.

    @router.post("/api/study/sources/{source_id}/prepare", status_code=202)
    def prepare(source_id: int):
        import media_ingest
        return media_ingest.start_prepare(source_id,path)

    @router.post("/api/study/youtube", status_code=202)
    def youtube(req: YouTubeRequest):
        import media_ingest
        try:
            sid = media_ingest.catalog_youtube(req.url,path)
        except ValueError as e:
            raise HTTPException(422,str(e)) from e
        media_ingest.start_prepare(sid,path)
        return {"source_id":sid}

    @router.post("/api/study/events")
    def events(req: Events):
        return record_events(req.events,path)

    @router.get("/api/study/reading/{work_id}")
    def reading(work_id: int):
        sid = reading_source(work_id,path)
        with connect(path) as db:
            p = db.execute("SELECT * FROM progress WHERE source_id=?",(sid,)).fetchone()
        return {"source_id":sid,"progress":dict(p) if p else None}

    @router.post("/api/study/captures", status_code=201)
    def save_capture(req: CaptureRequest):
        return capture(req,path)

    @router.post("/api/study/captures/{capture_id}/revisit")
    def revisit(capture_id: int, req: RevisitRequest):
        return complete_revisit(capture_id,req.event_id,path)

    @router.delete("/api/study/captures/{capture_id}")
    def archive(capture_id: int):
        with connect(path) as db:
            if not db.execute("UPDATE captures SET archived=1 WHERE id=?",(capture_id,)).rowcount:
                raise HTTPException(404,"Capture not found")
        return {"archived":True}

    @router.post("/api/study/upload", status_code=201)
    async def upload(title: str = Form(...), collection: str = Form("My listening"),
                     kind: str = Form("audio"), media: UploadFile = File(...),
                     transcript: UploadFile | None = File(None)):
        if kind not in ("audio","video","song") or not 0<len(title.strip())<=200:
            raise HTTPException(422,"Choose a title and audio, video, or song")
        suffix = Path(media.filename or "").suffix.lower()
        if suffix not in (".mp4",".m4a",".mp3",".wav",".ogg",".webm"):
            raise HTTPException(422,"Use MP4/WebM video or MP3/M4A/WAV/OGG audio")
        sub = ""
        if transcript:
            data = await transcript.read(5_000_001)
            if len(data)>5_000_000:
                raise HTTPException(413,"Transcript too large")
            sub = data.decode("utf-8-sig",errors="replace")
            if not parse_subtitles(sub):
                raise HTTPException(422,"Transcript must be timed SRT or WebVTT")
        MEDIA_DIR.mkdir(parents=True,exist_ok=True)
        dest = MEDIA_DIR/(str(uuid.uuid4())+suffix)
        total = 0
        sid = None
        try:
            with dest.open("wb") as output:
                while chunk := await media.read(1024*1024):
                    total += len(chunk)
                    if total>2_000_000_000:
                        raise HTTPException(413,"Media limit is 2 GB")
                    output.write(chunk)
            import asyncio
            import media_ingest
            info = await asyncio.to_thread(media_ingest.probe,dest)
            duration = float(info["format"]["duration"])
            sid = upsert_source("upload:"+dest.stem,title.strip(),collection[:200],kind,
                                {"subtitle_origin":"User supplied"},path)
            await asyncio.to_thread(install_media,sid,dest,sub,duration,path)
            return {"source_id":sid}
        except Exception as e:
            dest.unlink(missing_ok=True)
            if sid is not None:
                with connect(path) as db:
                    db.execute("UPDATE sources SET status='failed',error='Import failed' WHERE id=?",(sid,))
            if isinstance(e,HTTPException):
                raise
            raise HTTPException(422,"Could not read this media file or timed transcript") from e

    @router.post('/api/study/explanations', status_code=202)
    def explain(req: ExplanationRequest):
        import explanations
        with connect(path) as db:
            first=db.execute('SELECT * FROM cues WHERE id=?',(req.cue_id,)).fetchone()
            last=db.execute('SELECT * FROM cues WHERE id=?',(req.end_cue_id or req.cue_id,)).fetchone()
            if not first or not last:
                raise HTTPException(404,'Subtitle not found')
            if first['source_id']!=last['source_id'] or not 0<=last['ordinal']-first['ordinal']<=20:
                raise HTTPException(422,'Choose a continuous subtitle span')
            text=' '.join(r[0] for r in db.execute('SELECT text FROM cues WHERE source_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal',
                (first['source_id'],first['ordinal'],last['ordinal'])))
        if not text.strip() or len(text)>4000:
            raise HTTPException(422,'Choose a shorter spoken passage')
        return {'id':explanations.start(text)}

    @router.get('/api/study/explanations/{key}')
    def explanation(key: str):
        import explanations
        try:
            return explanations.get(key)
        except (ValueError,FileNotFoundError):
            raise HTTPException(404,'Explanation not found')

    return router


class YouTubeRequest(BaseModel):
    url: str = Field(max_length=2000)


class ExplanationRequest(BaseModel):
    cue_id: int
    end_cue_id: int | None = None
