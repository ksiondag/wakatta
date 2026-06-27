# Wakatta — Japanese Understanding App

A local-first tool for extracting, studying, and practicing Japanese from manga and literature. Feed in scanned pages; get a structured study deck with kanji breakdown, stroke practice, and SRS-driven review.

Initial target: **Nausicaä of the Valley of the Wind** (manga).

---

## Stack

| Layer | Choice |
|---|---|
| OCR | `manga-ocr` (ViT-based, CUDA-accelerated) |
| Text region detection | `comic-text-detector` |
| Tokenization | `fugashi` + MeCab |
| Database | SQLite + SQLAlchemy |
| Backend API | FastAPI |
| Frontend | React + HTML5 Canvas |
| SRS algorithm | FSRS |
| Kanji data | KanjiVG + KANJIDIC2 |

Runs locally on a machine with a 4090 GPU. No cloud dependencies.

---

## Data Model

```
Work
  └── Page (one image/PDF page)
        └── Sentence (text region / speech bubble)
              └── WordOccurrence (token position on page)
                    └── Word (canonical entry, deduplicated by dictionary form)
                          ├── reading (hiragana pronunciation)
                          ├── pitch accent
                          ├── example sentence (pulled from source)
                          └── KanjiComponent (per kanji in the word)
                                ├── radicals (from KANJIDIC2/KRADFILE)
                                └── stroke order (from KanjiVG SVGs)
```

---

## Subsystems

### 1. Ingestion & OCR
- Input: PNG or PDF page
- `comic-text-detector` finds text regions (bubbles, captions) with bounding boxes
- `manga-ocr` reads each region → Japanese string
- Output: text organized by region, ordered by reading direction

### 2. Text Segmentation & Word Analysis
- `fugashi` + MeCab tokenizes raw Japanese string (no spaces in Japanese)
- Each token: surface form, dictionary form, reading (yomi), part of speech
- Pitch accent: separate lookup via OJAD or accent dictionary
- Words deduplicated into canonical `Word` records across the whole work

### 3. Kanji Decomposition
- Per kanji character in a word:
  - Radicals from KANJIDIC2 / KRADFILE
  - Stroke order SVG paths from KanjiVG (numbered strokes with direction)
- Animated stroke display in the study UI

### 4. Study Coverage Engine
- Tracks which words the user has confirmed as known
- For any unit (sentence / page / work): `coverage = known ∩ unit_words / unit_words`
- Surfaces the minimum word set needed to unlock a unit
- "You need 8 more words to read this page"

### 5. SRS (Spaced Repetition)
- All extracted words enter the study deck automatically
- FSRS algorithm schedules reviews
- User confirms a word is known through the review interface
- Known words appear far less frequently; unknown words surface more often
- Each card shows: spelling, reading, pitch, example sentence from source

### 6. Writing Practice
- HTML5 Canvas captures stylus input as stroke sequences (Pointer Events API)
- User rewrites the presented kanji
- Validation against KanjiVG canonical strokes checks:
  - **Order**: strokes drawn in wrong sequence
  - **Direction**: stroke drawn against the canonical direction (start/end point comparison)
  - **Shape**: loose path similarity (DTW or Fréchet distance)

---

## Build Order

| Phase | POC | Goal |
|---|---|---|
| 1 | OCR pipeline | Image → structured Japanese text (manga-ocr + comic-text-detector + fugashi) |
| 2 | Data model + ingestion | Persist Work/Page/Sentence/Word hierarchy in SQLite |
| 3 | Study deck + SRS | FSRS cards per word, review UI with reading/example sentence |
| 4 | Kanji panel | Radical breakdown + animated stroke order from KanjiVG |
| 5 | Writing practice | Canvas stroke capture + validation against KanjiVG |

POCs 4 and 5 can be developed in parallel with POC 3.

---

## POC 1 Target Output

Given one Nausicaä page image, print:

```
Page: nausicaa_v1_p042.png

Sentence 1: 風の谷のナウシカ
  風  → kaze  / かぜ  (noun)
  の  → no    / の    (particle)
  谷  → tani  / たに  (noun)
  の  → no    / の    (particle)
  ナウシカ → Naushika / ナウシカ (proper noun)

Sentence 2: ...
```

No database, no frontend — just proving the OCR and tokenization pipeline work on real manga content.

### POC 1 Requirements
- Scan or digital image of a Nausicaä page
- Python environment with: `manga-ocr`, `comic-text-detector`, `fugashi`, MeCab system library
- CUDA available (4090) for fast inference
