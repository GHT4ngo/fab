"""
Flesh and Blood — Bronze Layer Ingestion
=========================================
Sources:
  1. The Fab Cube (GitHub)   — card printings, one row per edition/foiling
  2. The Fab Cube (GitHub)   — set catalogue with collectors-centre URLs
  3. Cardmarket game 16      — product catalogue (name→idProduct) + EUR prices
  4. Riksbank                — EUR/SEK and USD/SEK exchange rates
  5. Manual CSV              — printing_unique_id → idProduct crosswalk (data/fab_cm_manual.csv)
  6. Auto-match              — derive set_id+edition → idExpansion for unmapped sets

USD prices and sets the-fab-cube hasn't caught up with come from ingest_tcgcsv.py;
foil techniques per printing come from load_collectors_centre.py.

Run order:
  python setup_db.py        (first time only, or after schema changes)
  python ingest_bronze.py   (daily)
  python ingest_tcgcsv.py   (daily, then 'dbt run')

This script only lands raw data. All price matching (anchored / auto / fallback /
tcgcsv / manual tiers) happens in fab_dbt/models/silver/silver_cards.sql.
"""

import requests
import psycopg2
import psycopg2.extras
import json
import os
import sys
import csv
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(dotenv_path=HERE / ".env")

# ─────────────────────────────────────────────────────────────────────────────
#  Terminal UI helpers
# ─────────────────────────────────────────────────────────────────────────────

GREEN  = "\033[32m"
CYAN   = "\033[36m"
YELLOW = "\033[33m"
RED    = "\033[31m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

BANNER = r"""
 _______  __       _______
|   ____||  |     |   ____|
|  |__   |  |     |  |__
|   __|  |  |     |   __|
|  |     |  `---. |  |____
|__|     |______| |_______|
  Flesh and Blood Pipeline  [ Bronze Layer ]
"""

def print_banner():    print(GREEN + BOLD + BANNER + RESET)
def print_section(t):  print(f"\n{CYAN}{'─'*60}\n  {BOLD}{t}{RESET}\n{CYAN}{'─'*60}{RESET}")
def ok(msg):           print(f"  {GREEN}✔{RESET}  {msg}")
def info(msg):         print(f"  {CYAN}→{RESET}  {msg}")
def warn(msg):         print(f"  {YELLOW}⚠{RESET}  {msg}")
def err(msg):          print(f"  {RED}✘{RESET}  {msg}")

def progress(current, total, width=40, label=""):
    pct    = current / total if total > 0 else 0
    filled = int(width * pct)
    bar    = GREEN + "█" * filled + DIM + "░" * (width - filled) + RESET
    sys.stdout.write(f"\r  [{bar}] {GREEN}{current:,}{RESET}/{total:,}  {DIM}{label}{RESET}  ")
    sys.stdout.flush()
    if current >= total:
        print()


# ─────────────────────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────────────────────

DB_CONFIG = {
    "host":     os.getenv("PG_HOST", "localhost"),
    "port":     int(os.getenv("PG_PORT", 5432)),
    "database": "fab",
    "user":     os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}

DATA_DIR = str(HERE / "tmp" / "data")
os.makedirs(DATA_DIR, exist_ok=True)

# The Fab Cube — one row per card printing (set × edition × foiling)
FAB_CUBE_URL   = (
    "https://raw.githubusercontent.com/the-fab-cube/"
    "flesh-and-blood-cards/develop/json/english/card-flattened.json"
)
FAB_CUBE_LOCAL = os.path.join(DATA_DIR, "card-flattened.json")

# The Fab Cube — set catalogue with collectors-centre URLs
FAB_SETS_URL   = (
    "https://raw.githubusercontent.com/the-fab-cube/"
    "flesh-and-blood-cards/develop/json/english/set.json"
)
FAB_SETS_LOCAL = os.path.join(DATA_DIR, "set.json")

# Cardmarket game 16 = Flesh and Blood
CM_PRODUCTS_URL   = "https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_16.json"
CM_PRODUCTS_LOCAL = os.path.join(DATA_DIR, "products_singles_16.json")

CM_PRICES_URL     = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_16.json"
CM_PRICES_LOCAL   = os.path.join(DATA_DIR, "price_guide_16.json")

# Manual printing → Cardmarket product crosswalk (tier-5 last resort)
MANUAL_CSV = str(HERE / "data" / "fab_cm_manual.csv")

# Riksbank exchange rate pairs
RIKSBANK_PAIRS = [
    ("SEKEURPMI", "SEK", "EUR/SEK"),
    ("SEKUSDPMI", "SEK", "USD/SEK"),
]

# Auto-matching: minimum name overlap fraction to accept a set→idExpansion match
AUTO_MATCH_THRESHOLD = 0.10


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_remote_newer(url, local_file):
    """True if remote Last-Modified is newer than the cached stamp."""
    stamp_file = local_file + ".lastmod"
    try:
        r = requests.head(url, timeout=15)
        remote_lm = r.headers.get("Last-Modified")
        if remote_lm and os.path.exists(stamp_file):
            with open(stamp_file) as f:
                if f.read().strip() == remote_lm:
                    return False
        if remote_lm:
            with open(stamp_file, "w") as f:
                f.write(remote_lm)
    except Exception:
        pass
    return True


def download(url, local_file, label):
    frames = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    total  = int(r.headers.get("content-length", 0))
    done   = 0
    fi     = 0
    with open(local_file, "wb") as f:
        for chunk in r.iter_content(chunk_size=512 * 1024):
            f.write(chunk)
            done += len(chunk)
            mb    = done / 1024 / 1024
            tmb   = total / 1024 / 1024 if total else 0
            spin  = frames[fi % len(frames)]
            if total:
                sys.stdout.write(f"\r  {GREEN}{spin}{RESET}  {label}: {mb:.1f}/{tmb:.1f} MB  ")
            else:
                sys.stdout.write(f"\r  {GREEN}{spin}{RESET}  {label}: {mb:.1f} MB  ")
            sys.stdout.flush()
            fi += 1
    print(f"\r  {GREEN}✔{RESET}  {label}: {done/1024/1024:.1f} MB" + " " * 20)
    return done


# ─────────────────────────────────────────────────────────────────────────────
#  1 / 7 — The Fab Cube card printings
# ─────────────────────────────────────────────────────────────────────────────

def ingest_fab_cards(conn):
    print_section("1 / 7 — The Fab Cube: card printings")

    if is_remote_newer(FAB_CUBE_URL, FAB_CUBE_LOCAL) or not os.path.exists(FAB_CUBE_LOCAL):
        info("Downloading card-flattened.json from GitHub...")
        download(FAB_CUBE_URL, FAB_CUBE_LOCAL, "card-flattened.json")
    else:
        ok("Cached file is up to date, skipping download")

    info("Parsing JSON...")
    with open(FAB_CUBE_LOCAL, encoding="utf-8") as f:
        cards = json.load(f)

    total = len(cards)
    ok(f"Parsed {total:,} printings")

    cur = conn.cursor()
    now = datetime.now()

    cur.execute("TRUNCATE TABLE bronze.fab_printings")
    conn.commit()
    warn("Truncated bronze.fab_printings for full refresh")

    insert_sql = """
        INSERT INTO bronze.fab_printings
            (printing_unique_id, id, set_id, edition, foiling, rarity, raw_data, loaded_at)
        VALUES %s
        ON CONFLICT (printing_unique_id) DO UPDATE
            SET id        = EXCLUDED.id,
                set_id    = EXCLUDED.set_id,
                edition   = EXCLUDED.edition,
                foiling   = EXCLUDED.foiling,
                rarity    = EXCLUDED.rarity,
                raw_data  = EXCLUDED.raw_data,
                loaded_at = EXCLUDED.loaded_at
    """

    batch      = []
    batch_size = 500
    inserted   = 0

    for card in cards:
        pid = card.get("printing_unique_id") or card.get("unique_id")
        if not pid:
            continue
        batch.append((
            pid,
            card.get("id"),
            card.get("set_id"),
            card.get("edition"),
            card.get("foiling"),
            card.get("rarity"),
            json.dumps(card, ensure_ascii=False),
            now,
        ))
        if len(batch) >= batch_size:
            psycopg2.extras.execute_values(cur, insert_sql, batch)
            conn.commit()
            inserted += len(batch)
            batch = []
            progress(inserted, total, label="printings inserted")

    if batch:
        psycopg2.extras.execute_values(cur, insert_sql, batch)
        conn.commit()
        inserted += len(batch)

    progress(inserted, total, label="printings inserted")
    ok(f"Loaded {inserted:,} card printings")
    cur.close()


# ─────────────────────────────────────────────────────────────────────────────
#  2 / 7 — The Fab Cube set catalogue
# ─────────────────────────────────────────────────────────────────────────────

def ingest_fab_sets(conn):
    print_section("2 / 7 — The Fab Cube: set catalogue")

    if is_remote_newer(FAB_SETS_URL, FAB_SETS_LOCAL) or not os.path.exists(FAB_SETS_LOCAL):
        info("Downloading set.json from GitHub...")
        download(FAB_SETS_URL, FAB_SETS_LOCAL, "set.json")
    else:
        ok("Cached set.json is up to date, skipping download")

    with open(FAB_SETS_LOCAL, encoding="utf-8") as f:
        sets = json.load(f)

    cur = conn.cursor()
    rows = []

    for s in sets:
        set_id   = s.get("id")
        set_name = s.get("name", "")
        for printing in s.get("printings", []):
            edition   = printing.get("edition", "N")
            cc_url    = printing.get("collectors_center")  # note: "center" not "centre"
            start_id  = printing.get("start_card_id")
            end_id    = printing.get("end_card_id")
            oop       = printing.get("out_of_print", False)
            rel_raw   = printing.get("initial_release_date")
            try:
                rel_date = datetime.fromisoformat(rel_raw.replace("Z", "+00:00")).date() if rel_raw else None
            except Exception:
                rel_date = None

            rows.append((set_id, edition, set_name, cc_url, start_id, end_id, rel_date, oop, datetime.now()))

    insert_sql = """
        INSERT INTO bronze.fab_sets
            (set_id, edition, set_name, collectors_centre_url,
             start_card_id, end_card_id, initial_release_date, out_of_print, loaded_at)
        VALUES %s
        ON CONFLICT (set_id, edition) DO UPDATE
            SET set_name              = EXCLUDED.set_name,
                collectors_centre_url = EXCLUDED.collectors_centre_url,
                start_card_id         = EXCLUDED.start_card_id,
                end_card_id           = EXCLUDED.end_card_id,
                initial_release_date  = EXCLUDED.initial_release_date,
                out_of_print          = EXCLUDED.out_of_print,
                loaded_at             = EXCLUDED.loaded_at
    """
    psycopg2.extras.execute_values(cur, insert_sql, rows)
    conn.commit()
    ok(f"Loaded {len(rows):,} set+edition entries")
    cur.close()


# ─────────────────────────────────────────────────────────────────────────────
#  3 / 7 — Cardmarket product catalogue
# ─────────────────────────────────────────────────────────────────────────────

def ingest_cardmarket_products(conn):
    print_section("3 / 7 — Cardmarket: product catalogue (game 16)")

    if is_remote_newer(CM_PRODUCTS_URL, CM_PRODUCTS_LOCAL) or not os.path.exists(CM_PRODUCTS_LOCAL):
        info("Downloading products_singles_16.json from Cardmarket S3...")
        download(CM_PRODUCTS_URL, CM_PRODUCTS_LOCAL, "products_singles_16.json")
    else:
        ok("Cached product list is up to date, skipping download")

    info("Parsing product list...")
    with open(CM_PRODUCTS_LOCAL, encoding="utf-8") as f:
        raw = json.load(f)

    items = raw.get("products", raw) if isinstance(raw, dict) else raw
    total = len(items)
    ok(f"Parsed {total:,} products")

    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE bronze.cardmarket_products")
    conn.commit()
    warn("Truncated bronze.cardmarket_products for full refresh")

    insert_sql = """
        INSERT INTO bronze.cardmarket_products
            (idproduct, name, id_category, id_expansion, id_metacard, date_added)
        VALUES %s
        ON CONFLICT (idproduct) DO UPDATE
            SET name         = EXCLUDED.name,
                id_category  = EXCLUDED.id_category,
                id_expansion = EXCLUDED.id_expansion,
                id_metacard  = EXCLUDED.id_metacard,
                date_added   = EXCLUDED.date_added
    """

    batch      = []
    batch_size = 5000
    inserted   = 0

    for p in items:
        da = p.get("dateAdded")
        try:
            da_date = datetime.strptime(da[:10], "%Y-%m-%d").date() if da else None
        except Exception:
            da_date = None
        batch.append((
            int(p["idProduct"]),
            p.get("name", ""),
            p.get("idCategory"),
            p.get("idExpansion"),
            p.get("idMetacard"),
            da_date,
        ))
        if len(batch) >= batch_size:
            psycopg2.extras.execute_values(cur, insert_sql, batch)
            conn.commit()
            inserted += len(batch)
            batch = []
            progress(inserted, total, label="products inserted")

    if batch:
        psycopg2.extras.execute_values(cur, insert_sql, batch)
        conn.commit()
        inserted += len(batch)

    progress(inserted, total, label="products inserted")
    ok(f"Loaded {inserted:,} Cardmarket products")
    cur.close()


# ─────────────────────────────────────────────────────────────────────────────
#  4 / 7 — Cardmarket price guide
# ─────────────────────────────────────────────────────────────────────────────

def ingest_cardmarket_prices(conn):
    print_section("4 / 7 — Cardmarket: price guide (game 16)")

    if is_remote_newer(CM_PRICES_URL, CM_PRICES_LOCAL) or not os.path.exists(CM_PRICES_LOCAL):
        info("Downloading price_guide_16.json from Cardmarket S3...")
        download(CM_PRICES_URL, CM_PRICES_LOCAL, "price_guide_16.json")
    else:
        ok("Cached price guide is up to date, skipping download")

    info("Parsing price guide...")
    with open(CM_PRICES_LOCAL, encoding="utf-8") as f:
        raw = json.load(f)

    items = raw.get("priceGuides", raw) if isinstance(raw, dict) else raw
    total = len(items)
    ok(f"Parsed {total:,} price rows")

    cur   = conn.cursor()
    today = date.today()

    cur.execute("DELETE FROM bronze.cardmarket_prices WHERE loaded_date = %s", (today,))
    deleted = cur.rowcount
    if deleted:
        warn(f"Removed {deleted:,} existing rows for today (re-run)")

    insert_sql = """
        INSERT INTO bronze.cardmarket_prices (
            idproduct, avg, low, trend, avg1, avg7, avg30,
            avg_foil, low_foil, trend_foil, avg1_foil, avg7_foil, avg30_foil,
            loaded_date
        ) VALUES %s
    """

    batch      = []
    batch_size = 5000
    inserted   = 0

    for p in items:
        batch.append((
            int(p["idProduct"]) if p.get("idProduct") is not None else None,
            p.get("avg"),           p.get("low"),           p.get("trend"),
            p.get("avg1"),          p.get("avg7"),          p.get("avg30"),
            p.get("avg-foil"),      p.get("low-foil"),      p.get("trend-foil"),
            p.get("avg1-foil"),     p.get("avg7-foil"),     p.get("avg30-foil"),
            today,
        ))
        if len(batch) >= batch_size:
            psycopg2.extras.execute_values(cur, insert_sql, batch)
            conn.commit()
            inserted += len(batch)
            batch = []
            progress(inserted, total, label="prices inserted")

    if batch:
        psycopg2.extras.execute_values(cur, insert_sql, batch)
        conn.commit()
        inserted += len(batch)

    progress(inserted, total, label="prices inserted")
    ok(f"Loaded {inserted:,} price rows for {today}")
    cur.close()


# ─────────────────────────────────────────────────────────────────────────────
#  5 / 7 — Riksbank exchange rates
# ─────────────────────────────────────────────────────────────────────────────

def ingest_exchange_rates(conn):
    print_section("5 / 7 — Riksbank Exchange Rates")

    cur = conn.cursor()

    for series_id, series_id2, label in RIKSBANK_PAIRS:
        fetched = False
        for days_back in range(10):
            check_date = (datetime.today() - timedelta(days=days_back)).date()
            url = (
                f"https://api.riksbank.se/swea/v1/CrossRates/"
                f"{series_id}/{series_id2}/{check_date.isoformat()}"
            )
            try:
                r = requests.get(url, timeout=15)
                if r.status_code == 200 and r.json():
                    data       = r.json()[0]
                    rate_value = float(data["value"])
                    rate_date  = data["date"]
                    cur.execute("""
                        INSERT INTO bronze.exchange_rates
                            (series_id, rate_date, rate_value, loaded_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (series_id, rate_date) DO UPDATE
                            SET rate_value = EXCLUDED.rate_value,
                                loaded_at  = EXCLUDED.loaded_at
                    """, (label, rate_date, rate_value))
                    conn.commit()
                    ok(f"{label}: {rate_value:.4f}  (date: {rate_date})")
                    fetched = True
                    break
            except Exception as e:
                warn(f"Riksbank request failed for {label} on {check_date}: {e}")

        if not fetched:
            err(f"Could not fetch rate for {label}")

    cur.close()


# ─────────────────────────────────────────────────────────────────────────────
#  6 / 7 — Manual crosswalk (SSMS export)
# ─────────────────────────────────────────────────────────────────────────────

def ingest_cm_manual(conn):
    print_section("6 / 7 — Manual crosswalk (data/fab_cm_manual.csv)")

    if not os.path.exists(MANUAL_CSV):
        warn(f"Manual CSV not found at {MANUAL_CSV} — skipping")
        return

    rows = []
    with open(MANUAL_CSV, encoding="utf-8-sig") as f:
        for line in csv.reader(f):
            if len(line) < 7:
                continue
            printing_uid, card_id, set_id, edition, idexp, idprod, foil = (
                line[0].strip(), line[1].strip(), line[2].strip(),
                line[3].strip(), line[4].strip(), line[5].strip(), line[6].strip()
            )
            if not printing_uid:
                continue
            foil_val = None if foil.upper() in ("NULL", "") else foil
            try:
                idexp_val  = int(idexp)  if idexp  else None
                idprod_val = int(idprod) if idprod else None
            except ValueError:
                idexp_val = idprod_val = None
            rows.append((printing_uid, card_id, set_id, edition, idexp_val, idprod_val, foil_val))

    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE bronze.fab_cm_manual")
    conn.commit()

    insert_sql = """
        INSERT INTO bronze.fab_cm_manual
            (printing_unique_id, id, set_id, edition, idexpansion, idproduct, foil)
        VALUES %s
        ON CONFLICT (printing_unique_id) DO NOTHING
    """
    psycopg2.extras.execute_values(cur, insert_sql, rows)
    conn.commit()
    ok(f"Loaded {len(rows):,} manual mappings from CSV")
    cur.close()


# ─────────────────────────────────────────────────────────────────────────────
#  7 / 7 — Build fab_expansions (manual + auto-match)
# ─────────────────────────────────────────────────────────────────────────────

def build_fab_expansions(conn):
    print_section("7 / 7 — Building set → idExpansion mapping")

    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE bronze.fab_expansions")
    conn.commit()

    # ── Part A: extract from manual CSV ──────────────────────────────────────
    cur.execute("""
        INSERT INTO bronze.fab_expansions (set_id, edition, idexpansion, source, loaded_at)
        SELECT DISTINCT set_id, edition, idexpansion, 'manual', NOW()
        FROM bronze.fab_cm_manual
        WHERE idexpansion IS NOT NULL
        ON CONFLICT (set_id, edition) DO UPDATE
            SET idexpansion = EXCLUDED.idexpansion,
                source      = EXCLUDED.source,
                loaded_at   = EXCLUDED.loaded_at
    """)
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM bronze.fab_expansions WHERE source = 'manual'")
    manual_count = cur.fetchone()[0]
    ok(f"Manual mappings: {manual_count} set+edition combos")

    # ── Part B: hardcoded overrides ──────────────────────────────────────────
    # These sets are confirmed on Cardmarket but the auto-matcher can't find them
    # because blitz decks share card names with their parent set, causing the
    # parent's idExpansion to get claimed first by the blitz deck instead.
    # UZU (Uzuri Blitz Deck) and OUT (Outsiders) legitimately share idExpansion
    # 5254 since Uzuri blitz cards are Outsiders reprints listed under the same CM expansion.
    HARD_MAP = {
        # (set_id, edition): idExpansion   — verified via CM product lookup
        ("HNT", "N"): 5959,   # The Hunted
        ("OUT", "N"): 5254,   # Outsiders
        ("ROS", "N"): 5801,   # Rosetta
        ("NUU", "N"): 5756,   # Part the Mistveil: Nuu Blitz Deck
        ("UZU", "N"): 5254,   # Uzuri Blitz Deck (Outsiders reprints, same CM expansion)
        # MPW (Mastery Pack Warrior) omitted — not released yet (Aug 2026)
    }
    hard_rows = [(s, e, x, "hard", datetime.now()) for (s, e), x in HARD_MAP.items()]
    psycopg2.extras.execute_values(cur, """
        INSERT INTO bronze.fab_expansions (set_id, edition, idexpansion, source, loaded_at)
        VALUES %s
        ON CONFLICT (set_id, edition) DO UPDATE
            SET idexpansion = EXCLUDED.idexpansion,
                source      = EXCLUDED.source,
                loaded_at   = EXCLUDED.loaded_at
    """, hard_rows)
    conn.commit()
    ok(f"Hard-coded overrides: {len(hard_rows)} set+edition combos")

    # ── Part C: auto-match remaining sets ────────────────────────────────────
    # Get all set+editions in fab_printings that don't have a manual mapping
    cur.execute("""
        SELECT DISTINCT p.set_id, p.edition
        FROM bronze.fab_printings p
        LEFT JOIN bronze.fab_expansions e ON p.set_id = e.set_id AND p.edition = e.edition
        WHERE e.set_id IS NULL
    """)
    unmapped = cur.fetchall()
    info(f"Auto-matching {len(unmapped)} unmapped set+edition combos...")

    # Get idExpansion values not yet claimed
    cur.execute("""
        SELECT DISTINCT id_expansion
        FROM bronze.cardmarket_products
        WHERE id_expansion IS NOT NULL
          AND id_expansion NOT IN (SELECT idexpansion FROM bronze.fab_expansions)
    """)
    unknown_exps = [r[0] for r in cur.fetchall()]

    # Build CM name sets per unknown expansion (with pitch suffix normalised)
    cm_names_by_exp = {}
    if unknown_exps:
        cur.execute("""
            SELECT id_expansion, array_agg(upper(trim(name)))
            FROM bronze.cardmarket_products
            WHERE id_expansion = ANY(%s)
            GROUP BY id_expansion
        """, (unknown_exps,))
        for idexp, names in cur.fetchall():
            cm_names_by_exp[idexp] = set(names)

    auto_matches = []
    for set_id, edition in unmapped:
        # Build expected CM name set for this set+edition
        cur.execute("""
            SELECT array_agg(DISTINCT upper(trim(
                CASE (raw_data->>'pitch')
                    WHEN '1' THEN raw_data->>'name' || ' (Red)'
                    WHEN '2' THEN raw_data->>'name' || ' (Yellow)'
                    WHEN '3' THEN raw_data->>'name' || ' (Blue)'
                    ELSE raw_data->>'name'
                END
            )))
            FROM bronze.fab_printings
            WHERE set_id = %s AND edition = %s
        """, (set_id, edition))
        result = cur.fetchone()[0]
        if not result:
            continue
        fab_names = set(result)

        best_score = 0.0
        best_exp   = None
        for idexp, cm_names in cm_names_by_exp.items():
            overlap = len(fab_names & cm_names)
            score   = overlap / len(fab_names)
            if score > best_score:
                best_score = score
                best_exp   = idexp

        if best_exp and best_score >= AUTO_MATCH_THRESHOLD:
            auto_matches.append((set_id, edition, best_exp, best_score))
            # Remove this expansion from the pool so it can't be claimed twice
            del cm_names_by_exp[best_exp]

    if auto_matches:
        insert_sql = """
            INSERT INTO bronze.fab_expansions (set_id, edition, idexpansion, source, loaded_at)
            VALUES %s
            ON CONFLICT (set_id, edition) DO NOTHING
        """
        psycopg2.extras.execute_values(
            cur, insert_sql,
            [(s, e, x, "auto", datetime.now()) for s, e, x, _ in auto_matches]
        )
        conn.commit()
        ok(f"Auto-matched {len(auto_matches)} set+edition combos")
        for s, e, x, sc in sorted(auto_matches):
            info(f"  {s} {e} → idExpansion {x}  (score {sc:.0%})")
    else:
        ok("No additional auto-matches found")

    # Summary
    still_unmapped = [(s, e) for s, e in unmapped
                      if not any(s == a[0] and e == a[1] for a in auto_matches)]
    if still_unmapped:
        warn(f"{len(still_unmapped)} sets remain unmatched (no Cardmarket listing or new set):")
        for s, e in sorted(still_unmapped):
            info(f"  {s} {e}")

    cur.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print_banner()
    start = time.time()
    info(f"Run started at {datetime.now():%Y-%m-%d %H:%M:%S}")

    info("Connecting to PostgreSQL (fab)...")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        ok("Connected")
    except Exception as e:
        err(f"Connection failed: {e}")
        sys.exit(1)

    try:
        ingest_fab_cards(conn)
        ingest_fab_sets(conn)
        ingest_cardmarket_products(conn)
        ingest_cardmarket_prices(conn)
        ingest_exchange_rates(conn)
        ingest_cm_manual(conn)
        build_fab_expansions(conn)
    except Exception as e:
        err(f"Pipeline error: {e}")
        import traceback; traceback.print_exc()
        conn.close()
        sys.exit(1)

    conn.close()

    elapsed = time.time() - start
    print()
    print(CYAN + "─" * 60 + RESET)
    print(GREEN + BOLD + f"  ✔  Bronze ingestion complete in {elapsed:.1f}s" + RESET)
    print(CYAN + "─" * 60 + RESET)
    print()


if __name__ == "__main__":
    main()
