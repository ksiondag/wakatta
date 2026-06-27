/**
 * Client-side handwriting recognition — port of kanjivg_db.py.
 * Exported: loadDb(url), recognize(rawStrokes, db, topN)
 */

const SAMPLES_PER_STROKE = 16;

// Arc-length resample a stroke [{x,y},...] to n evenly-spaced points.
function resampleStroke(points, n = SAMPLES_PER_STROKE) {
  if (points.length < 2) {
    const p = points[0] ?? { x: 0, y: 0 };
    return Array.from({ length: n }, () => [p.x, p.y]);
  }

  // Cumulative arc lengths
  const cumlen = [0];
  for (let i = 1; i < points.length; i++) {
    const dx = points[i].x - points[i - 1].x;
    const dy = points[i].y - points[i - 1].y;
    cumlen.push(cumlen[i - 1] + Math.sqrt(dx * dx + dy * dy));
  }
  const total = cumlen[cumlen.length - 1];
  if (total === 0) return Array.from({ length: n }, () => [points[0].x, points[0].y]);

  const out = [];
  for (let i = 0; i < n; i++) {
    const t = (i / (n - 1)) * total;
    // Binary search for segment
    let lo = 0, hi = cumlen.length - 2;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (cumlen[mid] <= t) lo = mid; else hi = mid - 1;
    }
    const segLen = cumlen[lo + 1] - cumlen[lo];
    const alpha = segLen < 1e-8 ? 0 : (t - cumlen[lo]) / segLen;
    out.push([
      points[lo].x + alpha * (points[lo + 1].x - points[lo].x),
      points[lo].y + alpha * (points[lo + 1].y - points[lo].y),
    ]);
  }
  return out;
}

// Scale all strokes together so the character fits [0,1]×[0,1].
function normalizeStrokes(strokes) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const s of strokes) {
    for (const [x, y] of s) {
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x > maxX) maxX = x;
      if (y > maxY) maxY = y;
    }
  }
  const scaleX = maxX - minX || 1;
  const scaleY = maxY - minY || 1;
  return strokes.map(s => s.map(([x, y]) => [(x - minX) / scaleX, (y - minY) / scaleY]));
}

// DTW distance between two arrays of [x,y] points (both length SAMPLES_PER_STROKE).
function dtw(a, b) {
  const n = a.length, m = b.length;
  // Use flat Float32Array for the DP table
  const dp = new Float32Array((n + 1) * (m + 1)).fill(Infinity);
  dp[0] = 0;

  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      const dx = a[i - 1][0] - b[j - 1][0];
      const dy = a[i - 1][1] - b[j - 1][1];
      const d = Math.sqrt(dx * dx + dy * dy);
      const prev = Math.min(
        dp[(i - 1) * (m + 1) + j],
        dp[i * (m + 1) + (j - 1)],
        dp[(i - 1) * (m + 1) + (j - 1)],
      );
      dp[i * (m + 1) + j] = d + prev;
    }
  }
  return dp[n * (m + 1) + m];
}

/**
 * Recognize handwritten strokes against the KanjiVG database.
 * rawStrokes: [{x, y}, ...][]  (canvas pixel coordinates)
 * db: the object returned by loadDb()
 * Returns [{char, score}, ...] sorted by ascending score.
 */
export function recognize(rawStrokes, db, topN = 12) {
  const validRaw = rawStrokes.filter(s => s.length >= 2);
  if (validRaw.length === 0) return [];

  let userStrokes = validRaw.map(s => resampleStroke(s));
  userStrokes = normalizeStrokes(userStrokes);
  const n = userStrokes.length;

  const candidates = db.byCount[n] ?? [];
  const results = [];

  for (const char of candidates) {
    const refStrokes = db.chars[char];
    let dist = 0;
    for (let i = 0; i < n; i++) dist += dtw(userStrokes[i], refStrokes[i]);
    results.push({ char, score: Math.round(dist * 1000) / 1000 });
  }

  results.sort((a, b) => a.score - b.score);
  return results.slice(0, topN);
}

/**
 * Fetch and parse the KanjiVG database JSON.
 * Returns the db object expected by recognize().
 */
export async function loadDb(url = '/static/db.json') {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to load db: ${res.status}`);
  return res.json();
}
