"""
ingest_justtcg_sets.py — Fill catalogue gaps for sets the-fab-cube is missing.

the-fab-cube (our card catalogue spine) lags ~2 booster sets behind, so recent sets
(e.g. PEN Compendium of Rathe, OMN Omens of the Third Age) have no cards in the app.
This script sources those sets' cards + prices directly from the JustTCG API and writes
them to bronze.justtcg_cards (+ bronze.justtcg_prices), so dbt can fold them into
gold.gold_cards alongside the-fab-cube cards. Card images are derived from the
tcgplayerId via TCGplayer's CDN (JustTCG has no image field).

Which sets to pull is driven by justtcg_sets.csv (user-maintained: one row per missing
set). Adding a future set = one new line in that CSV.

Rate limits: free JustTCG tier is ~100 requests/day, 20 cards/request. This script is
resumable — by default it skips sets already fetched within JUSTTCG_REFETCH days; pass
--force to re-fetch. It stops cleanly when the daily quota is nearly exhausted.

Usage:
    python ingest_justtcg_sets.py            # fetch sets not fetched recently
    python ingest_justtcg_sets.py --force    # re-fetch all mapped sets
"""

import os
import re
import csv
import sys
import time
import argparse
from pathlib import Path

import requests
import psycopg2
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

JUSTTCG_BASE    = "https://api.justtcg.com/v1"
JUSTTCG_KEY     = os.getenv("JUSTTCG_API_KEY", "")
JUSTTCG_GAME    = os.getenv("JUSTTCG_GAME", "")   # optional override of the game slug
JUSTTCG_SLEEP   = 7      # seconds between requests (free plan: 10 req/min)
JUSTTCG_LIMIT   = 20     # cards per /cards page (free plan max)
JUSTTCG_REFETCH = 7      # skip a set fetched within this many days (unless --force)
DAILY_FLOOR     = 3      # stop when this few daily requests remain

SET_MAP_CSV     = HERE / "justtcg_sets.csv"

# TCGplayer CDN image URL derived from the product id (verified working).
def image_url(tcgplayer_id: str) -> str:
    return f"https://tcgplayer-cdn.tcgplayer.com/product/{tcgplayer_id}_in_1000x1000.jpg"

PITCH_CODE = {"red": "1", "yellow": "2", "blue": "3"}
PITCH_RE   = re.compile(r"\s*\((red|yellow|blue)\)\s*$", re.IGNORECASE)

# ── Terminal helpers ──────────────────────────────────────────────────────────
GREEN, CYAN, YELLOW, RED, BOLD, RESET = (
    "\033[32m", "\033[36m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
)
def section(t): print(f"\n{CYAN}{'─'*60}\n  {BOLD}{t}{RESET}\n{CYAN}{'─'*60}{RESET}")
def ok(m):      print(f"  {GREEN}✔{RESET}  {m}")
def info(m):    print(f"  {CYAN}→{RESET}  {m}")
def warn(m):    print(f"  {YELLOW}⚠{RESET}  {m}")
def err(m):     print(f"  {RED}✘{RESET}  {m}")


class DailyLimit(Exception):
    """Raised when JustTCG reports the daily request quota is exhausted."""


def api_get(path, params=None):
    """GET a JustTCG endpoint. Returns (data_list, metadata). Raises DailyLimit on 429."""
    resp = requests.get(
        f"{JUSTTCG_BASE}{path}",
        headers={"x-api-key": JUSTTCG_KEY},
        params=params or {},
        timeout=20,
    )
    if resp.status_code == 429:
        raise DailyLimit(resp.text[:200])
    if resp.status_code != 200:
        raise RuntimeError(f"JustTCG {resp.status_code} on {path}: {resp.text[:200]}")
    body = resp.json()
    data = body.get("data", body if isinstance(body, list) else [])
    meta = body.get("_metadata", {}) if isinstance(body, dict) else {}
    return data, meta


def split_pitch(name: str):
    """Split a trailing '(Red|Yellow|Blue)' pitch colour off a card name.
    Returns (clean_name, pitch_code or None)."""
    m = PITCH_RE.search(name or "")
    if not m:
        return name, None
    return PITCH_RE.sub("", name), PITCH_CODE[m.group(1).lower()]


# ── Set map (justtcg_sets.csv) ────────────────────────────────────────────────
def load_set_map(conn):
    """Load justtcg_sets.csv into bronze.justtcg_set_map and mirror rows into
    bronze.fab_sets so the /sets endpoint shows proper names + release dates."""
    if not SET_MAP_CSV.exists():
        err(f"{SET_MAP_CSV.name} not found — nothing to ingest.")
        return []

    rows = []
    with open(SET_MAP_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("set_code"):
                continue
            rows.append({
                "set_code":       r["set_code"].strip(),
                "set_name":       (r.get("set_name") or "").strip(),
                "edition":        (r.get("edition") or "N").strip() or "N",
                "release_date":   (r.get("release_date") or "").strip() or None,
                "justtcg_set_id": (r.get("justtcg_set_id") or "").strip() or None,
                "idexpansion":    (r.get("idexpansion") or "").strip() or None,
            })

    cur = conn.cursor()
    for r in rows:
        cur.execute("""
            INSERT INTO bronze.justtcg_set_map
                (set_code, set_name, edition, release_date, justtcg_set_id, idexpansion)
            VALUES (%(set_code)s, %(set_name)s, %(edition)s, %(release_date)s,
                    %(justtcg_set_id)s, %(idexpansion)s)
            ON CONFLICT (set_code) DO UPDATE SET
                set_name     = EXCLUDED.set_name,
                edition      = EXCLUDED.edition,
                release_date = EXCLUDED.release_date,
                -- keep a previously-resolved id if the CSV cell is blank
                justtcg_set_id = COALESCE(EXCLUDED.justtcg_set_id, bronze.justtcg_set_map.justtcg_set_id),
                idexpansion    = COALESCE(EXCLUDED.idexpansion, bronze.justtcg_set_map.idexpansion)
        """, r)
        # Mirror into fab_sets so /sets shows the name + release date.
        cur.execute("""
            INSERT INTO bronze.fab_sets
                (set_id, edition, set_name, initial_release_date, out_of_print)
            VALUES (%(set_code)s, %(edition)s, %(set_name)s, %(release_date)s, false)
            ON CONFLICT (set_id, edition) DO UPDATE SET
                set_name             = EXCLUDED.set_name,
                initial_release_date = EXCLUDED.initial_release_date
        """, r)
    conn.commit()
    cur.close()
    ok(f"Loaded {len(rows)} set(s) from {SET_MAP_CSV.name}; mirrored into fab_sets")
    return rows


# ── JustTCG discovery ─────────────────────────────────────────────────────────
def discover_game():
    """Find the FaB game slug via /games (or use the JUSTTCG_GAME override)."""
    if JUSTTCG_GAME:
        info(f"Using game slug from env: {JUSTTCG_GAME}")
        return JUSTTCG_GAME
    games, _ = api_get("/games")
    for g in games:
        name = (g.get("name") or g.get("game") or "")
        if "flesh" in name.lower():
            slug = g.get("id") or g.get("slug") or g.get("game")
            ok(f"Game: {name} → slug '{slug}'")
            return slug
    raise RuntimeError("Could not find a Flesh and Blood game in /games response")


def resolve_set_id(conn, game, row):
    """Return the JustTCG set id for a map row, resolving via /sets if not cached."""
    if row["justtcg_set_id"]:
        return row["justtcg_set_id"]
    sets, _ = api_get("/sets", {"game": game})
    target = row["set_name"].strip().lower()
    match = None
    for s in sets:
        nm = (s.get("name") or s.get("set_name") or s.get("set") or "").strip().lower()
        if nm == target or (target and target in nm):
            match = s
            break
    if not match:
        warn(f"  No JustTCG set matched '{row['set_name']}' — skipping")
        return None
    sid = match.get("id") or match.get("set") or match.get("set_id")
    # Cache the resolved id back into the map table.
    cur = conn.cursor()
    cur.execute("UPDATE bronze.justtcg_set_map SET justtcg_set_id=%s WHERE set_code=%s",
                (sid, row["set_code"]))
    conn.commit(); cur.close()
    ok(f"  Resolved '{row['set_name']}' → JustTCG set id '{sid}'")
    return sid


def recently_fetched(conn, set_code):
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM bronze.justtcg_cards
        WHERE set_code = %s AND fetched_at > NOW() - INTERVAL '%s days'
    """, (set_code, JUSTTCG_REFETCH))
    n = cur.fetchone()[0]
    cur.close()
    return n


# ── Card ingestion ────────────────────────────────────────────────────────────
def fetch_set_cards(conn, game, set_id, set_code, set_name):
    """Page through /cards for one set, upserting card metadata + variant prices.
    Returns the number of cards stored. Raises DailyLimit when quota runs out."""
    cur = conn.cursor()
    offset = 0
    stored_cards = 0
    stored_variants = 0

    while True:
        data, meta = api_get("/cards", {
            "game": game, "set": set_id,
            "limit": JUSTTCG_LIMIT, "offset": offset,
        })
        remaining = meta.get("apiDailyRequestsRemaining")

        for card in data:
            tcg_id = str(card.get("tcgplayerId") or "").strip()
            if not tcg_id:
                continue
            clean_name, pitch = split_pitch(card.get("name") or "")
            cur.execute("""
                INSERT INTO bronze.justtcg_cards
                    (tcgplayer_product_id, name, pitch, number, rarity,
                     set_code, set_name, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (tcgplayer_product_id) DO UPDATE SET
                    name=EXCLUDED.name, pitch=EXCLUDED.pitch, number=EXCLUDED.number,
                    rarity=EXCLUDED.rarity, set_code=EXCLUDED.set_code,
                    set_name=EXCLUDED.set_name, fetched_at=NOW()
            """, (tcg_id, clean_name, pitch, card.get("number"), card.get("rarity"),
                  set_code, set_name or card.get("set_name")))
            stored_cards += 1

            for v in card.get("variants", []):
                printing  = v.get("printing", "")
                condition = v.get("condition", "")
                if not printing or not condition:
                    continue
                cur.execute("""
                    INSERT INTO bronze.justtcg_prices
                        (tcgplayer_product_id, printing, condition, price_usd, fetched_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (tcgplayer_product_id, printing, condition)
                    DO UPDATE SET price_usd=EXCLUDED.price_usd, fetched_at=NOW()
                """, (tcg_id, printing, condition, v.get("price")))
                stored_variants += 1

        conn.commit()
        info(f"  {set_code}: {stored_cards} cards, {stored_variants} variants "
             f"(offset {offset}, daily_left={remaining})")

        if len(data) < JUSTTCG_LIMIT:
            break                                   # last page
        offset += JUSTTCG_LIMIT
        time.sleep(JUSTTCG_SLEEP)
        if remaining is not None and remaining <= DAILY_FLOOR:
            cur.close()
            raise DailyLimit(f"daily quota nearly exhausted after {set_code}")

    cur.close()
    return stored_cards


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-fetch sets even if fetched within the refetch window")
    args = ap.parse_args()

    section("JustTCG — missing-set catalogue")
    if not JUSTTCG_KEY:
        err("JUSTTCG_API_KEY not set in .env — cannot continue.")
        sys.exit(1)

    conn = psycopg2.connect(**DB_CONFIG)
    rows = load_set_map(conn)
    if not rows:
        conn.close(); return

    try:
        game = discover_game()
    except DailyLimit:
        warn("Daily JustTCG quota exhausted — try again tomorrow. Nothing fetched.")
        conn.close(); return
    except Exception as e:
        err(f"Game discovery failed: {e}"); conn.close(); sys.exit(1)

    total = 0
    try:
        for row in rows:
            sc = row["set_code"]
            if not args.force and recently_fetched(conn, sc):
                ok(f"{sc} ({row['set_name']}) already fetched < {JUSTTCG_REFETCH}d ago — skipping")
                continue
            info(f"Fetching {sc} — {row['set_name']}")
            set_id = resolve_set_id(conn, game, row)
            if not set_id:
                continue
            total += fetch_set_cards(conn, game, set_id, sc, row["set_name"])
            time.sleep(JUSTTCG_SLEEP)
    except DailyLimit as e:
        warn(f"Stopped early: {e}. Re-run tomorrow to continue (resumable).")

    ok(f"Done — {total} cards stored this run.")
    conn.close()


if __name__ == "__main__":
    main()
