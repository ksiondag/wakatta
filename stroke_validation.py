"""Validate handwriting against KanjiVG, rather than just recognizing it.

kanjivg_db.recognize() answers "which character is this?" by pairing the user's
strokes with each candidate's strokes in order and summing DTW distances. That
implicitly requires correct stroke order and gives no feedback — a character drawn
in the wrong order simply scores badly against itself and well against nothing.

Validation asks the opposite question: given that the target is known, what did the
writer get wrong? It reuses the same normalized-DTW machinery to separate four
failure modes:

  count      — wrong number of strokes
  order      — right strokes, drawn in the wrong sequence
  direction  — right stroke in the right place, drawn end-to-start
  shape      — right stroke, drawn badly

Order and direction are decided by *relative* comparisons (does this stroke match a
different reference stroke better? does reversing it help?), so they need no tuned
constants and are the trustworthy half of the output. Shape needs an absolute
threshold, and the ones below are calibrated against synthetic jitter rather than
real handwriting — see THRESHOLDS.
"""

import numpy as np

import kanjivg_db

# Per-stroke DTW distance bands, measured by perturbing KanjiVG's own strokes with
# gaussian noise: sigma 0.03 (slightly untidy) lands near 0.6, sigma 0.06 (visibly
# sloppy but readable) near 1.2, sigma 0.12 (barely the right shape) near 2.3. A
# stroke compared against a *different* stroke of the same character scores ~12.
#
# These are provisional. They come from synthetic noise, not from real pen input,
# and are the first thing to retune once there's actual drilling data to look at.
THRESH_OK = 1.5
THRESH_POOR = 4.0

# Reversing a stroke and getting a materially better match is strong evidence it was
# drawn backwards; the margin keeps near-symmetric strokes (dots, short ticks, where
# forward and reverse score almost the same) from being flagged on noise.
REVERSAL_RATIO = 0.6

# Same idea for order. Greedy assignment will happily pair stroke 5 with reference
# stroke 7 when the writing is messy enough that everything matches everything
# poorly, which would report a confident "drawn out of sequence" for what is really
# just untidy handwriting. So a cross-assignment is only believed when it beats
# staying in place by this margin; otherwise the stroke keeps its own index and the
# shape thresholds judge it. Order errors force a fail, so a false one is expensive.
ORDER_RATIO = 0.6


def _prepare(raw_strokes: list[list[dict]]) -> list[np.ndarray]:
    strokes = [
        np.array([[p["x"], p["y"]] for p in s], dtype=np.float32)
        for s in raw_strokes if len(s) >= 2
    ]
    if not strokes:
        return []
    strokes = [kanjivg_db._resample_stroke(s) for s in strokes]
    return kanjivg_db._normalize(strokes)


def _assign(cost: np.ndarray) -> list[int]:
    """Greedy min-cost matching of user strokes to reference strokes.

    Greedy rather than Hungarian to avoid a scipy dependency: stroke counts are small
    (under 30), and the interesting case — a writer swapping two adjacent strokes —
    is recovered correctly by greedy assignment since the swapped pair's own costs
    dominate. Returns reference index per user stroke, -1 when unmatched.
    """
    n_user, n_ref = cost.shape
    assignment = [-1] * n_user
    taken = set()
    order = np.dstack(np.unravel_index(np.argsort(cost, axis=None), cost.shape))[0]
    for i, j in order:
        if assignment[i] == -1 and j not in taken:
            assignment[i] = int(j)
            taken.add(int(j))
        if len(taken) == min(n_user, n_ref):
            break
    return assignment


def validate(char: str, raw_strokes: list[list[dict]], db) -> dict:
    """Grade a drawn character against its KanjiVG reference."""
    ref = db.chars.get(char)
    if ref is None:
        return {"char": char, "error": "no stroke data for this character"}

    user = _prepare(raw_strokes)
    if not user:
        return {"char": char, "error": "no usable strokes"}

    cost = np.array([[kanjivg_db._dtw(u, r) for r in ref] for u in user])
    cost_rev = np.array([[kanjivg_db._dtw(u[::-1], r) for r in ref] for u in user])
    assignment = _assign(cost)

    # Demote unconvincing cross-assignments back to the identity pairing (see
    # ORDER_RATIO): messy writing shouldn't be reported as a stroke-order mistake.
    for i, j in enumerate(assignment):
        if j < 0 or j == i or i >= len(ref):
            continue
        # A stroke that already matches its own position acceptably is not out of
        # order, whatever else it happens to resemble — characters like 腰 repeat
        # similar short strokes, and noise alone can make one match a sibling better.
        if cost[i][i] <= THRESH_OK or cost[i][j] >= cost[i][i] * ORDER_RATIO:
            assignment[i] = i

    strokes_out, issues = [], []
    for i, j in enumerate(assignment):
        if j < 0:
            strokes_out.append({"drawn": i, "expected": None, "verdict": "extra",
                                "distance": None})
            issues.append(f"stroke {i + 1} doesn't correspond to any stroke of {char}")
            continue
        d, d_rev = float(cost[i][j]), float(cost_rev[i][j])
        backwards = d_rev < d * REVERSAL_RATIO
        best = min(d, d_rev)

        if j != i:
            verdict = "order"
            issues.append(f"stroke {i + 1} is stroke {j + 1} of {char}, drawn out of sequence")
        elif backwards:
            verdict = "direction"
            issues.append(f"stroke {i + 1} was drawn backwards")
        elif best <= THRESH_OK:
            verdict = "ok"
        elif best <= THRESH_POOR:
            verdict = "shape"
            issues.append(f"stroke {i + 1} is the right stroke but off-shape")
        else:
            verdict = "wrong"
            issues.append(f"stroke {i + 1} doesn't match stroke {j + 1} of {char}")

        strokes_out.append({
            "drawn": i, "expected": j, "verdict": verdict,
            "distance": round(d, 3), "reversed_distance": round(d_rev, 3),
            "backwards": backwards,
        })

    missing = [j for j in range(len(ref)) if j not in {s["expected"] for s in strokes_out}]
    if len(user) != len(ref):
        issues.insert(0, f"{char} has {len(ref)} strokes, you drew {len(user)}")

    order_ok = all(s["expected"] == s["drawn"] for s in strokes_out)
    return {
        "char": char,
        "expected_strokes": len(ref),
        "drawn_strokes": len(user),
        "correct": (len(user) == len(ref) and order_ok
                    and all(s["verdict"] == "ok" for s in strokes_out)),
        "count_ok": len(user) == len(ref),
        "order_ok": order_ok,
        "strokes": strokes_out,
        "missing_strokes": missing,
        "issues": issues,
    }
