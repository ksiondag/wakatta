# Study Tools — Desired Outcomes

Design catalogue, not built state — see `README.md` for what exists. Written 2026-08-02.

**How to use this document.** It exists so the same design conversation doesn't get had
twice, not as a spec to implement. The working method is to build the smallest useful thing,
use it, and let real problems pick the next increment. Where this document contains a design
rather than a decision, that design is provisional and probably wrong in detail — it is worth
less than a week of actually using the thing. Prefer deleting from here over adding to it.

## The organizing principle

**Study material is generated from content already worked through — never authored
separately.** Every card traces back to a page, a panel, a timestamp. This is what makes
"where have I met this word before" possible at all, and it ties the transcription work to
the study work: transcribing isn't preparation for studying, it *is* studying, and its output
is what everything else is made of.

## Stated preferences driving the design

- Transcription is the current daily activity, and it's practice, not chores — regular
  handwriting is the point, so the canvas is the surface everything else defers to
- Reading already-transcribed pages is the future default activity, not yet the common case
- Correcting a box is one-time-only, per box, ever — correction tools shouldn't hold
  permanent screen space
- iPad-first, because the pen is how the writing happens
- Fewer things visible at once
- No reason to show OCR text once a known-truth transcription is saved
- No reason to show continuation links at all — they're context for downstream auto
  translation, not for the reader
- Knowing a word in context is often good enough; not every word needs to climb to production
- The real goal: regularly interact with words on an SRS interval, using sentences actually
  encountered in the wild — books, manga, video, audio

---

## A. Content sources

| Source | Truth comes from | How it gets in |
|---|---|---|
| **PDF manga** (built) | The user, via transcription | Upload → CTD → OCR → correct → known-truth |
| **Video + audio** (anime, drama) | Subtitle track if present, ASR otherwise | Load → segment by subtitle timing → confirm/correct lines as boxes are corrected now |
| **Audio-only** (podcast, audiobook) | Existing transcript if any, ASR otherwise | Load → segment → confirm lines |

---

## B. The model is wrong: a rectangle is not a sentence

**Decided.** The current `Sentence` row is a rectangle with text in it, and that conflates
three unrelated things: a layout anchor (where text sits, what gets drawn and resized), a
position in reading order, and a container of text. None of those is a sentence. A sentence
is a span of language and has no reason to respect any of them — it can break across boxes
and across pages.

The strain already shows: `continues_into_id` exists precisely because sentences outgrow
boxes, and it is enforced same-page, so a sentence broken by a page turn is currently
unrepresentable.

### Segment

What the rectangle actually is. An **anchor** + direction + reading order + a text fragment.
The anchor is a page rectangle *or* a time range, which is what lets audio and video use the
same model — audio has no rectangles and needs none. A segment holds as much text as belongs
together visually, which may be several sentences or part of one.

This generalization is cheap now and gets more expensive in proportion to what gets built on
the page-only assumption first.

### Break

**One mechanism defines sentences.** A break sits *between* two characters — not at an offset,
so it survives editing around it. Sentences are whatever lies between consecutive breaks,
reading segments in order.

- **Break inside a segment** — a marker in its text. Two possible forms: a sentinel codepoint
  that can't occur in Japanese (rides along through edits for free, needs stripping wherever
  text is consumed), or storing the text as an ordered list of pieces (structural, but every
  edit path becomes split/join logic).
- **Break at a segment seam** — no character position exists to hold a marker, so it's a flag
  on the boundary.
- **No break at a seam** means the sentence runs on. `continues_into_id` disappears: linking
  is the *absence* of a break, and cross-page continuation stops being a special case.

Auto-place breaks after 。！？ and closing 」』, and at every seam by default; the human
corrects exceptions. The gesture is toggling a break between two characters — tap the gap to
split, tap the marker to merge. No mode to enter or exit, unlike the current link mode.

Consequence worth having: tokenization currently runs per box, so a sentence split across two
balloons is tokenized as two fragments. Tokenizing whole sentences gives Unidic more to work
with, and better readings are what study activity #2 depends on.

Word occurrences keep local offsets into their segment. Sentence structure and word structure
become independent annotations over the same text.

---

## C. Working surfaces

The reader panel shows every tool for every job simultaneously, in one flat column at uniform
visual weight. Its elements operate at three scopes — character (draw, radicals, From OCR, ⌫),
sentence (your text, ask model, direction, fix words, links, delete), and page (box editing,
merge, reorder, visibility) — with no hierarchy. That flatness, more than the element count,
is what makes it busy.

Three surfaces, separated by how often each is needed:

- **Transcribe** — recurring, the actual study. Page, a large canvas, candidates, your text.
  OCR text only while `user_text` is empty. Deserves more room than it has: the canvas is
  320px inside a 360px panel, and it's what gets used most.
- **Correct** — once per box, ever. Ask model, direction, fix words, breaks, delete, box
  geometry, merge, reorder. Half of this already exists as page-toolbar toggles, so folding
  the panel's correction buttons in with them makes one mode instead of two surfaces.
- **Read** — rare now, the future default. Tokenized text, dictionary popover, no canvas.

Note **From OCR** is a character-finding tool, not an OCR display — it stays useful after
truth is saved, unlike the OCR text row.

### Open
- Explicit mode toggle, or inferred from state? Inferred is less clutter, but can feel like
  the UI moving on its own
- Is word lookup done *while* transcribing, or purely a reading activity? If the former, Words
  stays in Transcribe and the split is less clean
- How much bigger should the canvas be on iPad — panel width, wider panel, or overlaying the
  page?

---

## D. Study activities

Numbered as originally listed. Each is a way of interacting with a word or kanji; see §E for
how they get scheduled and unlocked.

- [ ] **1. Audio → write the characters** (dictation). Play a segment, write by hand, reveal,
      diff per character. Auto-gradable via the existing DTW recognizer. This is the activity
      that actually measures handwriting recall, since transcription only measures copying.
- [ ] **2. Written Japanese → hiragana.** Auto-gradable. Depends on readings being right,
      which is downstream of per-occurrence resolution and reading overrides.
- [ ] **3. Shadowing.** Two very differently sized outcomes: ungraded (play/pause/loop/speed,
      text shown or hidden) is useful alone; graded needs ASR and mic capture.
- [ ] **4. Kanji → readings.** Dictionary readings (KANJIDIC2), or the reading it actually had
      in a word encountered. The second is more useful and the data exists.
- [ ] **5. Kanji → stroke order.** Draw it, validate order/direction/shape. All data present
      (KanjiVG + DTW); the Stroke Validation item in the README.
- [ ] **6. Kanji → its components.** KRADFILE has the decomposition, the radical picker is
      already the input surface.
- [ ] **7. Radical review.** Radical → name and meaning. The 214-entry table is in `kanji.py`.
      Smallest of the eight.
- [ ] **8. Word repetition via where the word was met.** Reveal every place it's been
      encountered, each tappable to open that page at that box (or that timestamp). The data
      exists today; this is mostly presentation.
- [ ] **Confusable discrimination** (not in the original eight). The From OCR work already
      produces, per character, a ranked list of visually similar characters scored by a model
      trained on manga type — a confusable set for free.

---

## E. Progress, scheduling, cards

**Decided: words are the unit of progress.** Getting words right is what progress means.
Words already have identity in the schema (`Word`, deduped by lemma+reading across the
library); sentences do not need stable identity and can stay derived from breaks, with nothing
attached to them that an edit could invalidate.

### Cards grow per word

**Decided in shape.** A word accumulates *different* cards over time rather than having one
card forever — the diversity Anki lacks. Reaching a threshold unlocks another way to interact
with the same word. Unlocking is load management: meeting a word shouldn't immediately expose
eight ways of being tested on it.

Two unlock drivers, wanting different cards:

- **Doing well** → a deeper facet (recognition → reading → production → writing). Locks in
  what's known by proving more.
- **Doing badly** → a different route to the same facet (components, the sentence it was met
  in, audio). Not harder, another road in.

Most words stop early. "Context is often good enough" is what keeps card counts sane, and it
implies a **target depth per word** — open question who sets it (frequency in the library, an
explicit flag, or derived from repeated lookups).

### Scheduling

**Leaning, not decided:** schedule the *word*, choose the card at presentation time from its
unlocked set. Per-card intervals are the Anki answer and are where card explosion lives; per-
word gives diversity for free and stops card count from driving workload. Cost: writing decays
faster than recognition, so one interval under-tests the fragile facet — weight card selection
toward the stalest facet to recover most of that.

Cards render against a **real encountered sentence**, so the same card type shows different
content each time. Prefer a sentence where every *other* word is known, otherwise the card
tests three things at once — which makes "find a sentence where I know all the words" the card
renderer, not just a reading-practice feature. Diversity is capped by encounter count: a word
met once has one sentence, which is another argument for card variety.

### Evidence, not reviews

**Decided.** The scheduler consumes evidence, each with a timestamp, a facet, and a strength —
not "reviews". Early review then stops being a special case; elapsed fraction does the work.

| Event | Facet | Strength |
|---|---|---|
| Dictionary opened (`WordLookup`, already logged) | — | Strong fail |
| Word on a page read, no lookup | Recognition | Weak pass |
| Word hand-copied while transcribing | Writing | Weak, motor only — no recall tested |
| Word in a sentence shown during another review | Recognition | Weak pass |
| Kanji → reading, answered | Reading production | Strong pass |
| Audio → wrote characters unaided | Writing production | Strong pass |

The lookup signal is the best in the system and unavailable to apps that don't own the reading
surface: unprompted, unfakeable, already recorded.

### Interval policy

- **Strong pass:** restart the clock and grow the interval, scaled by how *due* it was.
  `new = interval * growth ^ min(1, elapsed / interval)` — full multiplier when on time, a
  small honest bonus when early. Roughly the shape FSRS reaches from a fitted model.
- **Weak pass:** restart the clock, don't grow the interval. This makes reading the primary
  review channel and drills the fallback for words reading doesn't reach — a word met
  constantly never comes due, one that goes quiet does, and the threshold is its own interval
  rather than a global constant.
- **Fail:** shorten. Reset belongs to failure, never to earliness — punishing a word for
  appearing in the text being studied would penalize the core activity.
- Frequency bias cuts both ways: as *interval growth* it's a trap (は, する drifting to huge
  intervals untested), as a *drill gate* it's correct (they need no flashcards). Weak evidence
  should gate drills, not grow intervals.
- **Log everything from day one, schedule conservatively.** These thresholds can't be chosen
  in advance; with logged evidence they can be answered empirically later from real reading.

---

## F. Sequencing observations

Relative cost, for choosing what to do first. Not a recommendation.

| Class | Items |
|---|---|
| Have the data, mostly presentation | 8, 7, 4, 6 |
| Have the data, needs real new logic | 5 (stroke validation) |
| Needs a new ingestion pipeline first | 1, 3, all video/audio |
| Needs correction quality first | 2 (readings must be right) |
| Blocks the most future work if deferred | the segment/break model (§B) |

## G. Open questions

- Are the eight separate drills to choose from, or one mixed daily queue?
- Is study iPad-only, or desktop too?
- For video: watching *in* the app, or mining sentences out of something watched elsewhere?
- Who sets a word's target depth?
- Sentinel codepoint or list-of-pieces for in-segment breaks?
