"""Quick test: run manga-ocr on a handwriting sample, with and without CTD region detection."""

import sys
from pathlib import Path

import cv2
import numpy as np
from manga_ocr import MangaOcr
from PIL import Image

import ctd

IMAGE_PATH = Path(sys.argv[1] if len(sys.argv) > 1 else "japanese-handwriting.png")


def main():
    if not IMAGE_PATH.exists():
        print(f"File not found: {IMAGE_PATH}")
        sys.exit(1)

    image = Image.open(IMAGE_PATH).convert("RGB")
    print(f"Image size: {image.size[0]}x{image.size[1]}")

    mocr = MangaOcr()

    # Pass 1: whole image at once
    print("\n--- Full image OCR ---")
    print(mocr(image))

    # Pass 2: CTD region detection first
    img_bgr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    regions = ctd.detect(img_bgr)
    print(f"\n--- CTD found {len(regions)} regions ---")
    for i, region in enumerate(regions):
        x1, y1, x2, y2 = region.xyxy
        crop = image.crop((x1, y1, x2, y2))
        text = mocr(crop).strip()
        print(f"[{i+1}] ({x1},{y1})-({x2},{y2}) conf={region.prob:.2f}: {text}")

    # Save visualization
    vis = image.copy()
    import PIL.ImageDraw
    draw = PIL.ImageDraw.Draw(vis)
    for i, region in enumerate(regions):
        x1, y1, x2, y2 = region.xyxy
        draw.rectangle([x1, y1, x2, y2], outline=(0, 200, 0), width=2)
        draw.text((x1, max(y1 - 12, 0)), str(i + 1), fill=(0, 200, 0))
    out = IMAGE_PATH.with_stem(IMAGE_PATH.stem + "_regions")
    vis.save(out)
    print(f"\nSaved region visualization -> {out}")


if __name__ == "__main__":
    main()
