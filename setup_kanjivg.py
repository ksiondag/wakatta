"""Download and extract KanjiVG stroke data into data/kanjivg/."""

import urllib.request
import zipfile
from pathlib import Path

API_URL = "https://api.github.com/repos/KanjiVG/kanjivg/releases/latest"
ZIP_PATH = Path("data/kanjivg.zip")
OUT_DIR = Path("data/kanjivg")


def _get_release_url() -> str:
    import json
    req = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    assets = [a for a in data["assets"] if a["name"].endswith(".zip")]
    if not assets:
        raise RuntimeError(f"No zip asset found in latest KanjiVG release: {data.get('tag_name')}")
    url = assets[0]["browser_download_url"]
    print(f"Latest release: {data['tag_name']}  ->  {url}")
    return url


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if list(OUT_DIR.glob("*.svg")):
        print(f"KanjiVG already extracted to {OUT_DIR}/ ({len(list(OUT_DIR.glob('*.svg')))} files)")
        return

    if not ZIP_PATH.exists():
        release_url = _get_release_url()
        print(f"Downloading KanjiVG ...")
        ZIP_PATH.parent.mkdir(parents=True, exist_ok=True)

        def _progress(count, block, total):
            print(f"\r  {min(count * block / total * 100, 100):.1f}%", end="", flush=True)

        urllib.request.urlretrieve(release_url, ZIP_PATH, _progress)
        print()

    print(f"Extracting to {OUT_DIR}/ ...")
    with zipfile.ZipFile(ZIP_PATH) as zf:
        svg_members = [m for m in zf.namelist() if m.endswith(".svg")]
        for i, member in enumerate(svg_members):
            filename = Path(member).name
            (OUT_DIR / filename).write_bytes(zf.read(member))
            if i % 500 == 0:
                print(f"  {i}/{len(svg_members)}")
    print(f"Done — {len(list(OUT_DIR.glob('*.svg')))} SVG files extracted.")


if __name__ == "__main__":
    main()
