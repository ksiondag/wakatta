"""Tunable thresholds for stroke validation, and the samples to tune them against.

`stroke_validation` has to draw two lines that no amount of reasoning can place
correctly: how far a stroke may deviate before it stops being the same stroke, and
how much better a cross-assignment must score before it counts as evidence of wrong
stroke order. The original values were derived by perturbing KanjiVG's own strokes
with gaussian noise, which turned out to describe a plotter rather than a hand.

So the thresholds live here, in the database, editable at runtime — and every
attempt is kept, with whatever the validator concluded, so the values can be chosen
against real handwriting instead of guessed. An attempt the writer marks as actually
correct while the validator failed it is the single most useful row in the table.

Distances are stored and compared as *deviation*: DTW distance divided by the number
of sample points, which makes the number mean "average distance between the stroke
you drew and the reference, as a fraction of the character's box". 0.10 is a tenth of
the character's width away, on average. That is a quantity a person can reason about;
the raw DTW sum, which scales with sample count, is not.
"""

import json
import time

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.engine import Engine

# Deviation units (see module docstring). Starting points only — the whole purpose of
# this module is that they get replaced by values measured from real attempts.
DEFAULTS = {
    # A stroke closer than this is simply right.
    "ok": 0.19,
    # Beyond this it is not a wobbly version of the reference stroke, it is a
    # different stroke.
    "poor": 0.34,
    # Reversing a stroke must improve its score by this factor before "you drew it
    # backwards" is believed.
    "reversal_ratio": 0.6,
    # A cross-assignment must beat staying in place by this factor before "out of
    # order" is believed. Order errors fail the card, so this is the expensive one.
    "order_ratio": 0.6,
}

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS stroke_settings (
        key   TEXT PRIMARY KEY,
        value REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stroke_samples (
        id         INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        char       TEXT NOT NULL,
        strokes    TEXT NOT NULL,   -- JSON: the raw drawn points, so a sample can be re-judged
        result     TEXT NOT NULL,   -- JSON: what validate() concluded at the time
        auto_ok    INTEGER NOT NULL,-- whether the validator called it correct
        user_label TEXT,            -- "correct" | "incorrect" | NULL if unjudged
        settings   TEXT NOT NULL    -- JSON: thresholds in force for that verdict
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_stroke_samples_char ON stroke_samples(char)",
    "CREATE INDEX IF NOT EXISTS idx_stroke_samples_label ON stroke_samples(user_label)",
]


def build_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        for stmt in _SCHEMA:
            conn.execute(text(stmt))


def settings(engine: Engine) -> dict:
    """Current thresholds, falling back to DEFAULTS for anything unset."""
    if not sa_inspect(engine).has_table("stroke_settings"):
        return dict(DEFAULTS)
    with engine.connect() as conn:
        stored = {k: v for k, v in conn.execute(text("SELECT key, value FROM stroke_settings"))}
    return {**DEFAULTS, **{k: stored[k] for k in DEFAULTS if k in stored}}


def set_settings(engine: Engine, values: dict) -> dict:
    build_schema(engine)
    with engine.begin() as conn:
        for key, value in values.items():
            if key not in DEFAULTS:
                continue
            conn.execute(text(
                "INSERT INTO stroke_settings (key, value) VALUES (:k, :v) "
                "ON CONFLICT(key) DO UPDATE SET value = :v"), {"k": key, "v": float(value)})
    return settings(engine)


def record(engine: Engine, char: str, strokes: list, result: dict, used: dict) -> int:
    """Keep an attempt so it can be re-judged later under different thresholds."""
    build_schema(engine)
    with engine.begin() as conn:
        return conn.execute(text(
            "INSERT INTO stroke_samples (created_at, char, strokes, result, auto_ok, "
            "user_label, settings) VALUES (:t, :c, :s, :r, :a, NULL, :g)"),
            {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "c": char,
             "s": json.dumps(strokes), "r": json.dumps(result),
             "a": int(bool(result.get("correct"))), "g": json.dumps(used)}
        ).lastrowid


def label(engine: Engine, sample_id: int, user_label: str) -> dict:
    if user_label not in ("correct", "incorrect", None):
        return {"error": "label must be 'correct' or 'incorrect'"}
    build_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE stroke_samples SET user_label = :l WHERE id = :i"),
                     {"l": user_label, "i": sample_id})
    return {"id": sample_id, "user_label": user_label}


def samples(engine: Engine, *, only_labelled: bool = False, limit: int = 200) -> list[dict]:
    build_schema(engine)
    where = "WHERE user_label IS NOT NULL" if only_labelled else ""
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT id, created_at, char, result, auto_ok, user_label, strokes "
            f"FROM stroke_samples {where} ORDER BY id DESC LIMIT :n"), {"n": limit}).mappings().all()
    out = []
    for r in rows:
        result = json.loads(r["result"])
        devs = [s.get("deviation") for s in result.get("strokes", [])
                if s.get("deviation") is not None]
        out.append({
            "id": r["id"], "created_at": r["created_at"], "char": r["char"],
            "auto_ok": bool(r["auto_ok"]), "user_label": r["user_label"],
            "issues": result.get("issues", []),
            "worst_deviation": round(max(devs), 3) if devs else None,
            "deviations": [round(d, 3) for d in devs],
            "strokes_drawn": result.get("drawn_strokes"),
            "strokes_expected": result.get("expected_strokes"),
        })
    return out


def replay(engine: Engine, candidate: dict, db) -> dict:
    """Re-judge every labelled sample under candidate thresholds.

    The point of the exercise: an attempt the writer called correct that the
    validator fails is a false alarm, and one they called wrong that it passes is a
    miss. Tuning is choosing where to sit between the two, with the counts visible.
    """
    import stroke_validation

    build_schema(engine)
    merged = {**settings(engine), **candidate}
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, char, strokes, user_label FROM stroke_samples "
            "WHERE user_label IS NOT NULL")).mappings().all()

    false_alarm = miss = agree = 0
    disagreements = []
    for r in rows:
        res = stroke_validation.validate(r["char"], json.loads(r["strokes"]), db,
                                         settings=merged)
        passed = bool(res.get("correct"))
        wanted = r["user_label"] == "correct"
        if passed == wanted:
            agree += 1
        elif wanted and not passed:
            false_alarm += 1
            disagreements.append({"id": r["id"], "char": r["char"], "kind": "false alarm",
                                  "issues": res.get("issues", [])[:2]})
        else:
            miss += 1
            disagreements.append({"id": r["id"], "char": r["char"], "kind": "missed error",
                                  "issues": []})
    return {
        "settings": merged, "labelled": len(rows), "agree": agree,
        "false_alarms": false_alarm, "missed_errors": miss,
        "disagreements": disagreements[:20],
    }
