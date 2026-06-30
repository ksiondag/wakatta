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
  (green = vertical, orange = horizontal). Click a region to select it; right panel shows
  auto-OCR text as reference. Draw on the handwriting canvas → recognition candidates → click
  to confirm; auto-saves to SQLite.
- SQLite (`data/wakatta.db`) stores Work → Page → Sentence; uploaded PDFs saved to `data/uploads/`

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

# 3. Start the server
#    On first start, downloads manga-ocr weights (~444 MB) and the CTD ONNX model (~50 MB)
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
              └── WordOccurrence (token position on page)
                    └── Word (canonical entry, deduplicated by dictionary form)
                          ├── reading (hiragana pronunciation)
                          ├── pitch accent
                          └── KanjiComponent (per kanji in the word)
                                ├── radicals (from KANJIDIC2/KRADFILE)
                                └── stroke order (from KanjiVG)
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
- [ ] **Word layer** — run `fugashi` tokenization on confirmed `user_text`; populate
      Word/WordOccurrence tables
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
- [ ] **Bounding box editing** — add, move, and resize detected regions directly on the page image
- [ ] **Pitch accent** — add OJAD or accent dictionary lookup to word analysis

### Kanji
- [ ] **KANJIDIC2 integration** — radical breakdown per kanji character
- [ ] **Animated stroke order** — render KanjiVG SVG strokes sequentially in the UI
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
