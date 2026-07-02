"""JMdict + Kanjium pitch-accent lookup, and fugashi tokenization helpers.

Reference data (dict_entries/dict_index/pitch_accents) is bulk-imported via
raw SQL into whatever engine the caller passes in (server.py's shared
data/wakatta.db engine) rather than through SQLAlchemy ORM models — it's
read-only, ~200k-row ETL output with no relational integration into the
Work/Page/Sentence tree, so keeping it decoupled from the app's model classes
means "wipe and reimport on a new JMdict release" is just deleting rows.
"""

import json
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

DATA_DIR = Path("data/dictionary")
ACCENTS_TXT = DATA_DIR / "accents.txt"

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS dict_entries (
        id   INTEGER PRIMARY KEY,   -- JMdict id/seq
        data TEXT NOT NULL          -- JSON blob: {"kanji": [...], "kana": [...], "sense": [...]}
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dict_index (
        surface  TEXT NOT NULL,
        entry_id INTEGER NOT NULL,
        is_kana  INTEGER NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0   -- 1 if jmdict-simplified marked this form "common"
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dict_index_surface ON dict_index(surface)",
    """
    CREATE TABLE IF NOT EXISTS pitch_accents (
        id       INTEGER PRIMARY KEY,
        headword TEXT NOT NULL,
        reading  TEXT NOT NULL,     -- hiragana
        pattern  TEXT NOT NULL      -- e.g. "0" or "1,3" (comma-separated accent-drop positions)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pitch_reading ON pitch_accents(reading)",
]


def _katakana_to_hiragana(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def is_ready(engine: Engine) -> bool:
    if not inspect(engine).has_table("dict_entries"):
        return False
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM dict_entries LIMIT 1")).first() is not None


def build_db(engine: Engine, force: bool = False) -> None:
    """Idempotent one-time ETL: raw JMdict JSON + Kanjium accents.txt -> the shared
    db's dict_entries/dict_index/pitch_accents tables. Safe to call on every server
    start; skips if already populated unless force=True."""
    if is_ready(engine) and not force:
        return

    jmdict_files = sorted(DATA_DIR.glob("jmdict-eng-*.json"))
    if not jmdict_files or not ACCENTS_TXT.exists():
        raise FileNotFoundError(
            "Missing data/dictionary/jmdict-eng-*.json or accents.txt — "
            "run `uv run setup_jmdict.py` and `uv run setup_pitch_accents.py` first."
        )

    jmdict_path = jmdict_files[-1]
    print(f"[dictionary] Building dictionary tables from {jmdict_path.name} + {ACCENTS_TXT.name} ...")
    raw = json.loads(jmdict_path.read_text(encoding="utf-8"))
    words = raw["words"]

    with engine.begin() as conn:
        for stmt in _SCHEMA_STATEMENTS:
            conn.execute(text(stmt))
        # Clear any partial data from a previous interrupted/forced build.
        conn.execute(text("DELETE FROM dict_entries"))
        conn.execute(text("DELETE FROM dict_index"))
        conn.execute(text("DELETE FROM pitch_accents"))

        for word in words:
            entry_id = int(word["id"])
            conn.execute(
                text("INSERT INTO dict_entries (id, data) VALUES (:id, :data)"),
                {"id": entry_id, "data": json.dumps({
                    "kanji": word.get("kanji", []),
                    "kana": word.get("kana", []),
                    "sense": word.get("sense", []),
                }, ensure_ascii=False)},
            )
            for k in word.get("kanji", []):
                conn.execute(
                    text("INSERT INTO dict_index (surface, entry_id, is_kana, priority) VALUES (:s, :e, 0, :p)"),
                    {"s": k["text"], "e": entry_id, "p": int(bool(k.get("common")))},
                )
            for k in word.get("kana", []):
                conn.execute(
                    text("INSERT INTO dict_index (surface, entry_id, is_kana, priority) VALUES (:s, :e, 1, :p)"),
                    {"s": k["text"], "e": entry_id, "p": int(bool(k.get("common")))},
                )

        for line in ACCENTS_TXT.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            headword, reading, pattern = parts
            conn.execute(
                text("INSERT INTO pitch_accents (headword, reading, pattern) VALUES (:h, :r, :p)"),
                {"h": headword, "r": reading, "p": pattern},
            )
    print(f"[dictionary] Loaded {len(words)} JMdict entries.")


def _entries_for_surface(engine: Engine, surface: str, kana_only: bool = False) -> list[int]:
    """All distinct entry ids whose kanji-form or kana-form text matches `surface`
    exactly, across every reading that surface has in JMdict."""
    query = "SELECT DISTINCT entry_id FROM dict_index WHERE surface = :s"
    if kana_only:
        query += " AND is_kana = 1"
    with engine.connect() as conn:
        rows = conn.execute(text(query + " ORDER BY entry_id"), {"s": surface}).fetchall()
    return [r[0] for r in rows]


def resolve_candidates(engine: Engine, *, lemma: str | None = None, surface: str | None = None,
                        reading: str | None = None) -> list[int]:
    """Fallback chain: lemma exact -> surface exact -> reading-only (kana-form) match.
    Returns distinct candidate entry ids from whichever step first found a hit —
    an empty list means no dictionary entry matches at all, more than one means
    the word is a genuine homograph (multiple JMdict entries share this form).

    When a surface/lemma match returns several entries (e.g. 私 has 13 JMdict
    entries — わたし/あたし/わたくし/etc — all written 私), and a `reading` is
    also known (tokenization already disambiguated the actual pronunciation
    used), narrow to just the entries whose kana-forms include that reading
    before treating it as ambiguous. If narrowing eliminates everything (the
    tokenizer's reading doesn't exactly match any entry's kana text), fall
    back to the unnarrowed set rather than silently returning nothing.
    """
    with engine.connect() as conn:
        for key in (lemma, surface):
            if not key:
                continue
            rows = conn.execute(
                text("SELECT DISTINCT entry_id FROM dict_index WHERE surface = :s ORDER BY entry_id"),
                {"s": key},
            ).fetchall()
            if not rows:
                continue
            entry_ids = [r[0] for r in rows]
            if reading and len(entry_ids) > 1:
                placeholders = ", ".join(f":id{i}" for i in range(len(entry_ids)))
                params = {f"id{i}": eid for i, eid in enumerate(entry_ids)}
                params["r"] = reading
                narrowed = conn.execute(
                    text(f"SELECT DISTINCT entry_id FROM dict_index "
                         f"WHERE entry_id IN ({placeholders}) AND surface = :r AND is_kana = 1"),
                    params,
                ).fetchall()
                if narrowed:
                    return sorted(r[0] for r in narrowed)
            return entry_ids
        if reading:
            rows = conn.execute(
                text("SELECT DISTINCT entry_id FROM dict_index WHERE surface = :s AND is_kana = 1 ORDER BY entry_id"),
                {"s": reading},
            ).fetchall()
            if rows:
                return [r[0] for r in rows]
    return []


def lookup(engine: Engine, *, lemma: str | None = None, surface: str | None = None,
           reading: str | None = None) -> dict:
    """Full picker payload for the dictionary panel: EVERY JMdict entry sharing this
    kanji/kana surface form, across all of its readings — not narrowed to whichever
    reading the tokenizer happened to guess. `resolve_candidates` (used to decide
    auto-resolution at tokenize/store time) narrows by reading as a heuristic, but
    that heuristic can pick the wrong reading entirely (e.g. 風 auto-analyzed as ふう
    "style/manner" when かぜ "wind" was actually meant) — if this function reused
    that narrowed set, the user could never see or pick the reading the tokenizer
    got wrong. So this always expands back out to every entry for the surface;
    `reading` (if given) is only used to sort a matching entry first, never to
    exclude the rest.
    """
    entry_ids: list[int] = []
    for key in (lemma, surface):
        if not key:
            continue
        entry_ids = _entries_for_surface(engine, key)
        if entry_ids:
            break
    if not entry_ids and reading:
        entry_ids = _entries_for_surface(engine, reading, kana_only=True)

    candidates: list[dict] = []
    with engine.connect() as conn:
        if entry_ids:
            placeholders = ", ".join(f":id{i}" for i in range(len(entry_ids)))
            params = {f"id{i}": eid for i, eid in enumerate(entry_ids)}
            entry_rows = conn.execute(
                text(f"SELECT id, data FROM dict_entries WHERE id IN ({placeholders})"), params
            ).fetchall()
            data_by_id = {row[0]: json.loads(row[1]) for row in entry_rows}
            priority_rows = conn.execute(
                text(f"SELECT entry_id, MAX(priority) FROM dict_index "
                     f"WHERE entry_id IN ({placeholders}) GROUP BY entry_id"),
                params,
            ).fetchall()
            priority_by_id = dict(priority_rows)

            for eid in entry_ids:
                data = data_by_id.get(eid)
                if data is None:
                    continue
                kana_forms = [k["text"] for k in data.get("kana", [])]
                pitch_rows = conn.execute(
                    text(f"SELECT DISTINCT pattern FROM pitch_accents WHERE reading IN "
                         f"({', '.join(f':k{i}' for i in range(len(kana_forms)))})"),
                    {f"k{i}": k for i, k in enumerate(kana_forms)},
                ).fetchall() if kana_forms else []
                candidates.append({
                    "entry_id": eid,
                    "kanji_forms": [k["text"] for k in data.get("kanji", [])],
                    "kana_forms": kana_forms,
                    "senses": [
                        {
                            "pos": s.get("partOfSpeech", []),
                            "glosses": [g["text"] for g in s.get("gloss", []) if g.get("lang", "eng") == "eng"],
                        }
                        for s in data.get("sense", [])
                    ],
                    "priority": priority_by_id.get(eid, 0),
                    "pitch_accents": [row[0] for row in pitch_rows],
                    "reading_match": bool(reading and reading in kana_forms),
                })
            # Reading-matching entries first (most likely to be what the tokenizer/user
            # meant), then by JMdict's own "common" priority.
            candidates.sort(key=lambda c: (not c["reading_match"], -c["priority"]))

    return {
        "candidates": candidates,
        "jj_definitions": [],   # always empty in v1 — reserved slot for a future JJ source
        "jj_available": False,
    }


def _tokenize_raw(text_: str, tagger) -> list[dict]:
    """Tokenize `text_` with fugashi, returning one dict per morpheme with char offsets —
    the tokenizer's un-corrected opinion, before any user segmentation overrides apply.

    Uses Unidic's `orthBase`/`kanaBase` features (the word's dictionary/citation
    form and that form's reading) for `lemma`/`lemma_reading` rather than the raw
    `lemma`/`kana` features — `lemma` is sometimes suffixed with a disambiguation
    tag (e.g. "私-代名詞") and can use archaic/variant kanji (e.g. "為る" for する),
    neither of which matches JMdict headwords. `kana`/`kanaBase` are katakana;
    JMdict kana-forms and Kanjium readings are hiragana, so both readings are
    converted here once.
    """
    results = []
    pos = 0
    for word in tagger(text_):
        f = word.feature
        surface = word.surface
        if getattr(f, "pos1", "?") == "補助記号":
            pos = text_.index(surface, pos) + len(surface)
            continue
        start = text_.index(surface, pos)
        end = start + len(surface)
        pos = end

        lemma = getattr(f, "orthBase", None) or surface
        lemma_reading = _katakana_to_hiragana(getattr(f, "kanaBase", None) or lemma)
        reading = _katakana_to_hiragana(getattr(f, "kana", None) or surface)

        results.append({
            "surface": surface,
            "lemma": lemma,
            "lemma_reading": lemma_reading,
            "reading": reading,
            "pos": getattr(f, "pos1", None) or "?",
            "start": start,
            "end": end,
        })
    return results


def load_overrides(engine: Engine) -> dict[str, tuple[list[str], str | None]]:
    """Load all user-defined segmentation corrections as {span_text: (words, reading)}.
    `words` is what `span_text` should actually be divided into — one entry means
    "always treat this exact span as a single atomic word" (a merge correction, e.g.
    Unidic doesn't recognize ナウシカ as a name and splits it into ナウ + シカ); more
    than one entry means "split this span into these words instead" (a correction for
    the opposite mistake, where the tokenizer wrongly fused separate words together).
    `reading` is only meaningful for the single-word case — Unidic can't be asked for
    a reading of a made-up atomic token, so the user may supply one (this is what lets
    a kanji name get the right reading; pure kana spans don't need it, see tokenize())."""
    if not inspect(engine).has_table("segmentation_overrides"):
        return {}
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT span_text, words_json, reading FROM segmentation_overrides")).fetchall()
    return {span: (json.loads(words_json), reading) for span, words_json, reading in rows}


def _apply_overrides(base_tokens: list[dict], tagger, overrides: dict[str, tuple[list[str], str | None]],
                      max_window: int = 6) -> list[dict]:
    """Re-group `base_tokens` wherever a run of consecutive tokens' concatenated surface
    matches a registered override span — longest match wins so a 3-token override takes
    priority over a 2-token one starting at the same position."""
    out: list[dict] = []
    i, n = 0, len(base_tokens)
    while i < n:
        match = None
        for j in range(min(n, i + max_window), i, -1):
            combined = "".join(t["surface"] for t in base_tokens[i:j])
            if combined in overrides:
                match = (j, overrides[combined])
                break
        if match is None:
            out.append(base_tokens[i])
            i += 1
            continue

        j, (words, forced_reading) = match
        start, end = base_tokens[i]["start"], base_tokens[j - 1]["end"]
        if len(words) == 1:
            # Merge correction: force one atomic token — Unidic will never be asked to
            # re-split it, since that's exactly the mistake being corrected.
            reading = forced_reading or _katakana_to_hiragana(words[0])
            out.append({
                "surface": words[0], "lemma": words[0], "lemma_reading": reading,
                "reading": reading, "pos": "名詞", "start": start, "end": end,
            })
        else:
            # Split correction: re-tokenize each corrected word in isolation so it gets
            # its own proper lemma/reading/pos from Unidic, now that it's not fused to
            # its neighbor.
            cursor = start
            for w in words:
                for sub in _tokenize_raw(w, tagger):
                    out.append({**sub, "start": cursor + sub["start"], "end": cursor + sub["end"]})
                cursor += len(w)
        i = j
    return out


def tokenize(text_: str, tagger, overrides: dict[str, tuple[list[str], str | None]] | None = None) -> list[dict]:
    """Tokenize `text_`, applying any user-defined segmentation overrides (see
    load_overrides) on top of Unidic's raw analysis."""
    base = _tokenize_raw(text_, tagger)
    if not overrides:
        return base
    return _apply_overrides(base, tagger, overrides)
