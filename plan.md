# Plan: PWA Offline Handwriting Recognition

## Goal

Convert the handwriting recognition webapp (`static/index.html` + `server.py`) into a
Progressive Web App that works fully offline on a client device (e.g. iPad with stylus)
after a single initial connection to the server.

---

## Current State

- `server.py` — FastAPI server, loads KanjiVG database on startup
- `kanjivg_db.py` — parses KanjiVG SVGs, DTW recognition, returns candidates
- `static/index.html` — canvas UI, POSTs stroke data to `POST /recognize`, displays candidates
- Recognition runs **server-side** — no server = no recognition

## Target State

- Recognition runs **client-side** in JavaScript
- Service worker caches the app shell and KanjiVG database on first load
- Device works fully offline after that first connection
- Server still exists (runs on the 4090 machine) but is only needed once per device

---

## Files to Create

### `static/manifest.json`
Standard PWA manifest. Minimum fields:
```json
{
  "name": "Wakatta",
  "short_name": "わかった",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#1a1a2e",
  "theme_color": "#1a1a2e",
  "icons": [...]
}
```
Add a simple icon (SVG or PNG). Link from `index.html` `<head>`.

### `static/sw.js`
Service worker. Responsibilities:
1. **Install** — cache app shell files: `/`, `/static/manifest.json`, `/static/sw.js`, `/static/db.json`
2. **Fetch** — serve all requests from cache; fall through to network only if not cached
3. **Activate** — delete old cache versions on update

Use a versioned cache name (e.g. `wakatta-v1`) so updates can bust the cache.

### `static/db.json` (generated, not hand-written)
The full KanjiVG database serialized as JSON. Served statically so the service worker
can cache it in one fetch. Format:

```json
{
  "byCount": {
    "1": ["一", "ー", "乙", ...],
    "2": ["二", "人", "八", ...],
    ...
  },
  "chars": {
    "一": [[[0.1, 0.5], [0.5, 0.5], [0.9, 0.5], ...]],
    "二": [[[...], ...], [[...], ...]],
    ...
  }
}
```

Each character maps to a list of strokes; each stroke is a list of `[x, y]` pairs
(already normalized to `[0,1]`, 16 points per stroke — matching `SAMPLES_PER_STROKE`
in `kanjivg_db.py`).

### `static/recognizer.js`
Client-side port of the Python recognition pipeline. Must implement:

**`resampleStroke(points, n=16)`**
Arc-length resample a stroke (array of `{x,y}`) to `n` evenly-spaced points.
Port of `_resample_stroke` in `kanjivg_db.py`.

**`normalizeStrokes(strokes)`**
Scale all strokes together so the character fits `[0,1]×[0,1]`.
Port of `_normalize` in `kanjivg_db.py`.

**`dtw(a, b)`**
DTW distance between two arrays of `[x,y]` points.
Port of `_dtw` in `kanjivg_db.py`. Use `Float32Array` for the DP matrix.

**`recognize(rawStrokes, db, topN=12)`**
Full pipeline: resample → normalize → look up by stroke count → DTW vs candidates → sort.
Port of `recognize` in `kanjivg_db.py`.
Returns `[{char, score}, ...]`.

---

## Files to Modify

### `server.py`
Add one endpoint to generate `static/db.json` from the loaded database:

```python
@app.get("/api/generate-db")
def generate_db():
    # Serialize kanjivg_db to static/db.json
    # Only needs to be called once after the database is loaded
```

Or better: generate `static/db.json` automatically during startup if it doesn't exist,
so the service worker can always fetch it as a static file.

### `static/index.html`
- Add `<link rel="manifest" href="/static/manifest.json">`
- Add service worker registration in `<script>`:
  ```javascript
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js');
  }
  ```
- Load `recognizer.js` and `db.json` on startup
- Replace the `fetch('/recognize', ...)` call with `recognize(strokes, db)` from `recognizer.js`
- Show a small indicator when running offline vs connected

---

## Implementation Notes

### db.json size
~6700 chars × ~10 strokes × 16 points × 2 coords × ~6 chars per float in JSON
≈ 12–20MB uncompressed. Enable gzip compression in FastAPI/uvicorn so the transfer
is 3–5MB. The service worker caches the decompressed response.

If size is a problem, switch to a binary format (Float32Array packed into a .bin file)
and parse it with a DataView in JavaScript. Avoid this complexity unless JSON proves
too large in practice.

### DTW performance in JavaScript
With 16-point strokes and stroke-count pre-filtering, each DTW call is a 16×16 DP table.
Should be fast enough in plain JS. If recognition feels slow, move the DP loop to a
Web Worker so it doesn't block the UI thread.

### Cache versioning
When the KanjiVG database is updated (rare), increment the cache version in `sw.js`
so devices re-fetch `db.json` on next connection.

### GPU machine vs Framework 12
The server (4090 machine) only needs to be reachable once per device for the initial
install. After that the PWA runs entirely on-device. The Framework 12 can also serve
as the server when on the same network.

---

## Acceptance Criteria

1. Opening `http://<server>/` on an iPad installs the PWA (Add to Home Screen works)
2. After initial load, disable wifi/network on the iPad
3. Open the app from the home screen icon — it loads
4. Draw a character — candidates appear correctly
5. No network requests are made (verify in browser DevTools → Network tab)
