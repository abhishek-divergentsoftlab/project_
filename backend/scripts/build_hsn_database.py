"""Build and index the complete HSN/SAC master database from HSN_SAC.xlsx.

Converts all 21,935+ goods HSN codes and 681 service SAC codes into an optimized,
indexed SQLite database with full-text search (FTS5), canonical category mapping,
and commercial trade aliases (e.g., moong, chickpea, solar, basmati).
"""

import logging
import os
import re
import sqlite3
import time
import zipfile
from collections import Counter
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger("build_hsn_database")


def normalize_code(raw: str) -> str:
    s = str(raw).strip()
    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return ""
    if len(digits) in (1, 3, 5, 7):
        digits = "0" + digits
    return digits[:8]


def leaf_prefixes(raw_codes: Iterable[str]) -> set[str]:
    """Every prefix of every 8-digit code in the source (used to tell which codes lost a leading zero)."""
    prefixes: set[str] = set()
    for raw in raw_codes:
        digits = re.sub(r"[^\d]", "", str(raw).strip())
        if len(digits) == 8:
            prefixes.update(digits[:i] for i in range(1, 9))
    return prefixes


def repair_code(raw: str, prefixes: set[str]) -> str:
    """Zero-padded code text. The source spreadsheet stored some codes as numbers, so the sub-levels of chapters
    01-09 lost their leading zero ("03041" -> "3041", filed under chapter 30 instead of 03). A 4 or 6 digit code
    that has no 8-digit descendants but whose zero-padded form does is such a code; where both readings exist
    the code as written is a genuine heading and is kept."""
    digits = re.sub(r"[^\d]", "", str(raw).strip())
    if not digits:
        return ""
    if len(digits) in (4, 6) and digits[0] != "0" and digits not in prefixes and ("0" + digits) in prefixes:
        return "0" + digits
    return normalize_code(digits)


def validate_row(code: str, chapter: str, is_service: bool, chapters: set[str]) -> Optional[str]:
    """Why a (code, chapter) pair is not acceptable, or None. Goods codes: digits only, 2/4/5/6/7/8 long, chapter
    equal to the first two digits and a known chapter. SAC codes: chapter 99, code starting 99."""
    if not code.isdigit() or len(code) not in (2, 4, 5, 6, 7, 8):
        return "malformed code"
    if chapter != code[:2]:
        return "chapter does not match the first two digits"
    if not is_service and chapter not in chapters:
        return "unknown chapter"
    if is_service and chapter != "99":
        return "service code outside 99"
    return None


def map_chapter_to_category(chapter: str) -> str:
    try:
        ch = int(chapter)
    except ValueError:
        return "Industrial"
    if 1 <= ch <= 24:
        return "Agriculture"
    elif ch in (28, 29, 31, 32, 34, 38):
        return "Chemicals"
    elif ch == 30:
        return "Healthcare & Medical"
    elif ch == 33:
        return "Beauty & Personal Care"
    elif ch in (39, 48):
        return "Packaging"
    elif ch in (40, 72, 73, 74, 75, 76, 78, 79, 80, 81, 82, 83, 84):
        return "Industrial"
    elif ch in (50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65):
        return "Textiles"
    elif ch in (25, 68, 69):
        return "Construction"
    elif ch in (85,):
        return "Electronics"
    elif ch in (86, 87, 88, 89):
        return "Automotive"
    elif ch in (90,):
        return "Healthcare & Medical"
    elif ch in (94,):
        return "Furniture"
    elif ch in (49,):
        return "Stationery"
    elif ch == 99:
        return "Services"
    return "Industrial"

# Trade aliases map (code prefix -> additional search keywords)
_TRADE_ALIASES: dict[str, str] = {
    "071331": "Moong Mung Sabut Moong Green Gram Vigna Radiata Urad Black Gram Mungo Dal",
    "071320": "Kabuli Chana Desi Chana Chickpeas Garbanzo Bengal Gram Chana Dal",
    "071333": "Rajma Kidney Beans Chitra Kashmiri Rajma",
    "071340": "Masoor Dal Lentils Red Lentils Brown Lentils",
    "071310": "Matar Peas Green Peas Dry Peas White Vatana",
    "071360": "Toor Dal Tur Arhar Dal Pigeon Peas Cajanus Cajan",
    "0713": "Pulses Dals Legumes Beans Peas Grains",
    "1006": "Basmati Rice Paddy White Rice Parboiled Sona Masoori 1121 Pusa Dhan",
    "1005": "Maize Corn Sweet Corn Yellow Corn Poultry Feed Makka",
    "1001": "Wheat Gehun Sharbati Lokwan Atta Flour",
    "0910": "Ginger Turmeric Haldi Adrak Sonth Jeera Cumin Spices Masala",
    "0802": "Almonds Badam Walnuts Akhrot Dry Fruits Nuts",
    "0801": "Cashews Kaju Coconuts Nariyal",
    "1201": "Soya Beans Soybean DOC Soya Meal Oilseeds",
    "1701": "Sugar Cane Sugar M-30 S-30 Crystal Sugar Jaggery",
    "8541": "Solar Panel Solar PV Module Photovoltaic Cells Mono PERC Bifacial Solar Modules",
    "8504": "Solar Inverter Power Inverter Static Converter UPS Transformer",
    "8544": "Cables USB Cable Type-C Lightning Copper Wire Power Cable Wiring",
    "8507": "Lithium Battery LiFePO4 Storage Battery Power Bank Lead Acid",
    "8708": "Brake Pad Brake Disc Rotor Auto Parts Car Spares Shock Absorber Struts",
    "8511": "Spark Plug Glow Plug Automobile Ignition",
    "2710": "Engine Oil Motor Oil Lubricant Grease 5W-30 15W-40 Synthetic Oil",
    "5201": "Cotton Raw Cotton Cotton Bales Shankar 6 Kapas",
    "5205": "Cotton Yarn Combed Yarn 30s 40s Ring Spun Weaving",
    "6109": "T-Shirt Tshirt Polo Shirt Cotton T-Shirt Knitwear",
    "6307": "Face Mask N95 Mask Surgical Mask 3-Ply PPE Non-Woven",
    "3926": "Nitrile Gloves Blue Nitrile Examination Gloves PPE",
    "4015": "Latex Gloves Surgical Gloves Examination Rubber Gloves",
    "4819": "Corrugated Box Boxes Cartons Packaging Kraft Paper Cardboard",
    "7210": "Galvanized Steel GI Sheet Steel Coil Iron Sheet",
    "7214": "TMT Bars Sariya Steel Rebar Fe 500D Fe 550D Construction Steel",
    "7222": "Stainless Steel SS 304 SS 316L Round Bars Bright Bar",
    "7306": "Steel Pipes MS Pipes ERW Pipes Hollow Sections",
    "7408": "Copper Wire Pure Copper Winding Wire Conductor",
    "2918": "Citric Acid Food Grade Monohydrate Anhydrous Preservative",
    "2815": "Caustic Soda Sodium Hydroxide Flakes Lye NaOH",
    "3102": "Urea Fertilizer 46 Nitrogen Agricultural Fertiliser",
    "3305": "Hair Spray Shampoo Hair Dye Conditioner Hair Styling Cosmetics",
    "9018": "Syringes Needles Surgical Instruments Medical Equipment",
}

def get_aliases_for_code(code: str) -> str:
    aliases = []
    for prefix, terms in _TRADE_ALIASES.items():
        if code.startswith(prefix) or prefix.startswith(code):
            aliases.append(terms)
    return " ".join(aliases)


# ---------------------------------------------------------------------------
# Source reading (stdlib only: no pandas / openpyxl needed at build time)
# ---------------------------------------------------------------------------
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_sheet(xlsx_path: str, sheet_name: str) -> list[tuple[str, str]]:
    """(column A, column B) text of every non-empty row of one sheet. Cells are read as text, so codes keep the
    digits the spreadsheet stores (numbers lose their leading zeros there; ``repair_code`` handles that)."""
    with zipfile.ZipFile(xlsx_path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % _NS["m"])))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid_target = {r.get("Id"): r.get("Target") for r in rels}
        target = None
        for sh in wb.find("m:sheets", _NS):
            if sh.get("name") == sheet_name:
                rid = sh.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                target = rid_target[rid].lstrip("/")
                if not target.startswith("xl/"):
                    target = "xl/" + target
        if target is None:
            return []
        out: list[tuple[str, str]] = []
        for _, row in ET.iterparse(z.open(target)):
            if not row.tag.endswith("}row"):
                continue
            vals = {"A": "", "B": ""}
            for c in row.findall("m:c", _NS):
                col = re.match(r"[A-Z]+", c.get("r", "")).group(0)
                if col not in vals:
                    continue
                t = c.get("t")
                if t == "inlineStr":
                    v = "".join(x.text or "" for x in c.iter("{%s}t" % _NS["m"]))
                else:
                    ve = c.find("m:v", _NS)
                    v = ve.text if ve is not None and ve.text is not None else ""
                    if t == "s" and v != "":
                        v = shared[int(v)]
                vals[col] = v
            row.clear()
            out.append((vals["A"].strip(), vals["B"]))
        return out


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------
_STOP_PIECES = frozenset({"of", "or", "in", "to", "on", "by", "as", "an", "and", "the", "for", "not", "nes", "with", "other"})
_FRAGMENT_START = re.compile(
    r"^(of|for|in|with|from|not|other|others|and|or|to|than|whether|having|containing|excluding|including|"
    r"used|made|by|at|on|if|than|whose|which|having)\b"
)


def clean_text(text: str) -> str:
    """Spreadsheet artefacts out: _x000D_ markers, line breaks, runs of spaces, curly quotes, and the '?' the
    export left where a hyphen was (GROUND?NUTS)."""
    s = (text or "").replace("_x000D_", " ").replace("’", "'").replace("‘", "'")
    s = re.sub(r"(?<=[A-Za-z])\?(?=[A-Za-z])", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+", text)


def build_word_counts(texts: Iterable[str]) -> Counter:
    """Corpus statistics: single words as str keys, adjacent word pairs as (a, b) tuple keys."""
    counts: Counter = Counter()
    for t in texts:
        words = [w.lower() for w in _tokens(t)]
        counts.update(words)
        counts.update(zip(words, words[1:]))
    return counts


def _is_word(piece: str, counts: Counter) -> bool:
    return (len(piece) >= 3 and counts[piece] >= 5) or piece in _STOP_PIECES


def _split_glued(word: str, counts: Counter, depth: int = 3) -> Optional[list[str]]:
    """Pieces of ``word`` when every piece is a corpus word and the pieces occur side by side, as separate
    words, more often in the corpus than the glued form does (OTHERALLIACEOUS vs "other alliaceous"). Real
    compounds such as FRESHWATER or INEDIBLE are used glued, so they survive. Generic: no word list."""
    low = word.lower()
    if depth == 0 or len(low) < 7:
        return None
    for i in range(2, len(low) - 1):
        left, right = low[:i], low[i:]
        if not _is_word(left, counts):
            continue
        if _is_word(right, counts):
            if counts[(left, right)] > counts[low]:
                return [left, right]
            continue
        rest = _split_glued(right, counts, depth - 1) if counts[right] <= 2 else None
        if rest and counts[(left, rest[0])] > counts[low]:
            return [left, *rest]
    return None


def unglue(text: str, counts: Counter, changes: Optional[dict] = None) -> str:
    """Split tokens such as OTHERALLIACEOUS / ANDOTHER into words (see ``_split_glued``); only tokens that are
    glued less often than they appear spaced are split; the original glued spelling stays searchable."""

    def repl(m: re.Match) -> str:
        w = m.group(0)
        pieces = _split_glued(w, counts)
        if not pieces:
            return w
        out, pos = [], 0
        for p in pieces:
            out.append(w[pos:pos + len(p)])
            pos += len(p)
        res = " ".join(out)
        if changes is not None:
            changes[w] = res
        return res

    return re.sub(r"[A-Za-z]{7,}", repl, text)


def sentence_case(text: str) -> str:
    t = text.strip().strip(":;, ")
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) >= 0.6:
        t = t.lower()
    return (t[:1].upper() + t[1:]) if t else t


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def strip_prefix_text(text: str, ancestor: str) -> Optional[str]:
    """``text`` without its leading copy of ``ancestor`` (compared ignoring case, spaces and punctuation), or None
    when ``text`` does not start with it. The source repeats the heading text in front of deep rows."""
    a = _squash(ancestor)
    if len(a) < 4:
        return None
    acc, i = "", 0
    while i < len(text) and len(acc) < len(a):
        ch = text[i]
        if ch.isalnum():
            acc += ch.lower()
            if not a.startswith(acc):
                return None
        i += 1
    if acc != a:
        return None
    return text[i:].lstrip(" :;,-~")


def own_label(desc: str, ancestor_descs: list[str]) -> str:
    """The row's own words: the description minus repeated ancestor prefixes (top-down), last ':' segment."""
    text = desc.split("~")[-1].strip() if "~" in desc else desc
    for anc in ancestor_descs:  # chapter ... parent
        rest = strip_prefix_text(text, anc.split("~")[-1]) if anc else None
        if rest:
            text = rest
    segs = [x.strip() for x in text.split(":") if x.strip(" ;,.-")]
    label = segs[-1] if segs else text
    return label.strip(" :;,")


def is_fragment(label: str) -> bool:
    norm = re.sub(r"[^a-z ]", " ", label.lower()).strip()
    norm = re.sub(r"\s+", " ", norm)
    return len(norm) < 4 or bool(_FRAGMENT_START.match(norm))


def _compounds(texts: Iterable[str]) -> str:
    """Hyphenated words also as one word ("ground-nuts" -> "groundnuts"), so both spellings are searchable."""
    out: set[str] = set()
    for t in texts:
        for m in re.finditer(r"[A-Za-z]+(?:-[A-Za-z]+)+", t):
            out.add(m.group(0).replace("-", "").lower())
    return " ".join(sorted(out))


def build_hierarchy(rows: list[dict]) -> None:
    """Fill parent / is_group / label / title / path on ``rows`` (dicts with code + description), in place."""
    by_code = {r["code"]: r for r in rows}
    codes = set(by_code)
    by7: dict[str, list[str]] = {}
    for c in codes:
        if len(c) == 8:
            by7.setdefault(c[:7], []).append(c)
    for r in rows:
        c = r["code"]
        # The tariff's own convention: xxxxxxx0 is the heading line of the xxxxxxx1..9 lines below it.
        r["is_group"] = int(
            len(c) in (5, 7) or (len(c) == 8 and c[7] == "0" and len(by7.get(c[:7], [])) > 1 and not r["is_service"])
        )
    for r in rows:
        c = r["code"]
        parent = None
        if len(c) == 8 and c[7] != "0":
            g = c[:7] + "0"
            if g in codes and by_code[g]["is_group"]:
                parent = g
        if parent is None:
            for n in range(len(c) - 1, 1, -1):
                if c[:n] in codes:
                    parent = c[:n]
                    break
        r["parent"] = parent or ""
    for r in sorted(rows, key=lambda x: (len(x["code"]), x["code"])):
        chain: list[dict] = []
        p = r["parent"]
        while p:
            chain.append(by_code[p])
            p = by_code[p]["parent"]
        chain.reverse()  # chapter ... parent
        r["label"] = clean_text(own_label(r["description"], [a["description"] for a in chain]))
        r["_chain"] = chain
    for r in sorted(rows, key=lambda x: (len(x["code"]), x["code"])):
        chain = r["_chain"]
        r["_frag"] = is_fragment(r["label"])
    for r in sorted(rows, key=lambda x: (len(x["code"]), x["code"])):
        chain = r["_chain"]
        label = sentence_case(r["label"])
        if r["_frag"]:
            parts: list[str] = []
            for a in reversed(chain):
                if len(a["code"]) < 4:
                    break
                lab = sentence_case(a["label"])
                if len(lab) > 40:
                    lab = re.split(r"[,;(]", lab)[0].strip() or lab[:40]
                parts.append(lab)
                if not a["_frag"] or len(parts) >= 3:
                    break
            title = f"{label} ({' > '.join(reversed(parts))})" if parts else label
        else:
            title = label
        names = [sentence_case(a["label"]) for a in chain if len(a["code"]) >= 4] + [label]
        path_parts: list[str] = []
        for n in names:
            if n and (not path_parts or path_parts[-1].lower() != n.lower()):
                path_parts.append(n)
        r["title"] = title[:200]
        r["path"] = " > ".join(path_parts)
        r["ancestors"] = " ".join(a["label"] for a in chain if len(a["code"]) >= 4)
        r["display_description"] = title if r["_frag"] else label


def build_database(excel_path: str, db_path: str):
    start_time = time.time()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    old: dict[str, tuple] = {}
    if os.path.exists(db_path):
        try:
            oc = sqlite3.connect(db_path)
            old = {r[0]: (r[1],) for r in oc.execute("SELECT code, description FROM hsn_codes")}
            oc.close()
        except sqlite3.Error:
            old = {}
        os.remove(db_path)

    print(f"Loading {excel_path}...")
    raw: list[tuple[str, str, int]] = []
    goods = read_sheet(excel_path, "HSN_MSTR")
    prefixes = leaf_prefixes(c for c, _ in goods if c)
    for raw_code, desc in goods[1:] if goods and not re.search(r"\d", goods[0][0]) else goods:
        code = repair_code(raw_code, prefixes)
        if code:
            raw.append((code, desc, 0))
    for raw_code, desc in read_sheet(excel_path, "SAC_MSTR"):
        code = normalize_code(raw_code)
        if code:
            raw.append((code, desc, 1))

    chapters = {c for c, _, s in raw if len(c) == 2 and not s}
    rows: list[dict] = []
    seen: set[str] = set()
    for code, desc, is_service in raw:
        problem = validate_row(code, code[:2] if not is_service else "99", bool(is_service), chapters)
        if problem and not (is_service and code.startswith("99")):
            print(f"skipping HSN row {code!r}: {problem}")
            continue
        cleaned = clean_text(desc.split("~")[-1] if "~" in desc else desc)
        if code in seen:  # the source lists a few codes twice: keep the first, longer-context wording is rebuilt anyway
            continue
        seen.add(code)
        rows.append({"code": code, "description": cleaned, "is_service": is_service, "src": desc})

    counts = build_word_counts(r["description"] for r in rows)
    glue: dict[str, str] = {}
    for r in rows:
        r["description"] = unglue(r["description"], counts, glue)
        r["src_desc"] = clean_text(r["src"])
    build_hierarchy(rows)
    for r in rows:
        r["label"] = unglue(r["label"], counts, glue)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE hsn_codes (
        code TEXT PRIMARY KEY,
        description TEXT NOT NULL,
        level INTEGER NOT NULL,
        chapter TEXT NOT NULL,
        heading TEXT NOT NULL,
        subheading TEXT NOT NULL,
        category TEXT NOT NULL,
        is_service INTEGER NOT NULL DEFAULT 0,
        aliases TEXT NOT NULL DEFAULT '',
        is_group INTEGER NOT NULL DEFAULT 0,
        parent TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        path TEXT NOT NULL DEFAULT ''
    )
    """)
    # Own FTS content (not external): porter stems so onion/onions, nut/nuts meet; groups are never indexed.
    cur.execute("""
    CREATE VIRTUAL TABLE hsn_fts USING fts5(
        code, label, aliases, ancestors, compounds, tokenize='porter unicode61'
    )
    """)
    for r in rows:
        code = r["code"]
        level = len(code)
        category = "Services" if r["is_service"] else map_chapter_to_category(code[:2])
        aliases = "Services GST SAC" if r["is_service"] else get_aliases_for_code(code)
        heading = code[:4] if level >= 4 else code
        subheading = code[:6] if level >= 6 else code
        cur.execute(
            "INSERT INTO hsn_codes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, r["display_description"], level, "99" if r["is_service"] else code[:2], heading, subheading,
             category, r["is_service"], aliases, r["is_group"], r["parent"], r["label"], r["title"], r["path"]),
        )
        r["rowid"] = cur.lastrowid
        if not r["is_group"] and level >= 4:
            cur.execute(
                "INSERT INTO hsn_fts(rowid, code, label, aliases, ancestors, compounds) VALUES (?,?,?,?,?,?)",
                (r["rowid"], code, r["label"], aliases, r["ancestors"],
                 (_compounds([r["src_desc"], r["label"]] + [a["label"] for a in r["_chain"]]) + " "
                  + " ".join(w.lower() for w in _tokens(r["src_desc"]) if w in glue)).strip()),
            )
    cur.execute("CREATE INDEX idx_hsn_chapter ON hsn_codes(chapter, level)")
    cur.execute("CREATE INDEX idx_hsn_heading ON hsn_codes(heading, level)")
    cur.execute("CREATE INDEX idx_hsn_subheading ON hsn_codes(subheading, level)")
    cur.execute("CREATE INDEX idx_hsn_category ON hsn_codes(category)")
    cur.execute("CREATE INDEX idx_hsn_level ON hsn_codes(level)")
    cur.execute("CREATE INDEX idx_hsn_parent ON hsn_codes(parent)")
    conn.commit()
    conn.close()

    # ---- diff report -------------------------------------------------------------------------------------
    new_desc = {r["code"]: r["display_description"] for r in rows}
    changed = [c for c in new_desc if c in old and old[c][0] != new_desc[c]]
    groups = [r["code"] for r in rows if r["is_group"]]
    frags = [r for r in rows if r["_frag"]]
    print(f"Successfully built {db_path} with {len(rows)} codes in {time.time() - start_time:.2f}s")
    print(f"DIFF: codes added {len(set(new_desc) - set(old))}, removed {len(set(old) - set(new_desc))}, "
          f"description changed {len(changed)}")
    print(f"DIFF: group rows flagged (never selectable): {len(groups)} e.g. {groups[:6]}")
    print(f"DIFF: fragment labels given hierarchical titles: {len(frags)}; sample: "
          + "; ".join(f"{r['code']} -> {r['title'][:70]}" for r in frags[:3]))
    print(f"DIFF: glued words split: {len(glue)}: " + ", ".join(f"{k}->{v}" for k, v in list(glue.items())[:25]))
    for c in changed[:8]:
        print(f"  changed {c}: {old[c][0][:60]!r} -> {new_desc[c][:60]!r}")
    dup = Counter((r["parent"], r["label"].lower()) for r in rows if len(r["code"]) == 8 and not r["is_group"])
    print(f"DIFF: sibling groups with identical label (disambiguated by path/parent): {sum(1 for v in dup.values() if v > 1)}")


if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    excel = os.path.join(os.path.dirname(base_dir), "HSN_SAC.xlsx")
    target_db = os.path.join(base_dir, "data", "hsn_master.db")
    build_database(excel, target_db)
