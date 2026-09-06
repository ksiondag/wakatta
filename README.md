# Wakatta — Japanese Understanding App

A local-first tool for extracting, studying, and practicing Japanese from manga and literature.
Feed in scanned pages; get a structured study deck with kanji breakdown, stroke practice,
and SRS-driven review.

Initial target: **Nausicaä of the Valley of the Wind** (manga).

---

## Stack

| Layer | Choice |
|---|---|
| OCR | `manga-ocr` (ViT-based, CUDA-accelerated) |
| Text region detection | Custom CTD inference (`ctd.py`) using `comictextdetector.pt.onnx` |
| Tokenization | `fugashi` + MeCab (Unidic) |
| Dictionary | JMdict (`jmdict-simplified`, English) + Kanjium pitch accents — `dictionary.py`, `data/wakatta.db` |
| Database | SQLite + SQLAlchemy |
| Backend API | FastAPI + uvicorn |
| Frontend | HTML5 Canvas (PWA target) |
| SRS algorithm | FSRS |
| Kanji stroke data | KanjiVG + KANJIDIC2 |
| Handwriting recognition | KanjiVG + DTW (`kanjivg_db.py`) |

Primary machine: 4090 GPU, Arch Linux. Also runs on Framework 12 (CPU-only, slower).

---

## What's Built

### OCR Pipeline (`main.py`)
- PDF page → `PyMuPDF` → image
- Image → `ctd.py` (CTD ONNX model) → text region bounding boxes
- Each region → `manga-ocr` → Japanese string
- String → `fugashi` → tokenized words with readings and POS

### Page Reader (`server.py` + `static/page-reader.html` + `static/reader.html`)
- **Library** (`/page-reader`) — upload a PDF via file browser; server queues all pages as a
  background job immediately. Shows all works with a live progress bar; browser notification
  fires when done. Job state persisted in SQLite — interrupted jobs resume automatically on
  server restart. Pages rendered at 600 DPI and stored as PNGs in `data/pages/`; readable pages
  appear as soon as they complete, without waiting for the whole job to finish. Click "Read" on
  a ready work to open it.
- **Reader** (`/read/{work_id}`) — its own page (no library chrome) so the manga page and
  handwriting panel get the full viewport. Navigate pages with prev/next (current page kept in
  the URL's `?page=` query so reloads/bookmarks return to the same spot), SVG bbox overlay
  (green = vertical, orange = horizontal). Pinch, ctrl/cmd+wheel, or two-finger trackpad gestures
  zoom the page image only — the navbar, page nav, and handwriting panel stay put; plain
  wheel/drag pans. Default view (and the "⤢ Fit" button) fits the whole page in the panel.
  Click a region to select it; right panel shows
  auto-OCR text as reference. Draw on the handwriting canvas → recognition candidates → click
  to confirm; auto-saves to SQLite. Below the OCR text, a "Words" row shows the sentence's
  confirmed text as clickable, tokenized spans — see Dictionary Lookup below.
- **Box editing** — the "▭ Edit Boxes" toggle repurposes the page panel for drawing new
  boxes and moving/resizing existing ones (disables pan/zoom while active). A separate
  "✋✏️ Touch+Pen" toggle (persisted in `localStorage`) changes gesture *routing* instead of
  disabling anything: with it on, a finger always pans/zooms and a pen always
  creates/edits/selects boxes (and draws in the handwriting canvas) regardless of the Edit
  Boxes toggle's own state — for touchscreen-with-stylus devices (e.g. iPad + Apple Pencil)
  where you want both without switching modes by hand. Mouse input is unaffected and still
  follows the manual Edit Boxes toggle
- **Reading order** — box numbering follows manga reading order (top row before bottom row,
  right column before left column within a row), not raw detection order. Computed by
  `reading_order.py` (a recursive row/column "xy-cut" heuristic: split the page along
  whichever axis has a clean gap, alternating axis, until every box stands alone) and stored
  per-sentence as `order_index`, recomputed whenever a box is OCR'd, drawn, or moved/resized.
  `resort_pages.py` re-applies it to already-ingested pages on demand; a startup migration in
  `server.py` backfills it automatically for pages ingested before this existed
- SQLite (`data/wakatta.db`) stores Work → Page → Sentence; uploaded PDFs saved to `data/uploads/`

### Dictionary Lookup (`dictionary.py` + `static/reader.html`)
- JMdict (`jmdict-simplified`, English glosses) and Kanjium pitch-accent data imported once
  into `data/wakatta.db` at first server start (`dictionary.build_db()`) — raw-SQL tables
  (`dict_entries`/`dict_index`/`pitch_accents`), decoupled from the app's ORM models since
  they're bulk-imported read-only reference data
- Every time a `Sentence`'s `ocr_text` or `user_text` is written, it's tokenized
  (`fugashi`/Unidic) and persisted as `WordOccurrence` rows — one set per text *source*
  (`ocr` vs `user`), tracked independently as different trust levels, dedicated to a
  canonical `Word` (deduplicated by dictionary form). `GET /api/sentences/{id}/tokens` is a
  pure read of this persisted data — nothing is tokenized live
  when the reader is opened, and no dictionary data is fetched until a word is clicked
- Clicking a word opens a tabbed popover (`GET /api/dict/lookup`): kana reading + pitch-accent
  pattern, English glosses in the "English" tab, a disabled "日本語" (JJ) tab placeholder for
  a future Japanese-Japanese source. Every panel-open is logged to `word_lookups`
  (`POST /api/word-lookups`) as a "didn't know this word" signal
- **Per-occurrence definition resolution**: unambiguous words auto-resolve at write time;
  genuine homographs (multiple JMdict entries sharing a surface/reading, e.g. 変/へん =
  "strange" or "change") are left unresolved and shown as a picker. Resolution is per
  *occurrence*, not per canonical word, since the same lemma+reading can mean different
  things in different sentences — and since Unidic's own reading guess can simply be wrong
  (e.g. 風 analyzed as ふう "style" when かぜ "wind" was meant), the picker always shows
  every JMdict entry for the surface form, across every reading, not just the guessed one
  (`POST /api/word-occurrences/{id}/resolve`)
- **Segmentation corrections**: Unidic sometimes splits a real word into pieces (a
  character's name like ナウシカ → ナウ + シカ) or fuses separate words together. The
  reader's "✎ Fix" mode lets the user select a run of adjacent words and re-specify the
  correct boundaries; saved as a global `SegmentationOverride` keyed by the literal text
  span (`POST /api/segmentation-overrides`) and applied in `dictionary.tokenize()` on top of
  Unidic's analysis, so it's fixed for every future occurrence of that text — the fix is
  also applied retroactively to every existing sentence containing the corrected span
- The panel/tab system is a small descriptor array in `reader.html` (`dictPanels`) — the
  Kanji panel (see below) is implemented as one more entry, no redesign needed

### Kanji Lookup (`kanji.py` + `static/reader.html`)
- KANJIDIC2 (readings, English meanings, grade, JLPT, frequency, classical radical number)
  and KRADFILE (per-kanji component/radical breakdown) imported once into `data/wakatta.db`
  at first server start (`kanji.build_db()`) — a single raw-SQL `kanji_entries` table, same
  decoupled-from-ORM convention as `dictionary.py`'s tables
- The dictionary popover's "漢字" tab (`dictPanels` in `reader.html`) extracts every unique
  kanji in the clicked word's surface form and fetches `GET /api/kanji/{char}` for each,
  showing on'yomi/kun'yomi readings, meanings, classical radical (char + name, from a
  hardcoded 214-entry Kangxi radical table), and full component breakdown per character
- **Stroke order** is rendered by fetching the raw KanjiVG SVG (`GET /api/kanji/{char}/svg`)
  and animating its strokes in drawing order client-side (stroke-dasharray reveal) —
  deliberately *not* reusing the DTW-normalized point arrays in `static/db.json`/`kwDb`,
  since those stretch each axis independently to fill `[0,1]` for stroke-matching purposes
  and visually distort simple/thin strokes (e.g. 一 renders as a diagonal line)

**Three routes to a character you don't know**, as a mode toggle above the handwriting
canvas — each covers where the others fail; all three end the same way, clicking a
character appends it to "Your text":
- **Draw kanji** — DTW stroke matching against KanjiVG (needs you to make out the strokes)
- **By radicals** — pick components you recognize, intersect via KRADFILE
  (`GET /api/kanji/by-components`; needs you to decompose the character)
- **From OCR** (`GET /api/sentences/{id}/ocr-candidates`) — for the case both of the above
  dead-end on: a character too small, blurry, or stylized to draw or decompose. Re-reads the
  box and reports what manga-ocr *nearly* read at each character position, scored. When it
  picks wrong, the right character is usually still in its top few guesses
  - manga-ocr's tokenizer is character-level for Japanese, so the decoder's output
    distribution at one step is a ranked, scored list of what that character could be —
    normally discarded in favour of the winner
  - Recovered in two passes: generate normally (so the text matches the stored `ocr_text`
    exactly), then one teacher-forced forward pass over the finished sequence to read off
    each position's distribution. The scores `generate()` returns directly are per-beam and
    don't align with the winning sequence — this model decodes with 4 beams — while
    re-running the decoder on the final text aligns by construction
  - Each position shows its confidence (chips tinted amber/red as it drops, panel opens on
    the weakest character); each candidate shows its probability plus a KANJIDIC2 gloss,
    since runner-ups are visually similar by construction (徹/徽/徴) and the meaning is
    usually what identifies the right one. The model's own pick is ranked in among the
    alternatives rather than above them — beam search optimizes the whole line, so a
    committed character can genuinely score below an alternative at its own position, which
    is itself a signal that this is where the read went wrong
  - Read-only: nothing is saved until a character is clicked

### The sync server this depends on (not in this repo)

The bridge is a *client*. The collection itself lives on a self-hosted Anki sync server
running on the same machine, which is set up outside this repository and is not version
controlled — the one part of the system a fresh clone does not give you.

```
~/.local/share/anki-sync-server/
  .venv/                  the `anki` package (26.8.x), installed on its own
  env                     SYNC_HOST/PORT/USER1, mode 600
  data/silk/              collection.anki2, media.db, media/  (~1.3 GB, mostly media)
~/.config/systemd/user/anki-sync-server.service
```

It is the sync server built into the `anki` Python package (`python -m anki.syncserver`),
run as a **systemd user unit**, enabled with lingering on so it starts at boot without a
login. It binds to the machine's **Tailscale address** (`SYNC_HOST=100.64.0.2`, port
8080) rather than `0.0.0.0`, so it is reachable from the tailnet and from nothing else —
`100.64.0.2` is also a local address on this machine, routed over `lo`, so the same URL
works here as on the iPad.

```bash
systemctl --user status anki-sync-server
journalctl --user -u anki-sync-server -f
```

Two things worth knowing:

- **Its credentials are its own.** `SYNC_USER1` in that `env` file has no relationship to
  any AnkiWeb account — a self-hosted server cannot use AnkiWeb credentials. The same
  values go in this repo's `.env` for the bridge to log in with.
- **Its media directory is what `ANKI_MEDIA_DIR` points at.** The bridge deliberately
  does not sync media (it would be a third copy of 1.3 GB already on this disk), so
  playback reads straight out of the server's own store. If wakatta is ever run on a
  different host from the sync server, unset `ANKI_MEDIA_DIR` and populate the bridge's
  own media directory with a real media sync instead.

HTTPS is not available on this tailnet (Headscale, no cert support), so this is plain
HTTP over the tunnel. AnkiMobile and AnkiDroid both accept that.

### Anki Bridge (`anki_bridge.py` + `anki_derive.py` + `anki_review.py` + `static/anki.html` + `static/drill.html`)

Wakatta does not schedule its own cards. A self-hosted Anki sync server holds the
collection (4.7k notes, 140k+ reviews of history), and the bridge keeps a private copy
under `data/anki/` that syncs against it as just another client — the same standing as
the desktop app or a phone. Credentials come from `.env`; see `.env.example`.

- **Projection** — notes are mirrored into `anki_notes` / `anki_note_words`, raw-SQL
  tables following the same convention as `dictionary.py`'s and `kanji.py`'s reference
  data: decoupled from the ORM, wiped and rebuilt rather than migrated. Anki-derived
  tokens dedupe into the canonical `words` table, so a word met on a manga page and the
  same word in a Core 2000 note are one identity.
- **A per-notetype field map** (`anki_bridge._FIELD_MAP`) decides what to index and as
  what role (`word` vs `sentence`). Load-bearing, not tidiness: Core 2000 repeats each
  example sentence four times over, and indexing all of them counted every word three or
  four times. Subtitle notes also carry parenthesised furigana and sound-effect captions
  that wreck tokenization if left in.
- **Browser** (`/anki`) — decks, notes, fields, and the tokens extracted from each, with
  filters for leeches and for notes missing audio. Media plays through
  `/api/anki/media/{filename}`, read from `ANKI_MEDIA_DIR` rather than synced: the files
  are already on this machine in the sync server's own store.
- **Derivation** (`anki_derive.py`) — turns the surplus already on a note into a further
  note: the recording alone, answered by writing the word. Reuses the source's own
  `[sound:...]` reference, so no media file is created. Lands in `Japanese::Wakatta::*`
  under its own notetype, making the output reversible by deleting one deck, and is
  idempotent — a source that already produced one is skipped.

  A per-kanji derivation existed and was removed: a bare character answered by its
  meanings, both readings and its components at once is four questions on one
  self-graded card, and has no counterpart in how Japanese is taught. The breakdown
  survives as reference on the answer screen, served live from `kanji.py`, where it
  costs no reviews. `DERIVED_NOTETYPES` refuses to derive from a derived note, which
  was previously prevented only by an absent adapter entry — accidental protection that
  would have broken the moment an adapter was added for some other reason.
- **Review** (`anki_review.py`, `/drill`) — cards come through the real v3 scheduler, so
  deck limits, learning steps and ordering behave exactly as in Anki, and answers land in
  the collection's history like any other. The interaction is chosen by notetype;
  anything unregistered falls back to show-answer-and-pick-a-button. Three modes: review
  (answers real cards), practice (same canvas, no scheduling), and session.
- **Sessions** — Anki's core refuses to answer a card that is not the scheduler's top
  card, so card selection is done with filtered decks built from any Anki search, order
  and limit. They ignore the home deck's daily limits, so the builder warns when a
  session outruns them and reports how many matches fall outside `Japanese::`.
- **Grading is pass/fail** — Again and Good only, on both the validated and the
  hand-answered path. Under SM-2, Hard costs 15 pp of ease and Easy adds 15 pp against a
  130% floor, and a third of this collection already sits on that floor. See
  `COLLECTION.md`. Writing quality is still measured and reported, it just cannot move
  scheduling.

### Failure triage (`anki_triage.py`)

Anki collapses every lapse to the same one-day interval, whether the word was missed by
a hair or never known. Measured over real drilling, **57% of failures pass the very next
relearning step** — so more than half the daily load is cards being punished for a
moment's hesitation.

The distinction is observable without asking for it. Fail once and recover: a jog. Fail
twice inside the window, or fail again immediately after being shown the answer: not
known. Behaviour rather than a button matters twice over — self-assessment of failure
magnitude is the judgement the four-button scale was dropped for, and behaviour also
catches a lucky guess, since passing by chance today and failing twice tomorrow still
lands the card in the bucket.

Repeats move to `Japanese::Hard`, out of daily rotation, to be tackled deliberately.
The origin deck is recorded as a tag rather than assumed, because a note's cards can
live in different decks, and cards that later string two clean passes together come home
on their own. Re-running moves nothing further. Driven from `/drill` → Session mode,
preview first.

### Publishing destructive clean-ups (`anki_bridge.full_upload`)

Removing a notetype is a schema change and Anki refuses to express it as an incremental
sync, so deleting a rejected experiment can only reach the server as a full upload.
`sync()` will never do that on its own. `full_upload()` exists so that an experiment
tried and found wanting can actually be removed rather than left in place because the
tooling made removal awkward — but it refuses unless passed `confirm="overwrite server"`,
and its docstring names the three things the caller must check first, because the
function cannot: that the server's revlog count matches so no reviews are lost, that no
other device holds unsynced work (each must full-download afterwards), and that the
server's current state is backed up.

### Stroke Validation (`stroke_validation.py`)

`kanjivg_db.recognize()` answers "which character is this?" and gives no feedback — a
character drawn in the wrong order simply scores badly against itself. Validation asks
the opposite question: given a known target, what went wrong? It separates wrong stroke
count, wrong order, a stroke drawn end-to-start, and a stroke drawn badly, reusing the
same normalized-DTW machinery.

Order and direction come from *relative* comparisons — does this stroke match a
different reference stroke better, does reversing it help — so they need no tuned
constants. Two guards keep order detection conservative, since an order error fails the
card: a cross-assignment must beat staying in place by a margin, and a stroke that
already matches its own position acceptably is never called out of order.

Shape is the one absolute judgement, and it is measured in **deviation** — DTW distance
divided by sample count, so the number means "average distance from the reference as a
fraction of the character's box". This matters more than it sounds. The original limit
was a raw DTW sum of 1.5, which reads as strict but is a **9% mean deviation** —
ordinary handwriting — so most correct strokes were failed as "off-shape". And because
the order guard only applies to strokes already under the shape limit, too tight a limit
silently disables it, letting false "out of order" verdicts through as well. Both
complaints from the first real drilling session were that one bug.

### Calibration (`stroke_calibration.py` + `static/calibrate.html`)

Thresholds are not constants in the source. They live in the database, are editable at
runtime from `/calibrate`, and are meant to be chosen by measurement rather than
argument — hard-coding them is exactly what produced the mistake above.

- **Every check is captured** — the drawn strokes, the verdict, and the thresholds in
  force — into `stroke_samples`, so an attempt can be re-judged later under different
  values.
- **The validator recommends; the writer answers.** It used to answer the card itself
  and offer a correction afterwards, which was backwards: re-answering Good after a
  false Again does not return the lost interval, it adds a second review — so a known
  word was being reset by a measurement that is explicitly not yet trusted, and a
  moment's pen static splitting one stroke in two was enough to do it. The verdict is
  now shown as advice beside Anki's own interval labels, with again/good buttons.
- **Labels come from the answer.** Because the writer's rating is the ground truth,
  every checked review produces a labelled sample as a side effect. Samples also record
  a `context` of `review` or `practice`, and only review samples are tuned against: a
  character drawn with the model in mind comes out tidier than one drawn from memory,
  so mixing practice in biases the thresholds toward passing.
- **`/calibrate`** has a slider per threshold and a replay: it re-judges every labelled
  attempt under candidate values and reports agreements, false alarms and missed errors.
  Tuning is moving the slider until the false alarms disappear without the real errors
  slipping through.

Anything synthetic must be kept out of `stroke_samples`, or the thresholds get fitted to
a plotter again.

The write drill (`/drill`) uses it in a trace-then-hide loop: study the animated model
with a live canvas over it, then the model is hidden and the same character written from
memory and graded. Copying a visible character is restudy; producing it from memory is
retrieval, and the combination beats either alone.

### Handwriting Recognition Webapp (`server.py` + `static/index.html`)
- FastAPI server loads KanjiVG stroke database on startup, generates `static/db.json`
- HTML5 Canvas captures stylus/pointer strokes
- Recognition runs **client-side** in `recognizer.js` (JS port of DTW pipeline) — no server needed after first load
- PWA: service worker caches app shell + `db.json`; works fully offline after initial visit
- User clicks candidate to confirm; canvas clears for next character
- KanjiVG data: 6700+ characters including kanji, hiragana, katakana, punctuation

### KanjiVG Database (`kanjivg_db.py`)
- Parses KanjiVG SVGs (lxml), samples stroke paths, normalizes to `[0,1]`
- Per-SVG `.npy` cache in `data/kanjivg_parsed/` — survives interrupted loads
- Full `.npz` cache in `data/kanjivg_cache.npz` — fast startup after first parse
- DTW-based recognition with stroke-count pre-filtering

### Custom CTD Inference (`ctd.py`)
- Minimal reimplementation of comic-text-detector inference
- CPU: ONNX model via `cv2.dnn` (no CUDA OpenCV needed)
- GPU path: swap to `onnxruntime-gpu` with `CUDAExecutionProvider` (~5 lines)

---

## Setup

### Prerequisites

- **Python 3.14** — managed automatically by uv
- **uv** — install from https://docs.astral.sh/uv/
- **MeCab** with UniDic — required by `fugashi` for tokenization:
  ```bash
  # Arch Linux
  sudo pacman -S mecab mecab-ipadic
  python -m unidic download

  # Debian / Ubuntu
  sudo apt install mecab libmecab-dev mecab-ipadic-utf8
  python -m unidic download
  ```
- **CUDA** (optional) — manga-ocr will use the GPU automatically if PyTorch detects CUDA

### First-time setup

```bash
# 1. Install Python dependencies
uv sync

# 2. Download and parse KanjiVG stroke data (~6700 characters)
#    Slow on first run (parallelised SVG parsing); fast on subsequent runs via cache
uv run setup_kanjivg.py

# 3. Download dictionary data (JMdict English + Kanjium pitch accents)
uv run setup_jmdict.py
uv run setup_pitch_accents.py

# 4. Download kanji data (KANJIDIC2 + KRADFILE)
uv run setup_kanjidic.py

# 5. Start the server
#    On first start, downloads manga-ocr weights (~444 MB) and the CTD ONNX model (~50 MB),
#    and imports JMdict (~220k entries) and KANJIDIC2 (~13k entries) into data/wakatta.db
#    (~20s, one-time)
uv run uvicorn server:app --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000 (or http://192.168.86.207:8000 from another device on the LAN):
- `/` → handwriting recognition (works offline after first load)
- `/page-reader` → library: upload a PDF, browse previous uploads
- `/read/{work_id}` → reader for a single work, opened from the library

### Anki bridge setup

```bash
cp .env.example .env        # sync server URL, credentials, and where its media lives
```

The bridge downloads the collection on its first sync (`POST /api/anki/sync`), which
also builds the word index. It never performs a full *upload* — that direction
overwrites the server, and nothing here should be able to do it as a side effect.

### Subsequent starts

```bash
uv run uvicorn server:app --host 0.0.0.0 --port 8000
```

Models and caches are already on disk; startup takes a few seconds to load manga-ocr and the KanjiVG cache.

### Offline use

After the initial setup and at least one server start, set `HF_HUB_OFFLINE=1` to prevent any outbound model-hub requests.

---

## Data Model

```
Work                              ← implemented
  └── Page (one image/PDF page)  ← implemented
        └── Sentence (text region / speech bubble)  ← implemented
              └── WordOccurrence (one per token, per text source: ocr | user)  ← implemented
                    ├── WordLookup (logged on each dictionary panel-open —        ← implemented
                    │     "didn't know this word" signal)
                    ├── dict_entry_id (resolved JMdict sense — auto if           ← implemented
                    │     unambiguous, else user-picked; per-occurrence, not
                    │     per-word, since homographs can differ by sentence)
                    └── Word (canonical entry, deduplicated by lemma+reading)     ← implemented

DictEntry / PitchAccent          ← implemented (raw-SQL reference tables in data/wakatta.db,
                                     bulk-imported from JMdict + Kanjium; joined to
                                     WordOccurrence by id/surface string, no formal FK — see
                                     dictionary.py)
KanjiEntry                       ← implemented (raw-SQL reference table in data/wakatta.db,
                                     bulk-imported from KANJIDIC2 + KRADFILE, keyed by the
                                     character itself; looked up live per kanji in a word's
                                     surface form, no formal FK — see kanji.py)
SegmentationOverride              ← implemented (user-defined tokenizer corrections, keyed by
                                     literal text span — see dictionary.py's tokenize())
```

---

## Subsystems (planned)

### Study Coverage Engine
- Tracks which words the user has confirmed as known
- For any unit (sentence / page / work): `coverage = known ∩ unit_words / unit_words`
- Surfaces the minimum word set to unlock a unit: "You need 8 more words to read this page"

### SRS (Spaced Repetition)

**Superseded in part — see the Anki Bridge above.** Anki now schedules, and wakatta
supplies the interactions it cannot: objectively graded handwriting, and card selection
by arbitrary search. What remains open from the original design is §E of
`STUDY_TOOLS.md` — scheduling the *word* rather than the card, and consuming evidence
(including the passive `WordLookup` signal) rather than reviews. That is a genuine
divergence from Anki's per-card model, not a thing Anki can be made to do, so it stays
an open question rather than a plan.

- All extracted words enter the study deck automatically
- FSRS algorithm schedules reviews
- Each card: spelling, reading, pitch accent, example sentence from source material
- Known words surface rarely; unknown words surface often

### Stroke Validation
- Same KanjiVG DTW infrastructure used for recognition repurposed for validation
- When user practises writing a known character, check:
  - **Order**: strokes drawn in wrong sequence
  - **Direction**: stroke drawn against canonical direction
  - **Shape**: path similarity via DTW

---

## Next Steps

### Ingestion
- [x] **SQLite data model** — Work/Page/Sentence schema with SQLAlchemy
- [x] **Whole-PDF ingestion** — `POST /api/works/{id}/process-all` queues all pages as a
      background job; client polls `GET /api/jobs/{id}` for progress
- [x] **Job persistence** — job state stored in SQLite; interrupted jobs resume on server restart
- [x] **Word layer** — `fugashi` tokenization runs whenever `ocr_text`/`user_text` is written,
      populating `WordOccurrence`/`Word`; see Dictionary Lookup above
- [ ] **Study deck** — connect ingested words to FSRS review cards

### Offline & Sync
- [ ] **Client-side SQLite** — embed SQLite WASM (`@sqlite.org/sqlite-wasm` + OPFS) in
      the webapp; mirror the Work/Page/Sentence schema so all reads work offline
- [ ] **Selective offline download** — "Download for offline" button on a work or chapter;
      fetches all page images and sentence data and writes them into the client DB.
      Service worker intercepts `/api/pages/{id}/image` requests and serves from local
      blob store when offline.
- [ ] **Online/offline sync** — track mutations in a client-side log (row + timestamp);
      on reconnect, replay unsynced writes to the server. Server wins for OCR data;
      client wins for `user_text` edits made offline.
- [x] **Upload UI** — file browser uploads PDF to `data/uploads/` and triggers ingestion in one step

### Card design — Deferred

- [ ] **The imported vocabulary cards do two jobs at once** — a Core 2000 or JLPT card
      asks for the reading *and* the English meaning on one card, and for a single-kanji
      note the reading question and the meaning question are different skills that decay
      at different rates. Wozniak's minimum information principle says split them; the
      cost is roughly doubling the card count for words already known, so the split
      probably wants to be earned (on failure) rather than applied wholesale.
      Related: the interference clusters below.

- [ ] **Interference sessions** — 52% of the 338 Japanese leeches are two-kanji compounds,
      and they cluster on a shared character (発: 開発 発生 発言 発見 発表; 対: 対立 対策
      対象 絶対に). Reviewing them weeks apart in isolation never builds a boundary
      between them. The remedy the research points at is to contrast the set explicitly,
      then test with the siblings visible as distractors. `anki_note_words` can generate
      a cluster from a shared kanji, and filtered-deck sessions can then keep it together.
      Trigger on failure of a *known* word — interleaving near-neighbours before each is
      individually solid makes things worse.

### Audio — Deferred

- [ ] **Synthesized word audio for decks that have none** — DaKanji (0/308 notes) and the
      JLPT N4 deck (0/640) carry no audio at all, so no listening or write-from-audio card
      can be built from them. Deferred deliberately: mining real audio from video is both
      more useful and more pleasant than synthesis, and reading practice covers pitch
      well enough in the meantime.

      If it does get built, the approach is decided. **VOICEVOX** (`voicevox_engine`, local,
      free, Docker) rather than a cloud TTS, for one reason: isolated words are the worst
      case for Japanese pitch accent — はし is 橋 (accent 2) or 箸 (accent 1) with no
      sentence to disambiguate — and VOICEVOX's `/audio_query` exposes an editable accent
      position per accent phrase, so the accent can be *set* rather than guessed. The
      correct values are already here: `pitch_accents` holds 124,137 Kanjium entries and
      covers 98% of the JLPT deck and 97% of DaKanji. Cloud engines sound better and offer
      no accent control, which is the wrong trade when the audio is meant to teach pitch.

      Prefer real recordings wherever they exist (JapanesePod101, Forvo) and synthesize
      only the remainder; a human recording is correct by definition.

      **Architectural consequence:** this is the first feature that would *add* media
      files. Everything in the Anki bridge so far reuses `[sound:...]` references that
      already exist in the collection, which is why the bridge syncs the collection but
      not media. Generated audio would need `col.media.add_file()` plus a real
      `sync_media()` to reach the iPad.

      Verify a sample by ear before generating in bulk — a wrong accent drilled 200 times
      is worse than no audio at all.

### Meta deck — Deferred

- [ ] **Vocabulary for talking about Japanese, in Japanese.** Not a category label for
      other decks — a subject in its own right. Two overlapping kinds of content: the
      metalinguistic terminology needed to read a Japanese explanation of the writing
      system (部首, 音読み, 形声文字, 偏, 旁), and the working language for having the
      conversation at all (意味, 読み方, これは日本語で何と言いますか). The leverage is
      that it makes a monolingual dictionary, a Japanese grammar explanation, or a
      teacher usable — unlike a vocabulary card, which only pays for itself.

      **Card shape, decided:** front = the written word, back = furigana + definition.
      This asks two questions at once, the same "two jobs" problem as the imported
      vocabulary cards, and is accepted here deliberately: for a word like 部首 the
      reading *is* most of what is unknown, and the deck is small. Repeats with other
      decks are fine.

      **Verified against the local JMdict** (`dictionary.lookup(engine, surface=...)`),
      which changed the list three times over and is worth redoing if it is ever
      regenerated:

      - 指事文字 and 会意文字 are **not in JMdict**. Their readings (しじもじ, かいいもじ)
        are standard but unconfirmed locally.
      - **The bare position words are a trap.** Only 偏 (へん) and 旁 (つくり) have the
        radical sense as their primary meaning. 冠 is "a cap worn by Shinto clergy",
        脚 is "foot; paw; arm of an octopus", and 垂れ is **"sauce"**. Teaching them as
        standalone cards would teach a wrong primary meaning, so the other positions
        should be taught only inside radical names — 草冠, 病垂, 国構え, 列火 — where
        the suffix is unambiguous, which is also how they are actually met.
      - Two readings need pinning against JMdict's default: 文 defaults to ふみ
        ("letter, mail") where ぶん "sentence" is wanted, and 訳 to わけ ("reason")
        where やく "translation" is wanted.

      **The 54 verified entries**, by group:

      *Writing system* — 漢字 かんじ · 平仮名 ひらがな · 片仮名 カタカナ · 文字 もじ ·
      部首 ぶしゅ · 音読み おんよみ · 訓読み くんよみ · 送り仮名 おくりがな ·
      振り仮名 ふりがな · 熟語 じゅくご · 画数 かくすう · 筆順 ひつじゅん

      *Formation* — 成り立ち なりたち · 象形文字 しょうけいもじ · 指事文字 しじもじ ⚠ ·
      会意文字 かいいもじ ⚠ · 形声文字 けいせいもじ

      *Positions and radical names* — 偏 へん · 旁 つくり · 三水 さんずい · 立刀 りっとう ·
      草冠 くさかんむり · 人偏 にんべん · 言偏 ごんべん · 病垂 やまいだれ ·
      国構え くにがまえ · 列火 れっか

      *Grammar* — 文法 ぶんぽう · 文 ぶん · 名詞 めいし · 動詞 どうし · 自動詞 じどうし ·
      他動詞 たどうし · 形容詞 けいようし · 形容動詞 けいようどうし · 副詞 ふくし ·
      助詞 じょし · 敬語 けいご

      *Talking about words* — 意味 いみ · 発音 はつおん · 単語 たんご · 言葉 ことば ·
      例 れい · 例文 れいぶん · 違い ちがい · 訳 やく · 辞書 じしょ · 説明 せつめい

      *Ways of doing* — 読み方 よみかた · 書き方 かきかた · 言い方 いいかた ·
      使い方 つかいかた · 練習 れんしゅう · 覚える おぼえる

- [ ] **Open: phrases as cards, or only their vocabulary.** Candidates were
      これは日本語で何と言いますか / どういう意味ですか / 何と読みますか /
      〜と〜の違いは何ですか / 例文を教えてください, which between them need only four
      more words (言う, 読む, 書く, 質問). Memorised phrases are useful immediately but
      learned as blocks; component words generalise but do not hand you the sentence.
      The stated preference is for *both*, with a stock of useful memorised sentences
      alongside eventual practice at composing new ones — which makes sentence
      *production* a card type this deck does not yet have, and a larger question than
      the word list.

### Stroke thresholds

- [ ] **Tune against real handwriting** — the machinery exists (`/calibrate`, captured
      samples, replay against labelled attempts), but the current values are still a
      reasoned starting point rather than a measured one: `ok = 0.19` deviation was
      chosen as roughly double the demonstrably-too-strict original, not fitted to
      anything. It needs a few dozen labelled attempts before it means much.
- [ ] **Per-character or per-stroke-count thresholds** — one global limit treats a
      one-stroke 一 and a 29-stroke 鬱 the same, and short strokes almost certainly
      tolerate less absolute deviation than long ones. Worth checking against real
      samples before adding the complexity.

### Quality
- [x] **Page transcription UI** — page reader with SVG bbox overlay; click a region to
      transcribe its text via handwriting input when OCR fails or is wrong
- [x] **Bounding box editing** — add, move, and resize detected regions directly on the page
      image ("▭ Edit Boxes" toggle); see Page Reader above
- [x] **Touch + pen input mode** — "✋✏️ Touch+Pen" toggle: finger always pans/zooms, pen
      always creates/edits/selects boxes, independent of the Edit Boxes toggle; see Page
      Reader above
- [x] **Reading order** — bounding boxes numbered/ordered per manga reading order rather than
      raw detection order (`reading_order.py`); see Page Reader above. Still iterating on edge
      cases (tall boxes spanning multiple rows, overlapping regions) as they show up in real
      pages
- [x] **Pitch accent** — Kanjium dataset imported via `dictionary.py`; shown per-candidate in
      the dictionary popover as raw pattern number(s) (e.g. `[0]`, `[1,3]`) — see "Dictionary —
      Deferred" below for the richer visual version
- [ ] **Highlight an individual character on the page** — selecting a character in the reader
      (the From OCR panel especially, where a box of 15 chips doesn't say which glyph you just
      tapped) should show where on the page that character is. Not a priority, but prototyped
      once already, so the findings are worth keeping:
      - *Cross-attention*: manga-ocr predicts no character boxes, but each decoding step's
        attention over the encoder's 14×14 patches aligns output character to image region —
        threshold at half its peak, bounding-box what survives. Needs the decoder's attention
        implementation set to `eager` (sdpa returns no weights). Centroids advanced along the
        reading direction in 99% of consecutive pairs, but extent is coarse: one patch is ~7%
        of the crop, so boxes came out loose and sometimes overlapping on dense lines
      - *Projection profile*: Japanese type is set on a fixed-width em square, so ink projected
        onto the reading axis forms a comb, one lobe per character — pure OpenCV, no GPU. Needs
        two stages (split lines across the cross axis first, or a paragraph's lines superimpose
        into mush), and the character count should be taken as given, which turns ambiguity into
        reconciliation: merge narrowest gaps where a character came apart (二, 三, か), split
        deepest valleys where two ran together. Line count has an independent geometric
        expectation that should *veto* splits but never demand them — an invented line break
        misassigns characters, a missed one only degrades to even division
      - The two fail in unrelated ways, so agreeing on where a character is turned out to be
        good evidence: over 30 real boxes the agreement score was sharply bimodal (27 scored
        0.67-1.00, 24 of them exactly 1.00; the 3 failures were all multi-line paragraphs of
        83+ characters, scoring 0.02/0.12/0.48), which cleanly separates trustworthy geometry
        from guesswork

### Dictionary — Deferred
- [ ] **Japanese-Japanese (JJ) definitions** — JMdict's own glosses are English-only; the
      reader already has a disabled "日本語" tab slot in `dictPanels` (`reader.html`) ready to
      wire up once a JJ-capable source is chosen (e.g. a parsed Japanese Wiktionary dump)
- [ ] **Rich pitch-accent rendering** — currently raw pattern numbers; a future version should
      draw the accent line over/under each mora (heiban/atamadaka/nakadaka/odaka), the
      convention used by Yomichan/OJAD
- [ ] **Trust-weighted coverage/recommendation** — `WordOccurrence.source` (`ocr` vs `user`)
      already distinguishes auto-OCR text from human-confirmed text; a future Study Coverage
      Engine could estimate "likely known words" even on unconfirmed OCR-only pages, trusting
      confirmed pages more, and recommend works with the most overlap with words already known
- [ ] **ML-assisted disambiguation** — homograph/reading resolution is currently entirely
      manual (the user picks via the popover, see Dictionary Lookup above); an ML-assisted
      default using sentence context is future work
- [ ] **Per-work majority-vote resolution default** — right now `WordOccurrence.dict_entry_id`
      is resolved strictly per occurrence, so picking かぜ over ふう for 風 on one page has no
      effect anywhere else. In practice a recurring word (names especially, but plenty of
      regular vocabulary too) tends to carry one consistent sense throughout a single work.
      Plan: once a `Word` has one or more `resolved_by='user'` picks within a given work
      (joining `word_occurrences` → `sentences` → `pages` → `work_id`), take the majority
      `dict_entry_id` among those picks as the default — (a) immediately back-fill every other
      occurrence of that `Word` in the same work that's currently unresolved or only
      auto-resolved (never overwrite a *different* `resolved_by='user'` pick — that's a
      deliberate contextual override, not noise), and (b) apply the same majority as the
      default for newly tokenized occurrences going forward. Votes only ever come from
      explicit user picks, never from auto-resolved ones, so the default can't reinforce
      itself. Scope is per-work, not library-wide, since the same word can carry a different
      sense in a different book. Worth a distinct `resolved_by` value (e.g. `"user_default"`)
      so the UI can eventually show "inherited from your choice elsewhere in this work" instead
      of conflating it with a fresh independent auto-resolution
- [ ] **Retroactive segmentation reapply across works** — `POST /api/segmentation-overrides`
      already re-tokenizes every sentence *containing* the corrected span; extending this to
      proactively suggest corrections (e.g. flagging likely-wrong proper-noun splits) is future
      work

### Kanji
- [x] **KANJIDIC2 integration** — radical breakdown per kanji character, see Kanji Lookup above
- [x] **Animated stroke order** — KanjiVG SVG strokes revealed sequentially in the UI, see
      Kanji Lookup above
- [ ] **Stroke validation** — reuse DTW for practice checking (order, direction, shape)

### GPU
- [ ] **Switch CTD to `onnxruntime-gpu`** on the 4090 machine for faster detection
- [ ] **`HF_HOME=./models`** — consolidate all model weights into the project directory
      for easy portability between machines

### QoL — Handwriting
- [ ] **Multi-character word input** — user should be able to write a whole word without
      confirming each character individually. Approach: after recognizing top character
      candidates, look them up as prefixes in a word dictionary (e.g. a frequency-ranked
      kanji-compound list bundled as a second JSON); surface word suggestions alongside
      single-character candidates. As the user writes more characters, intersect with words
      that match the growing prefix. Tapping a word suggestion confirms all characters at
      once and clears the canvas.
