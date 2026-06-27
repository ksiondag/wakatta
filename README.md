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

### Handwriting Recognition Webapp (`server.py` + `static/`)
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

```bash
# Install uv (https://docs.astral.sh/uv/)
uv sync

# Download KanjiVG stroke data
uv run setup_kanjivg.py

# Start handwriting recognition server
uv run uvicorn server:app --reload --port 8000
# Then open http://localhost:8000

# Run OCR pipeline on a manga page (edit PAGE_NUM in main.py first)
uv run main.py
```

Models download automatically on first run:
- `manga-ocr` weights from HuggingFace (~444MB, cached in `~/.cache/huggingface/`)
- CTD ONNX model from GitHub releases (~50MB, cached in `models/`)

For fully offline use after initial setup: set `HF_HUB_OFFLINE=1`.

---

## Data Model (planned)

```
Work
  └── Page (one image/PDF page)
        └── Sentence (text region / speech bubble)
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

### Pipeline
- [ ] **SQLite data model** — implement Work/Page/Sentence/Word schema with SQLAlchemy
- [ ] **Ingestion endpoint** — FastAPI route that accepts a PDF, runs the OCR pipeline,
      persists results to the database
- [ ] **Study deck** — connect ingested words to FSRS review cards

### Kanji
- [ ] **KANJIDIC2 integration** — radical breakdown per kanji character
- [ ] **Animated stroke order** — render KanjiVG SVG strokes sequentially in the UI
- [ ] **Stroke validation** — reuse DTW for practice checking (order, direction, shape)

### GPU
- [ ] **Switch CTD to `onnxruntime-gpu`** on the 4090 machine for faster detection
- [ ] **`HF_HOME=./models`** — consolidate all model weights into the project directory
      for easy portability between machines

### Quality
- [ ] **Bounding box correction UI** — web interface for manually adjusting detected
      text regions on a manga page and transcribing text if OCR fails (pen input
      serves double duty as study and correction)
- [ ] **Pitch accent** — add OJAD or accent dictionary lookup to word analysis
