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
