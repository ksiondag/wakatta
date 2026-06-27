"""Handwriting recognition server."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import kanjivg_db

db: kanjivg_db.KanjiVGDatabase = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await asyncio.to_thread(kanjivg_db.load_database)
    yield


app = FastAPI(lifespan=lifespan)
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
