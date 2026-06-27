"""
KanjiVG database: load stroke paths and recognize handwritten input via DTW.

Recognition pipeline:
  user strokes (pointer events) -> resample -> normalize -> DTW vs all
  KanjiVG entries with matching stroke count -> rank by total distance
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

import numpy as np
from svg.path import parse_path

DATA_DIR = Path("data/kanjivg")
SAMPLES_PER_STROKE = 16
SVG_NS = "http://www.w3.org/2000/svg"

# Load a subset for fast iteration during development, None loads everything
DEV_SUBSET: str | None = None


@dataclass
class KanjiVGDatabase:
    chars: dict[str, list[np.ndarray]] = field(default_factory=dict)
    by_count: dict[int, list[str]] = field(default_factory=dict)  # stroke count -> chars

    def add(self, char: str, strokes: list[np.ndarray]):
        self.chars[char] = strokes
        self.by_count.setdefault(len(strokes), []).append(char)


# ---------------------------------------------------------------------------
# SVG parsing
# ---------------------------------------------------------------------------

def _sample_svg_path(d: str, n: int = SAMPLES_PER_STROKE) -> np.ndarray:
    """Sample n evenly-spaced points along an SVG path (by parameter, not arc length)."""
    path = parse_path(d)
    points = []
    for i in range(n):
        pt = path.point(i / (n - 1))
        points.append([pt.real, pt.imag])
    return np.array(points, dtype=np.float32)


def _parse_svg_strokes(svg_text: str) -> list[np.ndarray]:
    """Return ordered list of sampled stroke arrays from a KanjiVG SVG."""
    root = etree.fromstring(svg_text.encode())
    stroke_group = None
    for g in root.iter(f"{{{SVG_NS}}}g"):
        if "StrokePaths" in (g.get("id") or ""):
            stroke_group = g
            break
    if stroke_group is None:
        return []
    return [
        _sample_svg_path(path.get("d", ""))
        for path in stroke_group.iter(f"{{{SVG_NS}}}path")
        if path.get("d")
    ]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize(strokes: list[np.ndarray]) -> list[np.ndarray]:
    """Scale all strokes together so the character fits in [0,1]×[0,1]."""
    if not strokes:
        return strokes
    all_pts = np.concatenate(strokes, axis=0)
    lo = all_pts.min(axis=0)
    hi = all_pts.max(axis=0)
    scale = hi - lo
    scale[scale == 0] = 1.0
    return [(s - lo) / scale for s in strokes]


def _resample_stroke(points: np.ndarray, n: int = SAMPLES_PER_STROKE) -> np.ndarray:
    """Resample a user stroke (arbitrary point count) to n arc-length points."""
    if len(points) < 2:
        return np.tile(points[:1], (n, 1)) if len(points) else np.zeros((n, 2), np.float32)
    diffs = np.diff(points, axis=0)
    seg_len = np.sqrt((diffs ** 2).sum(axis=1))
    cumlen = np.concatenate([[0.0], np.cumsum(seg_len)])
    total = cumlen[-1]
    if total == 0:
        return np.tile(points[:1], (n, 1))
    targets = np.linspace(0, total, n)
    out = np.empty((n, 2), dtype=np.float32)
    for i, t in enumerate(targets):
        idx = int(np.searchsorted(cumlen, t, side="right")) - 1
        idx = min(idx, len(diffs) - 1)
        alpha = (t - cumlen[idx]) / (seg_len[idx] + 1e-8)
        out[i] = points[idx] + alpha * diffs[idx]
    return out


# ---------------------------------------------------------------------------
# DTW
# ---------------------------------------------------------------------------

def _dtw(a: np.ndarray, b: np.ndarray) -> float:
    """DTW distance between two (N,2) stroke arrays."""
    # Pairwise euclidean distances (N x M)
    diff = a[:, None, :] - b[None, :, :]
    dist = np.sqrt((diff ** 2).sum(axis=2))

    n, m = dist.shape
    dp = np.full((n + 1, m + 1), np.inf)
    dp[0, 0] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i, j] = dist[i - 1, j - 1] + min(dp[i - 1, j], dp[i, j - 1], dp[i - 1, j - 1])
    return float(dp[n, m])


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def load_database(
    data_dir: Path = DATA_DIR,
    subset: str | None = DEV_SUBSET,
) -> KanjiVGDatabase:
    """
    Parse KanjiVG SVG files and return a KanjiVGDatabase.
    subset: string of characters to load; None loads everything (~11k files, slow).
    """
    db = KanjiVGDatabase()

    if subset is not None:
        wanted = {ord(c) for c in subset}
        files = []
        for cp in wanted:
            path = data_dir / f"{cp:05x}.svg"
            if path.exists():
                files.append(path)
            else:
                print(f"  [warn] no SVG for U+{cp:04X} ({chr(cp)!r})")
        print(f"Loading KanjiVG subset ({len(files)}/{len(wanted)} chars found)...")
    else:
        files = [f for f in sorted(data_dir.glob("*.svg")) if "-" not in f.stem]
        print(f"Loading full KanjiVG ({len(files)} files)...")

    errors: list[str] = []
    for svg_file in files:
        try:
            codepoint = int(svg_file.stem, 16)
            char = chr(codepoint)
        except ValueError:
            continue
        try:
            strokes = _parse_svg_strokes(svg_file.read_text(encoding="utf-8"))
            if strokes:
                db.add(char, _normalize(strokes))
            else:
                errors.append(f"  [empty] {svg_file.name} ({char!r})")
        except Exception as e:
            errors.append(f"  [error] {svg_file.name} ({char!r}): {e}")

    if errors:
        print(f"{len(errors)} problems:")
        for msg in errors[:20]:
            print(msg)
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")

    print(f"Loaded {len(db.chars)} characters.")
    return db


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------

def recognize(
    raw_strokes: list[list[dict]],
    db: KanjiVGDatabase,
    top_n: int = 10,
) -> list[dict]:
    """
    raw_strokes: list of strokes, each stroke = list of {"x": float, "y": float}
    Returns top_n candidates sorted by distance: [{"char": str, "score": float}, ...]
    """
    if not raw_strokes:
        return []

    user_strokes = [
        np.array([[p["x"], p["y"]] for p in stroke], dtype=np.float32)
        for stroke in raw_strokes if len(stroke) >= 2
    ]
    if not user_strokes:
        return []

    user_strokes = [_resample_stroke(s) for s in user_strokes]
    user_strokes = _normalize(user_strokes)
    n = len(user_strokes)

    results = []
    for char in db.by_count.get(n, []):
        dist = sum(_dtw(u, c) for u, c in zip(user_strokes, db.chars[char]))
        results.append((char, dist))

    results.sort(key=lambda x: x[1])
    return [{"char": char, "score": round(score, 3)} for char, score in results[:top_n]]
