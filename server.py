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
import fugashi
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from manga_ocr import MangaOcr
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Session

import ctd
import dictionary
import kanji
import kanjivg_db
import reading_order

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
    # Once a user manually fixes reading order (see PUT .../order below), automatic
    # re-sorting on box create/move/merge is suppressed for this page — the heuristic
    # would otherwise clobber the manual fix on the next geometry edit.
    order_locked = Column(Boolean, nullable=False, default=False)
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
    order_index = Column(Integer, nullable=False, default=0)
    # Set when this sentence's text is a direct grammatical continuation of another
    # (e.g. one line of dialogue split across two visually separate bubbles) — the
    # two stay separate boxes/geometry, this just records the reading link. At most
    # one sentence can continue into any given target (unique), enforced same-page.
    continues_into_id = Column(Integer, ForeignKey("sentences.id"), nullable=True, unique=True)


class JobRecord(Base):
    __tablename__ = "jobs"
    id = Column(String, primary_key=True)
    work_id = Column(Integer, ForeignKey("works.id"), nullable=False)
    status = Column(String, nullable=False, default="pending")
    pages_done = Column(Integer, nullable=False, default=0)
    pages_total = Column(Integer, nullable=False, default=0)
    error = Column(Text)
    created_at = Column(String, nullable=False)


class Word(Base):
    """Canonical dictionary-form entry, deduplicated by (lemma, reading) — e.g. every
    occurrence of 勉強/べんきょう across the whole library shares one row."""
    __tablename__ = "words"
    id = Column(Integer, primary_key=True)
    lemma = Column(String, nullable=False)
    reading = Column(String, nullable=False)
    __table_args__ = (UniqueConstraint("lemma", "reading"),)


class WordOccurrence(Base):
    """One tokenized instance of a Word in a Sentence, from one text source.

    `source` distinguishes OCR-derived occurrences from human-confirmed ones so a page
    that hasn't been manually checked yet can still contribute a (lower-trust) signal —
    a sentence whose ocr_text and user_text disagree ends up with two independent sets
    of occurrences. Definition resolution (`dict_entry_id`) lives here rather than on
    Word because the same lemma+reading can be a genuine homograph resolving differently
    sentence to sentence (e.g. 変/へん = "strange" vs "change").
    """
    __tablename__ = "word_occurrences"
    id = Column(Integer, primary_key=True)
    sentence_id = Column(Integer, ForeignKey("sentences.id"), nullable=False)
    word_id = Column(Integer, ForeignKey("words.id"), nullable=False)
    source = Column(String(4), nullable=False)  # "ocr" | "user"
    surface = Column(String, nullable=False)
    start = Column(Integer, nullable=False)  # char offset into that source's text
    end = Column(Integer, nullable=False)
    dict_entry_id = Column(Integer, nullable=True)  # soft ref -> dict_entries.id (raw-SQL table, see dictionary.py); NULL = unresolved
    resolved_by = Column(String, nullable=True)  # "auto" | "user" | None
    candidate_count = Column(Integer, nullable=False, default=0)  # how many dict_entries matched at tokenize time (0 = no entry at all, >1 = genuinely ambiguous)
    __table_args__ = (UniqueConstraint("sentence_id", "source", "start", "end"),)


class WordLookup(Base):
    """Logged every time a user opens the dictionary panel for a word occurrence —
    the lookup itself is treated as a "didn't know this word" signal."""
    __tablename__ = "word_lookups"
    id = Column(Integer, primary_key=True)
    occurrence_id = Column(Integer, ForeignKey("word_occurrences.id"), nullable=False)
    created_at = Column(String, nullable=False)


class SegmentationOverride(Base):
    """User-defined correction to how the tokenizer splits a piece of text into words —
    e.g. registering ナウシカ (a character's name Unidic doesn't recognize) as a single
    atomic word instead of the ナウ + シカ it gets split into, or the reverse: splitting
    a span the tokenizer wrongly fused into one word. Global (keyed only by the literal
    text span, not tied to any one sentence) because a recurring word like a character's
    name would otherwise need the same fix reapplied every time it appears."""
    __tablename__ = "segmentation_overrides"
    id = Column(Integer, primary_key=True)
    span_text = Column(String, nullable=False, unique=True)
    words_json = Column(Text, nullable=False)  # JSON list of literal substrings span_text divides into
    reading = Column(String, nullable=True)  # only used when words_json has exactly one entry


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
tagger: fugashi.Tagger = None


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


def _get_or_create_word(session: Session, lemma: str, reading: str) -> Word:
    word = session.query(Word).filter_by(lemma=lemma, reading=reading).first()
    if word is None:
        word = Word(lemma=lemma, reading=reading)
        session.add(word)
        session.flush()
    return word


def _store_occurrences(session: Session, sentence_id: int, text_: str | None, source: str) -> None:
    """(Re)tokenize `text_` and replace this sentence's WordOccurrence rows for `source`.
    No-op (just clearing stale rows) if there's no text yet or the tagger hasn't loaded."""
    session.query(WordOccurrence).filter_by(sentence_id=sentence_id, source=source).delete()
    if not text_ or tagger is None:
        return
    overrides = dictionary.load_overrides(engine)
    for tok in dictionary.tokenize(text_, tagger, overrides=overrides):
        word = _get_or_create_word(session, tok["lemma"], tok["lemma_reading"])
        candidates = dictionary.resolve_candidates(
            engine, lemma=tok["lemma"], surface=tok["surface"], reading=tok["lemma_reading"]
        )
        dict_entry_id = candidates[0] if len(candidates) == 1 else None
        session.add(WordOccurrence(
            sentence_id=sentence_id,
            word_id=word.id,
            source=source,
            surface=tok["surface"],
            start=tok["start"],
            end=tok["end"],
            dict_entry_id=dict_entry_id,
            resolved_by="auto" if dict_entry_id is not None else None,
            candidate_count=len(candidates),
        ))


def _reapply_override_everywhere(session: Session, span_text: str) -> None:
    """Re-tokenize every existing sentence whose ocr_text or user_text contains
    `span_text`, so a new segmentation override (e.g. registering a character's name)
    takes effect retroactively wherever it already appears — not just in whatever
    sentence prompted the correction. Cheap: fugashi tokenization has no GPU/network
    cost, and this only touches sentences that actually contain the substring."""
    like = f"%{span_text}%"
    rows = (
        session.query(Sentence)
        .filter(Sentence.ocr_text.like(like) | Sentence.user_text.like(like))
        .all()
    )
    for s in rows:
        if s.ocr_text and span_text in s.ocr_text:
            _store_occurrences(session, s.id, s.ocr_text, "ocr")
        if s.user_text and span_text in s.user_text:
            _store_occurrences(session, s.id, s.user_text, "user")


def _ensure_order_index_column() -> bool:
    """`Sentence.order_index` was added after the table already existed on disk in
    some installs — `Base.metadata.create_all` only creates missing tables, it
    won't add a column to one that's already there. Patch it in place. Returns
    True the first time the column has to be added, so the caller knows to
    backfill it."""
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(sentences)")}
        if "order_index" in cols:
            return False
        conn.exec_driver_sql("ALTER TABLE sentences ADD COLUMN order_index INTEGER NOT NULL DEFAULT 0")
        return True


def _ensure_order_locked_column() -> None:
    """`Page.order_locked` was added after the table already existed on disk in
    some installs — see `_ensure_order_index_column` above for why this is needed."""
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(pages)")}
        if "order_locked" in cols:
            return
        conn.exec_driver_sql("ALTER TABLE pages ADD COLUMN order_locked BOOLEAN NOT NULL DEFAULT 0")


def _ensure_continues_into_column() -> None:
    """`Sentence.continues_into_id` was added after the table already existed on
    disk in some installs — see `_ensure_order_index_column` above for why this is
    needed. SQLite can't add a UNIQUE column via ALTER TABLE, so the unique index
    backing that constraint is created separately, only on that same first run."""
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(sentences)")}
        if "continues_into_id" in cols:
            return
        conn.exec_driver_sql("ALTER TABLE sentences ADD COLUMN continues_into_id INTEGER REFERENCES sentences(id)")
        conn.exec_driver_sql("CREATE UNIQUE INDEX ix_sentences_continues_into_id ON sentences(continues_into_id)")


def _next_order_index(session: Session, page_id: int) -> int:
    """One past the current end of a page's order — where a newly-created box
    lands on a manually-ordered (`order_locked`) page, since the heuristic that
    would otherwise place it can't be trusted there."""
    max_idx = session.query(func.max(Sentence.order_index)).filter_by(page_id=page_id).scalar()
    return (max_idx if max_idx is not None else -1) + 1


def _resort_page(session: Session, page_id: int) -> None:
    """Recompute `order_index` for every sentence on a page from its current
    geometry. Cheap enough to call after any box create/move/resize.

    No-op once a page's order has been manually fixed (`order_locked`) — the
    heuristic isn't trusted for that page anymore, and re-running it on every
    geometry edit would silently undo the manual fix."""
    page = session.get(Page, page_id)
    if page is not None and page.order_locked:
        return
    sentences = session.query(Sentence).filter_by(page_id=page_id).all()
    order = reading_order.reading_order([(s.x1, s.y1, s.x2, s.y2) for s in sentences])
    for rank, idx in enumerate(order):
        sentences[idx].order_index = rank


async def _ocr_region(page_id: int, x1: int, y1: int, x2: int, y2: int) -> str | None:
    """Crop a region of a stored page PNG and run manga-ocr on it."""
    img_path = PAGES_DIR / f"{page_id}.png"
    if not img_path.exists():
        raise HTTPException(404, "Page image not found")
    if mocr is None:
        return None
    async with _process_lock:
        page_image = await asyncio.to_thread(Image.open, img_path)
        crop = crop_region(page_image, x1, y1, x2, y2)
        text = await asyncio.to_thread(mocr, crop)
        return text.strip() or None


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
            sentence = Sentence(
                page_id=page.id,
                x1=x1, y1=y1, x2=x2, y2=y2,
                direction=region.direction,
                prob=region.prob,
                ocr_text=ocr_text,
            )
            session.add(sentence)
            session.flush()
            _store_occurrences(session, sentence.id, ocr_text, "ocr")

        _resort_page(session, page.id)
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
    global kvg_db, mocr, tagger, _process_lock
    _process_lock = asyncio.Lock()

    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _ensure_order_locked_column()
    _ensure_continues_into_column()
    if _ensure_order_index_column():
        with Session(engine) as session:
            page_ids = [p.id for p in session.query(Page.id).all()]
            for page_id in page_ids:
                _resort_page(session, page_id)
            session.commit()
        print(f"[migration] Backfilled reading order for {len(page_ids)} pages.")

    try:
        await asyncio.to_thread(dictionary.build_db, engine)
    except FileNotFoundError as e:
        print(f"[dictionary] {e}")
        print("[dictionary] Dictionary lookups will return 503 until the setup scripts are run.")

    try:
        await asyncio.to_thread(kanji.build_db, engine)
        await asyncio.to_thread(kanji.build_component_index, engine)
    except FileNotFoundError as e:
        print(f"[kanji] {e}")
        print("[kanji] Kanji lookups will return 503 until setup_kanjidic.py is run.")

    kvg_db = await asyncio.to_thread(kanjivg_db.load_database)
    if not DB_JSON_PATH.exists():
        print("Generating static/db.json for PWA offline cache ...")
        await asyncio.to_thread(_generate_db_json, kvg_db)

    print("Loading manga-ocr ...")
    mocr = await asyncio.to_thread(MangaOcr)
    print("manga-ocr ready.")

    print("Loading fugashi tokenizer ...")
    tagger = await asyncio.to_thread(fugashi.Tagger)
    print("fugashi ready.")

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


class SentenceCreate(BaseModel):
    x1: int
    y1: int
    x2: int
    y2: int
    direction: str = "v"
    user_text: str | None = None


class SentenceUpdate(BaseModel):
    user_text: str | None = None
    x1: int | None = None
    y1: int | None = None
    x2: int | None = None
    y2: int | None = None
    direction: str | None = None


class SentenceMerge(BaseModel):
    sentence_ids: list[int]


class PageOrderUpdate(BaseModel):
    sentence_ids: list[int]  # every sentence on the page, in the desired reading order


class SentenceLink(BaseModel):
    target_id: int


class WordResolve(BaseModel):
    dict_entry_id: int


class WordLookupCreate(BaseModel):
    occurrence_id: int


class SegmentationOverrideCreate(BaseModel):
    span_text: str
    words: list[str]
    reading: str | None = None


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
        "continues_into_id": s.continues_into_id,
    }


def _occurrence_dict(o: WordOccurrence, word: Word) -> dict:
    return {
        "id": o.id,
        "surface": o.surface,
        "start": o.start,
        "end": o.end,
        "lemma": word.lemma,
        "lemma_reading": word.reading,
        "dict_entry_id": o.dict_entry_id,
        "resolved_by": o.resolved_by,
        "candidate_count": o.candidate_count,
    }


def _page_response(page: Page, sentences: list[Sentence]) -> dict:
    continues_from = {s.continues_into_id: s.id for s in sentences if s.continues_into_id is not None}
    sentence_dicts = []
    for s in sentences:
        d = _sentence_dict(s)
        d["continues_from_id"] = continues_from.get(s.id)
        sentence_dicts.append(d)
    return {
        "id": page.id,
        "work_id": page.work_id,
        "page_num": page.page_num,
        "width": page.width,
        "height": page.height,
        "order_locked": page.order_locked,
        "sentences": sentence_dicts,
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


@app.get("/read/{work_id}")
def read_work(work_id: int):
    return FileResponse("static/reader.html")


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
        sentences = (
            session.query(Sentence)
            .filter_by(page_id=page_id)
            .order_by(Sentence.order_index)
            .all()
        )
        return _page_response(page, sentences)


@app.get("/api/pages/{page_id}/image")
def page_image(page_id: int):
    img_path = PAGES_DIR / f"{page_id}.png"
    if not img_path.exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(img_path, media_type="image/png")


@app.post("/api/pages/{page_id}/sentences", status_code=201)
async def create_sentence(page_id: int, req: SentenceCreate):
    with Session(engine) as session:
        if session.get(Page, page_id) is None:
            raise HTTPException(404, "Page not found")

    x1, x2 = sorted((req.x1, req.x2))
    y1, y2 = sorted((req.y1, req.y2))
    if x2 - x1 < 4 or y2 - y1 < 4:
        raise HTTPException(400, "Bounding box too small")

    ocr_text = await _ocr_region(page_id, x1, y1, x2, y2)

    with Session(engine) as session:
        page = session.get(Page, page_id)
        s = Sentence(
            page_id=page_id,
            x1=x1, y1=y1, x2=x2, y2=y2,
            direction=req.direction if req.direction in ("v", "h") else "v",
            prob=1.0,
            ocr_text=ocr_text,
            user_text=req.user_text,
            order_index=_next_order_index(session, page_id) if page.order_locked else 0,
        )
        session.add(s)
        session.flush()
        _store_occurrences(session, s.id, ocr_text, "ocr")
        if req.user_text:
            _store_occurrences(session, s.id, req.user_text, "user")
        if not page.order_locked:
            _resort_page(session, page_id)
        session.commit()
        session.refresh(s)
        return _sentence_dict(s)


@app.put("/api/sentences/{sentence_id}")
async def update_sentence(sentence_id: int, update: SentenceUpdate):
    with Session(engine) as session:
        s = session.get(Sentence, sentence_id)
        if s is None:
            raise HTTPException(404, "Sentence not found")
        page_id = s.page_id

        geometry_changed = None not in (update.x1, update.y1, update.x2, update.y2)
        if geometry_changed:
            x1, x2 = sorted((update.x1, update.x2))
            y1, y2 = sorted((update.y1, update.y2))
            if x2 - x1 < 4 or y2 - y1 < 4:
                raise HTTPException(400, "Bounding box too small")

        if update.direction is not None and update.direction in ("v", "h"):
            s.direction = update.direction
        if update.user_text is not None:
            s.user_text = update.user_text
            _store_occurrences(session, sentence_id, s.user_text, "user")

        if geometry_changed:
            s.x1, s.y1, s.x2, s.y2 = x1, y1, x2, y2
            _resort_page(session, page_id)

        session.commit()
        session.refresh(s)

    if geometry_changed:
        ocr_text = await _ocr_region(page_id, x1, y1, x2, y2)
        with Session(engine) as session:
            s = session.get(Sentence, sentence_id)
            s.ocr_text = ocr_text
            _store_occurrences(session, sentence_id, ocr_text, "ocr")
            session.commit()
            session.refresh(s)

    return _sentence_dict(s)


@app.delete("/api/sentences/{sentence_id}", status_code=204)
def delete_sentence(sentence_id: int):
    with Session(engine) as session:
        s = session.get(Sentence, sentence_id)
        if s is None:
            raise HTTPException(404, "Sentence not found")
        occurrence_ids = [
            oid for (oid,) in session.query(WordOccurrence.id).filter_by(sentence_id=sentence_id).all()
        ]
        if occurrence_ids:
            session.query(WordLookup).filter(WordLookup.occurrence_id.in_(occurrence_ids)).delete(synchronize_session=False)
            session.query(WordOccurrence).filter_by(sentence_id=sentence_id).delete()
        # Drop the dangling end of any continuation link touching this box.
        session.query(Sentence).filter_by(continues_into_id=sentence_id).update({"continues_into_id": None})
        session.delete(s)
        session.commit()


@app.post("/api/pages/{page_id}/sentences/merge", status_code=201)
async def merge_sentences(page_id: int, req: SentenceMerge):
    """Combine two or more existing boxes (e.g. text a detector split across
    adjacent lines that actually belong to one word/sentence/bubble) into a
    single sentence: union their bounding box, re-OCR that crop once, and
    delete the originals."""
    ids = list(dict.fromkeys(req.sentence_ids))
    if len(ids) < 2:
        raise HTTPException(400, "Select at least two sentences to merge")

    with Session(engine) as session:
        sentences = session.query(Sentence).filter(Sentence.id.in_(ids)).all()
        if len(sentences) != len(ids):
            raise HTTPException(404, "One or more sentences not found")
        if any(s.page_id != page_id for s in sentences):
            raise HTTPException(400, "All sentences must belong to the given page")

        x1 = min(s.x1 for s in sentences)
        y1 = min(s.y1 for s in sentences)
        x2 = max(s.x2 for s in sentences)
        y2 = max(s.y2 for s in sentences)
        prob = min(s.prob for s in sentences)  # weakest link, same convention as ctd.py's own line merge
        v_count = sum(1 for s in sentences if s.direction == "v")
        direction = "v" if v_count * 2 >= len(sentences) else "h"

        # Any manually-confirmed text on the pieces is concatenated in reading order
        # (no separator — Japanese doesn't space words) rather than discarded.
        order = reading_order.reading_order([(s.x1, s.y1, s.x2, s.y2) for s in sentences])
        merged_user_text = "".join(sentences[i].user_text for i in order if sentences[i].user_text) or None

        order_locked = session.get(Page, page_id).order_locked
        # On a manually-ordered page, the merged box inherits the earliest position
        # among the pieces it replaces, since the heuristic won't get a chance to place it.
        merged_order_index = min(s.order_index for s in sentences) if order_locked else 0

        occurrence_ids = [
            oid for (oid,) in session.query(WordOccurrence.id).filter(WordOccurrence.sentence_id.in_(ids)).all()
        ]
        if occurrence_ids:
            session.query(WordLookup).filter(WordLookup.occurrence_id.in_(occurrence_ids)).delete(synchronize_session=False)
            session.query(WordOccurrence).filter(WordOccurrence.id.in_(occurrence_ids)).delete(synchronize_session=False)
        # Any continuation link pointing at one of the merged-away boxes is dropped —
        # its endpoint no longer exists, and there's no unambiguous single piece
        # among the merged set to hand it off to.
        session.query(Sentence).filter(Sentence.continues_into_id.in_(ids)).update(
            {"continues_into_id": None}, synchronize_session=False
        )
        for s in sentences:
            session.delete(s)
        session.commit()

    ocr_text = await _ocr_region(page_id, x1, y1, x2, y2)

    with Session(engine) as session:
        s = Sentence(
            page_id=page_id,
            x1=x1, y1=y1, x2=x2, y2=y2,
            direction=direction,
            prob=prob,
            ocr_text=ocr_text,
            user_text=merged_user_text,
            order_index=merged_order_index,
        )
        session.add(s)
        session.flush()
        _store_occurrences(session, s.id, ocr_text, "ocr")
        if merged_user_text:
            _store_occurrences(session, s.id, merged_user_text, "user")
        if not order_locked:
            _resort_page(session, page_id)
        session.commit()
        session.refresh(s)
        return _sentence_dict(s)


@app.put("/api/pages/{page_id}/order")
def set_page_order(page_id: int, req: PageOrderUpdate):
    """Apply a user-specified reading order (see the reorder mode in reader.html)
    and lock the page so the geometry-based heuristic stops overriding it."""
    with Session(engine) as session:
        page = session.get(Page, page_id)
        if page is None:
            raise HTTPException(404, "Page not found")

        sentences = session.query(Sentence).filter_by(page_id=page_id).all()
        by_id = {s.id: s for s in sentences}
        if set(req.sentence_ids) != set(by_id):
            raise HTTPException(400, "sentence_ids must list every sentence on the page exactly once")

        for rank, sentence_id in enumerate(req.sentence_ids):
            by_id[sentence_id].order_index = rank
        page.order_locked = True
        session.commit()

        sentences.sort(key=lambda s: s.order_index)
        return _page_response(page, sentences)


@app.post("/api/pages/{page_id}/resort")
def resort_page(page_id: int):
    """Discard any manual order fix and go back to the geometry-based heuristic."""
    with Session(engine) as session:
        page = session.get(Page, page_id)
        if page is None:
            raise HTTPException(404, "Page not found")
        page.order_locked = False
        _resort_page(session, page_id)
        session.commit()

        sentences = (
            session.query(Sentence)
            .filter_by(page_id=page_id)
            .order_by(Sentence.order_index)
            .all()
        )
        return _page_response(page, sentences)


@app.post("/api/sentences/{sentence_id}/link-next")
def link_sentences(sentence_id: int, req: SentenceLink):
    """Mark `sentence_id`'s text as continuing directly into `req.target_id` — e.g.
    one line of dialogue split across two visually separate bubbles/boxes that a
    plain merge shouldn't combine (they're genuinely different areas of the page).
    Doesn't touch geometry: repositions the target to immediately follow the
    source in reading order and locks the page, the same way manual reordering
    does, so the two are guaranteed to read back-to-back."""
    if sentence_id == req.target_id:
        raise HTTPException(400, "A sentence can't continue into itself")

    with Session(engine) as session:
        src = session.get(Sentence, sentence_id)
        dst = session.get(Sentence, req.target_id)
        if src is None or dst is None:
            raise HTTPException(404, "Sentence not found")
        if src.page_id != dst.page_id:
            raise HTTPException(400, "Both sentences must belong to the same page")

        existing = session.query(Sentence).filter_by(continues_into_id=req.target_id).first()
        if existing is not None and existing.id != sentence_id:
            raise HTTPException(400, "That box already continues from another one")

        # Cycle guard: walk forward from the proposed target and make sure it
        # never loops back to the source.
        cur, hops = dst, 0
        while cur is not None:
            if cur.id == sentence_id:
                raise HTTPException(400, "That link would create a cycle")
            hops += 1
            if hops > 1000:
                raise HTTPException(500, "Continuation chain too long to validate")
            cur = session.get(Sentence, cur.continues_into_id) if cur.continues_into_id is not None else None

        src.continues_into_id = req.target_id

        page_id = src.page_id
        sentences = (
            session.query(Sentence).filter_by(page_id=page_id).order_by(Sentence.order_index).all()
        )
        order_ids = [s.id for s in sentences]
        order_ids.remove(req.target_id)
        order_ids.insert(order_ids.index(sentence_id) + 1, req.target_id)
        by_id = {s.id: s for s in sentences}
        for rank, sid in enumerate(order_ids):
            by_id[sid].order_index = rank

        page = session.get(Page, page_id)
        page.order_locked = True
        session.commit()

        sentences = (
            session.query(Sentence).filter_by(page_id=page_id).order_by(Sentence.order_index).all()
        )
        return _page_response(page, sentences)


@app.delete("/api/sentences/{sentence_id}/link-next")
def unlink_sentence(sentence_id: int):
    with Session(engine) as session:
        s = session.get(Sentence, sentence_id)
        if s is None:
            raise HTTPException(404, "Sentence not found")
        s.continues_into_id = None
        session.commit()


# ── Routes — dictionary ─────────────────────────────────────────────────────────

@app.get("/api/sentences/{sentence_id}/tokens")
def get_sentence_tokens(sentence_id: int):
    with Session(engine) as session:
        s = session.get(Sentence, sentence_id)
        if s is None:
            raise HTTPException(404, "Sentence not found")
        source = "user" if s.user_text else "ocr"
        rows = (
            session.query(WordOccurrence, Word)
            .join(Word, Word.id == WordOccurrence.word_id)
            .filter(WordOccurrence.sentence_id == sentence_id, WordOccurrence.source == source)
            .order_by(WordOccurrence.start)
            .all()
        )
        return {"source": source, "tokens": [_occurrence_dict(o, w) for o, w in rows]}


@app.get("/api/dict/lookup")
def dict_lookup(lemma: str | None = None, surface: str | None = None, reading: str | None = None):
    if not dictionary.is_ready(engine):
        raise HTTPException(503, "Dictionary data not loaded — run setup_jmdict.py / setup_pitch_accents.py")
    if not any([lemma, surface, reading]):
        raise HTTPException(400, "Provide at least one of lemma, surface, reading")
    return dictionary.lookup(engine, lemma=lemma, surface=surface, reading=reading)


@app.get("/api/radicals")
def list_radicals():
    return [
        {"number": number, "char": char, "name": name}
        for number, (char, name) in kanji.KANGXI_RADICALS.items()
    ]


@app.get("/api/kanji/by-components")
def kanji_by_components(chars: str):
    if not kanji.is_ready(engine):
        raise HTTPException(503, "Kanji data not loaded — run setup_kanjidic.py")
    components = [c for c in dict.fromkeys(chars.split(",")) if c]
    if not components:
        raise HTTPException(400, "Provide at least one component character")
    return kanji.search_by_components(engine, components)


@app.get("/api/kanji/{char}")
def kanji_lookup(char: str):
    if not kanji.is_ready(engine):
        raise HTTPException(503, "Kanji data not loaded — run setup_kanjidic.py")
    data = kanji.lookup(engine, char)
    if data is None:
        raise HTTPException(404, "No KANJIDIC2 entry for this character")
    return data


@app.get("/api/kanji/{char}/svg")
def kanji_svg(char: str):
    """Raw KanjiVG stroke-order SVG — served as-is (not the DTW-normalized point
    arrays in static/db.json, which stretch each axis independently to fill [0,1]
    and so distort proportions for thin/simple strokes like 一) so the frontend
    can animate strokes at their true visual proportions."""
    try:
        path = kanjivg_db.DATA_DIR / f"{ord(char):05x}.svg"
    except TypeError:
        raise HTTPException(404, "No stroke data for this character")
    if not path.exists():
        raise HTTPException(404, "No stroke data for this character")
    return FileResponse(path, media_type="image/svg+xml")


@app.post("/api/word-occurrences/{occurrence_id}/resolve")
def resolve_word_occurrence(occurrence_id: int, req: WordResolve):
    with Session(engine) as session:
        occ = session.get(WordOccurrence, occurrence_id)
        if occ is None:
            raise HTTPException(404, "Word occurrence not found")
        word = session.get(Word, occ.word_id)
        occ.dict_entry_id = req.dict_entry_id
        occ.resolved_by = "user"
        session.commit()
        session.refresh(occ)
        return _occurrence_dict(occ, word)


@app.post("/api/word-lookups", status_code=201)
def log_word_lookup(req: WordLookupCreate):
    with Session(engine) as session:
        if session.get(WordOccurrence, req.occurrence_id) is None:
            raise HTTPException(404, "Word occurrence not found")
        session.add(WordLookup(
            occurrence_id=req.occurrence_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        session.commit()
    return {"status": "logged"}


@app.post("/api/segmentation-overrides", status_code=201)
def create_segmentation_override(req: SegmentationOverrideCreate):
    if not req.words or any(not w for w in req.words):
        raise HTTPException(400, "words must be a non-empty list of non-empty strings")
    if "".join(req.words) != req.span_text:
        raise HTTPException(400, "words must concatenate back to exactly span_text")

    with Session(engine) as session:
        existing = session.query(SegmentationOverride).filter_by(span_text=req.span_text).first()
        if existing is None:
            existing = SegmentationOverride(span_text=req.span_text)
            session.add(existing)
        existing.words_json = json.dumps(req.words, ensure_ascii=False)
        existing.reading = req.reading if len(req.words) == 1 else None
        session.commit()  # commit the override first — load_overrides() reads via a separate connection

        _reapply_override_everywhere(session, req.span_text)
        session.commit()

    return {"status": "saved"}
