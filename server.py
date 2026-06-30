"""Handwriting recognition server + page ingestion pipeline."""

import asyncio
import io
import json
import traceback
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import cv2
import fitz
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from manga_ocr import MangaOcr
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import Column, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

import ctd
import kanjivg_db

# ── Paths ──────────────────────────────────────────────────────────────────────

DB_PATH = Path("data/wakatta.db")
PAGES_DIR = Path("data/pages")
UPLOADS_DIR = Path("data/uploads")
DB_JSON_PATH = Path("static/db.json")
DPI = 600


# ── SQLAlchemy models ──────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Work(Base):
    __tablename__ = "works"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    path = Column(String, nullable=False, unique=True)


class Page(Base):
    __tablename__ = "pages"
    id = Column(Integer, primary_key=True)
    work_id = Column(Integer, ForeignKey("works.id"), nullable=False)
    page_num = Column(Integer, nullable=False)
    width = Column(Integer)
    height = Column(Integer)
    __table_args__ = (UniqueConstraint("work_id", "page_num"),)


class Sentence(Base):
    __tablename__ = "sentences"
    id = Column(Integer, primary_key=True)
    page_id = Column(Integer, ForeignKey("pages.id"), nullable=False)
    x1 = Column(Integer, nullable=False)
    y1 = Column(Integer, nullable=False)
    x2 = Column(Integer, nullable=False)
    y2 = Column(Integer, nullable=False)
    direction = Column(String(1), nullable=False)
    prob = Column(Float, nullable=False)
    ocr_text = Column(Text)
    user_text = Column(Text)


class JobRecord(Base):
    __tablename__ = "jobs"
    id = Column(String, primary_key=True)
    work_id = Column(Integer, ForeignKey("works.id"), nullable=False)
    status = Column(String, nullable=False, default="pending")
    pages_done = Column(Integer, nullable=False, default=0)
    pages_total = Column(Integer, nullable=False, default=0)
    error = Column(Text)
    created_at = Column(String, nullable=False)


engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


# ── Job tracking ───────────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    done    = "done"
    failed  = "failed"


@dataclass
class Job:
    id: str
    work_id: int
    status: JobStatus = JobStatus.pending
    pages_done: int = 0
    pages_total: int = 0
    error: str | None = None


jobs: dict[str, Job] = {}
_running_tasks: set[asyncio.Task] = set()

# Serializes CTD + OCR so GPU resources aren't contended across concurrent jobs.
_process_lock: asyncio.Lock = None


# ── Global state ───────────────────────────────────────────────────────────────

kvg_db: kanjivg_db.KanjiVGDatabase = None
mocr: MangaOcr = None


# ── Image pipeline helpers ─────────────────────────────────────────────────────

def extract_page(pdf_path: Path, page_num: int, dpi: int = DPI) -> Image.Image:
    doc = fitz.open(str(pdf_path))
    pix = doc[page_num].get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def pdf_page_count(pdf_path: Path) -> int:
    doc = fitz.open(str(pdf_path))
    return len(doc)


def crop_region(image: Image.Image, x1: int, y1: int, x2: int, y2: int) -> Image.Image:
    margin = 4
    w, h = image.size
    return image.crop((
        max(0, x1 - margin), max(0, y1 - margin),
        min(w, x2 + margin), min(h, y2 + margin),
    ))


async def _process_one_page(
    pdf_path: Path,
    work_id: int,
    page_num: int,
) -> None:
    """Run the full pipeline for a single page and persist results. No-op if already done."""
    with Session(engine) as session:
        existing = session.query(Page).filter_by(work_id=work_id, page_num=page_num).first()
        if existing is not None:
            return

    async with _process_lock:
        page_image = await asyncio.to_thread(extract_page, pdf_path, page_num)
        img_bgr = cv2.cvtColor(np.array(page_image.convert("RGB")), cv2.COLOR_RGB2BGR)
        regions = await asyncio.to_thread(ctd.detect, img_bgr)

        region_texts: list[str | None] = []
        for region in regions:
            x1, y1, x2, y2 = region.xyxy
            ocr_text = None
            if mocr is not None:
                crop = crop_region(page_image, x1, y1, x2, y2)
                text = await asyncio.to_thread(mocr, crop)
                ocr_text = text.strip() or None
            region_texts.append(ocr_text)

    with Session(engine) as session:
        # Re-check inside session in case another request raced us
        if session.query(Page).filter_by(work_id=work_id, page_num=page_num).first() is not None:
            return

        page = Page(
            work_id=work_id,
            page_num=page_num,
            width=page_image.width,
            height=page_image.height,
        )
        session.add(page)
        session.flush()
        page_image.save(PAGES_DIR / f"{page.id}.png")

        for region, ocr_text in zip(regions, region_texts):
            x1, y1, x2, y2 = region.xyxy
            session.add(Sentence(
                page_id=page.id,
                x1=x1, y1=y1, x2=x2, y2=y2,
                direction=region.direction,
                prob=region.prob,
                ocr_text=ocr_text,
            ))

        session.commit()


def _persist_job(job: Job) -> None:
    with Session(engine) as session:
        rec = session.get(JobRecord, job.id)
        if rec is None:
            rec = JobRecord(
                id=job.id,
                work_id=job.work_id,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            session.add(rec)
        rec.status = job.status
        rec.pages_done = job.pages_done
        rec.pages_total = job.pages_total
        rec.error = job.error
        session.commit()


async def _run_job(job: Job, pdf_path: Path) -> None:
    job.status = JobStatus.running
    _persist_job(job)
    try:
        job.pages_total = await asyncio.to_thread(pdf_page_count, pdf_path)
        _persist_job(job)
        for page_num in range(job.pages_total):
            await _process_one_page(pdf_path, job.work_id, page_num)
            job.pages_done = page_num + 1
            _persist_job(job)
            print(f"[job {job.id[:8]}] page {job.pages_done}/{job.pages_total}")
        job.status = JobStatus.done
        _persist_job(job)
    except Exception:
        job.status = JobStatus.failed
        job.error = traceback.format_exc()
        _persist_job(job)
        traceback.print_exc()


# ── KanjiVG db.json helper ─────────────────────────────────────────────────────

def _generate_db_json(database: kanjivg_db.KanjiVGDatabase) -> None:
    def _round_stroke(stroke):
        return [[round(float(x), 4), round(float(y), 4)] for x, y in stroke]

    payload = {
        "byCount": {str(k): v for k, v in database.by_count.items()},
        "chars": {
            char: [_round_stroke(stroke) for stroke in strokes]
            for char, strokes in database.chars.items()
        },
    }
    DB_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    size_mb = DB_JSON_PATH.stat().st_size / 1_000_000
    print(f"Wrote {DB_JSON_PATH} ({size_mb:.1f} MB, {len(database.chars)} chars)")


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global kvg_db, mocr, _process_lock
    _process_lock = asyncio.Lock()

    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)

    kvg_db = await asyncio.to_thread(kanjivg_db.load_database)
    if not DB_JSON_PATH.exists():
        print("Generating static/db.json for PWA offline cache ...")
        await asyncio.to_thread(_generate_db_json, kvg_db)

    print("Loading manga-ocr ...")
    mocr = await asyncio.to_thread(MangaOcr)
    print("manga-ocr ready.")

    # Resume any jobs that were running when the server last stopped
    with Session(engine) as session:
        interrupted = (
            session.query(JobRecord)
            .filter(JobRecord.status.in_(["pending", "running"]))
            .all()
        )
        for rec in interrupted:
            work = session.get(Work, rec.work_id)
            if work is None:
                continue
            job = Job(
                id=rec.id,
                work_id=rec.work_id,
                status=JobStatus.running,
                pages_done=rec.pages_done,
                pages_total=rec.pages_total,
            )
            jobs[job.id] = job
            print(f"Resuming job {job.id[:8]} for '{work.title}' ({rec.pages_done}/{rec.pages_total} done)")
            task = asyncio.create_task(_run_job(job, Path(work.path)))
            _running_tasks.add(task)
            task.add_done_callback(_running_tasks.discard)

    yield


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class Stroke(BaseModel):
    points: list[dict]


class RecognizeRequest(BaseModel):
    strokes: list[Stroke]


class WorkCreate(BaseModel):
    pdf_path: str
    title: str | None = None


class SentenceUpdate(BaseModel):
    user_text: str


# ── Response helpers ───────────────────────────────────────────────────────────

def _work_dict(w: Work) -> dict:
    return {"id": w.id, "title": w.title, "path": w.path}


def _sentence_dict(s: Sentence) -> dict:
    return {
        "id": s.id,
        "x1": s.x1, "y1": s.y1, "x2": s.x2, "y2": s.y2,
        "direction": s.direction,
        "prob": round(s.prob, 3),
        "ocr_text": s.ocr_text,
        "user_text": s.user_text,
    }


def _page_response(page: Page, sentences: list[Sentence]) -> dict:
    return {
        "id": page.id,
        "work_id": page.work_id,
        "page_num": page.page_num,
        "width": page.width,
        "height": page.height,
        "sentences": [_sentence_dict(s) for s in sentences],
    }


def _job_dict(j: Job) -> dict:
    return {
        "id": j.id,
        "work_id": j.work_id,
        "status": j.status,
        "pages_done": j.pages_done,
        "pages_total": j.pages_total,
        "error": j.error,
    }


# ── Routes — pages ─────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/page-reader")
def page_reader():
    return FileResponse("static/page-reader.html")


@app.post("/recognize")
def recognize(req: RecognizeRequest):
    raw = [s.points for s in req.strokes]
    return kanjivg_db.recognize(raw, kvg_db, top_n=12)


# ── Routes — works ─────────────────────────────────────────────────────────────

@app.get("/api/works")
def list_works():
    with Session(engine) as session:
        works = session.query(Work).all()
        return [_work_dict(w) for w in works]


@app.post("/api/upload", status_code=201)
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    dest = UPLOADS_DIR / file.filename
    if not dest.exists():
        content = await file.read()
        dest.write_bytes(content)

    with Session(engine) as session:
        work = session.query(Work).filter_by(path=str(dest)).first()
        if work is None:
            work = Work(title=dest.stem, path=str(dest))
            session.add(work)
            session.commit()
            session.refresh(work)
        work_data = _work_dict(work)
        work_id = work.id

    # Return existing active job if one is already running
    for job in jobs.values():
        if job.work_id == work_id and job.status in (JobStatus.pending, JobStatus.running):
            return {"work": work_data, "job": _job_dict(job)}

    job = Job(id=str(uuid.uuid4()), work_id=work_id)
    jobs[job.id] = job
    task = asyncio.create_task(_run_job(job, dest))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)

    return {"work": work_data, "job": _job_dict(job)}


@app.post("/api/works", status_code=201)
def create_work(req: WorkCreate):
    pdf_path = Path(req.pdf_path)
    if not pdf_path.exists():
        raise HTTPException(404, f"PDF not found: {pdf_path}")
    with Session(engine) as session:
        work = session.query(Work).filter_by(path=str(pdf_path)).first()
        if work is None:
            work = Work(title=req.title or pdf_path.stem, path=str(pdf_path))
            session.add(work)
            session.commit()
            session.refresh(work)
        return _work_dict(work)


@app.get("/api/works/{work_id}/pages")
def list_work_pages(work_id: int):
    with Session(engine) as session:
        if session.get(Work, work_id) is None:
            raise HTTPException(404, "Work not found")
        pages = (
            session.query(Page)
            .filter_by(work_id=work_id)
            .order_by(Page.page_num)
            .all()
        )
        return [
            {"id": p.id, "page_num": p.page_num, "width": p.width, "height": p.height}
            for p in pages
        ]


@app.post("/api/works/{work_id}/process-all")
async def process_all_pages(work_id: int):
    with Session(engine) as session:
        work = session.get(Work, work_id)
        if work is None:
            raise HTTPException(404, "Work not found")
        pdf_path = Path(work.path)

    # Return existing active job for this work if one is running
    for job in jobs.values():
        if job.work_id == work_id and job.status in (JobStatus.pending, JobStatus.running):
            return _job_dict(job)

    job = Job(id=str(uuid.uuid4()), work_id=work_id)
    jobs[job.id] = job

    task = asyncio.create_task(_run_job(job, pdf_path))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)

    return _job_dict(job)


@app.get("/api/works/{work_id}/job")
def get_work_job(work_id: int):
    # In-memory first — has live page counts during active processing
    active = next(
        (j for j in jobs.values() if j.work_id == work_id and j.status in (JobStatus.pending, JobStatus.running)),
        None,
    )
    if active:
        return _job_dict(active)
    with Session(engine) as session:
        rec = (
            session.query(JobRecord)
            .filter_by(work_id=work_id)
            .order_by(JobRecord.created_at.desc())
            .first()
        )
        if rec is None:
            raise HTTPException(404, "No job for this work")
        return {"id": rec.id, "work_id": rec.work_id, "status": rec.status,
                "pages_done": rec.pages_done, "pages_total": rec.pages_total, "error": rec.error}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id in jobs:
        return _job_dict(jobs[job_id])
    with Session(engine) as session:
        rec = session.get(JobRecord, job_id)
        if rec is None:
            raise HTTPException(404, "Job not found")
        return {"id": rec.id, "work_id": rec.work_id, "status": rec.status,
                "pages_done": rec.pages_done, "pages_total": rec.pages_total, "error": rec.error}



@app.get("/api/pages/{page_id}")
def get_page(page_id: int):
    with Session(engine) as session:
        page = session.get(Page, page_id)
        if page is None:
            raise HTTPException(404, "Page not found")
        sentences = session.query(Sentence).filter_by(page_id=page_id).all()
        return _page_response(page, sentences)


@app.get("/api/pages/{page_id}/image")
def page_image(page_id: int):
    img_path = PAGES_DIR / f"{page_id}.png"
    if not img_path.exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(img_path, media_type="image/png")


@app.put("/api/sentences/{sentence_id}")
def update_sentence(sentence_id: int, update: SentenceUpdate):
    with Session(engine) as session:
        s = session.get(Sentence, sentence_id)
        if s is None:
            raise HTTPException(404, "Sentence not found")
        s.user_text = update.user_text
        session.commit()
        return _sentence_dict(s)
