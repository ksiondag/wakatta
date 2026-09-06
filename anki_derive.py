"""Derive new Anki notes from existing ones.

A word you already have a card for usually carries more material than the one card
tests. A Core 2000 note holds the written form, the reading, a recorded pronunciation
and an example sentence, but only ever asks "what does this written word mean?" —
so the audio sits unused, and the word is never produced, only recognised.

This module turns that surplus into additional notes:

  audio_writing — the recording alone, answered by writing the word. Trains listening
                  and handwriting production at once. Reuses the source note's own
                  [sound:...] reference verbatim, so no new media file is created and
                  nothing needs a media sync.

A per-kanji notetype used to be derived here too: a bare character on the front,
answered by producing its meanings, both readings and its components at once. It was
dropped. Four questions on one self-graded card is the "minimum information" mistake
several times over, and nothing in how Japanese is actually taught resembles being
shown a character in isolation and asked to produce everything about it — schools teach
kanji inside vocabulary, and test them with 書き取り, which is what audio_writing is.
The kanji breakdown itself is not lost: it is reference material on the answer screen,
served live from kanji.py, where it costs no reviews.

Derived notes live in their own notetypes and their own Japanese::Wakatta:: subdecks,
never mixed into the source deck, so the whole output is reversible by deleting two
decks. Every derived note records the note it came from in `SourceNoteId`, which is
also what makes derivation idempotent — a source that already has a derived note of
a given kind is skipped rather than duplicated.
"""

import re
from dataclasses import dataclass, field as dc_field

from sqlalchemy.engine import Engine

import anki_bridge

DECK_PREFIX = "Japanese::Wakatta"
_RE_KANJI = re.compile(r"[㐀-䶿一-鿿]")
_RE_HTML = re.compile(r"<[^>]+>")
_RE_SOUND = re.compile(r"\[sound:([^\]]+)\]")

NOTETYPE_AUDIO_WRITING = "Wakatta Audio Writing"


@dataclass
class Derived:
    """One note that would be (or was) created."""
    kind: str
    notetype: str
    deck: str
    fields: dict[str, str]
    source_note_id: int
    skipped: str | None = None       # reason, if this one won't be created
    note_id: int | None = None       # filled in once written


# ── Reading the source note ────────────────────────────────────────────────────

# Where each source notetype keeps the pieces a derivation needs. Notetypes that
# lack audio (DaKanji, the JLPT deck) simply can't produce an audio_writing note —
# that gap is what step 4's audio sourcing is for.
_SOURCE_ADAPTERS: dict[str, dict[str, str]] = {
    "Core 2000": {
        "word": "Vocabulary-Kanji", "reading": "Vocabulary-Kana",
        "meaning": "Vocabulary-English", "audio": "Vocabulary-Audio",
        "sentence": "Expression", "sentence_audio": "Sentence-Audio",
    },
    "DaKanji": {
        "word": "Kanji", "reading": "Kana", "meaning": "Translations",
        "audio": "Audio", "sentence": "Example",
    },
    "DaKanji2": {
        "word": "Kanji", "reading": "Kana", "meaning": "Translations",
        "audio": "Audio", "sentence": "Example",
    },
    "passjapanesetest.com: JLPT Vocabulary": {
        "word": "kanji", "reading": "kana", "meaning": "english",
        "sentence": "ex1_ja",
    },
    # Example Usage and Stroke Order Image hold inline SVG, not text, so neither is a
    # usable sentence — the radical itself is the whole content of these notes.
    "Japanese Radicals": {
        "word": "Radical", "reading": "Japanese Name", "meaning": "English Keyword",
    },
    "PrettyYomitan": {
        "word": "Expression", "reading": "Reading", "meaning": "Meaning",
        "audio": "Audio", "sentence": "Sentence",
    },
}


def _plain(value: str) -> str:
    return _RE_HTML.sub("", value or "").replace("&nbsp;", " ").strip()


def _extract(note: dict) -> dict[str, str] | None:
    """Pull the derivation inputs out of a mirrored note, or None if unsupported."""
    adapter = _SOURCE_ADAPTERS.get(note["notetype"])
    if adapter is None:
        return None
    fields = note["fields"]
    out = {}
    for key, source_field in adapter.items():
        raw = fields.get(source_field, "")
        # Audio is passed through untouched: it's a [sound:...] reference to a file
        # that already lives in this collection's media, and stripping or rewriting
        # it would break the link.
        out[key] = raw.strip() if key.endswith("audio") else _plain(raw)
    return out


# ── Notetypes ──────────────────────────────────────────────────────────────────

_CSS = """.card {
  font-family: sans-serif;
  font-size: 22px;
  text-align: center;
  color: #e0e0e0;
  background: #1a1a2e;
}
.jp { font-size: 46px; }
.reading { color: #9a9ac0; font-size: 20px; }
.meaning { color: #b0b0c8; font-size: 18px; }
.sentence { color: #8a8aa8; font-size: 18px; margin-top: 14px; }
.components { color: #9a9ac0; font-size: 20px; letter-spacing: 4px; }
.hint { color: #6a6a88; font-size: 15px; margin-top: 18px; }
"""

_SPECS = {
    NOTETYPE_AUDIO_WRITING: {
        "fields": ["Word", "Reading", "Meaning", "Audio", "Sentence",
                   "SentenceAudio", "SourceNoteId", "SourceDeck"],
        "sort": 0,
        "template": ("Listen and write", """{{Audio}}
<div class="hint">Write the word you heard.</div>""", """{{FrontSide}}
<hr id=answer>
<div class="jp">{{Word}}</div>
<div class="reading">{{Reading}}</div>
<div class="meaning">{{Meaning}}</div>
{{#Sentence}}<div class="sentence">{{Sentence}}</div>{{/Sentence}}
{{#SentenceAudio}}<div>{{SentenceAudio}}</div>{{/SentenceAudio}}"""),
    },
}


def ensure_notetypes(col) -> None:
    """Create the derived notetypes if they don't exist yet. Idempotent."""
    for name, spec in _SPECS.items():
        if col.models.by_name(name) is not None:
            continue
        nt = col.models.new(name)
        for field_name in spec["fields"]:
            col.models.add_field(nt, col.models.new_field(field_name))
        tmpl_name, qfmt, afmt = spec["template"]
        tmpl = col.models.new_template(tmpl_name)
        tmpl["qfmt"], tmpl["afmt"] = qfmt, afmt
        col.models.add_template(nt, tmpl)
        nt["css"] = _CSS
        col.models.add(nt)
        col.models.set_sort_index(col.models.by_name(name), spec["sort"])


# ── Planning ───────────────────────────────────────────────────────────────────

def _existing_sources(col, notetype_name: str) -> set[int]:
    """Source note ids that already have a derived note of this kind."""
    nt = col.models.by_name(notetype_name)
    if nt is None:
        return set()
    idx = [f["name"] for f in nt["flds"]].index("SourceNoteId")
    out = set()
    for nid in col.find_notes(f'mid:{nt["id"]}'):
        raw = col.get_note(nid).fields[idx].strip()
        if raw.isdigit():
            out.add(int(raw))
    return out


def _plan_audio_writing(src: dict, note: dict, existing: set[int]) -> Derived:
    d = Derived(
        kind="audio_writing", notetype=NOTETYPE_AUDIO_WRITING,
        deck=f"{DECK_PREFIX}::Audio Writing",
        source_note_id=note["note_id"],
        fields={
            "Word": src["word"], "Reading": src.get("reading", ""),
            "Meaning": src.get("meaning", ""), "Audio": src.get("audio", ""),
            "Sentence": src.get("sentence", ""),
            "SentenceAudio": src.get("sentence_audio", ""),
            "SourceNoteId": str(note["note_id"]),
            "SourceDeck": ", ".join(note["decks"]),
        },
    )
    if note["note_id"] in existing:
        d.skipped = "already derived"
    elif not _RE_SOUND.search(src.get("audio", "")):
        d.skipped = "source note has no audio"
    elif not src["word"]:
        d.skipped = "source note has no word"
    return d


def _plan_audio_writing(src: dict, note: dict, existing: set[int]) -> Derived:
    d = Derived(
        kind="audio_writing", notetype=NOTETYPE_AUDIO_WRITING,
        deck=f"{DECK_PREFIX}::Audio Writing",
        source_note_id=note["note_id"],
        fields={
            "Word": src["word"], "Reading": src.get("reading", ""),
            "Meaning": src.get("meaning", ""), "Audio": src.get("audio", ""),
            "Sentence": src.get("sentence", ""),
            "SentenceAudio": src.get("sentence_audio", ""),
            "SourceNoteId": str(note["note_id"]),
            "SourceDeck": ", ".join(note["decks"]),
        },
    )
    if note["note_id"] in existing:
        d.skipped = "already derived"
    elif not _RE_SOUND.search(src.get("audio", "")):
        d.skipped = "source note has no audio"
    elif not src["word"]:
        d.skipped = "source note has no word"
    return d


# KRADFILE can only spell components with JIS X 0208 characters, so radicals outside
# that codeset are written as a stand-in kanji containing them — 汁 for 氵, 忙 for 忄,
# and so on (see the comment above _RADICAL_VARIANT_ALIASES in kanji.py). That's the
# right key for matching a *drawn* radical, but showing "渇 is built from 汁" on a card
# is simply false, so display flips the mapping back to the glyph actually written.
def _radical_label(entry: dict) -> str:
    rad = entry.get("radical") or {}
    if not rad:
        return ""
    return f"{rad.get('char', '')} {rad.get('name', '')}".strip()


def plan(engine: Engine, note_ids: list[int], kinds: list[str]) -> list[Derived]:
    """Work out what would be created, without writing anything."""
    results: list[Derived] = []
    with anki_bridge.open_collection() as col:
        # Deliberately does not call ensure_notetypes(): a preview must not write to
        # the collection. Both lookups below already treat a missing notetype as
        # "nothing derived yet", which is exactly right before the first apply().
        existing_aw = _existing_sources(col, NOTETYPE_AUDIO_WRITING)

        for note_id in note_ids:
            note = anki_bridge.note(engine, note_id)
            if note is None:
                continue
            src = _extract(note)
            if src is None:
                results.append(Derived(
                    kind="-", notetype="-", deck="-", fields={},
                    source_note_id=note_id,
                    skipped=f"unsupported notetype: {note['notetype']}"))
                continue
            if "audio_writing" in kinds:
                results.append(_plan_audio_writing(src, note, existing_aw))
    return results


def apply(engine: Engine, note_ids: list[int], kinds: list[str]) -> dict:
    """Plan, then write the non-skipped notes into the collection.

    Does not sync — the caller decides when to push, so a batch of derivations
    goes up as one sync rather than one per note.
    """
    planned = plan(engine, note_ids, kinds)
    created = 0
    with anki_bridge.open_collection() as col:
        ensure_notetypes(col)
        for d in planned:
            if d.skipped:
                continue
            nt = col.models.by_name(d.notetype)
            deck_id = col.decks.id(d.deck)  # creates the deck if absent
            note = col.new_note(nt)
            names = [f["name"] for f in nt["flds"]]
            for i, name in enumerate(names):
                note.fields[i] = d.fields.get(name, "")
            col.add_note(note, deck_id)
            d.note_id = note.id
            created += 1
    return {
        "created": created,
        "skipped": sum(1 for d in planned if d.skipped),
        "notes": [d.__dict__ for d in planned],
    }


# ── Drill queue ────────────────────────────────────────────────────────────────

def drill_items(engine: Engine, *, deck: str | None = None, leech_only: bool = False,
                limit: int = 20) -> list[dict]:
    """Notes to drill as listen-and-write, newest-selected first.

    Works off derived Audio Writing notes when they exist, and falls back to reading
    any source note that already carries both audio and a word — so the drill is
    usable before a single note has been derived, straight off Core 2000.
    """
    target = deck or f"{DECK_PREFIX}::Audio Writing"
    found = anki_bridge.notes(engine, deck=target, limit=limit)
    if not found["notes"] and deck is None:
        found = anki_bridge.notes(engine, deck="Japanese::Core 2000",
                                  leech=True, limit=limit)

    items = []
    for note in found["notes"]:
        if note["notetype"] == NOTETYPE_AUDIO_WRITING:
            f = note["fields"]
            src = {"word": _plain(f.get("Word", "")), "reading": _plain(f.get("Reading", "")),
                   "meaning": _plain(f.get("Meaning", "")), "audio": f.get("Audio", "").strip(),
                   "sentence": _plain(f.get("Sentence", ""))}
        else:
            src = _extract(note)
            if src is None:
                continue
        if not src.get("word") or not _RE_SOUND.search(src.get("audio", "")):
            continue
        items.append({
            "note_id": note["note_id"],
            "deck": target,
            "word": src["word"],
            "reading": src.get("reading", ""),
            "meaning": src.get("meaning", ""),
            "audio": _RE_SOUND.findall(src["audio"])[0] if _RE_SOUND.search(src["audio"]) else "",
            "sentence": src.get("sentence", ""),
            # One canvas per character. Kana are in KanjiVG too, so a word like 触る
            # grades both halves rather than only the kanji.
            "chars": list(src["word"]),
        })
    return items
