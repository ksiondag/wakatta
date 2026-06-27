"""Handwriting recognition server."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import kanjivg_db

db: kanjivg_db.KanjiVGDatabase = None

DB_JSON_PATH = Path("static/db.json")


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncio.to_thread(kanjivg_db.load_database)
    if not DB_JSON_PATH.exists():
        print("Generating static/db.json for PWA offline cache ...")
        await asyncio.to_thread(_generate_db_json, db)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory="static"), name="static")


class Stroke(BaseModel):
    points: list[dict]  # [{"x": float, "y": float}, ...]


class RecognizeRequest(BaseModel):
    strokes: list[Stroke]


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/recognize")
def recognize(req: RecognizeRequest):
    raw = [s.points for s in req.strokes]
    return kanjivg_db.recognize(raw, db, top_n=12)
