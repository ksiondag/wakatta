"""Recovers manga reading order (top row before bottom row; right column before
left column within a row) from a flat set of text-region bounding boxes.

Uses the recursive row/column "xy-cut" heuristic from document layout analysis:
repeatedly split the current group of boxes along whichever axis has a clean
projection gap (a band with nothing crossing it), alternating axis on each
recursive step, until every group is a single box. A cluster that can't be
split on either axis (e.g. overlapping boxes) falls back to a direct
top-to-bottom-then-right-to-left sort.
"""

Box = tuple[float, float, float, float]  # x1, y1, x2, y2


def reading_order(boxes: list[Box]) -> list[int]:
    """Return indices into `boxes`, ordered top-to-bottom / right-to-left."""
    return _cut(boxes, list(range(len(boxes))), axis="y")


def _cut(boxes: list[Box], indices: list[int], axis: str) -> list[int]:
    if len(indices) <= 1:
        return list(indices)

    other = "x" if axis == "y" else "y"

    groups = _split(boxes, indices, axis)
    if len(groups) > 1:
        return [i for g in groups for i in _cut(boxes, g, other)]

    groups = _split(boxes, indices, other)
    if len(groups) > 1:
        return [i for g in groups for i in _cut(boxes, g, axis)]

    # Neither axis found a gap to split this cluster on.
    return sorted(indices, key=lambda i: (boxes[i][1], -boxes[i][2]))


def _split(boxes: list[Box], indices: list[int], axis: str) -> list[list[int]]:
    """Group `indices` into reading-order bands along `axis`, splitting at any
    point where no box's extent crosses (a gap in the projection profile)."""
    if axis == "y":
        def interval(i: int) -> tuple[float, float]:
            return boxes[i][1], boxes[i][3]  # y1, y2 — ascending = top-to-bottom
    else:
        def interval(i: int) -> tuple[float, float]:
            return -boxes[i][2], -boxes[i][0]  # mirrored x — ascending = right-to-left

    ordered = sorted(indices, key=lambda i: interval(i)[0])
    groups: list[list[int]] = []
    current: list[int] = []
    running_hi: float | None = None
    for i in ordered:
        lo, hi = interval(i)
        if current and lo >= running_hi:
            groups.append(current)
            current = []
            running_hi = None
        current.append(i)
        running_hi = hi if running_hi is None else max(running_hi, hi)
    if current:
        groups.append(current)
    return groups
