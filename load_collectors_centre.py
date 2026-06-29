"""
Parse FabTcg_manual_scrape.md and load into bronze.collectors_centre.

Each section in the file starts with a fabtcg.com URL, followed by scraped page
content. Card rows are tab-separated: card_id, card_name, printing_technique, notes.
Cards with multiple printing techniques have continuation lines starting with the
technique (tab-separated, no card ID). Note continuations are plain text lines
with no tab.

Run after setup_db.py to populate bronze.collectors_centre.
"""

import re
import os
import sys
import psycopg2
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(dotenv_path=HERE / ".env")

PG_HOST     = os.getenv("PG_HOST", "localhost")
PG_PORT     = int(os.getenv("PG_PORT", 5432))
PG_USER     = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

SCRAPE_FILE = str(HERE / "FabTcg_manual_scrape.md")

GREEN = "\033[32m"
CYAN  = "\033[36m"
RED   = "\033[31m"
BOLD  = "\033[1m"
RESET = "\033[0m"

def ok(msg):   print(f"  {GREEN}OK{RESET}  {msg}")
def info(msg): print(f"  {CYAN}->{RESET} {msg}")
def err(msg):  print(f"  {RED}ERR{RESET} {msg}")


# Card ID: 2-10 uppercase alphanumeric chars followed by exactly 3 digits,
# then an optional rarity/type suffix like -P, -C, -R, -T, -F, -M, -L, -S
CARD_ID_RE = re.compile(r'^([A-Z0-9]{2,10}\d{3}(?:-[A-Z]+)?)\t')

# First-field prefixes that indicate a header row (not card data)
HEADER_PREFIXES = ("Code\t", "Set Code\t", "Set / Code\t", "Set / Code / Rarity\t")

# Valid printing techniques contain one of these substrings
VALID_TECH_RE = re.compile(r'Normal|Foil|Regular|Extended Art|Standard|Gold', re.IGNORECASE)


def strip_rarity_suffix(raw_id: str) -> str:
    """'WTR005-C' → 'WTR005', 'IRA001-P' → 'IRA001', 'WTR000-F' → 'WTR000'."""
    return re.sub(r'-[A-Z]+$', '', raw_id)


def extract_set_code(base_id: str) -> str:
    """'WTR005' → 'WTR', '1HP001' → '1HP', 'WTR000' → 'WTR'."""
    return re.sub(r'\d{3}$', '', base_id)


def parse_scrape(filepath: str) -> list[tuple]:
    """
    Returns list of (set_code, card_id, card_name, printing_technique, notes).
    """
    rows = []

    # Current pending row
    cur_set_code = None
    cur_card_id  = None
    cur_name     = None
    cur_tech     = None
    cur_notes    = None

    def flush():
        if cur_card_id and cur_tech:
            rows.append((
                cur_set_code,
                cur_card_id,
                cur_name or "",
                cur_tech,
                cur_notes or None,
            ))

    with open(filepath, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\r\n")

            # ── New section: URL line ────────────────────────────────────────
            if line.startswith("https://fabtcg.com/"):
                flush()
                cur_set_code = None
                cur_card_id  = None
                cur_name     = None
                cur_tech     = None
                cur_notes    = None
                continue

            # ── Skip header rows (all variants) ──────────────────────────────
            if line.startswith(HEADER_PREFIXES):
                continue

            # ── New card row ─────────────────────────────────────────────────
            m = CARD_ID_RE.match(line)
            if m:
                flush()
                all_parts = line.split("\t")
                raw_id = all_parts[0]
                base_id = strip_rarity_suffix(raw_id)
                set_code = extract_set_code(base_id)

                cur_set_code = set_code
                cur_card_id  = base_id
                cur_name     = all_parts[1].strip() if len(all_parts) > 1 else ""

                # Detect multi-language rows: 7+ fields where technique is near end
                # Format: id, name_en, name_de, name_it, name_sp, name_fr, technique, [language]
                if len(all_parts) >= 7 and not VALID_TECH_RE.search(all_parts[2]):
                    # Technique is second-to-last or last field; find first valid one
                    tech_field = next(
                        (p.strip() for p in reversed(all_parts[2:]) if VALID_TECH_RE.search(p)),
                        None
                    )
                    cur_tech  = tech_field
                    cur_notes = None
                else:
                    cur_tech  = all_parts[2].strip() if len(all_parts) > 2 else None
                    raw_notes = all_parts[3].strip() if len(all_parts) > 3 else ""
                    cur_notes = raw_notes if raw_notes else None

                # Skip rows where technique is still not a valid printing technique
                if cur_tech and not VALID_TECH_RE.search(cur_tech):
                    cur_card_id = None  # will not be flushed
                continue

            # ── Continuation: new printing technique (has tab, no card ID) ───
            if "\t" in line and cur_card_id:
                parts = line.split("\t", 1)
                tech = parts[0].strip()
                if not tech or not VALID_TECH_RE.search(tech):
                    continue  # blank or non-technique continuation, skip
                flush()
                cur_tech  = tech
                raw_notes = parts[1].strip() if len(parts) > 1 else ""
                cur_notes = raw_notes if raw_notes else None
                continue

            # ── Continuation: note text (no tab, no card ID) ─────────────────
            stripped = line.strip()
            if stripped and cur_card_id and "\t" not in line:
                if cur_notes:
                    cur_notes += "\n" + stripped
                else:
                    cur_notes = stripped

    # Flush last pending row
    flush()
    return rows


def load(rows: list[tuple]) -> None:
    info(f"Connecting to PostgreSQL (fab)...")
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            database="fab",
            user=PG_USER, password=PG_PASSWORD,
        )
    except Exception as e:
        err(f"Connection failed: {e}")
        sys.exit(1)

    cur = conn.cursor()

    # Truncate and reload for idempotency
    cur.execute("TRUNCATE bronze.collectors_centre")

    insert_sql = """
        INSERT INTO bronze.collectors_centre
            (set_code, card_id, card_name, printing_technique, notes)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (set_code, card_id, printing_technique) DO UPDATE
            SET card_name = EXCLUDED.card_name,
                notes     = EXCLUDED.notes
    """
    cur.executemany(insert_sql, rows)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM bronze.collectors_centre")
    count = cur.fetchone()[0]
    ok(f"bronze.collectors_centre: {count:,} rows loaded")

    cur.close()
    conn.close()


def main():
    print()
    print(CYAN + BOLD + "  FaB Collectors Centre - Load" + RESET)
    print(CYAN + "-" * 50 + RESET)
    print()

    info(f"Parsing {SCRAPE_FILE}...")
    rows = parse_scrape(SCRAPE_FILE)
    ok(f"Parsed {len(rows):,} (set_code, card_id, technique) rows")

    # Quick sanity check: show distinct set codes found
    set_codes = sorted({r[0] for r in rows})
    info(f"Set codes found: {', '.join(set_codes)}")

    load(rows)

    print()
    print(GREEN + BOLD + "  Done." + RESET)
    print()


if __name__ == "__main__":
    main()
