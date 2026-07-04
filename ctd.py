"""
Minimal Comic Text Detector (CTD) inference using the ONNX CPU model.

Distilled from manga-image-translator's detection pipeline:
  - letterbox preprocessing
  - cv2.dnn ONNX inference
  - SegDetector postprocessing (shapely + pyclipper for polygon unclip)
  - proximity-based merging of individual detected text lines into blocks

Only the CPU/ONNX path is implemented; GPU (.pt) is not needed here.
"""

import hashlib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pyclipper
from shapely.geometry import Polygon

MODEL_URL = (
    "https://github.com/zyddnys/manga-image-translator/releases/download/"
    "beta-0.3/comictextdetector.pt.onnx"
)
MODEL_HASH = "1a86ace74961413cbd650002e7bb4dcec4980ffa21b2f19b86933372071d718f"
MODEL_PATH = Path(__file__).parent / "models" / "comictextdetector.pt.onnx"
INPUT_SIZE = 1024

# The model segments text line-by-line, so a single word or sentence often comes back as
# several adjacent line boxes (e.g. one vertical column split into two stacked pieces, or
# several columns of the same bubble). Boxes closer than this, as a multiple of their own
# character size, are merged into one block — tuned to bridge normal intra-bubble spacing
# while leaving the (usually much larger) gap between separate bubbles alone.
MERGE_GAP_RATIO = 0.6


@dataclass
class TextRegion:
    pts: np.ndarray  # (4, 2) int32 corner points, clockwise from top-left
    prob: float
    direction: str  # 'v' (vertical) or 'h' (horizontal)

    @property
    def xyxy(self) -> tuple[int, int, int, int]:
        x1, y1 = self.pts.min(axis=0)
        x2, y2 = self.pts.max(axis=0)
        return int(x1), int(y1), int(x2), int(y2)


# --- model download ----------------------------------------------------------

def _verify_hash(path: Path) -> bool:
    return hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_HASH


def download_model():
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if MODEL_PATH.exists() and _verify_hash(MODEL_PATH):
        return
    if MODEL_PATH.exists():
        print("CTD model hash mismatch — re-downloading.")
        MODEL_PATH.unlink()

    print(f"Downloading CTD ONNX model ({MODEL_URL}) ...")

    def _progress(count, block, total):
        print(f"\r  {min(count * block / total * 100, 100):.1f}%", end="", flush=True)

    urllib.request.urlretrieve(MODEL_URL, MODEL_PATH, _progress)
    print()


# --- preprocessing -----------------------------------------------------------

def _letterbox(img: np.ndarray, size: int = INPUT_SIZE):
    """Resize-with-padding to size×size, padding bottom and right."""
    h, w = img.shape[:2]
    r = min(size / h, size / w)
    new_w, new_h = int(round(w * r)), int(round(h * r))
    dw, dh = size - new_w, size - new_h
    img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    img = cv2.copyMakeBorder(img, 0, dh, 0, dw, cv2.BORDER_CONSTANT, value=(0, 0, 0))
    return img, dw, dh


# --- postprocessing ----------------------------------------------------------

def _unclip(box: np.ndarray, ratio: float) -> np.ndarray | None:
    poly = Polygon(box)
    if poly.area <= 0:
        return None
    distance = poly.area * ratio / poly.length
    offset = pyclipper.PyclipperOffset()
    offset.AddPath(box.tolist(), pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    expanded = offset.Execute(distance)
    return np.array(expanded[0]) if expanded else None


def _mini_boxes(contour) -> tuple[list, float]:
    rect = cv2.minAreaRect(contour)
    pts = sorted(cv2.boxPoints(rect).tolist(), key=lambda p: p[0])
    i1, i4 = (0, 1) if pts[1][1] > pts[0][1] else (1, 0)
    i2, i3 = (2, 3) if pts[3][1] > pts[2][1] else (3, 2)
    return [pts[i1], pts[i2], pts[i3], pts[i4]], min(rect[1])


def _box_score(bitmap: np.ndarray, box: np.ndarray) -> float:
    h, w = bitmap.shape[:2]
    xmin = int(np.clip(np.floor(box[:, 0].min()), 0, w - 1))
    xmax = int(np.clip(np.ceil(box[:, 0].max()),  0, w - 1))
    ymin = int(np.clip(np.floor(box[:, 1].min()), 0, h - 1))
    ymax = int(np.clip(np.ceil(box[:, 1].max()),  0, h - 1))
    mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), dtype=np.uint8)
    b = box.copy().astype(np.int32)
    b[:, 0] -= xmin
    b[:, 1] -= ymin
    cv2.fillPoly(mask, b.reshape(1, -1, 2), 1)
    return float(cv2.mean(bitmap[ymin:ymax + 1, xmin:xmax + 1], mask)[0])


def _extract_boxes(
    lines_map: np.ndarray,
    dest_h: int,
    dest_w: int,
    seg_thresh: float = 0.3,
    box_thresh: float = 0.6,
    unclip_ratio: float = 2.3,
    min_size: int = 3,
) -> list[tuple[np.ndarray, float]]:
    """
    Run SegDetectorRepresenter logic on a lines_map output (N, C, H, W).
    Returns list of (pts_4x2, score).
    """
    pred = lines_map[0, 0]          # (H, W)
    bitmap = (pred > seg_thresh).astype(np.uint8)
    map_h, map_w = pred.shape

    contours, _ = cv2.findContours(bitmap * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    results = []
    for contour in contours[:1000]:
        contour = contour.squeeze(1)
        pts, sside = _mini_boxes(contour)
        if sside < 2:
            continue
        score = _box_score(pred, np.array(pts))
        if score < box_thresh:
            continue
        box = _unclip(np.array(pts), unclip_ratio)
        if box is None:
            continue
        box, sside = _mini_boxes(box.reshape(-1, 1, 2))
        if sside < min_size + 2:
            continue
        box = np.array(box)
        box[:, 0] = np.clip(np.round(box[:, 0] / map_w * dest_w), 0, dest_w)
        box[:, 1] = np.clip(np.round(box[:, 1] / map_h * dest_h), 0, dest_h)
        results.append((box.astype(np.int32), score))

    return results


# --- line -> block merging ----------------------------------------------------

def _dilated_xyxy(region: "TextRegion") -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = region.xyxy
    char_size = max(min(x2 - x1, y2 - y1), 4)  # short side of a line box ~ one character
    m = char_size * MERGE_GAP_RATIO
    return x1 - m, y1 - m, x2 + m, y2 + m


def _rects_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return ax1 < bx2 and bx1 < ax2 and ay1 < by2 and by1 < ay2


def merge_regions(regions: list["TextRegion"]) -> list["TextRegion"]:
    """Merge individually-detected text lines that likely belong to the same word,
    sentence, or speech bubble into single blocks — union-find over line boxes
    dilated by a fraction of their own character size, only merging boxes that
    share a reading direction (never mixes vertical and horizontal text)."""
    if len(regions) <= 1:
        return regions

    parent = list(range(len(regions)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    dilated = [_dilated_xyxy(r) for r in regions]
    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            if regions[i].direction != regions[j].direction:
                continue
            if _rects_overlap(dilated[i], dilated[j]):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(regions)):
        groups.setdefault(find(i), []).append(i)

    merged = []
    for idxs in groups.values():
        pts = np.concatenate([regions[i].pts for i in idxs], axis=0)
        x1, y1 = pts.min(axis=0)
        x2, y2 = pts.max(axis=0)
        rect_pts = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.int32)
        prob = min(regions[i].prob for i in idxs)
        merged.append(TextRegion(pts=rect_pts, prob=prob, direction=regions[idxs[0]].direction))

    return merged


# --- public API --------------------------------------------------------------

_net: cv2.dnn.Net | None = None


def load_model():
    global _net
    download_model()
    _net = cv2.dnn.readNetFromONNX(str(MODEL_PATH))
    print("CTD model ready.")


def detect(image_bgr: np.ndarray) -> list[TextRegion]:
    """Detect text regions in a BGR image. Returns TextRegion list."""
    global _net
    if _net is None:
        load_model()

    im_h, im_w = image_bgr.shape[:2]
    img, dw, dh = _letterbox(image_bgr, INPUT_SIZE)
    blob = cv2.dnn.blobFromImage(img, scalefactor=1.0 / 255.0, size=(INPUT_SIZE, INPUT_SIZE))

    _net.setInput(blob)
    layer_names = _net.getUnconnectedOutLayersNames()
    outputs = _net.forward(layer_names)   # [blks, mask, lines_map] — order may vary

    blks, mask, lines_map = outputs

    # Some OpenCV versions return mask and lines_map swapped
    if mask.shape[1] == 2:
        mask, lines_map = lines_map, mask

    # Trim letterbox padding (added to bottom/right)
    lines_map = lines_map[:, :, : lines_map.shape[2] - dh, : lines_map.shape[3] - dw]

    raw = _extract_boxes(lines_map, dest_h=im_h, dest_w=im_w)

    regions = []
    for pts, score in raw:
        w_box = int(pts[:, 0].max() - pts[:, 0].min())
        h_box = int(pts[:, 1].max() - pts[:, 1].min())
        direction = "v" if h_box > w_box * 1.5 else "h"
        regions.append(TextRegion(pts=pts, prob=score, direction=direction))

    return merge_regions(regions)
