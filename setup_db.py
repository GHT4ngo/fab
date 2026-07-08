"""
Flesh and Blood — One-time database setup.
Creates the 'fab' PostgreSQL database, schemas, and bronze tables.
Run this once before using the pipeline.
"""

import psycopg2
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(dotenv_path=HERE / ".env")

PG_HOST     = os.getenv("PG_HOST", "localhost")
PG_PORT     = int(os.getenv("PG_PORT", 5432))
PG_USER     = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

GREEN = "\033[32m"
CYAN  = "\033[36m"
RED   = "\033[31m"
BOLD  = "\033[1m"
RESET = "\033[0m"

def ok(msg):  print(f"  {GREEN}✔{RESET}  {msg}")
def info(msg): print(f"  {CYAN}→{RESET}  {msg}")
def err(msg):  print(f"  {RED}✘{RESET}  {msg}")


CREATE_TABLES_SQL = """
-- Extension for trigram text search on gold.gold_cards
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS app;

-- ── Bronze: card printings from The Fab Cube ─────────────────────────────────
-- One row per printing (set × edition × foiling).
-- raw_data holds the full flattened JSON so silver can read any field.
CREATE TABLE IF NOT EXISTS bronze.fab_printings (
    printing_unique_id  TEXT        NOT NULL PRIMARY KEY,
    id                  TEXT,               -- Display ID e.g. "MST131"
    set_id              TEXT,               -- Set code e.g. "MST", "WTR"
    edition             TEXT,               -- A=Alpha U=Unlimited F=First N=None
    foiling             TEXT,               -- S=Standard R=Rainbow C=Cold
    rarity              TEXT,               -- C R S M L F T
    raw_data            JSONB       NOT NULL,
    loaded_at           TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS fab_printings_set_id   ON bronze.fab_printings (set_id);
CREATE INDEX IF NOT EXISTS fab_printings_rarity   ON bronze.fab_printings (rarity);
CREATE INDEX IF NOT EXISTS fab_printings_edition  ON bronze.fab_printings (edition);

-- ── Bronze: exchange rates (Riksbank) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bronze.exchange_rates (
    series_id   TEXT        NOT NULL,
    rate_date   DATE        NOT NULL,
    rate_value  NUMERIC     NOT NULL,
    loaded_at   TIMESTAMP   NOT NULL DEFAULT NOW(),
    UNIQUE (series_id, rate_date)
);

-- ── Bronze: Cardmarket product catalogue (game 16 = Flesh and Blood) ─────────
-- One row per product (card name × edition × foiling).
-- idExpansion groups cards by set/edition — can be mapped to (set_id, edition)
-- once the expansion name list is downloaded (see ingest_bronze.py).
CREATE TABLE IF NOT EXISTS bronze.cardmarket_products (
    idproduct       BIGINT      NOT NULL PRIMARY KEY,
    name            TEXT        NOT NULL,
    id_category     INT,
    id_expansion    INT,
    id_metacard     INT,
    date_added      DATE
);

CREATE INDEX IF NOT EXISTS cm_products_name        ON bronze.cardmarket_products (upper(trim(name)));
CREATE INDEX IF NOT EXISTS cm_products_expansion   ON bronze.cardmarket_products (id_expansion);

-- ── Bronze: FaB set catalogue (from The Fab Cube set.json) ───────────────────
-- One row per set × edition; collectors_centre_url drives the scraper.
CREATE TABLE IF NOT EXISTS bronze.fab_sets (
    set_id                  TEXT        NOT NULL,
    edition                 TEXT        NOT NULL,
    set_name                TEXT        NOT NULL,
    collectors_centre_url   TEXT,
    start_card_id           TEXT,
    end_card_id             TEXT,
    initial_release_date    DATE,
    out_of_print            BOOLEAN,
    loaded_at               TIMESTAMP   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (set_id, edition)
);

-- ── Bronze: manual printing → Cardmarket product crosswalk (from SSMS export) ─
-- Covers early/complex sets that the automatic matcher can't resolve.
-- Each printing_unique_id maps to exactly one idproduct + foil type.
CREATE TABLE IF NOT EXISTS bronze.fab_cm_manual (
    printing_unique_id  TEXT    NOT NULL PRIMARY KEY,
    id                  TEXT,           -- Collector number e.g. "ARC001"
    set_id              TEXT,
    edition             TEXT,
    idexpansion         INT,
    idproduct           INT,
    foil                TEXT            -- N, RF, CF, EARF, EACF, AARF, GF, EXRF
);

CREATE INDEX IF NOT EXISTS fab_cm_manual_idproduct ON bronze.fab_cm_manual (idproduct);
CREATE INDEX IF NOT EXISTS fab_cm_manual_set       ON bronze.fab_cm_manual (set_id, edition);

-- ── Bronze: collectors centre card list (scraped from fabtcg.com) ─────────────
-- One row per (card_id, printing_technique). Cards with both foil and Normal
-- variants have two rows. This is the source of truth for foil type per card.
CREATE TABLE IF NOT EXISTS bronze.collectors_centre (
    set_code            TEXT        NOT NULL,
    card_id             TEXT        NOT NULL,   -- e.g. "ELE007"
    card_name           TEXT        NOT NULL,
    printing_technique  TEXT        NOT NULL,   -- "Cold Foil – First Edition" etc.
    notes               TEXT,
    loaded_at           TIMESTAMP   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (set_code, card_id, printing_technique)
);

CREATE INDEX IF NOT EXISTS cc_set_code ON bronze.collectors_centre (set_code);

-- ── Bronze: set+edition → Cardmarket idExpansion mapping ─────────────────────
-- Derived from fab_cm_manual (manual) and auto-matching (programmatic).
-- source: 'manual' = from SSMS CSV, 'auto' = from name-matching heuristic.
CREATE TABLE IF NOT EXISTS bronze.fab_expansions (
    set_id          TEXT        NOT NULL,
    edition         TEXT        NOT NULL,
    idexpansion     INT         NOT NULL,
    source          TEXT        NOT NULL DEFAULT 'manual',
    loaded_at       TIMESTAMP   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (set_id, edition)
);

-- ── Bronze: Cardmarket price guide (game 16 = Flesh and Blood) ───────────────
-- All prices are in EUR.
-- trend     = current non-foil market trend price
-- trend_foil = current foil market trend price
CREATE TABLE IF NOT EXISTS bronze.cardmarket_prices (
    idproduct       BIGINT      NOT NULL,
    avg             NUMERIC,
    low             NUMERIC,
    trend           NUMERIC,
    avg1            NUMERIC,
    avg7            NUMERIC,
    avg30           NUMERIC,
    avg_foil        NUMERIC,
    low_foil        NUMERIC,
    trend_foil      NUMERIC,
    avg1_foil       NUMERIC,
    avg7_foil       NUMERIC,
    avg30_foil      NUMERIC,
    loaded_date     DATE        NOT NULL,
    PRIMARY KEY (idproduct, loaded_date)
);

-- ── Bronze: JustTCG / TCGPlayer prices per printing ──────────────────────────
-- One row per (tcgplayer_product_id x printing x condition).
-- printing matches JustTCG variant strings e.g. "1st Edition Cold Foil", "Normal".
-- fetched_at is updated on each upsert to support staleness checks.
CREATE TABLE IF NOT EXISTS bronze.justtcg_prices (
    tcgplayer_product_id  TEXT        NOT NULL,
    printing              TEXT        NOT NULL,
    condition             TEXT        NOT NULL,
    price_usd             NUMERIC(10,2),
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tcgplayer_product_id, printing, condition)
);

CREATE INDEX IF NOT EXISTS justtcg_prices_tcg_id ON bronze.justtcg_prices (tcgplayer_product_id);

-- ── Bronze: JustTCG set map (user-maintained, loaded from justtcg_sets.csv) ───
-- One row per set that the-fab-cube is missing and we source from JustTCG instead.
-- justtcg_set_id is filled in by ingest_justtcg_sets.py after resolving it via /sets.
-- idexpansion is optional (Cardmarket expansion id) for precise EUR pricing.
CREATE TABLE IF NOT EXISTS bronze.justtcg_set_map (
    set_code        TEXT        PRIMARY KEY,
    set_name        TEXT        NOT NULL,
    edition         TEXT        NOT NULL DEFAULT 'N',
    release_date    DATE,
    justtcg_set_id  TEXT,
    idexpansion     INTEGER
);

-- ── Bronze: JustTCG card catalogue for missing sets ──────────────────────────
-- One row per TCGplayer product (card printing) discovered via JustTCG /cards.
-- Prices for these cards live in bronze.justtcg_prices (shared with the price refresh).
CREATE TABLE IF NOT EXISTS bronze.justtcg_cards (
    tcgplayer_product_id  TEXT        PRIMARY KEY,
    name                  TEXT        NOT NULL,
    pitch                 TEXT,
    number                TEXT,
    rarity                TEXT,
    set_code              TEXT        NOT NULL,
    set_name              TEXT,
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS justtcg_cards_set_code ON bronze.justtcg_cards (set_code);

-- ── Bronze: tcgcsv.com set list (TCGplayer groups, FaB = category 62) ─────────
-- One row per set. abbreviation is the TCGplayer set code (e.g. OMN, PEN).
CREATE TABLE IF NOT EXISTS bronze.tcgcsv_groups (
    group_id        INTEGER     NOT NULL PRIMARY KEY,
    abbreviation    TEXT,
    name            TEXT,
    is_supplemental BOOLEAN,
    published_on    TIMESTAMPTZ,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Bronze: tcgcsv.com card catalogue (one row per TCGplayer single product) ──
-- Primary source for USD prices (join product_id = tcgplayer_product_id) and for
-- cards in sets the-fab-cube is missing (folded into silver as match_tier = 4).
-- raw_data holds the full product JSON (incl. extendedData) for any field silver needs.
CREATE TABLE IF NOT EXISTS bronze.tcgcsv_cards (
    product_id   BIGINT      NOT NULL PRIMARY KEY,
    group_id     INTEGER,
    set_code     TEXT,                  -- e.g. "OMN" (from Number prefix / group abbr)
    number       TEXT,                  -- e.g. "OMN001"
    name         TEXT,
    clean_name   TEXT,
    rarity       TEXT,
    card_type    TEXT,
    class        TEXT,
    talent       TEXT,
    cost         TEXT,
    pitch        TEXT,
    power        TEXT,
    defense      TEXT,
    life         TEXT,
    intellect    TEXT,
    description  TEXT,                  -- card functional text (may contain HTML)
    image_url    TEXT,
    raw_data     JSONB,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tcgcsv_cards_set_code ON bronze.tcgcsv_cards (set_code);
CREATE INDEX IF NOT EXISTS tcgcsv_cards_group    ON bronze.tcgcsv_cards (group_id);

-- ── Bronze: tcgcsv.com USD prices (one row per product × sub_type) ────────────
-- sub_type matches TCGplayer's subTypeName: Normal / Cold Foil / Rainbow Foil /
-- Gold Foil. market_price is the USD figure silver uses.
CREATE TABLE IF NOT EXISTS bronze.tcgcsv_prices (
    product_id        BIGINT      NOT NULL,
    sub_type          TEXT        NOT NULL,
    low_price         NUMERIC(10,2),
    mid_price         NUMERIC(10,2),
    high_price        NUMERIC(10,2),
    market_price      NUMERIC(10,2),
    direct_low_price  NUMERIC(10,2),
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (product_id, sub_type)
);

CREATE INDEX IF NOT EXISTS tcgcsv_prices_product ON bronze.tcgcsv_prices (product_id);

-- ── App: phone/native scanner captures ──────────────────────────────────────
-- Append-only scan log. printing_unique_id points at gold.gold_cards when the
-- scanner can resolve a concrete printing; name-only visual fallback is still
-- stored for review with printing_unique_id NULL.
CREATE TABLE IF NOT EXISTS app.scanned_cards (
    scan_id             BIGSERIAL   PRIMARY KEY,
    scanned_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scanner             TEXT        NOT NULL DEFAULT 'native_android',
    printing_unique_id  TEXT,
    display_id          TEXT,
    name                TEXT,
    method              TEXT,
    confidence          NUMERIC(5,3),
    raw_text            TEXT,
    debug_paths         TEXT[],
    response            JSONB
);

CREATE INDEX IF NOT EXISTS scanned_cards_scanned_at ON app.scanned_cards (scanned_at DESC);
CREATE INDEX IF NOT EXISTS scanned_cards_printing   ON app.scanned_cards (printing_unique_id);
CREATE INDEX IF NOT EXISTS scanned_cards_display_id ON app.scanned_cards (display_id);

-- ── App: email accounts + named cardlists (Phase 2) ──────────────────────────
-- Portable account = email. Magic-link auth (passwordless): request a link, verify
-- it to mint a session token. Named cardlists belong to a user and hold printings
-- (printing_unique_id → gold.gold_cards) with quantities. api.py self-migrates the
-- same DDL at startup (ensure_app_auth_schema), so this stays the canonical copy.
CREATE TABLE IF NOT EXISTS app.users (
    user_id        BIGSERIAL   PRIMARY KEY,
    email          TEXT        NOT NULL UNIQUE,
    username       TEXT        UNIQUE,
    password_hash  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at  TIMESTAMPTZ
);
ALTER TABLE app.users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE app.users ADD COLUMN IF NOT EXISTS password_hash TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS users_username_unique
    ON app.users (lower(username)) WHERE username IS NOT NULL;

CREATE TABLE IF NOT EXISTS app.magic_tokens (
    token       TEXT        PRIMARY KEY,
    email       TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS magic_tokens_email ON app.magic_tokens (email);

CREATE TABLE IF NOT EXISTS app.sessions (
    session_token TEXT        PRIMARY KEY,
    user_id       BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS sessions_user ON app.sessions (user_id);

CREATE TABLE IF NOT EXISTS app.cardlists (
    cardlist_id BIGSERIAL   PRIMARY KEY,
    user_id     BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS cardlists_user ON app.cardlists (user_id);

CREATE TABLE IF NOT EXISTS app.cardlist_items (
    item_id            BIGSERIAL   PRIMARY KEY,
    cardlist_id        BIGINT      NOT NULL REFERENCES app.cardlists(cardlist_id) ON DELETE CASCADE,
    printing_unique_id TEXT        NOT NULL,
    qty                INTEGER     NOT NULL DEFAULT 1,
    added_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cardlist_id, printing_unique_id)
);
CREATE INDEX IF NOT EXISTS cardlist_items_list ON app.cardlist_items (cardlist_id);

-- ── App: Phase 4 trading — trade-flagged lists + offers ──────────────────────
-- A cardlist with is_trade_list = true is publicly browsable via /trade/listings.
-- Offers hold give/want item bundles; value_sek snapshots gold.trade_value_sek
-- ("trend, or low if higher") at send time. Accepting records the deal only —
-- the physical swap happens in person.
ALTER TABLE app.cardlists ADD COLUMN IF NOT EXISTS is_trade_list BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS app.trade_offers (
    offer_id     BIGSERIAL   PRIMARY KEY,
    from_user_id BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    to_user_id   BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    status       TEXT        NOT NULL DEFAULT 'pending',
    kind         TEXT        NOT NULL DEFAULT 'cards',
    offer_list_id BIGINT,
    offer_list_name TEXT,
    offer_list_total_sek NUMERIC,
    request_list_id BIGINT,
    request_list_name TEXT,
    request_list_total_sek NUMERIC,
    message      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE app.trade_offers ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'cards';
ALTER TABLE app.trade_offers ADD COLUMN IF NOT EXISTS offer_list_id BIGINT;
ALTER TABLE app.trade_offers ADD COLUMN IF NOT EXISTS offer_list_name TEXT;
ALTER TABLE app.trade_offers ADD COLUMN IF NOT EXISTS offer_list_total_sek NUMERIC;
ALTER TABLE app.trade_offers ADD COLUMN IF NOT EXISTS request_list_id BIGINT;
ALTER TABLE app.trade_offers ADD COLUMN IF NOT EXISTS request_list_name TEXT;
ALTER TABLE app.trade_offers ADD COLUMN IF NOT EXISTS request_list_total_sek NUMERIC;
CREATE INDEX IF NOT EXISTS trade_offers_to   ON app.trade_offers (to_user_id, status);
CREATE INDEX IF NOT EXISTS trade_offers_from ON app.trade_offers (from_user_id, status);

CREATE TABLE IF NOT EXISTS app.trade_offer_items (
    item_id            BIGSERIAL PRIMARY KEY,
    offer_id           BIGINT    NOT NULL REFERENCES app.trade_offers(offer_id) ON DELETE CASCADE,
    side               TEXT      NOT NULL,
    printing_unique_id TEXT      NOT NULL,
    qty                INTEGER   NOT NULL DEFAULT 1,
    base_value_sek     NUMERIC,
    value_sek          NUMERIC,
    discount_type      TEXT,
    discount_value     NUMERIC,
    UNIQUE (offer_id, side, printing_unique_id)
);
ALTER TABLE app.trade_offer_items ADD COLUMN IF NOT EXISTS base_value_sek NUMERIC;
ALTER TABLE app.trade_offer_items ADD COLUMN IF NOT EXISTS discount_type TEXT;
ALTER TABLE app.trade_offer_items ADD COLUMN IF NOT EXISTS discount_value NUMERIC;
"""


def main():
    print()
    print(CYAN + BOLD + "  Flesh and Blood — Database Setup" + RESET)
    print(CYAN + "─" * 50 + RESET)
    print()

    # ── Step 1: create the database ──────────────────────────────────────────
    info("Connecting to PostgreSQL (postgres DB)...")
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            database="postgres",
            user=PG_USER, password=PG_PASSWORD,
        )
        conn.autocommit = True
    except Exception as e:
        err(f"Connection failed: {e}")
        sys.exit(1)

    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = 'fab'")
    if cur.fetchone():
        ok("Database 'fab' already exists")
    else:
        cur.execute("CREATE DATABASE fab ENCODING 'UTF8'")
        ok("Created database 'fab'")
    cur.close()
    conn.close()

    # ── Step 2: create schemas and tables ────────────────────────────────────
    info("Creating schemas and tables in 'fab'...")
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            database="fab",
            user=PG_USER, password=PG_PASSWORD,
        )
        cur = conn.cursor()
        cur.execute(CREATE_TABLES_SQL)
        conn.commit()
    except Exception as e:
        err(f"Table creation failed: {e}")
        sys.exit(1)

    ok("schema: bronze, silver, gold")
    ok("bronze.fab_printings")
    ok("bronze.exchange_rates")
    ok("bronze.cardmarket_products")
    ok("bronze.cardmarket_prices")
    ok("bronze.fab_sets")
    ok("bronze.fab_cm_manual")
    ok("bronze.collectors_centre")
    ok("bronze.fab_expansions")
    ok("bronze.justtcg_prices")
    ok("bronze.justtcg_set_map")
    ok("bronze.justtcg_cards")
    ok("bronze.tcgcsv_groups")
    ok("bronze.tcgcsv_cards")
    ok("bronze.tcgcsv_prices")
    ok("app.scanned_cards / scan_sessions")
    ok("app.users / magic_tokens / sessions")
    ok("app.cardlists / cardlist_items")
    ok("app.trade_offers / trade_offer_items")
    ok("extension: pg_trgm")
    cur.close()
    conn.close()

    print()
    print(GREEN + BOLD + "  Setup complete — run ingest_bronze.py next." + RESET)
    print()


if __name__ == "__main__":
    main()
