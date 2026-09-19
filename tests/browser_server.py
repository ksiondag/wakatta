"""Isolated browser-test server: no writes to the user's study history or Anki."""
from pathlib import Path
import sqlite3
import tempfile
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
import dictionary
import study

TMP = tempfile.TemporaryDirectory(prefix="wakatta-browser-")
DB = Path(TMP.name)/"study.db"
with study.connect() as src:
    dst=sqlite3.connect(DB);src.backup(dst);dst.close()
with study.connect(DB) as db:
    db.execute('DELETE FROM revisits');db.execute('DELETE FROM captures')
    db.execute('DELETE FROM activity');db.execute('DELETE FROM progress')
app=FastAPI()
app.mount('/static',StaticFiles(directory=study.ROOT/'static'),name='static')
app.include_router(study.create_router(DB))
engine=create_engine(f"sqlite:///{study.ROOT/'data/wakatta.db'}")

@app.get('/api/dict/lookup')
def lookup(lemma:str|None=None,surface:str|None=None,reading:str|None=None):
    return dictionary.lookup(engine,lemma=lemma,surface=surface,reading=reading)
