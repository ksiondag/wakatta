"""Download Kanjium's pitch-accent data into data/dictionary/."""

import urllib.request
from pathlib import Path

ACCENTS_URL = "https://raw.githubusercontent.com/mifunetoshiro/kanjium/master/data/source_files/raw/accents.txt"
OUT_DIR = Path("data/dictionary")
ACCENTS_PATH = OUT_DIR / "accents.txt"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if ACCENTS_PATH.exists():
        print(f"Pitch accent data already downloaded to {ACCENTS_PATH}")
        return

    print("Downloading Kanjium pitch accent data ...")
    urllib.request.urlretrieve(ACCENTS_URL, ACCENTS_PATH)
    line_count = sum(1 for _ in ACCENTS_PATH.open(encoding="utf-8"))
    print(f"Done — {ACCENTS_PATH} ({line_count} entries).")


if __name__ == "__main__":
    main()
