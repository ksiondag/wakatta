"""KANJIDIC2 + KRADFILE lookup: readings, meanings, and radical/component
breakdown per kanji character.

Like dictionary.py, kanji_entries is bulk-imported via raw SQL into whatever
engine the caller passes in rather than through SQLAlchemy ORM models — it's
read-only ETL output with no relational integration into the Work/Page/
Sentence tree.
"""

import json
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Engine

DATA_DIR = Path("data/dictionary")
KANJIDIC_XML = DATA_DIR / "kanjidic2.xml"
KRADFILE_PATH = DATA_DIR / "kradfile"

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS kanji_entries (
        char TEXT PRIMARY KEY,
        data TEXT NOT NULL   -- JSON blob: {on, kun, meanings, grade, jlpt,
                              --   stroke_count, freq, radical, components}
    )
    """,
]

# The 214 traditional Kangxi radicals: number -> (radical char, common English name).
KANGXI_RADICALS: dict[int, tuple[str, str]] = {
    1: ("一", "one"), 2: ("丨", "line"), 3: ("丶", "dot"), 4: ("丿", "slash"),
    5: ("乙", "second"), 6: ("亅", "hook"), 7: ("二", "two"), 8: ("亠", "lid"),
    9: ("人", "person"), 10: ("儿", "legs"), 11: ("入", "enter"), 12: ("八", "eight"),
    13: ("冂", "wide"), 14: ("冖", "cover"), 15: ("冫", "ice"), 16: ("几", "table"),
    17: ("凵", "open box"), 18: ("刀", "knife"), 19: ("力", "power"), 20: ("勹", "wrap"),
    21: ("匕", "spoon"), 22: ("匚", "box"), 23: ("匸", "hiding enclosure"), 24: ("十", "ten"),
    25: ("卜", "divination"), 26: ("卩", "seal"), 27: ("厂", "cliff"), 28: ("厶", "private"),
    29: ("又", "again"), 30: ("口", "mouth"), 31: ("囗", "enclosure"), 32: ("土", "earth"),
    33: ("士", "scholar"), 34: ("夂", "go"), 35: ("夊", "go slowly"), 36: ("夕", "evening"),
    37: ("大", "big"), 38: ("女", "woman"), 39: ("子", "child"), 40: ("宀", "roof"),
    41: ("寸", "inch"), 42: ("小", "small"), 43: ("尢", "lame"), 44: ("尸", "corpse"),
    45: ("屮", "sprout"), 46: ("山", "mountain"), 47: ("巛", "river"), 48: ("工", "work"),
    49: ("己", "self"), 50: ("巾", "cloth"), 51: ("干", "dry"), 52: ("幺", "short thread"),
    53: ("广", "dotted cliff"), 54: ("廴", "long stride"), 55: ("廾", "two hands"), 56: ("弋", "arrow"),
    57: ("弓", "bow"), 58: ("彐", "snout"), 59: ("彡", "bristle"), 60: ("彳", "step"),
    61: ("心", "heart"), 62: ("戈", "halberd"), 63: ("戶", "door"), 64: ("手", "hand"),
    65: ("支", "branch"), 66: ("攴", "rap"), 67: ("文", "script"), 68: ("斗", "dipper"),
    69: ("斤", "axe"), 70: ("方", "square"), 71: ("无", "not"), 72: ("日", "sun"),
    73: ("曰", "say"), 74: ("月", "moon"), 75: ("木", "tree"), 76: ("欠", "lack"),
    77: ("止", "stop"), 78: ("歹", "death"), 79: ("殳", "weapon"), 80: ("毋", "do not"),
    81: ("比", "compare"), 82: ("毛", "fur"), 83: ("氏", "clan"), 84: ("气", "steam"),
    85: ("水", "water"), 86: ("火", "fire"), 87: ("爪", "claw"), 88: ("父", "father"),
    89: ("爻", "trigrams"), 90: ("爿", "split wood"), 91: ("片", "slice"), 92: ("牙", "fang"),
    93: ("牛", "cow"), 94: ("犬", "dog"), 95: ("玄", "profound"), 96: ("玉", "jade"),
    97: ("瓜", "melon"), 98: ("瓦", "tile"), 99: ("甘", "sweet"), 100: ("生", "life"),
    101: ("用", "use"), 102: ("田", "field"), 103: ("疋", "bolt of cloth"), 104: ("疒", "sickness"),
    105: ("癶", "dotted tent"), 106: ("白", "white"), 107: ("皮", "skin"), 108: ("皿", "dish"),
    109: ("目", "eye"), 110: ("矛", "spear"), 111: ("矢", "arrow"), 112: ("石", "stone"),
    113: ("示", "spirit"), 114: ("禸", "track"), 115: ("禾", "grain"), 116: ("穴", "cave"),
    117: ("立", "stand"), 118: ("竹", "bamboo"), 119: ("米", "rice"), 120: ("糸", "thread"),
    121: ("缶", "jar"), 122: ("网", "net"), 123: ("羊", "sheep"), 124: ("羽", "feather"),
    125: ("老", "old"), 126: ("而", "and"), 127: ("耒", "plow"), 128: ("耳", "ear"),
    129: ("聿", "brush"), 130: ("肉", "meat"), 131: ("臣", "minister"), 132: ("自", "self"),
    133: ("至", "arrive"), 134: ("臼", "mortar"), 135: ("舌", "tongue"), 136: ("舛", "oppose"),
    137: ("舟", "boat"), 138: ("艮", "stopping"), 139: ("色", "color"), 140: ("艸", "grass"),
    141: ("虍", "tiger"), 142: ("虫", "insect"), 143: ("血", "blood"), 144: ("行", "walk enclosure"),
    145: ("衣", "clothes"), 146: ("襾", "west"), 147: ("見", "see"), 148: ("角", "horn"),
    149: ("言", "speech"), 150: ("谷", "valley"), 151: ("豆", "bean"), 152: ("豕", "pig"),
    153: ("豸", "badger"), 154: ("貝", "shell"), 155: ("赤", "red"), 156: ("走", "run"),
    157: ("足", "foot"), 158: ("身", "body"), 159: ("車", "cart"), 160: ("辛", "bitter"),
    161: ("辰", "morning"), 162: ("辵", "walk"), 163: ("邑", "city"), 164: ("酉", "wine"),
    165: ("釆", "distinguish"), 166: ("里", "village"), 167: ("金", "gold"), 168: ("長", "long"),
    169: ("門", "gate"), 170: ("阜", "mound"), 171: ("隶", "slave"), 172: ("隹", "short-tailed bird"),
    173: ("雨", "rain"), 174: ("青", "blue"), 175: ("非", "wrong"), 176: ("面", "face"),
    177: ("革", "leather"), 178: ("韋", "tanned leather"), 179: ("韭", "leek"), 180: ("音", "sound"),
    181: ("頁", "head"), 182: ("風", "wind"), 183: ("飛", "fly"), 184: ("食", "eat"),
    185: ("首", "head"), 186: ("香", "fragrant"), 187: ("馬", "horse"), 188: ("骨", "bone"),
    189: ("高", "tall"), 190: ("髟", "hair"), 191: ("鬥", "fight"), 192: ("鬯", "sacrificial wine"),
    193: ("鬲", "cauldron"), 194: ("鬼", "ghost"), 195: ("魚", "fish"), 196: ("鳥", "bird"),
    197: ("鹵", "salt"), 198: ("鹿", "deer"), 199: ("麥", "wheat"), 200: ("麻", "hemp"),
    201: ("黃", "yellow"), 202: ("黍", "millet"), 203: ("黑", "black"), 204: ("黹", "embroidery"),
    205: ("黽", "frog"), 206: ("鼎", "tripod"), 207: ("鼓", "drum"), 208: ("鼠", "rat"),
    209: ("鼻", "nose"), 210: ("齊", "even"), 211: ("齒", "tooth"), 212: ("龍", "dragon"),
    213: ("龜", "turtle"), 214: ("龠", "flute"),
}


def is_ready(engine: Engine) -> bool:
    if not inspect(engine).has_table("kanji_entries"):
        return False
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM kanji_entries LIMIT 1")).first() is not None


def _parse_kradfile(path: Path) -> dict[str, list[str]]:
    """Parse `char : c1 c2 c3 ...` lines into {char: [components]}, skipping
    '#'-comment lines."""
    components: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        char, _, rest = line.partition(":")
        char = char.strip()
        parts = rest.split()
        if char and parts:
            components[char] = parts
    return components


def _parse_kanjidic(path: Path) -> dict[str, dict]:
    """Parse kanjidic2.xml into {char: {on, kun, meanings, grade, jlpt,
    stroke_count, freq, radical}} (radical/components merged in by build_db)."""
    entries: dict[str, dict] = {}
    for _, elem in ET.iterparse(path, events=("end",)):
        if elem.tag != "character":
            continue
        literal_el = elem.find("literal")
        if literal_el is None or not literal_el.text:
            elem.clear()
            continue
        char = literal_el.text

        misc = elem.find("misc")
        grade_el = misc.find("grade") if misc is not None else None
        jlpt_el = misc.find("jlpt") if misc is not None else None
        freq_el = misc.find("freq") if misc is not None else None
        stroke_el = misc.find("stroke_count") if misc is not None else None

        rad_num = None
        radical_el = elem.find("radical")
        if radical_el is not None:
            rad_values = {rv.get("rad_type"): rv.text for rv in radical_el.findall("rad_value")}
            classical = rad_values.get("classical")
            rad_num = int(classical) if classical else (
                int(next(iter(rad_values.values()))) if rad_values else None
            )

        on: list[str] = []
        kun: list[str] = []
        meanings: list[str] = []
        rm = elem.find("reading_meaning")
        if rm is not None:
            for rmgroup in rm.findall("rmgroup"):
                for r in rmgroup.findall("reading"):
                    if r.get("r_type") == "ja_on" and r.text:
                        on.append(r.text)
                    elif r.get("r_type") == "ja_kun" and r.text:
                        kun.append(r.text)
                for m in rmgroup.findall("meaning"):
                    if m.get("m_lang") is None and m.text:
                        meanings.append(m.text)

        entries[char] = {
            "on": on,
            "kun": kun,
            "meanings": meanings,
            "grade": int(grade_el.text) if grade_el is not None and grade_el.text else None,
            "jlpt": int(jlpt_el.text) if jlpt_el is not None and jlpt_el.text else None,
            "freq": int(freq_el.text) if freq_el is not None and freq_el.text else None,
            "stroke_count": int(stroke_el.text) if stroke_el is not None and stroke_el.text else None,
            "radical_number": rad_num,
        }
        elem.clear()
    return entries


def build_db(engine: Engine, force: bool = False) -> None:
    """Idempotent one-time ETL: KANJIDIC2 XML + KRADFILE -> kanji_entries.
    Safe to call on every server start; skips if already populated unless
    force=True."""
    if is_ready(engine) and not force:
        return

    if not KANJIDIC_XML.exists() or not KRADFILE_PATH.exists():
        raise FileNotFoundError(
            "Missing data/dictionary/kanjidic2.xml or kradfile — "
            "run `uv run setup_kanjidic.py` first."
        )

    print(f"[kanji] Building kanji table from {KANJIDIC_XML.name} + {KRADFILE_PATH.name} ...")
    kanjidic = _parse_kanjidic(KANJIDIC_XML)
    components_by_char = _parse_kradfile(KRADFILE_PATH)

    with engine.begin() as conn:
        for stmt in _SCHEMA_STATEMENTS:
            conn.execute(text(stmt))
        conn.execute(text("DELETE FROM kanji_entries"))

        for char, info in kanjidic.items():
            rad_num = info.pop("radical_number")
            rad_char, rad_name = KANGXI_RADICALS.get(rad_num, (None, None))
            data = {
                **info,
                "radical": {"number": rad_num, "char": rad_char, "name": rad_name} if rad_num else None,
                "components": components_by_char.get(char, []),
            }
            conn.execute(
                text("INSERT INTO kanji_entries (char, data) VALUES (:c, :d)"),
                {"c": char, "d": json.dumps(data, ensure_ascii=False)},
            )
    print(f"[kanji] Loaded {len(kanjidic)} KANJIDIC2 entries.")


def lookup(engine: Engine, char: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM kanji_entries WHERE char = :c"), {"c": char}
        ).first()
    if row is None:
        return None
    data = json.loads(row[0])
    return {"char": char, **data}


# KRADFILE decomposes kanji using only JIS X 0208 characters, so radicals not
# in that codeset are represented by a stand-in kanji that contains them (e.g.
# the "water" radical isn't itself in JIS X 0208, so 汁 stands in for it —
# see the comment header of data/dictionary/kradfile). The compact radical
# glyphs a user would actually draw (氵, 忄, 扌, 辶, 艹 — also present as their
# own standalone characters, and thus recognizable by the DTW handwriting
# matcher) are different Unicode code points from those stand-ins, so a drawn
# radical needs translating to its KRADFILE stand-in before it'll match any
# component data. Verified empirically against kradfile (e.g. 河 contains 汁,
# 情 contains 忙, 持 contains 扎, 近/道 contain 込, 花/草 contain 艾).
_RADICAL_VARIANT_ALIASES: dict[str, str] = {
    "氵": "汁",
    "忄": "忙",
    "扌": "扎",
    "辶": "込",
    "艹": "艾",
}

_component_index: dict[str, set[str]] | None = None


def build_component_index(engine: Engine) -> None:
    """One-time reverse index: component/radical char -> set of kanji chars
    that contain it (per KRADFILE), used by search_by_components() to let a
    user narrow a kanji search by the pieces they can see/draw rather than
    the whole character."""
    global _component_index
    index: dict[str, set[str]] = {}
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT char, data FROM kanji_entries")).all()
    for char, data_json in rows:
        data = json.loads(data_json)
        for comp in data.get("components", []):
            index.setdefault(comp, set()).add(char)
    _component_index = index


def search_by_components(engine: Engine, chars: list[str], limit: int = 300) -> list[dict]:
    """Kanji containing ALL of the given component chars, sorted by stroke
    count then frequency. Returns [] if any component is unknown or the
    intersection is empty."""
    if _component_index is None:
        build_component_index(engine)
    if not chars:
        return []

    chars = [_RADICAL_VARIANT_ALIASES.get(c, c) for c in chars]
    sets = [_component_index.get(c, set()) for c in chars]
    matches = set.intersection(*sets) if all(sets) else set()
    if not matches:
        return []

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT char, data FROM kanji_entries WHERE char IN :chars").bindparams(
                bindparam("chars", expanding=True)
            ),
            {"chars": list(matches)},
        ).all()

    results = [{"char": char, **json.loads(data_json)} for char, data_json in rows]
    results.sort(key=lambda d: (d.get("stroke_count") or 99, d.get("freq") or 9999))
    return results[:limit]
