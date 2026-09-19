"""Read-only Plex-file import. Browser copies live locally; originals stay intact.

Run: uv run python media_ingest.py --scan
Then: uv run python media_ingest.py --prepare SOURCE_ID
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import re
from urllib.parse import urlparse, parse_qs

from fastapi import HTTPException
from dotenv import load_dotenv
import study

POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-prepare")
load_dotenv(study.ROOT / '.env')
HOST = os.getenv("WAKATTA_PLEX_HOST", "")
REMOTE_ROOT = os.getenv("WAKATTA_PLEX_ROOT", "")
SCAN_SCRIPT = r'''
from pathlib import Path
import json,re
root=Path(__WAKATTA_MEDIA_ROOT__)
names=['That Time I Got Reincarnated as a Slime','K-ON!',"Frieren - Beyond Journey's End",
       'Steins;Gate','Ascendance of a Bookworm','Fullmetal Alchemist - Brotherhood',
       'Witch Hat Atelier','Magical Shopping Arcade Abenobashi','Berserk']
out=[]
for name in names:
 p=root/name
 videos=sorted(f for f in p.rglob('*') if f.suffix.lower() in ('.mkv','.mp4'))
 subs=[f for f in p.rglob('*') if f.suffix.lower() in ('.srt','.vtt')]
 for f in videos:
  matches=[s for s in subs if s.parent==f.parent and (s.name.startswith(f.stem+'.ja.') or s.name.startswith(f.stem+'.jpn.'))]
  sub=next(iter(matches),None)
  m=re.search(r'S(\d+)E(\d+)',f.name,re.I)
  if m: season,episode=map(int,m.groups())
  else:
   ep=re.search(r' - (\d{2,3})(?:\D|$)',f.name)
   se=re.search(r'Season\s+(\d+)',f.parent.name,re.I)
   season=int(se[1]) if se else None;episode=int(ep[1]) if ep else None
  label=f'S{season:02}E{episode:02}' if season is not None and episode is not None else f.stem
  out.append(dict(title=label,collection=name,remote_path=str(f),subtitle_path=str(sub) if sub else None,
                  season=season,episode=episode,subtitle_origin='Existing Plex sidecar; alignment not yet verified',
                  warning='Known wrong Japanese subtitle for episode 1; replace before studying' if name=='K-ON!' and episode==1 else None))
print(json.dumps(out,ensure_ascii=False))
'''


def scan(path=study.DB_PATH):
    check_config()
    result = subprocess.run(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=10",HOST,"python3 -"],
                            input=SCAN_SCRIPT.replace('__WAKATTA_MEDIA_ROOT__',repr(REMOTE_ROOT)),
                            text=True,capture_output=True,timeout=60,check=True)
    rows = json.loads(result.stdout)
    for row in rows:
        row["host"] = HOST
        study.upsert_source("plex:"+HOST+":"+row["remote_path"],row["title"],row["collection"],"video",row,path)
    return len(rows)


def probe(file):
    return json.loads(subprocess.run(["ffprobe","-v","error","-show_format","-show_streams",
                      "-of","json",str(file)],capture_output=True,text=True,timeout=60,check=True).stdout)


def remote_copy(host, remote, destination):
    check_config()
    # Only catalogued files under the media root can be fetched. No client paths.
    if host != HOST or not Path(remote).is_relative_to(REMOTE_ROOT) or ".." in Path(remote).parts:
        raise ValueError("Source is outside configured Plex library")
    with destination.open("wb") as output:
        subprocess.run(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=10",host,
                        "cat -- "+shlex.quote(remote)],stdout=output,stderr=subprocess.PIPE,
                       timeout=1800,check=True)


def check_config():
    if not HOST or not REMOTE_ROOT or not Path(REMOTE_ROOT).is_absolute():
        raise ValueError('Set WAKATTA_PLEX_HOST and an absolute WAKATTA_PLEX_ROOT in .env')


def prepare(source_id, path=study.DB_PATH):
    with study.connect(path) as db:
        row = db.execute("SELECT * FROM sources WHERE id=?",(source_id,)).fetchone()
    if not row:
        raise ValueError("Source not found")
    if row["status"]=="ready":
        return
    meta = json.loads(row["metadata"])
    if meta.get("youtube_url"):
        return prepare_youtube(source_id,meta["youtube_url"],path)
    if not meta.get("remote_path"):
        raise ValueError("This source has no Plex file")
    if meta.get("warning"):
        raise ValueError(meta["warning"])
    study.MEDIA_DIR.mkdir(parents=True,exist_ok=True)
    def status(value):
        with study.connect(path) as db:
            db.execute("UPDATE sources SET status=?,error=NULL WHERE id=?",(value,source_id))
    try:
        status("copying")
        with tempfile.TemporaryDirectory(prefix="prepare-",dir=study.MEDIA_DIR) as tmp:
            tmp = Path(tmp)
            original = tmp/"source.mkv"
            remote_copy(meta["host"],meta["remote_path"],original)
            subtitles = ""
            if meta.get("subtitle_path"):
                sf = tmp/"source.srt"
                remote_copy(meta["host"],meta["subtitle_path"],sf)
                subtitles = sf.read_text(encoding="utf-8-sig")
            info = probe(original)
            audio = next((s for s in info["streams"] if s["codec_type"]=="audio" and
                          s.get("tags",{}).get("language") in ("jpn","ja")),None)
            if audio is None:
                raise ValueError("No Japanese audio track found; refusing to import a dub")
            status("converting")
            output = tmp/"browser.mp4"
            base = ["ffmpeg","-nostdin","-v","error","-y","-i",str(original),
                    "-map","0:v:0","-map",f"0:{audio['index']}","-vf","scale=-2:720,format=yuv420p",
                    "-c:a","aac","-b:a","128k","-sn","-dn","-map_metadata","-1",
                    "-movflags","+faststart"]
            gpu = subprocess.run(base+["-c:v","h264_nvenc","-preset","p4","-cq","25",str(output)],
                                 capture_output=True,timeout=1800)
            if gpu.returncode:
                subprocess.run(base+["-c:v","libx264","-preset","veryfast","-crf","23",str(output)],
                               capture_output=True,timeout=3600,check=True)
            dest = study.MEDIA_DIR/f"source-{source_id}.mp4"
            output.replace(dest)
            status("indexing")
            study.install_media(source_id,dest,subtitles,float(info["format"]["duration"]),path)
    except Exception as e:
        with study.connect(path) as db:
            db.execute("UPDATE sources SET status='failed',error=? WHERE id=?",(str(e)[:500],source_id))
        raise


def start_prepare(source_id,path=study.DB_PATH):
    with study.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM sources WHERE id=?",(source_id,)).fetchone()
        if not row:
            raise HTTPException(404,"Source not found")
        if row["status"] in ("queued","copying","converting","indexing","ready"):
            return {"status":row["status"]}
        meta = json.loads(row["metadata"])
        if not (meta.get("remote_path") or meta.get("youtube_url")) or meta.get("warning"):
            raise HTTPException(409,meta.get("warning") or "No remote media source")
        db.execute("UPDATE sources SET status='queued',error=NULL WHERE id=?",(source_id,))
    POOL.submit(prepare,source_id,path)
    return {"status":"queued"}


def catalog_youtube(url,path=study.DB_PATH):
    parsed=urlparse(url)
    if parsed.scheme not in ('http','https') or parsed.hostname not in ('youtube.com','www.youtube.com','m.youtube.com','youtu.be'):
        raise ValueError('Use a YouTube episode URL')
    video_id=parsed.path.strip('/') if parsed.hostname=='youtu.be' else parse_qs(parsed.query).get('v',[''])[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):
        raise ValueError('Use a single YouTube video, not a playlist or channel')
    with study.connect(path) as db:
        row=db.execute('SELECT id FROM sources WHERE external_key=?',('youtube:'+video_id,)).fetchone()
        if row:return row[0]
    return study.upsert_source('youtube:'+video_id,'YouTube episode','Your listening','audio',
        {'youtube_url':'https://www.youtube.com/watch?v='+video_id},path)


def prepare_youtube(source_id,url,path=study.DB_PATH):
    command=['uv','tool','run','--from','yt-dlp','yt-dlp','--no-playlist','--no-warnings']
    try:
        with study.connect(path) as db:db.execute("UPDATE sources SET status='copying',error=NULL WHERE id=?",(source_id,))
        info=json.loads(subprocess.run(command+['--skip-download','--dump-single-json',url],capture_output=True,text=True,timeout=120,check=True).stdout)
        meta={'youtube_url':url,'url':url,'chapters':info.get('chapters',[]),
              'subtitle_origin':'YouTube Japanese captions; not manually verified. Automatic captions are not imported.'}
        with study.connect(path) as db:
            db.execute('UPDATE sources SET title=?,collection=?,metadata=? WHERE id=?',
                       (info['title'],info.get('channel') or info.get('uploader') or 'YouTube',json.dumps(meta,ensure_ascii=False),source_id))
        study.MEDIA_DIR.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='youtube-',dir=study.MEDIA_DIR) as tmp:
            tmp=Path(tmp)
            subprocess.run(command+['--no-progress','--write-subs','--no-write-auto-subs','--sub-langs','ja',
                '--sub-format','vtt','-f','bestaudio[ext=m4a]/bestaudio','-o',str(tmp/'audio.%(ext)s'),url],
                capture_output=True,timeout=1800,check=True)
            audio=next((p for p in tmp.iterdir() if p.suffix in ('.m4a','.webm','.opus','.mp3','.ogg')),None)
            if audio is None:raise ValueError('No audio downloaded')
            dest=study.MEDIA_DIR/f'source-{source_id}.m4a'
            if audio.suffix=='.m4a':audio.replace(dest)
            else:subprocess.run(['ffmpeg','-v','error','-y','-i',str(audio),'-vn','-c:a','aac',str(dest)],capture_output=True,timeout=1800,check=True)
            sub=next(tmp.glob('*.ja.vtt'),None)
            study.install_media(source_id,dest,sub.read_text() if sub else '',float(info['duration']),path)
    except Exception as e:
        with study.connect(path) as db:
            db.execute("UPDATE sources SET status='failed',error=? WHERE id=?",('YouTube import failed: '+str(e)[:400],source_id))
        raise


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--scan",action="store_true")
    parser.add_argument("--prepare",type=int)
    parser.add_argument("--youtube",help="Import one YouTube episode as audio with available Japanese captions")
    args=parser.parse_args()
    study.build_schema()
    if args.scan:
        print(f"Catalogued {scan()} episodes")
    if args.prepare:
        prepare(args.prepare)
        print(f"Ready: /watch/{args.prepare}")
    if args.youtube:
        sid=catalog_youtube(args.youtube)
        prepare(sid)
        print(f"Ready: /watch/{sid}")
