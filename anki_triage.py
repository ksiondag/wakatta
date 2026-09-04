"""Sort failures into "nearly had it" and "didn't know it", by behaviour not self-report.

Anki's lapse handling treats every failure identically: the word you missed by a hair
and the word you have never once retrieved both collapse to the same one-day interval.
Measured over real drilling, 57% of failures pass the very next relearning step — so
more than half of the daily load is cards being punished for a moment's hesitation.

The distinction is visible without asking for it. A card that fails once and then
recovers was a jog. A card that fails repeatedly, or fails again immediately after
being shown the answer, was not known. Using observed behaviour rather than a button
matters twice over: self-assessment of failure magnitude is exactly the judgement the
four-button scale was dropped for, and behaviour also catches a lucky guess — pass a
hard word by chance today, fail it twice tomorrow, and it lands in the hard bucket
anyway.

Cards judged hard move to their own deck, out of the daily rotation, to be tackled
deliberately. Their origin is recorded in a tag so the move is reversible, and cards
that later string together passes come home on their own.
"""

import collections
import itertools

import anki_derive

HARD_DECK = "Japanese::Hard"
ORIGIN_TAG_PREFIX = "wakatta::from::"

# A card is hard if, inside the window, it failed at least this many times, or failed
# twice in a row without recovering in between. Two is deliberately low: the bucket is
# meant to be worked through, not to be a graveyard, and a card that comes back and
# behaves will graduate out again.
HARD_AGAINS = 2
WINDOW_DAYS = 14

# Consecutive passes, while sitting in the hard deck, that send a card home.
GRADUATE_PASSES = 2


def _sequences(col, card_ids: set[int], window_days: int) -> dict[int, list[int]]:
    """Recent answer eases per card, oldest first."""
    if not card_ids:
        return {}
    ids = ",".join(map(str, card_ids))
    cutoff = f"(select (crt + 86400 * {col.sched.today - window_days}) * 1000 from col)"
    seqs = collections.defaultdict(list)
    for cid, ease in col.db.all(
        f"SELECT cid, ease FROM revlog WHERE cid IN ({ids}) AND id > {cutoff} ORDER BY cid, id"
    ):
        seqs[cid].append(ease)
    return seqs


def _verdict(eases: list[int]) -> str:
    agains = sum(1 for e in eases if e == 1)
    if not agains:
        return "clean"
    longest_run = max((len(list(g)) for k, g in itertools.groupby(eases) if k == 1),
                      default=0)
    return "hard" if (agains >= HARD_AGAINS or longest_run >= 2) else "jog"


def classify(col, *, window_days: int = WINDOW_DAYS, deck: str = "Japanese") -> dict:
    """Split recently-reviewed cards into clean / jog / hard, plus who can graduate."""
    in_scope = set(col.find_cards(f'deck:"{deck}::*" OR deck:"{deck}"'))
    hard_now = set(col.find_cards(f'deck:"{HARD_DECK}"'))
    seqs = _sequences(col, in_scope | hard_now, window_days)

    buckets = {"clean": [], "jog": [], "hard": [], "graduate": []}
    for cid, eases in seqs.items():
        verdict = _verdict(eases)
        if cid in hard_now:
            # In the bucket already: a run of clean passes at the end sends it home.
            tail = list(itertools.takewhile(lambda e: e != 1, reversed(eases)))
            if len(tail) >= GRADUATE_PASSES:
                buckets["graduate"].append(cid)
            continue
        if verdict == "hard":
            buckets["hard"].append(cid)
        else:
            buckets[verdict].append(cid)
    return buckets


def _describe(col, cid: int) -> dict:
    card = col.get_card(cid)
    note = col.get_note(card.nid)
    # Read the headword through anki_derive's per-notetype field map. Taking the first
    # non-empty field instead gives Core 2000's "Optimized-Voc-Index", i.e. a number.
    notetype = col.models.get(note.mid)
    fields = dict(zip([f["name"] for f in notetype["flds"]], note.fields))
    src = anki_derive._extract({"notetype": notetype["name"], "fields": fields}) or {}
    word = src.get("word") or anki_derive._plain(
        next((f for f in note.fields if f.strip()), ""))[:16]
    return {"card_id": cid, "word": word, "deck": col.decks.name(card.did),
            "lapses": card.lapses, "interval": card.ivl}


def preview(col, *, window_days: int = WINDOW_DAYS) -> dict:
    b = classify(col, window_days=window_days)
    return {
        "window_days": window_days,
        "counts": {k: len(v) for k, v in b.items()},
        "hard": [_describe(col, c) for c in b["hard"][:40]],
        "graduate": [_describe(col, c) for c in b["graduate"][:40]],
    }


def apply(col, *, window_days: int = WINDOW_DAYS) -> dict:
    """Move hard cards out of rotation and bring graduates back.

    The origin deck is stored as a tag on the note rather than assumed, because a
    note's cards can live in different decks and "put it back where it was" has to
    mean somewhere specific.
    """
    b = classify(col, window_days=window_days)
    hard_id = col.decks.id(HARD_DECK)

    moved = []
    for cid in b["hard"]:
        card = col.get_card(cid)
        origin = col.decks.name(card.odid or card.did)
        if origin == HARD_DECK:
            continue
        note = col.get_note(card.nid)
        tag = ORIGIN_TAG_PREFIX + origin.replace(" ", "_")
        if tag not in note.tags:
            note.tags.append(tag)
            col.update_note(note)
        col.set_deck([cid], hard_id)
        moved.append({"card_id": cid, "from": origin})

    returned = []
    for cid in b["graduate"]:
        card = col.get_card(cid)
        note = col.get_note(card.nid)
        origin = next((t[len(ORIGIN_TAG_PREFIX):].replace("_", " ")
                       for t in note.tags if t.startswith(ORIGIN_TAG_PREFIX)), None)
        if not origin:
            continue
        did = col.decks.id_for_name(origin)
        if did is None:
            continue
        col.set_deck([cid], did)
        note.tags = [t for t in note.tags if not t.startswith(ORIGIN_TAG_PREFIX)]
        col.update_note(note)
        returned.append({"card_id": cid, "to": origin})

    return {"moved_to_hard": len(moved), "returned_home": len(returned),
            "moved": moved[:40], "returned": returned[:40]}
