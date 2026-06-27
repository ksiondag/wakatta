"""
POC 1: PDF page -> CTD region detection -> manga-ocr -> fugashi tokenization
"""

import io
from pathlib import Path

import cv2
import fitz  # PyMuPDF
import fugashi
import numpy as np
from manga_ocr import MangaOcr
from PIL import Image

import ctd

PDF_PATH = next(Path("/home/silk/Downloads").glob("*ナウシカ*01*.pdf"))
PAGE_NUM = 2
DPI = 150


def extract_page(pdf_path: Path, page_num: int, dpi: int) -> Image.Image:
    doc = fitz.open(str(pdf_path))
    pix = doc[page_num].get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def crop_region(image: Image.Image, x1: int, y1: int, x2: int, y2: int) -> Image.Image:
    margin = 4
    w, h = image.size
    return image.crop((
        max(0, x1 - margin), max(0, y1 - margin),
        min(w, x2 + margin), min(h, y2 + margin),
    ))


def tokenize(text: str, tagger: fugashi.Tagger) -> list[dict]:
    results = []
    for word in tagger(text):
        f = word.feature
        if getattr(f, "pos1", "?") == "補助記号":
            continue
        results.append({
            "surface": word.surface,
            "lemma": getattr(f, "lemma", None) or word.surface,
            "reading": getattr(f, "kana", None) or "?",
            "pos": getattr(f, "pos1", None) or "?",
        })
    return results


def draw_regions(image: Image.Image, regions: list[ctd.TextRegion]) -> Image.Image:
    vis = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    for i, region in enumerate(regions):
        x1, y1, x2, y2 = region.xyxy
        color = (0, 200, 0) if region.direction == "v" else (200, 100, 0)
        cv2.rectangle(vis, (x1, y1), (x2, y2), color, 2)
        cv2.putText(vis, f"{i+1} {region.prob:.2f}", (x1, max(y1 - 4, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
    return Image.fromarray(cv2.cvtColor(vis, cv2.COLOR_BGR2RGB))


def main():
    print(f"Extracting page {PAGE_NUM} ...")
    page_image = extract_page(PDF_PATH, PAGE_NUM, DPI)
    print(f"Page size: {page_image.size[0]}x{page_image.size[1]}")

    img_bgr = cv2.cvtColor(np.array(page_image.convert("RGB")), cv2.COLOR_RGB2BGR)

    print("\nDetecting text regions ...")
    regions = ctd.detect(img_bgr)
    print(f"Found {len(regions)} regions")

    vis = draw_regions(page_image, regions)
    vis.save("page_regions.png")
    print("Saved bounding box visualization -> page_regions.png")

    if not regions:
        print("No regions found. Try a different page or loosen thresholds in ctd.py.")
        return

    print("\nLoading manga-ocr ...")
    mocr = MangaOcr()
    tagger = fugashi.Tagger()

    print("\n" + "=" * 60)
    for i, region in enumerate(regions):
        x1, y1, x2, y2 = region.xyxy
        crop = crop_region(page_image, x1, y1, x2, y2)
        text = mocr(crop).strip()
        if not text:
            continue

        tokens = tokenize(text, tagger)
        print(f"\n[{i+1}] {region.direction} conf={region.prob:.2f}  ({x1},{y1})-({x2},{y2})")
        print(f"  OCR: {text}")
        for w in tokens:
            print(f"    {w['surface']:8} -> {w['reading']:12} ({w['pos']})  [{w['lemma']}]")
    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
