"""Plain-text card list → resolved printings (2026-08-04).

People send collections as free text — a Cardmarket message, a Discord dump, a
spreadsheet paste:

    *Majestic Foil*
    Mark of the Beast (Extended Art Rainbow Foil)
    Nourishing Emptiness (Rainbow Foil)
    3x Tome of Torment (Regular)

This turns that into priced lines. It is deliberately **deterministic** — card
names are matched against the catalogue, not guessed — because the hard part
isn't reading English, it's that the catalogue holds printings that share every
visible attribute and differ 10× in price (MON034 1st-Ed Rainbow Foil exists at
7 kr and at 873 kr). Those lines are returned as `ambiguous` with every
candidate so a human picks; nothing silently resolves to the wrong money.

Statuses per entry:
  matched     — exactly one printing fits
  auto_picked — several fit but they're priced within a hair of each other
  ambiguous   — several fit and the price genuinely differs → needs a human
  unmatched   — no name match (near-misses are offered as suggestions)
"""

import difflib
import re
import unicodedata
from typing import Optional

# ── Treatment vocabulary ─────────────────────────────────────────────────────
# Order matters: the specific foils must win before the bare word "foil".
_FOILING_PATTERNS: list[tuple[str, str]] = [
    (r"cold\s*-?\s*foil|\bcf\b", "C"),
    (r"rainbow\s*-?\s*foil|\brf\b", "R"),
    (r"gold\s*-?\s*foil|\bgf\b", "G"),
    (r"regular|non\s*-?\s*foil|nonfoil|normal|unfoiled|\bnf\b", "S"),
    (r"\bfoil(s)?\b", "R"),        # bare "foil" — assumed rainbow, flagged
]

# Alt-art treatments live in gold.variant.
_VARIANT_PATTERNS: list[tuple[str, str]] = [
    (r"extended\s*art|\bea\b", "Extended Art"),
    (r"\bmarvel\b", "Marvel"),
    (r"\bgolden\b", "Golden"),
    (r"alternate\s*art|\balt\s*art\b", "Alternate Art"),
    (r"\bjapanese\b|\bjpn?\b", "Japanese"),
    (r"\bchinese\b", "Chinese"),
    (r"\bcc\s*tag\b", "CC Tag"),
]

_PITCH_WORDS = {"red": "1", "yellow": "2", "blue": "3"}

# Words that can only be a heading, never part of a card name we care about.
_HEADING_WORDS = {
    "majestic", "majestics", "rare", "rares", "common", "commons", "legendary",
    "legendaries", "fabled", "token", "tokens", "promo", "promos", "marvel",
    "marvels", "super", "basic", "basics", "equipment", "hero", "heroes",
    "weapon", "weapons", "card", "cards", "list", "collection",
    "regular", "regulars", "foil", "foils", "rainbow", "cold", "gold", "nonfoil",
    "non", "normal", "extended", "art", "first", "1st", "edition", "unlimited",
    "alpha", "alfa", "and", "the", "of", "in", "x",
}

_EDITION_HINTS = [
    (r"first\s*edition|\b1st\s*edition\b|\balpha\b|\balfa\b", "F"),
    (r"\bunlimited\b", "U"),
]

_COLLECTOR_RE = re.compile(r"\b([A-Z]{2,4}\d{3,4}[A-Za-z]?)\b")
_QTY_PREFIX_RE = re.compile(r"^(\d{1,3})\s*[xX]?[\s.:)-]+")
_QTY_SUFFIX_RE = re.compile(r"[\s(]+[xX×]\s*(\d{1,3})\)?$")
_TRAILING_GROUP_RE = re.compile(r"\s*[\(\[]([^()\[\]]*)[\)\]]\s*$")
_BULLET_RE = re.compile(r"^[\s\-–—•*·>»#]+")


def _norm(text: str) -> str:
    """Fold to a comparable key: no case, accents, or punctuation."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", stripped.lower())


def _read_treatment(text: str) -> tuple[Optional[str], Optional[str], bool]:
    """(foiling, variant_hint, assumed) from a treatment phrase."""
    low = text.lower()
    foiling = None
    assumed = False
    for pattern, code in _FOILING_PATTERNS:
        if re.search(pattern, low):
            foiling = code
            assumed = code == "R" and not re.search(r"rainbow|\brf\b", low)
            break
    variant = None
    for pattern, name in _VARIANT_PATTERNS:
        if re.search(pattern, low):
            variant = name
            break
    return foiling, variant, assumed


def _is_heading(line: str, was_marked: bool) -> bool:
    """Headings are wrapped in */#, end with ':', or are made only of
    rarity/treatment words. The word rule is capped at 5 words so a real card
    name can't be swallowed; every skipped line is reported back anyway."""
    if was_marked or line.rstrip().endswith(":"):
        return True
    words = [w for w in re.split(r"[\s/,&]+", line.lower()) if w]
    if not words or len(words) > 5:
        return False
    return all(re.sub(r"[^a-z0-9]", "", w) in _HEADING_WORDS for w in words)


def looks_like_heading(name: str, set_name: Optional[str]) -> bool:
    """Post-match rescue: a line that matched no card AND names an edition or the
    set is a title, not a missing card. Only applied to lines that already
    failed to match anything, so a real card name can't be swallowed."""
    low = (name or "").lower()
    if any(re.search(pattern, low) for pattern, _ in _EDITION_HINTS):
        return True
    return bool(set_name) and _norm(set_name) in _norm(name)


def looks_like_noise(name: str) -> bool:
    """Prose that came along with the list — a greeting, a signature, an email.
    Only ever consulted for lines that already matched no card at all."""
    text = (name or "").strip()
    if not text:
        return True
    low = text.lower()
    if "@" in text or "http" in low:
        return True
    if len(text.split()) >= 7:
        return True
    if re.search(r"\d", text) and re.search(r"\b(kr|sek|eur|usd|euro|dollar)\b|[€$£]", low):
        return True
    digits = sum(ch.isdigit() for ch in text)
    letters = sum(ch.isalpha() for ch in text)
    return digits > letters


def parse_text(text: str) -> dict:
    """Free text → entries + the set/edition it looks like, without touching the DB."""
    entries: list[dict] = []
    skipped: list[str] = []
    section: Optional[str] = None
    section_foiling: Optional[str] = None
    section_variant: Optional[str] = None
    edition_hint: Optional[str] = None

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        marked = bool(re.match(r"^\s*[*#]{1,2}\s*\S", line)) and bool(re.search(r"[*#]\s*$", line))
        line = _BULLET_RE.sub("", line).strip().strip("*#").strip()
        if not line:
            continue

        for pattern, code in _EDITION_HINTS:
            if re.search(pattern, line.lower()):
                edition_hint = edition_hint or code

        if _is_heading(line, marked):
            skipped.append(raw_line.strip())
            section = line.strip(": ")
            section_foiling, section_variant, _ = _read_treatment(line)
            continue

        entry = _parse_card_line(line, section, section_foiling, section_variant)
        if entry:
            entries.append(entry)
        else:
            skipped.append(raw_line.strip())

    # Fold repeated lines ("Tome of Torment" three times in a row → qty 3).
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for e in entries:
        key = (_norm(e["name"]), e["foiling"], e["variant_hint"], e["pitch"], e["display_id"])
        if key in merged:
            merged[key]["qty"] += e["qty"]
            merged[key]["raw"] += f"\n{e['raw']}"
        else:
            merged[key] = e
            order.append(key)

    return {
        "entries": [merged[k] for k in order],
        "skipped_lines": skipped,
        "edition_hint": edition_hint,
        "set_text": text or "",
    }


def _parse_card_line(line: str, section, section_foiling, section_variant) -> Optional[dict]:
    """One card line → name + qty + treatment. None when nothing usable is left."""
    notes: list[str] = []
    qty = 1

    m = _QTY_SUFFIX_RE.search(line)
    if m:
        qty = max(1, min(999, int(m.group(1))))
        line = line[:m.start()].strip()
    m = _QTY_PREFIX_RE.match(line)
    if m:
        qty = max(1, min(999, int(m.group(1))))
        line = line[m.end():].strip()

    foiling = variant = pitch = display_id = None
    assumed_foil = False

    # Peel trailing "(...)" / "[...]" groups: treatment, pitch colour, set code.
    # Trailing punctuation first — "Dread Scythe (Regular)," must still peel.
    while True:
        line = line.rstrip(" ,;.")
        m = _TRAILING_GROUP_RE.search(line)
        if not m:
            break
        token = m.group(1).strip()
        line = line[:m.start()].strip()
        if not token:
            continue
        low = token.lower()
        if low in _PITCH_WORDS:
            pitch = _PITCH_WORDS[low]
            continue
        code = _COLLECTOR_RE.search(token.upper())
        if code and _norm(token) == _norm(code.group(1)):
            display_id = code.group(1).upper()
            continue
        f, v, assumed = _read_treatment(token)
        if f and not foiling:
            foiling, assumed_foil = f, assumed
        if v and not variant:
            variant = v
        if not f and not v:
            notes.append(f"Ignored “{token}”")

    # "Name - MON004" / "Name MON004"
    m = re.search(r"[\s\-–—]+([A-Z]{2,4}\d{3,4}[A-Za-z]?)\s*$", line.upper())
    if m and not display_id:
        display_id = m.group(1)
        line = line[:m.start()].strip()

    name = line.strip(" -–—:·.,").strip()
    if not name or not re.search(r"[A-Za-z]", name):
        return None

    if foiling is None:
        foiling = section_foiling
    if variant is None:
        variant = section_variant
    if assumed_foil:
        notes.append("“Foil” read as Rainbow Foil")

    return {
        "raw": line if not notes else line,
        "name": name,
        "qty": qty,
        "foiling": foiling,
        "variant_hint": variant,
        "pitch": pitch,
        "display_id": display_id,
        "section": section,
        "notes": notes,
    }


# ── Matching against the catalogue ───────────────────────────────────────────

_CATALOGUE_SQL = """
    SELECT g.printing_unique_id, g.name, g.display_id, g.set_id,
           coalesce(s.set_name, g.set_id) AS set_name,
           g.edition, g.foiling, g.rarity, g.variant, g.pitch, g.image_url,
           g.trade_value_sek, g.price_sek
    FROM gold.gold_cards g
    LEFT JOIN bronze.fab_sets s ON s.set_id = g.set_id AND s.edition = g.edition
    {where}
"""

# Two printings priced this close make the choice immaterial — pick for the user.
_AUTO_PICK_RATIO = 1.25
_AUTO_PICK_ABS_SEK = 20


def _row(card: dict) -> dict:
    value = card.get("trade_value_sek")
    if value is None:
        value = card.get("price_sek")
    return {
        "printing_unique_id": card["printing_unique_id"],
        "name": card["name"],
        "display_id": card["display_id"],
        "set_id": card["set_id"],
        "set_name": card["set_name"],
        "edition": card["edition"],
        "foiling": card["foiling"],
        "rarity": card["rarity"],
        "variant": card["variant"],
        "pitch": card["pitch"],
        "image_url": card["image_url"],
        "value_sek": int(round(float(value))) if value is not None else None,
    }


def detect_set(cur, set_text: str) -> Optional[dict]:
    """Find the set a heading like 'Flesh and Blood Monarch First Edition' names.
    Matched line by line — normalising the whole blob at once would let a set
    name appear by accident across a line break."""
    if not set_text.strip():
        return None
    # bronze.fab_sets still carries the composite-product rows ("MON105 // MON")
    # that dbt keeps out of gold — only trust set codes that exist in gold and
    # have no delimiter in them.
    cur.execute(
        """
        SELECT DISTINCT s.set_id, s.set_name
        FROM bronze.fab_sets s
        WHERE s.set_name IS NOT NULL
          AND s.set_id !~ '[^A-Za-z0-9]'
          AND EXISTS (SELECT 1 FROM gold.gold_cards g WHERE g.set_id = s.set_id)
        """
    )
    sets = [dict(r) for r in cur.fetchall()]
    lines = [_norm(l) for l in set_text.splitlines() if l.strip()]
    best = None
    for row in sets:
        needle = _norm(row["set_name"])
        if not needle or len(needle) < 4:
            continue
        if any(needle in line for line in lines):
            # Longest set name wins ("Tales of Aria" over a shorter accidental
            # hit); shortest code breaks a tie between duplicates.
            if (best is None
                    or len(needle) > len(_norm(best["set_name"]))
                    or (len(needle) == len(_norm(best["set_name"])) and len(row["set_id"]) < len(best["set_id"]))):
                best = row
    if best:
        return {"set_id": best["set_id"], "set_name": best["set_name"]}
    # Fall back to a bare set code ("MON") — only in the opening lines, where a
    # heading lives; scanning the whole paste would hit random capitalised words.
    head = "\n".join(set_text.splitlines()[:5])
    for token in re.findall(r"\b([A-Z]{2,4})\b", head.upper()):
        cur.execute(
            "SELECT s.set_id, min(s.set_name) AS set_name FROM bronze.fab_sets s "
            "WHERE s.set_id = %s AND EXISTS (SELECT 1 FROM gold.gold_cards g WHERE g.set_id = s.set_id) "
            "GROUP BY s.set_id",
            [token],
        )
        row = cur.fetchone()
        if row:
            return {"set_id": row["set_id"], "set_name": row["set_name"]}
    return None


def _index(catalogue: list[dict]) -> tuple[dict[str, list[dict]], list[str]]:
    by_name: dict[str, list[dict]] = {}
    for card in catalogue:
        by_name.setdefault(_norm(card["name"]), []).append(card)
    return by_name, list(by_name.keys())


def match_entries(cur, entries: list[dict], set_id: Optional[str], edition: Optional[str]) -> list[dict]:
    """Resolve every entry against the catalogue. One query, then in-memory
    matching — a 200-line paste would otherwise be 200 round trips."""
    where, params = [], []
    if set_id:
        where.append("g.set_id = %s")
        params.append(set_id.upper())
    if edition:
        where.append("g.edition = %s")
        params.append(edition.upper())
    sql = _CATALOGUE_SQL.format(where=("WHERE " + " AND ".join(where)) if where else "")
    cur.execute(sql, params)
    by_name, known_names = _index([dict(r) for r in cur.fetchall()])

    results = [_match_one(entry, by_name, known_names) for entry in entries]

    # People paste mixed collections. A name that isn't in the chosen set is
    # usually a card from another set, not a typo — look it up everywhere rather
    # than reporting it missing.
    misses = [r for r in results if r["status"] == "unmatched"]
    if misses and (set_id or edition):
        keys = sorted({_norm(r["name"]) for r in misses})
        cur.execute(
            _CATALOGUE_SQL.format(
                where="WHERE lower(regexp_replace(g.name, '[^a-zA-Z0-9]', '', 'g')) = ANY(%s)"),
            [keys],
        )
        wide_by_name, wide_names = _index([dict(r) for r in cur.fetchall()])
        if wide_by_name:
            for r in misses:
                again = _match_one(r, wide_by_name, wide_names)
                if again["status"] == "unmatched":
                    continue
                again["reasons"].insert(0, f"Not in {set_id or 'the chosen set'} — showing every set")
                r.clear()
                r.update(again)
    return results


def _match_one(entry: dict, by_name: dict[str, list[dict]], known_names: list[str]) -> dict:
    out = {**entry, "status": "unmatched", "chosen": None, "candidates": [], "reasons": list(entry["notes"])}
    key = _norm(entry["name"])
    pool = by_name.get(key, [])

    if not pool:
        close = difflib.get_close_matches(key, known_names, n=3, cutoff=0.82)
        suggestions = [c for name in close for c in by_name[name]]
        out["candidates"] = [_row(c) for c in suggestions[:12]]
        out["reasons"].append(
            f"No card named “{entry['name']}”" + (" — did you mean one of these?" if suggestions else "")
        )
        return out

    # Narrow by everything the line told us.
    narrowed = pool
    if entry["display_id"]:
        exact = [c for c in narrowed if (c["display_id"] or "").upper() == entry["display_id"]]
        narrowed = exact or narrowed
    if entry["pitch"]:
        exact = [c for c in narrowed if (c["pitch"] or "") == entry["pitch"]]
        narrowed = exact or narrowed
    if entry["foiling"]:
        exact = [c for c in narrowed if (c["foiling"] or "") == entry["foiling"]]
        if exact:
            narrowed = exact
        else:
            out["reasons"].append(
                f"No {entry['foiling']} printing here — showing the other finishes")
    if entry["variant_hint"]:
        hint = entry["variant_hint"].lower()
        exact = [c for c in narrowed if hint in (c["variant"] or "").lower()]
        if exact:
            narrowed = exact
        else:
            out["reasons"].append(
                f"“{entry['variant_hint']}” isn't recorded as a separate printing here — "
                "compare the artwork; the alt-art version is normally the pricier one")
    elif len(narrowed) > 1:
        plain = [c for c in narrowed if not c["variant"]]
        if plain:
            narrowed = plain

    out["candidates"] = [_row(c) for c in sorted(
        narrowed, key=lambda c: -(float(c["trade_value_sek"] or c["price_sek"] or 0)))]

    if len(narrowed) == 1:
        out["status"] = "matched"
        out["chosen"] = out["candidates"][0]
        return out

    values = [v["value_sek"] for v in out["candidates"] if v["value_sek"] is not None]
    if values:
        lo, hi = min(values), max(values)
        # Same money either way → don't make a human click. Otherwise they must.
        if hi <= lo * _AUTO_PICK_RATIO or hi - lo <= _AUTO_PICK_ABS_SEK:
            out["status"] = "auto_picked"
            out["chosen"] = next(c for c in out["candidates"] if c["value_sek"] == lo)
            out["reasons"].append(f"{len(out['candidates'])} near-identical printings — took the {lo} kr one")
            return out
        out["status"] = "ambiguous"
        out["reasons"].append(
            f"{len(out['candidates'])} printings look the same but run {lo}–{hi} kr — pick the right one")
        return out

    out["status"] = "ambiguous"
    out["reasons"].append("Several printings match and none of them is priced")
    return out
