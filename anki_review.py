"""Review Anki cards through wakatta, with a custom interaction per card type.

Anki's review loop ends in self-assessment: it shows the answer and asks whether you
got it right. For a recognition card that's the only option available — nothing can
check what happened in your head. For a *production* card it's the weak link, because
the thing being tested is externally observable and could simply be measured.

So this module keeps Anki as the scheduler and replaces only the answering step. A
card is fetched through the real v3 scheduler (so deck limits, learning steps and
ordering all behave exactly as they do in Anki), presented with whatever interaction
its notetype calls for, and answered with a rating derived from what the interaction
observed. The review lands in the collection's history like any other.

Interactions are chosen by notetype, so adding a new drilled card type is a matter of
building its interaction and registering it here — the scheduling half never changes.
Notetypes with no registered interaction fall back to Anki's own behaviour: show the
answer, let the user pick a button.
"""

from anki import scheduler_pb2

import anki_bridge
import anki_derive

# notetype name -> interaction the client should present
INTERACTIONS: dict[str, str] = {
    anki_derive.NOTETYPE_AUDIO_WRITING: "audio_writing",
    anki_derive.NOTETYPE_KANJI: "self_graded",
}
DEFAULT_INTERACTION = "self_graded"

RATINGS = {
    "again": scheduler_pb2.CardAnswer.AGAIN,
    "hard": scheduler_pb2.CardAnswer.HARD,
    "good": scheduler_pb2.CardAnswer.GOOD,
    "easy": scheduler_pb2.CardAnswer.EASY,
}

# Boundaries used only to describe how tidy the writing was. They do not affect the
# rating — see grade() — but are reported so the quality signal is visible and can be
# analysed later, once there's real drilling data to calibrate against.
CLEAN_DISTANCE = 0.75


def grade(results: list[dict | None]) -> dict:
    """Turn per-character stroke validation into a pass/fail Anki rating.

    Deliberately two-valued. Anki's SM-2 moves a card's ease permanently on Hard
    (-15pp) and Easy (+15pp), floored at 130%, and both the Anki manual and the FSRS
    FAQ note that Again/Good alone is a legitimate — sometimes better — scheme. Two
    facts about *this* grader make it the clear choice here:

    Handwriting is intrinsically variable, so "right strokes, right order, somewhat
    untidy" is the normal outcome rather than the exceptional one. A Hard that fires
    on most correct answers would walk ease down to the floor on exactly the cards
    being drilled to rescue them.

    And the confidence isn't evenly spread. Stroke count, order and direction come
    from relative comparisons with no tuned constants, and are trustworthy. Shape
    rests on thresholds calibrated against synthetic jitter, which are the least
    reliable numbers in the system. Grading on the trustworthy half and merely
    *reporting* the rest puts the uncertainty where it can't damage scheduling.

    Returns the rating, why, and a quality band describing the writing itself.
    """
    def fail(reason: str) -> dict:
        return {"rating": "again", "reason": reason, "quality": None,
                "worst_distance": None}

    if not results or any(r is None for r in results):
        return fail("not every character was attempted")

    for r in results:
        if r.get("error"):
            return fail(r["error"])
        if not r.get("count_ok"):
            return fail(f"{r['char']}: {r['drawn_strokes']} strokes, expected "
                        f"{r['expected_strokes']}")
        if not r.get("order_ok"):
            return fail(f"{r['char']}: strokes drawn out of order")
        if any(s["verdict"] == "direction" for s in r["strokes"]):
            return fail(f"{r['char']}: a stroke was drawn backwards")
        # A stroke past the poor threshold, or one matching no reference stroke at
        # all, isn't untidy handwriting — it's a different stroke.
        if any(s["verdict"] in ("wrong", "extra") for s in r["strokes"]):
            return fail(f"{r['char']}: a stroke doesn't match the character")

    distances = [s["distance"] for r in results for s in r["strokes"]
                 if s["distance"] is not None]
    worst = max(distances) if distances else 0.0
    off_shape = sum(1 for r in results for s in r["strokes"] if s["verdict"] == "shape")
    quality = ("clean" if worst <= CLEAN_DISTANCE
               else "tidy" if not off_shape else "untidy")
    return {
        "rating": "good",
        "reason": "correct" + (f" — {off_shape} stroke(s) off-shape" if off_shape else ""),
        "quality": quality,
        "worst_distance": round(worst, 3),
        "off_shape_strokes": off_shape,
    }


def _card_payload(col, queued) -> dict:
    # queued.card is a protobuf message, not the Python Card — refetch the real one,
    # which is what carries question()/answer() and the note link.
    card = col.get_card(queued.card.id)
    note = col.get_note(card.nid)
    notetype = col.models.get(note.mid)
    names = [f["name"] for f in notetype["flds"]]
    fields = dict(zip(names, note.fields))
    # anki_derive already knows which field means what for every notetype we handle
    # (Core 2000's "Reading" is the *sentence* with furigana, not the word's reading —
    # an alias list ordered by field name gets that wrong). Reuse it rather than
    # keeping a second, subtly different copy of that mapping here.
    src = anki_derive._extract({"notetype": notetype["name"], "fields": fields}) or {}
    if not src:
        def first(*names):
            for n in names:
                v = anki_derive._plain(fields.get(n, "") or "")
                if v:
                    return v
            return ""
        src = {"word": first("Word", "Kanji", "Expression", "Front"),
               "reading": first("Reading", "Kana"),
               "meaning": first("Meaning", "Meanings", "Back"),
               "sentence": first("Sentence", "Example")}

    word = src.get("word", "")
    reading = src.get("reading", "")
    meaning = src.get("meaning", "")
    sentence = src.get("sentence", "")
    audio = anki_derive._RE_SOUND.findall(fields.get("Audio", "") or "")
    return {
        "card_id": card.id,
        "note_id": card.nid,
        "notetype": notetype["name"],
        "deck": col.decks.name(card.did),
        "interaction": INTERACTIONS.get(notetype["name"], DEFAULT_INTERACTION),
        "fields": fields,
        "word": word,
        "reading": reading,
        "meaning": meaning,
        "sentence": sentence if sentence != word else "",
        "chars": list(word),
        "audio": audio[0] if audio else "",
        "question": card.question(),
        "answer": card.answer(),
        # Anki renders media as [anki:play:q:N] / [anki:play:a:N] placeholders and
        # leaves substitution to the client. These are the filenames those indices
        # refer to, in order, so the page can put real players in their place instead
        # of showing the raw tag.
        "question_av": [t.filename for t in card.question_av_tags()
                        if hasattr(t, "filename")],
        "answer_av": [t.filename for t in card.answer_av_tags()
                      if hasattr(t, "filename")],
        # What each button would do, in Anki's own words ("10m", "4d", ...) — shown
        # so an objective rating is still legible as a scheduling decision.
        "next_states": list(col.sched.describe_next_states(queued.states)),
    }


def next_card(deck: str | None = None) -> dict:
    """The next card the scheduler would serve, honouring the deck's daily limits."""
    with anki_bridge.open_collection() as col:
        if deck:
            did = col.decks.id_for_name(deck)
            if did is None:
                return {"error": f"no such deck: {deck}"}
            col.decks.select(did)
        queued = col.sched.get_queued_cards(fetch_limit=1)
        counts = {"new": queued.new_count, "learning": queued.learning_count,
                  "review": queued.review_count}
        if not queued.cards:
            return {"card": None, "counts": counts}
        return {"card": _card_payload(col, queued.cards[0]), "counts": counts}


def answer(card_id: int, rating: str, milliseconds: int = 0) -> dict:
    """Answer a card as the scheduler's current top card.

    States are re-fetched rather than round-tripped through the client: they're a
    protobuf the browser has no business holding, and re-fetching also catches the
    case where the queue moved on between question and answer, which would otherwise
    silently apply a rating to the wrong card.
    """
    if rating not in RATINGS:
        return {"error": f"unknown rating: {rating}"}
    with anki_bridge.open_collection() as col:
        card = col.get_card(card_id)
        col.decks.select(card.did)
        queued = col.sched.get_queued_cards(fetch_limit=1)
        if not queued.cards or queued.cards[0].card.id != card_id:
            return {"error": "the queue moved on — refetch the next card"}
        states = queued.cards[0].states
        labels = list(col.sched.describe_next_states(states))
        # build_answer reads the card's own review timer to fill in how long the
        # answer took; nothing started it here, so start it and overwrite below with
        # the time the client actually measured.
        card.start_timer()
        card_answer = col.sched.build_answer(
            card=card, states=states, rating=RATINGS[rating])
        if milliseconds:
            card_answer.milliseconds_taken = int(milliseconds)
        col.sched.answer_card(card_answer)
        order = ["again", "hard", "good", "easy"]
        return {
            "card_id": card_id,
            "rating": rating,
            "interval": labels[order.index(rating)] if len(labels) == 4 else None,
        }


# ── Session decks (Anki filtered decks) ────────────────────────────────────────
#
# Anki's core refuses to answer a card that isn't the scheduler's top card
# ("not at top of queue"), so choosing what to study can't be done by picking cards
# and answering them out of band. A filtered deck is the supported inversion: it
# pulls a search's worth of cards out of their home decks and *becomes* the queue,
# so selection stays ours while intervals stay Anki's.
#
# Two things to know about them. They ignore the home deck's daily limits — the
# filtered deck's own limit is the only cap — so a big one walks straight around the
# 50/day the Japanese preset sets. And with reschedule=False nothing is written to
# scheduling at all, which is the honest way to practise handwriting without
# disturbing a card's interval.

DEFAULT_SESSION_DECK = "Wakatta Drill"

# anki.decks_pb2.Deck.Filtered.SearchTerm.Order
ORDERS = {
    "oldest_reviewed": 0, "random": 1, "intervals_ascending": 2,
    "intervals_descending": 3, "lapses": 4, "added": 5, "due": 6,
    "reverse_added": 7, "retrievability_ascending": 8,
    "retrievability_descending": 9, "relative_overdueness": 10,
}


def session_decks() -> list[dict]:
    """Existing filtered decks, with how many cards are currently pulled into each."""
    out = []
    with anki_bridge.open_collection() as col:
        for d in col.decks.all_names_and_ids():
            deck = col.decks.get(d.id)
            if not deck.get("dyn"):
                continue
            terms = deck.get("terms") or []
            out.append({
                "name": d.name,
                "cards": len(col.find_cards(f'deck:"{d.name}"')),
                "search": terms[0][0] if terms else "",
                "limit": terms[0][1] if terms else 0,
                "reschedule": bool(deck.get("resched", True)),
            })
    return out


def preview_search(search: str) -> dict:
    """How many cards a search matches, before committing to a session.

    Also counts how many fall outside the Japanese tree. A search like `deck:*`
    looks harmless and quietly drags in whatever else lives in the collection —
    worth seeing before a session is built, not after.
    """
    with anki_bridge.open_collection() as col:
        try:
            matched = col.find_cards(search)
            japanese = set(col.find_cards("deck:Japanese::*"))
            return {"search": search, "matches": len(matched),
                    "outside_japanese": sum(1 for c in matched if c not in japanese)}
        except Exception as e:
            return {"search": search, "error": str(e)}


def build_session(search: str, *, name: str = DEFAULT_SESSION_DECK, limit: int = 20,
                  order: str = "relative_overdueness", reschedule: bool = True) -> dict:
    """Create or rebuild a filtered deck from an arbitrary search."""
    if order not in ORDERS:
        return {"error": f"unknown order: {order}"}
    if not search.strip():
        return {"error": "a session needs a search — an empty one matches the whole collection"}
    with anki_bridge.open_collection() as col:
        existing = col.decks.id_for_name(name)
        fd = col.sched.get_or_create_filtered_deck(deck_id=existing or 0)
        fd.name = name
        fd.allow_empty = True
        del fd.config.search_terms[:]
        term = fd.config.search_terms.add()
        term.search, term.limit, term.order = search, limit, ORDERS[order]
        fd.config.reschedule = reschedule
        try:
            did = col.sched.add_or_update_filtered_deck(fd).id
        except Exception as e:
            return {"error": str(e)}
        return {
            "deck": col.decks.name(did),
            "cards": len(col.find_cards(f'deck:"{col.decks.name(did)}"')),
            "search": search, "limit": limit, "order": order,
            "reschedule": reschedule,
        }


def end_session(name: str = DEFAULT_SESSION_DECK, delete: bool = False) -> dict:
    """Return a session's cards to their home decks, optionally removing the deck.

    Emptying is always safe: a filtered deck holds no state of its own, and every
    card remembers the deck it came from.
    """
    with anki_bridge.open_collection() as col:
        did = col.decks.id_for_name(name)
        if did is None:
            return {"error": f"no such deck: {name}"}
        returned = len(col.find_cards(f'deck:"{name}"'))
        col.sched.empty_filtered_deck(did)
        if delete:
            col.decks.remove([did])
        return {"deck": name, "returned": returned, "deleted": delete}


def decks_with_counts() -> list[dict]:
    """Every deck with what the scheduler would currently serve from it.

    Straight off the due tree rather than the mirrored index, so it includes decks
    that exist in the collection but haven't been re-indexed yet — a freshly derived
    deck, or a filtered session built a moment ago.
    """
    out = []

    def walk(node, prefix):
        # Tree nodes carry only the leaf component, so the full ::-joined path has to
        # be accumulated on the way down — looking it up by leaf name afterwards would
        # collide wherever two decks share one (Japanese::Wakatta vs a bare Wakatta).
        path = f"{prefix}::{node.name}" if prefix else node.name
        out.append({
            "name": path,
            "new": node.new_count,
            "learning": node.learn_count,
            "review": node.review_count,
            "filtered": bool(node.filtered),
        })
        for child in node.children:
            walk(child, path)

    with anki_bridge.open_collection() as col:
        for child in col.sched.deck_due_tree().children:
            walk(child, "")
    return out
