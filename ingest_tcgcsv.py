"""
ingest_tcgcsv.py — TCGplayer card data + USD prices via tcgcsv.com.

tcgcsv.com is a free, no-API-key, no-rate-limit public mirror of TCGplayer's data.
Flesh and Blood is category 62. For every set — including brand-new ones the-fab-cube
lags behind — it provides card names, set numbers, rarity, full card text, type/class/
cost/stats, image URLs, and USD prices (Normal / Cold Foil / Rainbow Foil variants).

This replaces JustTCG as the price + missing-set source. It does two jobs:
  1. USD prices for our existing the-fab-cube cards (silver joins by
     productId = tcgplayer_product_id).
  2. Full card data for sets the-fab-cube is missing (silver folds these in as
     match_tier = 4, deduped by productId).

It walks every FaB group (set), loading:
    bronze.tcgcsv_groups  — one row per set (groupId, abbreviation = set code, name)
    bronze.tcgcsv_cards   — one row per single card (product), with extracted fields + raw JSON
    bronze.tcgcsv_prices  — one row per (product, sub_type), USD low/mid/high/market

No rate limit, so it fetches everything in one run (~97 groups × 2 requests).

Usage:
    python ingest_tcgcsv.py
"""

import os
import re
import sys
import json
from pathlib import Path

import requests
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(dotenv_path=HERE / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("PG_HOST", "localhost"),
    "port":     int(os.getenv("PG_PORT", 5432)),
    "database": "fab",
    "user":     os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}

TCGCSV_BASE = "https://tcgcsv.com/tcgplayer"
FAB_CATEGORY = 62                       # Flesh and Blood on TCGplayer
# tcgcsv 401s the default urllib/requests UA — send an explicit one.
HEADERS = {"User-Agent": "fab-pipeline/1.0 (+https://github.com/GHT4ngo/retro-data-display)"}
TIMEOUT = 30

# Set code = the leading letters of a collector Number (a leading digit cluster like the
# "1H" in "1HP141" is kept; the trailing collector digits are dropped).
#   "OMN001"->"OMN"  "1HB024"->"1HB"  "WTR042-C"->"WTR"  "ZENO29"->"ZENO"
SET_CODE_RE = re.compile(r"^([0-9]*[A-Z]+)", re.I)

# ── Terminal helpers ──────────────────────────────────────────────────────────
GREEN, CYAN, YELLOW, RED, BOLD, RESET = (
    "\033[32m", "\033[36m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
)
def section(t): print(f"\n{CYAN}{'─'*60}\n  {BOLD}{t}{RESET}\n{CYAN}{'─'*60}{RESET}")
def ok(m):      print(f"  {GREEN}✔{RESET}  {m}")
def info(m):    print(f"  {CYAN}→{RESET}  {m}")
def warn(m):    print(f"  {YELLOW}⚠{RESET}  {m}")
def err(m):     print(f"  {RED}✘{RESET}  {m}")


def api_get(path):
    """GET a tcgcsv endpoint, returning the `results` list."""
    r = requests.get(f"{TCGCSV_BASE}{path}", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    return body.get("results", body if isinstance(body, list) else [])


def ext_map(product):
    """Flatten a product's extendedData list into a {name: value} dict."""
    return {e.get("name"): e.get("value") for e in product.get("extendedData", [])}


def set_code_for(number, group_abbr):
    """Derive the set code from a tcgcsv collector Number.

    The set code is the leading-letter prefix of the FIRST number. Double-faced /
    paired products carry split values like ``HNT002//HNT055`` or ``SEA//082`` — taking
    the first segment's prefix keeps them in their real set (``HNT`` / ``SEA``) instead of
    minting fake codes like ``HNT00``. Falls back to the group abbreviation if no segment
    has letters (e.g. a bare numeric id).
    """
    if number:
        for seg in re.split(r"/+", number.strip()):
            m = SET_CODE_RE.match(seg.strip().upper())
            if m:
                return m.group(1)
    return (group_abbr or "").upper() or None


# ── Loaders ───────────────────────────────────────────────────────────────────
def load_groups(conn):
    """Fetch the FaB set list and upsert into bronze.tcgcsv_groups. Returns the list."""
    groups = api_get(f"/{FAB_CATEGORY}/groups")
    rows = [(
        g["groupId"],
        (g.get("abbreviation") or "").upper() or None,
        g.get("name"),
        bool(g.get("isSupplemental")),
        (g.get("publishedOn") or None),
    ) for g in groups]
    cur = conn.cursor()
    execute_values(cur, """
        INSERT INTO bronze.tcgcsv_groups
            (group_id, abbreviation, name, is_supplemental, published_on)
        VALUES %s
        ON CONFLICT (group_id) DO UPDATE SET
            abbreviation    = EXCLUDED.abbreviation,
            name            = EXCLUDED.name,
            is_supplemental = EXCLUDED.is_supplemental,
            published_on    = EXCLUDED.published_on,
            loaded_at       = NOW()
    """, rows)
    conn.commit()
    cur.close()
    ok(f"Loaded {len(rows)} FaB sets into bronze.tcgcsv_groups")
    return groups


def load_group_cards_prices(conn, group):
    """Fetch products + prices for one group and upsert into bronze.tcgcsv_cards /
    bronze.tcgcsv_prices. Returns (card_count, price_count)."""
    gid = group["groupId"]
    abbr = (group.get("abbreviation") or "").upper() or None
    products = api_get(f"/{FAB_CATEGORY}/{gid}/products")
    prices   = api_get(f"/{FAB_CATEGORY}/{gid}/prices")

    card_rows = []
    for p in products:
        e = ext_map(p)
        number = e.get("Number")
        # Singles carry a Number; skip sealed products (boosters, boxes, decks).
        if not number:
            continue
        card_rows.append((
            p["productId"],
            gid,
            set_code_for(number, abbr),
            number,
            p.get("name"),
            p.get("cleanName"),
            e.get("Rarity"),
            e.get("CardType"),
            e.get("Class"),
            e.get("Talent"),
            e.get("Cost"),
            e.get("Pitch") or e.get("PitchValue"),
            e.get("Power"),
            e.get("Defense"),
            e.get("Life"),
            e.get("Intellect") or e.get("Intelligence"),
            e.get("Description"),
            p.get("imageUrl"),
            json.dumps(p),
        ))

    valid_ids = {r[0] for r in card_rows}
    price_rows = []
    for pr in prices:
        pid = pr.get("productId")
        if pid not in valid_ids:            # only keep prices for singles we stored
            continue
        price_rows.append((
            pid,
            pr.get("subTypeName") or "Normal",
            pr.get("lowPrice"),
            pr.get("midPrice"),
            pr.get("highPrice"),
            pr.get("marketPrice"),
            pr.get("directLowPrice"),
        ))

    cur = conn.cursor()
    if card_rows:
        execute_values(cur, """
            INSERT INTO bronze.tcgcsv_cards
                (product_id, group_id, set_code, number, name, clean_name, rarity,
                 card_type, class, talent, cost, pitch, power, defense, life, intellect,
                 description, image_url, raw_data, fetched_at)
            VALUES %s
            ON CONFLICT (product_id) DO UPDATE SET
                group_id=EXCLUDED.group_id, set_code=EXCLUDED.set_code,
                number=EXCLUDED.number, name=EXCLUDED.name, clean_name=EXCLUDED.clean_name,
                rarity=EXCLUDED.rarity, card_type=EXCLUDED.card_type, class=EXCLUDED.class,
                talent=EXCLUDED.talent, cost=EXCLUDED.cost, pitch=EXCLUDED.pitch,
                power=EXCLUDED.power, defense=EXCLUDED.defense, life=EXCLUDED.life,
                intellect=EXCLUDED.intellect, description=EXCLUDED.description,
                image_url=EXCLUDED.image_url, raw_data=EXCLUDED.raw_data, fetched_at=NOW()
        """, card_rows, template="(" + ",".join(["%s"] * 19) + ", NOW())")
    if price_rows:
        execute_values(cur, """
            INSERT INTO bronze.tcgcsv_prices
                (product_id, sub_type, low_price, mid_price, high_price,
                 market_price, direct_low_price, fetched_at)
            VALUES %s
            ON CONFLICT (product_id, sub_type) DO UPDATE SET
                low_price=EXCLUDED.low_price, mid_price=EXCLUDED.mid_price,
                high_price=EXCLUDED.high_price, market_price=EXCLUDED.market_price,
                direct_low_price=EXCLUDED.direct_low_price, fetched_at=NOW()
        """, price_rows, template="(" + ",".join(["%s"] * 7) + ", NOW())")
    conn.commit()
    cur.close()
    return len(card_rows), len(price_rows)


def mirror_missing_sets(conn):
    """Give the sets the-fab-cube lacks a proper name + release date in bronze.fab_sets
    so the API's /sets endpoint shows them (it left-joins gold → fab_sets for names).
    Only mirrors set codes NOT already present in bronze.fab_printings; existing sets
    keep their canonical names. One row per (set_code, edition N/F/U) so the join finds a
    name whichever edition silver assigns."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bronze.fab_sets
            (set_id, edition, set_name, initial_release_date, out_of_print)
        SELECT sc.set_code, ed.edition, sc.set_name, sc.release_date, false
        FROM (
            SELECT c.set_code,
                   mode() WITHIN GROUP (ORDER BY g.name)  AS set_name,
                   min(g.published_on)::date              AS release_date
            FROM bronze.tcgcsv_cards c
            JOIN bronze.tcgcsv_groups g ON g.group_id = c.group_id
            WHERE c.set_code IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM bronze.fab_printings p WHERE p.set_id = c.set_code
              )
            GROUP BY c.set_code
        ) sc
        CROSS JOIN (VALUES ('N'), ('F'), ('U')) ed(edition)
        ON CONFLICT (set_id, edition) DO NOTHING
    """)
    n = cur.rowcount
    conn.commit()
    cur.close()
    ok(f"Mirrored {n} (set, edition) name row(s) into bronze.fab_sets")


def main():
    section("tcgcsv.com — TCGplayer cards + USD prices (FaB, category 62)")

    try:
        conn = psycopg2.connect(**DB_CONFIG)
    except Exception as e:
        err(f"DB connection failed: {e}")
        sys.exit(1)

    try:
        groups = load_groups(conn)
    except Exception as e:
        err(f"Failed to load groups: {e}")
        conn.close(); sys.exit(1)

    total_cards = total_prices = 0
    failed = 0
    for i, g in enumerate(groups, 1):
        name = g.get("name", "?")
        try:
            c, p = load_group_cards_prices(conn, g)
            total_cards += c; total_prices += p
            info(f"[{i:>2}/{len(groups)}] {name}: {c} cards, {p} prices")
        except Exception as e:
            failed += 1
            warn(f"[{i:>2}/{len(groups)}] {name}: skipped ({e})")

    try:
        mirror_missing_sets(conn)
    except Exception as e:
        warn(f"Could not mirror set names into fab_sets: {e}")

    conn.close()
    print()
    ok(f"Done — {total_cards:,} cards, {total_prices:,} prices across "
       f"{len(groups)-failed}/{len(groups)} sets.")
    if failed:
        warn(f"{failed} set(s) failed — re-run to retry (upserts are idempotent).")


if __name__ == "__main__":
    main()
