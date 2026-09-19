# Repository audit — 2026-09-19

Scope: source and design-document review, read-only SQLite queries, Anki inspection
on a temporary SQLite backup, and targeted JavaScript checks. No live sync,
collection migration, history deletion, or scheduler-setting changes. This is the initial audit snapshot, before the watching feature was implemented.
Subsequent validation is documented in WATCHING.md. Findings below distinguish
observed data from inferred risks; this is not an exhaustive security audit.

## Fit to the current goal

The application already provides a substantial local manga reader: PDF detection
and OCR, corrections, dictionary/pitch/kanji lookup, word identities, and an Anki
bridge using Anki's real scheduler. Those are useful foundations. Source material
and existing vocabulary can already meet through canonical words.

The priorities in STUDY_TOOLS.md were written around transcription and handwriting.
The current user goal emphasizes anime with Japanese subtitles, real Japanese,
conversation, and preparation for December N1. Preserve handwriting as an option;
the default workflow should now help the user return to comprehensible Japanese.
At the time of this audit there was no anime ingestion or general activity log.
The new library/player now implements these foundations; test diagnostics and
mining-to-Anki remain future work. Anki derivation makes audio-writing cards
from existing notes.

## Findings, in priority order

### High: lookup history loses its identity during retokenization

`server.py:_store_occurrences` deletes and recreates occurrence rows, while
`WordLookup` stores only occurrence_id. Ordinary corrections and segmentation
overrides can invalidate those references. Read-only inspection found **125/201
lookup events already orphaned**. SQLite foreign keys are declared but this
engine does not enable enforcement. Reused row IDs could also silently attach
old events to different tokens; that possibility was not measured.

Before using lookup counts for recommendations, preserve stable event context
(word, source text, source location at lookup time), or deliberately remap stable
occurrences during edits. Don't silently reconstruct the 125 missing identities.
Migration and regression tests should cover text edits, segmentation changes,
deletions, and ID reuse. Merely deleting orphan events stops neither recurrence
nor loss of useful history.

### High: triage does not safely represent per-card return destinations

`anki_triage.py:apply` writes origin tags on the **note**, while moving individual
**cards**. Multiple sibling cards with different original decks can share several
origin tags; graduation selects the first and clears every origin tag. One card
can return to the wrong deck and its sibling can lose its return information.
Replacing spaces with underscores also makes existing underscores ambiguous.
This is a source-level finding; no triage was run against the user's collection.

Use durable per-card origin metadata, with an explicit path for legacy ambiguity.
Test siblings, staggered graduation, and names containing spaces/underscores
before relying on automated moves. Moving into Japanese::Hard alone also does
not exclude cards from reviews of the Japanese parent; exclusion depends on the
selected deck and configuration.

### Medium: review success can conceal a sync failure

`server.py:anki_review_answer` commits a review and can return `sync.status=failed`.
`static/drill.html:submit` displays the rating but ignores the sync result. The
user can leave believing another device has the updated review. Retrying the
answer itself would be the wrong recovery operation.

Show local-save and sync status separately, and retry sync without re-answering.
Also add a regression check for duplicate submissions and lost responses before
building more automatic scheduling actions.

### Medium: defaults pull attention toward writing and an endless queue

The home route is handwriting; the review picker preferred derived audio writing;
there was no bounded review visit, and the empty-deck message encouraged raising
new-card limits. This conflicts with the current 60–90-minute study budget.

First increment implemented: Core 2000 preference, explicit review-time budget,
and a return-to-input message on completion. Handwriting and all existing decks
remain available. A reading/listening-oriented home is a later product decision.

### Medium: startup and testing are coupled to heavyweight resources

`server.py:lifespan` loads KanjiVG, manga-ocr, and fugashi before serving, even for
Anki-only use. Interrupted ingestion resumes at startup. Source-level review and
isolated checks are therefore safer than starting the app simply to inspect it.
No tracked automated test suite was found. Isolate data operations and lazy-load
OCR when revisiting backend reliability; add tests for the stateful bugs above.

### Documentation and signal quality

- README's stack says FSRS, but Anki owns scheduling and the local collection has
  FSRS disabled. Updated the stack description in this iteration.
- The review-answer endpoint docstring says validations win, while code correctly
  gives an explicit user rating precedence. Updated that docstring.
- A dictionary-panel open is evidence of interest or uncertainty, not proof that
  a word is unknown. Likewise, repeated immediate successes do not prove durable
  retention. Avoid turning either directly into mastery or scheduling changes.
- `Sentence` actually models a page rectangle, as STUDY_TOOLS.md already explains.
  Its same-page links do not solve cross-page or timed-media segmentation. Don't
  force anime phrases through fake PDF pages; use a source anchor when that small
  capture feature is built.

## Validation of this iteration

The updated drill's entire inline JavaScript parses with Node. Isolated execution
checks exercised the exact budget boundary, prevention of a next-card fetch after
expiry, changing the budget, hiding the budget in practice mode, and explicit
restart. Full browser interaction, mobile layout, and live Anki review/sync were
not tested. The timer is a per-page elapsed-time aid, not persistent habit tracking;
it never automatically submits an answer and only stops before the next card.

See WATCHING.md for the implemented watching/listening workflow and its validation.
The personal study plan and full local audit snapshot remain in ignored data/notes/.
