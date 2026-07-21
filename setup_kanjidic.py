"""Download KANJIDIC2 + KRADFILE into data/dictionary/."""

import gzip
import urllib.request
from pathlib import Path

KANJIDIC_URL = "http://www.edrdg.org/kanjidic/kanjidic2.xml.gz"
KRADFILE_URL = "http://ftp.edrdg.org/pub/Nihongo/kradfile.gz"

OUT_DIR = Path("data/dictionary")
KANJIDIC_PATH = OUT_DIR / "kanjidic2.xml"
KRADFILE_PATH = OUT_DIR / "kradfile"


def _download_gz(url: str, out_path: Path, encoding: str | None = None) -> None:
    print(f"Downloading {url} ...")
    with urllib.request.urlopen(url) as r:
        raw = gzip.decompress(r.read())
    if encoding is not None:
        raw = raw.decode(encoding).encode("utf-8")
    out_path.write_bytes(raw)
    print(f"  -> {out_path} ({out_path.stat().st_size / 1_000_000:.1f} MB)")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if KANJIDIC_PATH.exists():
        print(f"{KANJIDIC_PATH} already exists.")
    else:
        _download_gz(KANJIDIC_URL, KANJIDIC_PATH)

    if KRADFILE_PATH.exists():
        print(f"{KRADFILE_PATH} already exists.")
    else:
        # KRADFILE ships EUC-JP encoded; normalize to UTF-8 like the rest of the app.
        _download_gz(KRADFILE_URL, KRADFILE_PATH, encoding="euc-jp")

    print("Done.")


if __name__ == "__main__":
    main()
