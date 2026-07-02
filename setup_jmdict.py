"""Download the English JMdict-simplified release into data/dictionary/."""

import json
import urllib.request
import zipfile
from pathlib import Path

API_URL = "https://api.github.com/repos/scriptin/jmdict-simplified/releases/latest"
OUT_DIR = Path("data/dictionary")
ZIP_PATH = OUT_DIR / "jmdict-eng.zip"


def _get_release_asset_url() -> str:
    req = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    assets = [a for a in data["assets"] if a["name"].startswith("jmdict-eng-") and a["name"].endswith(".json.zip")]
    if not assets:
        raise RuntimeError(f"No jmdict-eng-*.json.zip asset found in latest release: {data.get('tag_name')}")
    url = assets[0]["browser_download_url"]
    print(f"Latest release: {data['tag_name']}  ->  {url}")
    return url


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if list(OUT_DIR.glob("jmdict-eng-*.json")):
        print(f"JMdict already extracted to {OUT_DIR}/")
        return

    if not ZIP_PATH.exists():
        release_url = _get_release_asset_url()
        print("Downloading JMdict (English) ...")

        def _progress(count, block, total):
            print(f"\r  {min(count * block / total * 100, 100):.1f}%", end="", flush=True)

        urllib.request.urlretrieve(release_url, ZIP_PATH, _progress)
        print()

    print(f"Extracting to {OUT_DIR}/ ...")
    with zipfile.ZipFile(ZIP_PATH) as zf:
        json_members = [m for m in zf.namelist() if m.endswith(".json")]
        for member in json_members:
            filename = Path(member).name
            (OUT_DIR / filename).write_bytes(zf.read(member))
    print(f"Done — {', '.join(Path(m).name for m in json_members)} extracted.")


if __name__ == "__main__":
    main()
