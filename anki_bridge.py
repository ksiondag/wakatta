"""Anki collection bridge — mirrors a synced Anki collection into the shared db.

Wakatta owns no cards of its own here. It keeps a private Anki collection under
data/anki/ that syncs against the self-hosted sync server as just another client
(the same standing as the desktop app or a phone), and projects the parts it needs
— notes, their decks, and a tokenized word index — into the shared data/wakatta.db.

The projected tables (anki_notes/anki_note_words) follow the same convention as
dictionary.py's dict_entries and kanji.py's kanji_entries: bulk-imported raw-SQL
reference data, decoupled from the ORM models, wiped and rebuilt rather than
migrated. The Anki collection is the source of truth; these tables are a cache and
can be dropped at any time.

The one exception is `words`: Anki-derived tokens are deduplicated into the app's
canonical Word table so a word met in a manga page and the same word sitting in a
Core 2000 note are one identity. anki_note_words holds a soft ref to words.id, the
same way WordOccurrence does — no FK, since this table is rebuilt wholesale.
"""

import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.engine import Engine

import dictionary

COLLECTION_DIR = Path("data/anki")
COLLECTION_PATH = COLLECTION_DIR / "collection.anki2"

# Anki's own field-level markup, stripped before tokenization: HTML tags, sound/image
# references, and the furigana bracket notation (漢字[かんじ] -> 漢字) that would
# otherwise reach fugashi as literal kana inside the surface form.
_RE_HTML = re.compile(r"<[^>]+>")
_RE_MEDIA = re.compile(r"\[(?:sound|anki):[^\]]*\]")
_RE_FURIGANA = re.compile(r"\[[^\]]*\]")
_RE_JAPANESE = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")

# Subtitle-derived notes (movies2anki) carry two kinds of parenthetical that are not
# part of the spoken line, and both wreck tokenization if left in:
#   （ノックの音）  full-width — a sound effect or speaker label, not dialogue
#   閉(し)めました   half-width — a reading gloss; leaving it in makes Unidic read
#                    閉 as its on'yomi へい and strand し as a separate verb
# Only kana-only contents are stripped, so a genuine parenthetical aside survives.
_RE_PAREN_FULL = re.compile(r"（[^）]*）")
_RE_PAREN_KANA = re.compile(r"[(（]([぀-ヿー]+)[)）]")

# Which fields of a known notetype carry indexable Japanese, and what each one *is*.
#
# Necessary rather than nice-to-have: most of these notetypes carry the same sentence
# several times over in different renderings (Core 2000 repeats its example as
# Expression / Reading / Sentence-Kana / Sentence-Clozed), and indexing all of them
# counts every word three or four times — which would quietly skew any frequency- or
# coverage-based ranking built on this index later.
#
# "word" = the note's headword, the thing the card is nominally about.
# "sentence" = running text the word was met in.
_FIELD_MAP: dict[str, dict[str, str]] = {
    "Core 2000": {"Vocabulary-Kanji": "word", "Expression": "sentence"},
    "movies2anki": {"Expression": "sentence"},
    "DaKanji": {"Kanji": "word", "Example": "sentence"},
    "DaKanji2": {"Kanji": "word", "Example": "sentence"},
    "passjapanesetest.com: JLPT Vocabulary": {
        "kanji": "word", "ex1_ja": "sentence", "ex2_ja": "sentence"},
    "Japanese Radicals": {"Example Usage": "sentence"},
    "PrettyYomitan": {"Expression": "word", "Sentence": "sentence"},
}

# Fallback for notetypes not in the map: skip fields that duplicate another with
# furigana baked in, or that hold media/metadata rather than text.
_SKIP_FIELD_HINTS = ("furigana", "audio", "video", "image", "link", "index", "url",
                     "sequencemarker", "frequency", "pos", "tag", "kana", "reading",
                     "clozed", "note")

# Reentrant, with a shared handle and a depth count: the Rust backend takes an
# exclusive file lock, so a nested open_collection() must reuse the handle the outer
# one already has rather than trying to open a second (a plain Lock deadlocks here,
# and an RLock alone would open the collection twice). Callers that legitimately nest
# — answering a card and then asking for the next one — then just work.
_lock = threading.RLock()
_collection = None
_depth = 0

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS anki_notes (
        note_id    INTEGER PRIMARY KEY,   -- Anki's own note id (epoch ms at creation)
        notetype   TEXT NOT NULL,
        decks      TEXT NOT NULL,         -- JSON list: deck names this note's cards live in
        tags       TEXT NOT NULL,
        fields     TEXT NOT NULL,         -- JSON object: {field name: raw field value}
        card_count INTEGER NOT NULL,
        has_audio  INTEGER NOT NULL,      -- any field carrying a [sound:...] reference
        mod        INTEGER NOT NULL       -- Anki note mtime, epoch seconds
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_anki_notes_notetype ON anki_notes(notetype)",
    """
    CREATE TABLE IF NOT EXISTS anki_note_words (
        id      INTEGER PRIMARY KEY,
        note_id INTEGER NOT NULL,
        word_id INTEGER NOT NULL,   -- soft ref -> words.id (ORM table), no FK: rebuilt wholesale
        field   TEXT NOT NULL,      -- which field the token came from
        role    TEXT NOT NULL,      -- "word" (the note's headword) | "sentence" | "unknown"
        surface TEXT NOT NULL,
        start   INTEGER NOT NULL,   -- char offset into the cleaned field text
        end     INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_anki_note_words_note ON anki_note_words(note_id)",
    "CREATE INDEX IF NOT EXISTS idx_anki_note_words_word ON anki_note_words(word_id)",
    """
    CREATE TABLE IF NOT EXISTS anki_sync_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
]


# ── Collection access ──────────────────────────────────────────────────────────

def credentials() -> tuple[str, str, str] | None:
    """(endpoint, username, password) from the environment, or None if unconfigured."""
    url = os.getenv("ANKI_SYNC_URL")
    user = os.getenv("ANKI_SYNC_USER")
    password = os.getenv("ANKI_SYNC_PASS")
    if not (url and user and password):
        return None
    return url, user, password


def media_dirs() -> list[Path]:
    """Directories to look for a note's media in, most specific first.

    The bridge syncs the collection but deliberately not the media (1.3 GB, and it
    would be a third copy of files already on this machine). So playback reads from
    an existing store instead: ANKI_MEDIA_DIR, normally pointed at the sync server's
    own media folder, whose files are named exactly as the [sound:...] references
    are. Falls back to the bridge's own media dir, which is what a deployment on a
    different host from the sync server would populate with col.sync_media().
    """
    dirs = []
    configured = os.getenv("ANKI_MEDIA_DIR")
    if configured:
        dirs.append(Path(configured).expanduser())
    dirs.append(COLLECTION_DIR / "collection.media")
    return [d for d in dirs if d.is_dir()]


def media_path(filename: str) -> Path | None:
    """Resolve a media filename to a readable file, or None.

    Rejects anything that isn't a bare filename, and re-checks containment after
    resolving, so a symlink or crafted name can't escape the media directory —
    this is reachable by anything that can reach the server.
    """
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        return None
    for base in media_dirs():
        candidate = (base / filename).resolve()
        if candidate.is_file() and candidate.is_relative_to(base.resolve()):
            return candidate
    return None


@contextmanager
def open_collection():
    """Open the local Anki collection, serialized against concurrent use.

    The Rust backend takes an exclusive lock on the file, so every caller in this
    process has to queue; opening per operation (rather than holding one handle for
    the server's lifetime) also means a sync's changes are always visible to the
    next reader.
    """
    from anki.collection import Collection

    global _collection, _depth
    COLLECTION_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _depth == 0:
            _collection = Collection(str(COLLECTION_PATH))
        _depth += 1
        try:
            yield _collection
        finally:
            _depth -= 1
            if _depth == 0:
                _collection.close()
                _collection = None


def sync(full_download_if_needed: bool = True) -> dict:
    """Sync the local collection against the configured server.

    A collection that has never synced needs a one-time full download; after that
    every sync is incremental. Full *upload* is never performed automatically —
    that direction overwrites the server, and nothing here should be able to do
    that as a side effect of a refresh.
    """
    from anki.sync_pb2 import SyncCollectionResponse

    creds = credentials()
    if creds is None:
        raise RuntimeError(
            "Anki sync is not configured — set ANKI_SYNC_URL, ANKI_SYNC_USER and "
            "ANKI_SYNC_PASS (see .env.example)."
        )
    endpoint, user, password = creds

    with open_collection() as col:
        auth = col.sync_login(username=user, password=password, endpoint=endpoint)
        out = col.sync_collection(auth, sync_media=False)
        required = out.required
        names = {v.number: v.name for v in SyncCollectionResponse.ChangesRequired.DESCRIPTOR.values}

        if required == SyncCollectionResponse.FULL_DOWNLOAD and full_download_if_needed:
            # server_usn is media-sync bookkeeping and is not carried on a collection
            # sync response; None is what the desktop client passes for a download.
            col.full_upload_or_download(auth=auth, server_usn=None, upload=False)
            return {"status": "full_download", "detail": "collection downloaded from server"}
        if required == SyncCollectionResponse.FULL_UPLOAD:
            return {
                "status": "full_upload_required",
                "detail": "server and local collection have diverged; resolve in the Anki "
                          "desktop app rather than here",
            }
        return {"status": names.get(required, str(required)).lower()}


def full_upload(confirm: str = "") -> dict:
    """Overwrite the server with this collection. Destructive, and deliberately awkward.

    `sync()` will never do this on its own: a full upload discards whatever the server
    holds, and every other device then has to take a one-time full download. But some
    clean-ups can only be published this way — removing a notetype is a schema change,
    and Anki refuses to express it as an incremental sync — and an experiment that has
    been tried and rejected should be removable rather than left lying around because
    the tooling made it inconvenient.

    Check before calling, because this function cannot:
      * the server's revlog count matches this collection's, so no reviews are lost;
      * no other device holds unsynced work, since it will have to full-download;
      * the server's current state is backed up, because it is about to be replaced.

    Pass confirm="overwrite server" to acknowledge all three.
    """
    if confirm != "overwrite server":
        return {"error": 'refused: pass confirm="overwrite server" (see the docstring)'}
    creds = credentials()
    if creds is None:
        raise RuntimeError("Anki sync is not configured — see .env.example")
    endpoint, user, password = creds
    with open_collection() as col:
        auth = col.sync_login(username=user, password=password, endpoint=endpoint)
        before = col.db.scalar("select count(*) from revlog")
        col.full_upload_or_download(auth=auth, server_usn=None, upload=True)
        return {"status": "uploaded", "notes": col.note_count(), "revlog": before,
                "note": "every other device must now take a one-time full download"}


# ── Projection into the shared db ──────────────────────────────────────────────

def is_ready(engine: Engine) -> bool:
    if not sa_inspect(engine).has_table("anki_notes"):
        return False
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM anki_notes LIMIT 1")).first() is not None


def _clean_field(value: str) -> str:
    """Strip Anki markup so what reaches fugashi is the bare Japanese text."""
    value = _RE_MEDIA.sub("", value)
    value = _RE_HTML.sub("", value)
    value = _RE_FURIGANA.sub("", value)
    value = _RE_PAREN_KANA.sub("", value)
    value = _RE_PAREN_FULL.sub(" ", value)
    return value.replace("&nbsp;", " ").strip()


def _field_role(notetype: str, field_name: str, value: str) -> str | None:
    """The role to index this field under, or None to skip it entirely."""
    if not _RE_JAPANESE.search(value or ""):
        return None
    mapping = _FIELD_MAP.get(notetype)
    if mapping is not None:
        return mapping.get(field_name)  # None => not indexed for this notetype
    lowered = field_name.lower()
    if any(hint in lowered for hint in _SKIP_FIELD_HINTS):
        return None
    return "unknown"


def _get_or_create_word_id(conn, lemma: str, reading: str) -> int:
    """Dedupe into the app's canonical `words` table, matching _get_or_create_word()
    in server.py but over raw SQL — this runs inside the bulk rebuild, not a session."""
    conn.execute(
        text("INSERT OR IGNORE INTO words (lemma, reading) VALUES (:l, :r)"),
        {"l": lemma, "r": reading},
    )
    return conn.execute(
        text("SELECT id FROM words WHERE lemma = :l AND reading = :r"),
        {"l": lemma, "r": reading},
    ).scalar_one()


def build_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        # These tables are a rebuildable cache, so a schema change is a drop rather
        # than a migration — cheaper than an ALTER path we'd have to keep correct,
        # and the next rebuild() repopulates from the collection anyway.
        if sa_inspect(engine).has_table("anki_note_words"):
            columns = {c["name"] for c in sa_inspect(engine).get_columns("anki_note_words")}
            if "role" not in columns:
                conn.execute(text("DROP TABLE anki_note_words"))
        for stmt in _SCHEMA_STATEMENTS:
            conn.execute(text(stmt))


def rebuild(engine: Engine, tagger, progress=None) -> dict:
    """Re-project the whole Anki collection into anki_notes/anki_note_words.

    Wholesale rather than incremental: the collection is ~5k notes, tokenization is
    CPU-only and cheap, and a full rebuild sidesteps having to reason about notes
    deleted or re-typed on another device since the last pass.
    """
    build_schema(engine)
    overrides = dictionary.load_overrides(engine)

    with open_collection() as col:
        deck_names = {d.id: d.name for d in col.decks.all_names_and_ids()}
        notetype_names = {m.id: m.name for m in col.models.all_names_and_ids()}
        note_ids = col.find_notes("")
        rows, word_rows = [], []

        for i, nid in enumerate(note_ids):
            note = col.get_note(nid)
            notetype = col.models.get(note.mid)
            field_names = [f["name"] for f in notetype["flds"]]
            fields = dict(zip(field_names, note.fields))
            cards = note.cards()
            decks = sorted({deck_names.get(c.did, "") for c in cards})
            has_audio = any(_RE_MEDIA.search(v or "") for v in note.fields)

            rows.append({
                "note_id": nid,
                "notetype": notetype_names.get(note.mid, ""),
                "decks": json.dumps(decks, ensure_ascii=False),
                "tags": " ".join(note.tags),
                "fields": json.dumps(fields, ensure_ascii=False),
                "card_count": len(cards),
                "has_audio": int(has_audio),
                "mod": note.mod or 0,
            })

            for name, raw in fields.items():
                role = _field_role(rows[-1]["notetype"], name, raw or "")
                if role is None:
                    continue
                cleaned = _clean_field(raw)
                if not cleaned:
                    continue
                for tok in dictionary.tokenize(cleaned, tagger, overrides=overrides):
                    word_rows.append({
                        "note_id": nid,
                        "lemma": tok["lemma"],
                        "reading": tok["lemma_reading"],
                        "field": name,
                        "role": role,
                        "surface": tok["surface"],
                        "start": tok["start"],
                        "end": tok["end"],
                    })

            if progress and i % 200 == 0:
                progress(i, len(note_ids))

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM anki_note_words"))
        conn.execute(text("DELETE FROM anki_notes"))
        for row in rows:
            conn.execute(text(
                "INSERT INTO anki_notes (note_id, notetype, decks, tags, fields, "
                "card_count, has_audio, mod) VALUES (:note_id, :notetype, :decks, "
                ":tags, :fields, :card_count, :has_audio, :mod)"
            ), row)
        for row in word_rows:
            word_id = _get_or_create_word_id(conn, row["lemma"], row["reading"])
            conn.execute(text(
                "INSERT INTO anki_note_words (note_id, word_id, field, role, surface, start, end) "
                "VALUES (:note_id, :word_id, :field, :role, :surface, :start, :end)"
            ), {**row, "word_id": word_id})

    return {"notes": len(rows), "tokens": len(word_rows)}


# ── Queries ────────────────────────────────────────────────────────────────────

def decks(engine: Engine, indexed_only: bool = True) -> list[dict]:
    """Deck names with note counts, from the projection rather than the collection —
    a deck a note's cards are split across is counted once per deck it touches.

    `indexed_only` drops decks holding no Japanese at all (Phone Numbers, Spark,
    YouTube Channels...). The test is whether the tokenizer found anything, not the
    deck's name, so a Japanese deck named something else still shows up.

    `notes` and `with_audio` count *indexed* notes, so a deck like Default — 156
    notes, 2 of them Japanese — reports 2 rather than implying 154 more to work
    through. `notes_total` keeps the real figure for when that matters.
    """
    with engine.connect() as conn:
        indexed = {r[0] for r in conn.execute(text("SELECT DISTINCT note_id FROM anki_note_words"))}
        total: dict[str, int] = {}
        counts: dict[str, int] = {}
        audio: dict[str, int] = {}
        leeches: dict[str, int] = {}
        for note_id, decks_json, has_audio, tags in conn.execute(
            text("SELECT note_id, decks, has_audio, tags FROM anki_notes")
        ):
            is_indexed = note_id in indexed
            is_leech = "leech" in (tags or "").split()
            for name in json.loads(decks_json):
                total[name] = total.get(name, 0) + 1
                counts.setdefault(name, 0)
                audio.setdefault(name, 0)
                leeches.setdefault(name, 0)
                # Leeches are counted over every note in the deck, not just indexed
                # ones, so the badge matches what the leech filter actually returns —
                # a card being hard to remember has nothing to do with whether the
                # tokenizer found Japanese in it.
                leeches[name] += is_leech
                if is_indexed:
                    counts[name] += 1
                    audio[name] += has_audio
    return [
        {"name": name, "notes": counts[name], "notes_total": total[name],
         "with_audio": audio[name], "leeches": leeches[name]}
        for name in sorted(total)
        if counts[name] or not indexed_only
    ]


def notes(engine: Engine, *, deck: str | None = None, notetype: str | None = None,
          missing_audio: bool = False, leech: bool = False,
          limit: int = 50, offset: int = 0) -> dict:
    clauses, params = [], {"limit": limit, "offset": offset}
    if deck:
        clauses.append("decks LIKE :deck")
        params["deck"] = f'%"{deck}"%'
    if notetype:
        clauses.append("notetype = :notetype")
        params["notetype"] = notetype
    if missing_audio:
        clauses.append("has_audio = 0")
    if leech:
        # Anki tags are space-separated; pad both sides so this can't match a tag
        # that merely contains "leech" as a substring.
        clauses.append("(' ' || tags || ' ') LIKE '% leech %'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with engine.connect() as conn:
        total = conn.execute(text(f"SELECT COUNT(*) FROM anki_notes {where}"), params).scalar_one()
        rows = conn.execute(text(
            f"SELECT note_id, notetype, decks, tags, fields, card_count, has_audio "
            f"FROM anki_notes {where} ORDER BY note_id LIMIT :limit OFFSET :offset"
        ), params).mappings().all()
    return {"total": total, "notes": [_note_dict(r) for r in rows]}


def note(engine: Engine, note_id: int) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT note_id, notetype, decks, tags, fields, card_count, has_audio "
            "FROM anki_notes WHERE note_id = :id"
        ), {"id": note_id}).mappings().first()
        if row is None:
            return None
        words = conn.execute(text(
            "SELECT w.word_id, w.field, w.role, w.surface, w.start, w.end, wd.lemma, wd.reading "
            "FROM anki_note_words w JOIN words wd ON wd.id = w.word_id "
            "WHERE w.note_id = :id ORDER BY w.field, w.start"
        ), {"id": note_id}).mappings().all()
    out = _note_dict(row)
    out["words"] = [dict(w) for w in words]
    return out


def _note_dict(row) -> dict:
    return {
        "note_id": row["note_id"],
        "notetype": row["notetype"],
        "decks": json.loads(row["decks"]),
        "tags": row["tags"].split() if row["tags"] else [],
        "fields": json.loads(row["fields"]),
        "card_count": row["card_count"],
        "has_audio": bool(row["has_audio"]),
    }


def stats(engine: Engine) -> dict:
    """Coverage summary: how much of the Anki collection the word index reaches, and
    how many distinct words it contributes to the shared Word table."""
    with engine.connect() as conn:
        notes_total = conn.execute(text("SELECT COUNT(*) FROM anki_notes")).scalar_one()
        indexed = conn.execute(text(
            "SELECT COUNT(DISTINCT note_id) FROM anki_note_words")).scalar_one()
        distinct_words = conn.execute(text(
            "SELECT COUNT(DISTINCT word_id) FROM anki_note_words")).scalar_one()
        tokens = conn.execute(text("SELECT COUNT(*) FROM anki_note_words")).scalar_one()
        no_audio = conn.execute(text(
            "SELECT COUNT(*) FROM anki_notes WHERE has_audio = 0")).scalar_one()
    return {
        "notes": notes_total,
        "notes_indexed": indexed,
        "notes_without_audio": no_audio,
        "distinct_words": distinct_words,
        "tokens": tokens,
        "collection_present": COLLECTION_PATH.exists(),
        "configured": credentials() is not None,
    }
