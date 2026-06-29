"""Handwriting recognition server + page ingestion pipeline."""

import asyncio
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import fitz
import numpy as np
from fastapi import FastAPI, HTTPException
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
DB_JSON_PATH = Path("static/db.json")
DPI = 150


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


engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


# ── Global state ───────────────────────────────────────────────────────────────

kvg_db: kanjivg_db.KanjiVGDatabase = None
mocr: MangaOcr = None


# ── Image pipeline helpers ─────────────────────────────────────────────────────

def extract_page(pdf_path: Path, page_num: int, dpi: int = DPI) -> Image.Image:
    doc = fitz.open(str(pdf_path))
    pix = doc[page_num].get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def crop_region(image: Image.Image, x1: int, y1: int, x2: int, y2: int) -> Image.Image:
    margin = 4
    w, h = image.size
    return image.crop((
        max(0, x1 - margin), max(0, y1 - margin),
        min(w, x2 + margin), min(h, y2 + margin),
    ))


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
    global kvg_db, mocr
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)

    kvg_db = await asyncio.to_thread(kanjivg_db.load_database)
    if not DB_JSON_PATH.exists():
        print("Generating static/db.json for PWA offline cache ...")
        await asyncio.to_thread(_generate_db_json, kvg_db)

    print("Loading manga-ocr ...")
    mocr = await asyncio.to_thread(MangaOcr)
    print("manga-ocr ready.")

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


class ProcessRequest(BaseModel):
    pdf_path: str
    page_num: int


class SentenceUpdate(BaseModel):
    user_text: str


# ── Response helpers ───────────────────────────────────────────────────────────

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


# ── Routes ─────────────────────────────────────────────────────────────────────

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


@app.post("/api/process")
async def process_page(req: ProcessRequest):
    pdf_path = Path(req.pdf_path)
    if not pdf_path.exists():
        raise HTTPException(404, f"PDF not found: {pdf_path}")

    # Return cached result if page was already processed
    with Session(engine) as session:
        work = session.query(Work).filter_by(path=str(pdf_path)).first()
        work_id = work.id if work else None
        if work_id is not None:
            page = session.query(Page).filter_by(work_id=work_id, page_num=req.page_num).first()
            if page is not None:
                sentences = session.query(Sentence).filter_by(page_id=page.id).all()
                return _page_response(page, sentences)

    # Run pipeline outside any DB session (blocking work in threads)
    page_image = await asyncio.to_thread(extract_page, pdf_path, req.page_num)
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

    # Persist
    with Session(engine) as session:
        if work_id is None:
            work = Work(title=pdf_path.stem, path=str(pdf_path))
            session.add(work)
            session.flush()
            work_id = work.id

        page = Page(
            work_id=work_id,
            page_num=req.page_num,
            width=page_image.width,
            height=page_image.height,
        )
        session.add(page)
        session.flush()
        page_id = page.id

        page_image.save(PAGES_DIR / f"{page_id}.png")

        for region, ocr_text in zip(regions, region_texts):
            x1, y1, x2, y2 = region.xyxy
            session.add(Sentence(
                page_id=page_id,
                x1=x1, y1=y1, x2=x2, y2=y2,
                direction=region.direction,
                prob=region.prob,
                ocr_text=ocr_text,
            ))

        session.commit()

        page = session.get(Page, page_id)
        sentences = session.query(Sentence).filter_by(page_id=page_id).all()
        return _page_response(page, sentences)


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
