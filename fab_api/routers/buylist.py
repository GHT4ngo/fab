"""Buylist — "sell your cards to the store" (2026-07-30).

One designated account (the store: username `t4ngo`, override with
BUYLIST_STORE_USERNAME, else the lowest-id is_dev user) publishes a **buy
policy**: a base % of market value, per-condition and per-language multipliers,
a minimum card value, and rules that BLOCK or re-price whole slices of the
catalogue (rarity / foiling / edition / set). Sellers build a list — every line
pinned to one exact printing plus its condition and language — and get a live,
non-binding quote from that policy. Sending it creates a `sell_offer`.

Nothing is a deal until the store says so. The offer is a turn-based
negotiation exactly like `trade.py`: `awaiting_user_id` says whose move it is.
The store can `counter` (re-price or drop individual lines, add a note) which
flips the turn back to the seller, or `accept` / `decline` outright. Once
accepted, either side can `complete` (money changed hands) or `cancel` (the
meet-up fell through).

Valuation basis: `gold.trade_value_sek` (greatest of CM trend / CM low, USD
fallback) with `price_sek` as a backstop — the same number the trading post
uses, so a card is worth the same whether you trade it or sell it.
"""

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from psycopg2.extras import Json
from pydantic import BaseModel

from fab_api.core import get_conn, send_email_async
from fab_api.routers.auth import _current_user, _public_base_url

router = APIRouter()

APP_BUYLIST_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.buylist_settings (
    user_id       BIGINT      PRIMARY KEY REFERENCES app.users(user_id) ON DELETE CASCADE,
    enabled       BOOLEAN     NOT NULL DEFAULT TRUE,
    base_pct      NUMERIC     NOT NULL DEFAULT 60,
    min_card_sek  NUMERIC     NOT NULL DEFAULT 0,
    condition_pct JSONB       NOT NULL DEFAULT '{}'::jsonb,
    language_pct  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    notes         TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app.buylist_rules (
    rule_id    BIGSERIAL   PRIMARY KEY,
    user_id    BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    field      TEXT        NOT NULL,               -- rarity|foiling|edition|set_id|condition|language
    value      TEXT        NOT NULL,
    action     TEXT        NOT NULL DEFAULT 'block',  -- block | pct
    pct        NUMERIC,
    max_value_sek NUMERIC,                         -- rule only bites at/below this market value
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, field, value)
);
ALTER TABLE app.buylist_rules ADD COLUMN IF NOT EXISTS max_value_sek NUMERIC;
CREATE INDEX IF NOT EXISTS buylist_rules_user ON app.buylist_rules (user_id);

CREATE TABLE IF NOT EXISTS app.sell_offers (
    sell_offer_id     BIGSERIAL   PRIMARY KEY,
    seller_user_id    BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    buyer_user_id     BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    status            TEXT        NOT NULL DEFAULT 'submitted',
    awaiting_user_id  BIGINT,
    seller_message    TEXT,
    buyer_message     TEXT,
    quoted_total_sek  NUMERIC,
    offered_total_sek NUMERIC,
    policy_snapshot   JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at       TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sell_offers_seller ON app.sell_offers (seller_user_id, status);
CREATE INDEX IF NOT EXISTS sell_offers_buyer  ON app.sell_offers (buyer_user_id, status);

CREATE TABLE IF NOT EXISTS app.sell_offer_items (
    item_id            BIGSERIAL   PRIMARY KEY,
    sell_offer_id      BIGINT      NOT NULL REFERENCES app.sell_offers(sell_offer_id) ON DELETE CASCADE,
    printing_unique_id TEXT        NOT NULL,
    qty                INTEGER     NOT NULL DEFAULT 1,
    card_condition     TEXT        NOT NULL DEFAULT 'NM',
    card_language      TEXT        NOT NULL DEFAULT 'EN',
    market_sek         NUMERIC,
    quoted_sek         NUMERIC,
    offered_sek        NUMERIC,
    accepted           BOOLEAN     NOT NULL DEFAULT TRUE,
    note               TEXT,
    added_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS sell_offer_items_offer ON app.sell_offer_items (sell_offer_id);
"""

_buylist_schema_ready = False


def ensure_app_buylist_schema():
    global _buylist_schema_ready
    if _buylist_schema_ready:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(APP_BUYLIST_SCHEMA_SQL)
    _buylist_schema_ready = True


# ── Catalogues (server-driven so the UI never invents its own codes) ─────────

CONDITIONS = [
    {"code": "NM",  "label": "Near Mint"},
    {"code": "LP",  "label": "Lightly Played"},
    {"code": "MP",  "label": "Moderately Played"},
    {"code": "HP",  "label": "Heavily Played"},
    {"code": "DMG", "label": "Damaged"},
]
DEFAULT_CONDITION_PCT = {"NM": 100, "LP": 90, "MP": 75, "HP": 55, "DMG": 30}

LANGUAGES = [
    {"code": "EN",    "label": "English"},
    {"code": "FR",    "label": "French"},
    {"code": "DE",    "label": "German"},
    {"code": "IT",    "label": "Italian"},
    {"code": "ES",    "label": "Spanish"},
    {"code": "JP",    "label": "Japanese"},
    {"code": "ZH",    "label": "Chinese"},
    {"code": "OTHER", "label": "Other"},
]
DEFAULT_LANGUAGE_PCT = {"EN": 100, "FR": 85, "DE": 85, "IT": 85, "ES": 85,
                        "JP": 90, "ZH": 80, "OTHER": 70}

# gold.rarity carries both short codes (the-fab-cube) and long names (tcgcsv) for
# the same rarity — collapse them so one blacklist rule catches both spellings.
RARITY_ALIASES = {
    "c": "common", "common": "common", "basic": "basic", "b": "basic",
    "r": "rare", "rare": "rare",
    "s": "super_rare", "super rare": "super_rare",
    "m": "majestic", "majestic": "majestic",
    "l": "legendary", "legendary": "legendary",
    "f": "fabled", "fabled": "fabled",
    "v": "marvel", "marvel": "marvel",
    "p": "promo", "promo": "promo",
    "t": "token", "token": "token",
    "none": "none",
}
RARITIES = [
    {"code": "common", "label": "Common"},
    {"code": "rare", "label": "Rare"},
    {"code": "super_rare", "label": "Super Rare"},
    {"code": "majestic", "label": "Majestic"},
    {"code": "legendary", "label": "Legendary"},
    {"code": "fabled", "label": "Fabled"},
    {"code": "marvel", "label": "Marvel"},
    {"code": "promo", "label": "Promo"},
    {"code": "token", "label": "Token"},
    {"code": "basic", "label": "Basic"},
    {"code": "other", "label": "Other / unclassified"},
]

FOILINGS = [
    {"code": "S", "label": "Non-foil"},
    {"code": "R", "label": "Rainbow Foil"},
    {"code": "C", "label": "Cold Foil"},
    {"code": "G", "label": "Gold Foil"},
]
EDITIONS = [
    {"code": "F", "label": "1st Edition"},
    {"code": "U", "label": "Unlimited"},
    {"code": "A", "label": "Alpha"},
    {"code": "N", "label": "Standard / only printing"},
]

RULE_FIELDS = ["rarity", "foiling", "edition", "set_id", "condition", "language"]

OPEN_STATUSES = ("submitted", "countered")


def _norm_rarity(raw: Optional[str]) -> str:
    return RARITY_ALIASES.get((raw or "").strip().lower(), "other")


def _norm_condition(raw: Optional[str]) -> str:
    code = (raw or "NM").strip().upper()
    return code if code in DEFAULT_CONDITION_PCT else "NM"


def _norm_language(raw: Optional[str]) -> str:
    code = (raw or "EN").strip().upper()
    return code if code in DEFAULT_LANGUAGE_PCT else "OTHER"


def _label(catalog: list[dict], code: str) -> str:
    for entry in catalog:
        if entry["code"] == code:
            return entry["label"]
    return code


# ── The store account + its policy ───────────────────────────────────────────

STORE_USERNAME = os.getenv("BUYLIST_STORE_USERNAME", "t4ngo")


def _store_user(cur) -> Optional[dict]:
    """Who buys cards. The named store account if it exists, else the oldest
    dev account (so a fresh install still has a working buylist)."""
    cur.execute(
        "SELECT user_id, username, email FROM app.users WHERE lower(username) = lower(%s)",
        [STORE_USERNAME],
    )
    row = cur.fetchone()
    if row:
        return dict(row)
    cur.execute(
        "SELECT user_id, username, email FROM app.users WHERE is_dev ORDER BY user_id LIMIT 1"
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _require_store(cur) -> dict:
    store = _store_user(cur)
    if not store:
        raise HTTPException(status_code=503, detail="No buylist store account exists yet")
    return store


def _policy(cur, store_user_id: int) -> dict:
    """Settings + rules for the store, with the default rate cards merged in so
    a key the owner never touched still prices sensibly."""
    cur.execute(
        """
        INSERT INTO app.buylist_settings (user_id) VALUES (%s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        [store_user_id],
    )
    cur.execute("SELECT * FROM app.buylist_settings WHERE user_id = %s", [store_user_id])
    s = dict(cur.fetchone())
    condition_pct = {**DEFAULT_CONDITION_PCT, **(s.get("condition_pct") or {})}
    language_pct = {**DEFAULT_LANGUAGE_PCT, **(s.get("language_pct") or {})}

    cur.execute(
        "SELECT rule_id, field, value, action, pct, max_value_sek FROM app.buylist_rules "
        "WHERE user_id = %s ORDER BY field, value",
        [store_user_id],
    )
    rules = [{
        "rule_id": r["rule_id"],
        "field": r["field"],
        "value": r["value"],
        "action": r["action"],
        "pct": float(r["pct"]) if r["pct"] is not None else None,
        "max_value_sek": float(r["max_value_sek"]) if r["max_value_sek"] is not None else None,
        "label": _rule_label(r["field"], r["value"]),
    } for r in cur.fetchall()]

    return {
        "user_id": store_user_id,
        "enabled": bool(s["enabled"]),
        "base_pct": float(s["base_pct"]),
        "min_card_sek": float(s["min_card_sek"]),
        "condition_pct": {k: float(v) for k, v in condition_pct.items()},
        "language_pct": {k: float(v) for k, v in language_pct.items()},
        "notes": s.get("notes"),
        "rules": rules,
        "updated_at": s["updated_at"].isoformat() if s.get("updated_at") else None,
    }


def _rule_label(field: str, value: str) -> str:
    if field == "rarity":
        return _label(RARITIES, value)
    if field == "foiling":
        return _label(FOILINGS, value)
    if field == "edition":
        return _label(EDITIONS, value)
    if field == "condition":
        return _label(CONDITIONS, value)
    if field == "language":
        return _label(LANGUAGES, value)
    return value


# ── Quoting ──────────────────────────────────────────────────────────────────

def _market_sek(card: dict) -> Optional[float]:
    for key in ("trade_value_sek", "price_sek"):
        v = card.get(key)
        if v is not None and float(v) > 0:
            return float(v)
    return None


def _quote_card(policy: dict, card: dict, condition: str, language: str) -> dict:
    """Price ONE copy under the policy. Always explains itself — the seller sees
    exactly why a card is worth what it is, or why it isn't bought at all."""
    market = _market_sek(card)
    rarity = _norm_rarity(card.get("rarity"))
    attrs = {
        "rarity": rarity,
        "foiling": (card.get("foiling") or "").upper(),
        "edition": (card.get("edition") or "").upper(),
        "set_id": (card.get("set_id") or "").upper(),
        "condition": condition,
        "language": language,
    }

    blocked_by: list[str] = []
    rule_pcts: list[dict] = []
    for rule in policy["rules"]:
        want = (rule["value"] or "").strip()
        got = attrs.get(rule["field"])
        if got is None or got.lower() != want.lower():
            continue
        # A value-scoped rule only bites on cheap copies: "Commons at or under
        # 5 kr" must not touch a 208 kr Cold Foil common.
        cap = rule.get("max_value_sek")
        if cap is not None and (market is None or market > cap):
            continue
        scope = f" at or under {int(cap)} kr" if cap is not None else ""
        if rule["action"] == "block":
            blocked_by.append(f"{rule['field'].replace('_', ' ')}: {rule['label']}{scope}")
        elif rule["pct"] is not None:
            rule_pcts.append(rule)

    condition_pct = policy["condition_pct"].get(condition, 100.0)
    language_pct = policy["language_pct"].get(language, 100.0)

    reasons: list[str] = []
    accepted = True
    if not policy["enabled"]:
        accepted = False
        reasons.append("The buylist is closed right now")
    if market is None:
        accepted = False
        reasons.append("No market price on record — ask the store directly")
    if blocked_by:
        accepted = False
        reasons.append("Not bought — " + "; ".join(blocked_by))
    if market is not None and market < policy["min_card_sek"]:
        accepted = False
        reasons.append(f"Under the {int(policy['min_card_sek'])} kr minimum card value")

    unit = 0.0
    pct_total = policy["base_pct"] / 100 * condition_pct / 100 * language_pct / 100
    for rule in rule_pcts:
        pct_total *= rule["pct"] / 100
    if accepted and market is not None:
        unit = round(market * pct_total)
        if unit <= 0:
            accepted = False
            reasons.append("Rounds to 0 kr at the current rate")

    return {
        "market_sek": round(market) if market is not None else None,
        "unit_sek": int(unit) if accepted else 0,
        "accepted": accepted,
        "reasons": reasons,
        "effective_pct": round(pct_total * 100, 1),
        "breakdown": {
            "base_pct": policy["base_pct"],
            "condition_pct": condition_pct,
            "language_pct": language_pct,
            "rule_pcts": [{"label": r["label"], "pct": r["pct"]} for r in rule_pcts],
        },
    }


_CARD_SQL = """
    SELECT g.printing_unique_id, g.display_id, g.name, g.set_id,
           coalesce(s.set_name, g.set_id) AS set_name,
           g.edition, g.foiling, g.rarity, g.variant, g.pitch, g.image_url,
           g.trade_value_sek, g.price_sek
    FROM gold.gold_cards g
    LEFT JOIN bronze.fab_sets s ON s.set_id = g.set_id AND s.edition = g.edition
    WHERE g.printing_unique_id = ANY(%s)
"""


def _load_cards(cur, printing_ids: list[str]) -> dict[str, dict]:
    if not printing_ids:
        return {}
    cur.execute(_CARD_SQL, [printing_ids])
    return {r["printing_unique_id"]: dict(r) for r in cur.fetchall()}


def _card_display(card: dict) -> dict:
    """The fields the UI needs to show a card unambiguously."""
    return {
        "name": card.get("name"),
        "display_id": card.get("display_id"),
        "set_id": card.get("set_id"),
        "set_name": card.get("set_name"),
        "edition": card.get("edition"),
        "edition_label": _label(EDITIONS, (card.get("edition") or "").upper()),
        "foiling": card.get("foiling"),
        "foiling_label": _label(FOILINGS, (card.get("foiling") or "").upper()),
        "rarity": card.get("rarity"),
        "rarity_label": _label(RARITIES, _norm_rarity(card.get("rarity"))),
        "variant": card.get("variant"),
        "pitch": card.get("pitch"),
        "image_url": card.get("image_url"),
    }


# ── Models ───────────────────────────────────────────────────────────────────

class QuoteItem(BaseModel):
    printing_unique_id: str
    qty: int = 1
    condition: str = "NM"
    language: str = "EN"


class QuoteRequest(BaseModel):
    items: list[QuoteItem] = []


class SellOfferCreate(BaseModel):
    items: list[QuoteItem] = []
    message: Optional[str] = None


class SettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    base_pct: Optional[float] = None
    min_card_sek: Optional[float] = None
    condition_pct: Optional[dict[str, float]] = None
    language_pct: Optional[dict[str, float]] = None
    notes: Optional[str] = None


class RuleCreate(BaseModel):
    field: str
    value: str
    action: str = "block"
    pct: Optional[float] = None
    max_value_sek: Optional[float] = None   # rule only applies at/below this market value


class ItemUpdate(BaseModel):
    item_id: int
    offered_sek: Optional[float] = None   # per unit
    qty: Optional[int] = None
    accepted: Optional[bool] = None
    note: Optional[str] = None


class SellOfferAction(BaseModel):
    action: str                            # accept | decline | cancel | complete | counter
    item_updates: list[ItemUpdate] = []
    message: Optional[str] = None


# ── Public: store front + live quote ─────────────────────────────────────────

@router.get("/buylist/store")
def buylist_store():
    """The shop window: who buys, at what rates, and what they won't take.
    Public so the Sell page can show the terms before anyone signs in."""
    ensure_app_buylist_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            store = _store_user(cur)
            policy = _policy(cur, store["user_id"]) if store else None
    return {
        "store": {
            "user_id": store["user_id"],
            "username": store["username"],
        } if store else None,
        "policy": policy,
        "conditions": CONDITIONS,
        "languages": LANGUAGES,
        "rarities": RARITIES,
        "foilings": FOILINGS,
        "editions": EDITIONS,
        "rule_fields": RULE_FIELDS,
    }


@router.post("/buylist/quote")
def buylist_quote(req: QuoteRequest):
    """Non-binding price estimate for a basket. Every line is quoted on its own
    exact printing + condition + language, and carries the reasons behind the
    number. Public: no sign-in needed to see what your cards are worth."""
    ensure_app_buylist_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            store = _require_store(cur)
            policy = _policy(cur, store["user_id"])
            cards = _load_cards(cur, [(i.printing_unique_id or "").strip() for i in req.items])

    lines = []
    total = 0
    accepted_count = 0
    for it in req.items:
        pid = (it.printing_unique_id or "").strip()
        qty = max(1, min(999, it.qty))
        condition = _norm_condition(it.condition)
        language = _norm_language(it.language)
        card = cards.get(pid)
        if not card:
            lines.append({
                "printing_unique_id": pid, "qty": qty,
                "condition": condition, "language": language,
                "market_sek": None, "unit_sek": 0, "line_sek": 0,
                "accepted": False, "reasons": ["Unknown card printing"],
                "effective_pct": 0, "breakdown": None,
            })
            continue
        q = _quote_card(policy, card, condition, language)
        line_total = q["unit_sek"] * qty if q["accepted"] else 0
        total += line_total
        accepted_count += qty if q["accepted"] else 0
        lines.append({
            "printing_unique_id": pid,
            "qty": qty,
            "condition": condition,
            "condition_label": _label(CONDITIONS, condition),
            "language": language,
            "language_label": _label(LANGUAGES, language),
            "line_sek": line_total,
            **q,
            **_card_display(card),
        })
    return {
        "store": {"user_id": store["user_id"], "username": store["username"]},
        "enabled": policy["enabled"],
        "items": lines,
        "total_sek": int(total),
        "accepted_cards": accepted_count,
        "rejected_lines": sum(1 for l in lines if not l["accepted"]),
        "binding": False,
        "notice": "Estimate only — nothing is agreed until the store accepts or counters your offer.",
    }


# ── Store settings (owner only) ──────────────────────────────────────────────

def _require_store_owner(cur, user: dict) -> dict:
    store = _require_store(cur)
    if store["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="Only the store account can change the buylist")
    return store


@router.get("/buylist/settings")
def buylist_settings_get(user: dict = Depends(_current_user)):
    ensure_app_buylist_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            store = _require_store_owner(cur, user)
            policy = _policy(cur, store["user_id"])
    return {"store": {"user_id": store["user_id"], "username": store["username"]}, "policy": policy}


@router.put("/buylist/settings")
def buylist_settings_put(req: SettingsUpdate, user: dict = Depends(_current_user)):
    """Update the rate card. Percentages are clamped to 0-1000 so a stray keypress
    can't quietly offer 100× market."""
    ensure_app_buylist_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            store = _require_store_owner(cur, user)
            _policy(cur, store["user_id"])   # make sure the row exists

            sets, params = [], []
            if req.enabled is not None:
                sets.append("enabled = %s")
                params.append(req.enabled)
            if req.base_pct is not None:
                sets.append("base_pct = %s")
                params.append(max(0.0, min(1000.0, float(req.base_pct))))
            if req.min_card_sek is not None:
                sets.append("min_card_sek = %s")
                params.append(max(0.0, float(req.min_card_sek)))
            if req.condition_pct is not None:
                clean = {_norm_condition(k): max(0.0, min(1000.0, float(v)))
                         for k, v in req.condition_pct.items()}
                sets.append("condition_pct = %s")
                params.append(Json(clean))
            if req.language_pct is not None:
                clean = {_norm_language(k): max(0.0, min(1000.0, float(v)))
                         for k, v in req.language_pct.items()}
                sets.append("language_pct = %s")
                params.append(Json(clean))
            if req.notes is not None:
                sets.append("notes = %s")
                params.append((req.notes or "").strip()[:2000] or None)
            if not sets:
                return JSONResponse(status_code=400, content={"error": "Nothing to update"})

            params.append(store["user_id"])
            cur.execute(
                f"UPDATE app.buylist_settings SET {', '.join(sets)}, updated_at = NOW() WHERE user_id = %s",
                params,
            )
            policy = _policy(cur, store["user_id"])
    return {"policy": policy}


@router.post("/buylist/rules")
def buylist_rule_add(req: RuleCreate, user: dict = Depends(_current_user)):
    """Add a blacklist ("never buy Gold Foil") or a re-price rule
    ("Commons at 20% of the base rate")."""
    ensure_app_buylist_schema()
    field = (req.field or "").strip().lower()
    if field not in RULE_FIELDS:
        return JSONResponse(status_code=400, content={"error": f"field must be one of {', '.join(RULE_FIELDS)}"})
    value = (req.value or "").strip()
    if not value:
        return JSONResponse(status_code=400, content={"error": "value is required"})
    if field == "rarity":
        value = _norm_rarity(value)
    elif field in ("foiling", "edition", "set_id"):
        value = value.upper()
    elif field == "condition":
        value = _norm_condition(value)
    elif field == "language":
        value = _norm_language(value)

    action = (req.action or "block").strip().lower()
    if action not in ("block", "pct"):
        action = "block"
    pct = None
    if action == "pct":
        if req.pct is None:
            return JSONResponse(status_code=400, content={"error": "A % rule needs a percentage"})
        pct = max(0.0, min(1000.0, float(req.pct)))
    cap = None if req.max_value_sek is None else max(0.0, float(req.max_value_sek))

    with get_conn() as conn:
        with conn.cursor() as cur:
            store = _require_store_owner(cur, user)
            cur.execute(
                """
                INSERT INTO app.buylist_rules (user_id, field, value, action, pct, max_value_sek)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, field, value)
                DO UPDATE SET action = EXCLUDED.action, pct = EXCLUDED.pct,
                              max_value_sek = EXCLUDED.max_value_sek
                RETURNING rule_id
                """,
                [store["user_id"], field, value, action, pct, cap],
            )
            cur.fetchone()
            policy = _policy(cur, store["user_id"])
    return {"policy": policy}


@router.delete("/buylist/rules/{rule_id}")
def buylist_rule_delete(rule_id: int, user: dict = Depends(_current_user)):
    ensure_app_buylist_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            store = _require_store_owner(cur, user)
            cur.execute(
                "DELETE FROM app.buylist_rules WHERE rule_id = %s AND user_id = %s",
                [rule_id, store["user_id"]],
            )
            policy = _policy(cur, store["user_id"])
    return {"policy": policy}


# ── Sell offers ──────────────────────────────────────────────────────────────

_OFFER_BASE_SQL = """
    SELECT o.*,
           su.email AS seller_email, su.username AS seller_username,
           bu.email AS buyer_email,  bu.username AS buyer_username
    FROM app.sell_offers o
    JOIN app.users su ON su.user_id = o.seller_user_id
    JOIN app.users bu ON bu.user_id = o.buyer_user_id
"""


def _offer_dict(cur, row: dict) -> dict:
    cur.execute(
        """
        SELECT i.item_id, i.printing_unique_id, i.qty, i.card_condition, i.card_language,
               i.market_sek, i.quoted_sek, i.offered_sek, i.accepted, i.note,
               g.name, g.display_id, g.set_id, g.edition, g.foiling, g.rarity,
               g.variant, g.pitch, g.image_url,
               coalesce(s.set_name, g.set_id) AS set_name
        FROM app.sell_offer_items i
        LEFT JOIN gold.gold_cards g ON g.printing_unique_id = i.printing_unique_id
        LEFT JOIN bronze.fab_sets s ON s.set_id = g.set_id AND s.edition = g.edition
        WHERE i.sell_offer_id = %s
        ORDER BY i.item_id
        """,
        [row["sell_offer_id"]],
    )
    items = []
    quoted_total = 0
    offered_total = 0
    for r in cur.fetchall():
        d = dict(r)
        unit_quoted = float(d["quoted_sek"]) if d["quoted_sek"] is not None else 0.0
        unit_offered = float(d["offered_sek"]) if d["offered_sek"] is not None else unit_quoted
        line = int(round(unit_offered * d["qty"])) if d["accepted"] else 0
        quoted_total += int(round(unit_quoted * d["qty"])) if d["accepted"] else 0
        offered_total += line
        items.append({
            "item_id": d["item_id"],
            "printing_unique_id": d["printing_unique_id"],
            "qty": d["qty"],
            "condition": d["card_condition"],
            "condition_label": _label(CONDITIONS, d["card_condition"]),
            "language": d["card_language"],
            "language_label": _label(LANGUAGES, d["card_language"]),
            "market_sek": int(round(float(d["market_sek"]))) if d["market_sek"] is not None else None,
            "quoted_sek": int(round(unit_quoted)),
            "offered_sek": int(round(unit_offered)),
            "line_sek": line,
            "accepted": bool(d["accepted"]),
            "note": d["note"],
            **_card_display(d),
        })

    return {
        "sell_offer_id": row["sell_offer_id"],
        "seller_user_id": row["seller_user_id"],
        "seller_username": row.get("seller_username"),
        "seller_email": row.get("seller_email"),
        "buyer_user_id": row["buyer_user_id"],
        "buyer_username": row.get("buyer_username"),
        "buyer_email": row.get("buyer_email"),
        "status": row["status"],
        "awaiting_user_id": row.get("awaiting_user_id"),
        "seller_message": row.get("seller_message"),
        "buyer_message": row.get("buyer_message"),
        "quoted_total_sek": int(round(float(row["quoted_total_sek"]))) if row.get("quoted_total_sek") is not None else quoted_total,
        "offered_total_sek": offered_total,
        "policy_snapshot": row.get("policy_snapshot"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "accepted_at": row["accepted_at"].isoformat() if row.get("accepted_at") else None,
        "completed_at": row["completed_at"].isoformat() if row.get("completed_at") else None,
        "items": items,
    }


def _kr(v) -> str:
    return f"{int(round(float(v))):,} kr".replace(",", " ") if v is not None else "—"


def _offer_email_html(offer: dict, intro: str) -> str:
    rows = "".join(
        f"<li>{i['name'] or i['printing_unique_id']} "
        f"({i['set_id']} {i['display_id']} · {i['foiling_label']} · {i['condition']} · {i['language']}) "
        f"×{i['qty']} — {_kr(i['line_sek'])}"
        + ("" if i["accepted"] else " <i>(not bought)</i>")
        + (f" <span style='color:#888'>{i['note']}</span>" if i.get("note") else "")
        + "</li>"
        for i in offer["items"]
    )
    base = _public_base_url() or "https://fabmatrix.t4ngo.com"
    note = f"<p style='color:#555'>Note: “{offer['buyer_message']}”</p>" if offer.get("buyer_message") else ""
    msg = f"<p style='color:#555'>Seller's message: “{offer['seller_message']}”</p>" if offer.get("seller_message") else ""
    return (
        "<div style='font-family:sans-serif;max-width:520px'>"
        "<h2 style='color:#0891b2'>The FAB Matrix — Buylist</h2>"
        f"{intro}"
        f"<ul>{rows}</ul>"
        f"<p><b>Total offered: {_kr(offer['offered_total_sek'])}</b></p>"
        f"{note}{msg}"
        f"<p><a href='{base}/sell' style='display:inline-block;padding:10px 18px;"
        "background:#0891b2;color:#fff;text-decoration:none;border-radius:6px'>"
        "Open the buylist</a></p>"
        "</div>"
    )


def _notify(offer: dict, event: str) -> None:
    """Email whoever needs to act. Best-effort, never blocks the request."""
    seller = offer.get("seller_username") or offer.get("seller_email")
    buyer = offer.get("buyer_username") or offer.get("buyer_email")
    if event == "submitted":
        to, subject = offer["buyer_email"], f"New sale offer from {seller}"
        intro = (f"<p><b>{seller}</b> wants to sell you cards. Review, re-price or "
                 "accept it in the buylist:</p>")
    elif event == "countered":
        to, subject = offer["seller_email"], f"{buyer} adjusted your sale offer"
        intro = (f"<p><b>{buyer}</b> reviewed your cards and sent back an adjusted offer. "
                 "Nothing is agreed until you accept it:</p>")
    elif event == "accepted":
        # Whoever was NOT on turn gets told.
        to = offer["seller_email"] if offer["_actor_id"] == offer["buyer_user_id"] else offer["buyer_email"]
        subject = "Your sale offer was accepted"
        intro = ("<p>The sale is agreed! Message each other in the app to arrange the "
                 "hand-over — your local game store is a great spot:</p>")
    elif event == "declined":
        to = offer["seller_email"] if offer["_actor_id"] == offer["buyer_user_id"] else offer["buyer_email"]
        subject = "Sale offer declined"
        intro = "<p>The sale offer was declined:</p>"
    else:
        return
    send_email_async(to, subject, _offer_email_html(offer, intro))


@router.post("/buylist/offers")
def sell_offer_create(req: SellOfferCreate, user: dict = Depends(_current_user)):
    """Send a sell list to the store. Every line is re-quoted server-side at
    send time (the client's estimate is never trusted) and the policy is
    snapshotted, so the offer stays auditable if the rates change tomorrow.
    Cards the policy won't buy are kept on the offer, flagged and priced 0 —
    the seller sees exactly what happened rather than cards silently vanishing."""
    ensure_app_buylist_schema()
    if not req.items:
        return JSONResponse(status_code=400, content={"error": "Add at least one card"})
    if len(req.items) > 500:
        return JSONResponse(status_code=400, content={"error": "Max 500 lines per offer — split it up"})
    message = (req.message or "").strip()[:1000] or None

    with get_conn() as conn:
        with conn.cursor() as cur:
            store = _require_store(cur)
            if store["user_id"] == user["user_id"]:
                return JSONResponse(status_code=400, content={"error": "The store cannot sell to itself"})
            policy = _policy(cur, store["user_id"])
            if not policy["enabled"]:
                return JSONResponse(status_code=409, content={"error": "The buylist is closed right now"})

            # Merge duplicate (printing, condition, language) lines up front.
            merged: dict[tuple[str, str, str], int] = {}
            order: list[tuple[str, str, str]] = []
            for it in req.items:
                key = ((it.printing_unique_id or "").strip(),
                       _norm_condition(it.condition), _norm_language(it.language))
                if not key[0]:
                    continue
                if key not in merged:
                    order.append(key)
                merged[key] = merged.get(key, 0) + max(1, min(999, it.qty))
            if not merged:
                return JSONResponse(status_code=400, content={"error": "Add at least one card"})

            cards = _load_cards(cur, [k[0] for k in order])
            unknown = [k[0] for k in order if k[0] not in cards]
            if unknown:
                raise HTTPException(status_code=404, detail=f"Unknown printing: {unknown[0]}")

            cur.execute(
                """
                INSERT INTO app.sell_offers
                    (seller_user_id, buyer_user_id, status, awaiting_user_id, seller_message, policy_snapshot)
                VALUES (%s, %s, 'submitted', %s, %s, %s)
                RETURNING sell_offer_id
                """,
                [user["user_id"], store["user_id"], store["user_id"], message, Json(policy)],
            )
            offer_id = cur.fetchone()["sell_offer_id"]

            quoted_total = 0
            for key in order:
                pid, condition, language = key
                qty = merged[key]
                q = _quote_card(policy, cards[pid], condition, language)
                if q["accepted"]:
                    quoted_total += q["unit_sek"] * qty
                cur.execute(
                    """
                    INSERT INTO app.sell_offer_items
                        (sell_offer_id, printing_unique_id, qty, card_condition, card_language,
                         market_sek, quoted_sek, offered_sek, accepted, note)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [offer_id, pid, qty, condition, language,
                     q["market_sek"], q["unit_sek"], q["unit_sek"], q["accepted"],
                     "; ".join(q["reasons"]) or None],
                )
            cur.execute(
                "UPDATE app.sell_offers SET quoted_total_sek = %s, offered_total_sek = %s "
                "WHERE sell_offer_id = %s",
                [quoted_total, quoted_total, offer_id],
            )

            cur.execute(_OFFER_BASE_SQL + " WHERE o.sell_offer_id = %s", [offer_id])
            result = _offer_dict(cur, dict(cur.fetchone()))
    _notify(result, "submitted")
    return result


@router.get("/buylist/offers")
def sell_offers_list(user: dict = Depends(_current_user)):
    """Every sale I'm part of — mine as a seller, plus everything sent to me if
    I'm the store. Newest first."""
    ensure_app_buylist_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _OFFER_BASE_SQL + " WHERE o.seller_user_id = %s OR o.buyer_user_id = %s "
                "ORDER BY o.created_at DESC LIMIT 200",
                [user["user_id"], user["user_id"]],
            )
            rows = [dict(r) for r in cur.fetchall()]
            return [_offer_dict(cur, r) for r in rows]


@router.get("/buylist/pending-count")
def sell_offers_pending_count(user: dict = Depends(_current_user)):
    """How many sales are waiting on ME — drives the nav badge."""
    ensure_app_buylist_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM app.sell_offers "
                "WHERE awaiting_user_id = %s AND status = ANY(%s)",
                [user["user_id"], list(OPEN_STATUSES)],
            )
            n = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT COUNT(*) AS n FROM app.sell_offers "
                "WHERE (seller_user_id = %s OR buyer_user_id = %s) AND status = 'accepted'",
                [user["user_id"], user["user_id"]],
            )
            live = int(cur.fetchone()["n"])
    return {"pending": n, "live": live}


@router.get("/buylist/offers/{sell_offer_id}")
def sell_offer_get(sell_offer_id: int, user: dict = Depends(_current_user)):
    ensure_app_buylist_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_OFFER_BASE_SQL + " WHERE o.sell_offer_id = %s", [sell_offer_id])
            row = cur.fetchone()
            if not row or user["user_id"] not in (row["seller_user_id"], row["buyer_user_id"]):
                raise HTTPException(status_code=404, detail="Sale offer not found")
            return _offer_dict(cur, dict(row))


@router.patch("/buylist/offers/{sell_offer_id}")
def sell_offer_act(sell_offer_id: int, req: SellOfferAction, user: dict = Depends(_current_user)):
    """Drive the sale:
    - counter — the store only, while the offer is open: re-price lines, change
      quantities, drop cards it won't take, add a note. Flips the turn back to
      the seller, who must accept again. Nothing is binding before that.
    - accept / decline — whoever is on turn.
    - cancel — the party NOT on turn while open, or either party once accepted
      (the hand-over fell through).
    - complete — either party once accepted: money and cards changed hands."""
    ensure_app_buylist_schema()
    action = (req.action or "").strip().lower()
    if action not in ("accept", "decline", "cancel", "complete", "counter"):
        return JSONResponse(status_code=400, content={
            "error": "action must be accept, decline, cancel, complete or counter"})

    notify_event = None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_OFFER_BASE_SQL + " WHERE o.sell_offer_id = %s FOR UPDATE OF o", [sell_offer_id])
            row = cur.fetchone()
            if not row or user["user_id"] not in (row["seller_user_id"], row["buyer_user_id"]):
                raise HTTPException(status_code=404, detail="Sale offer not found")
            offer = dict(row)
            is_buyer = user["user_id"] == offer["buyer_user_id"]
            on_turn = user["user_id"] == offer.get("awaiting_user_id")
            other_id = offer["seller_user_id"] if is_buyer else offer["buyer_user_id"]

            if action in ("accept", "decline", "counter"):
                if offer["status"] not in OPEN_STATUSES:
                    return JSONResponse(status_code=409, content={"error": f"This sale is already {offer['status']}"})
                if not on_turn:
                    raise HTTPException(status_code=403, detail="It's the other party's turn to respond")
            elif action == "cancel":
                if offer["status"] in OPEN_STATUSES and on_turn:
                    raise HTTPException(status_code=403, detail="It's your turn — accept, decline or counter instead")
                if offer["status"] not in OPEN_STATUSES + ("accepted",):
                    return JSONResponse(status_code=409, content={"error": f"This sale is already {offer['status']}"})
            elif action == "complete":
                if offer["status"] != "accepted":
                    return JSONResponse(status_code=409, content={"error": "Only an accepted sale can be completed"})

            message = (req.message or "").strip()[:1000] or None

            if action == "counter":
                if not is_buyer:
                    raise HTTPException(status_code=403, detail="Only the buyer can re-price a sale offer")
                for upd in req.item_updates:
                    sets, params = [], []
                    if upd.offered_sek is not None:
                        sets.append("offered_sek = %s")
                        params.append(max(0.0, min(1_000_000.0, float(upd.offered_sek))))
                    if upd.qty is not None:
                        sets.append("qty = %s")
                        params.append(max(1, min(999, int(upd.qty))))
                    if upd.accepted is not None:
                        sets.append("accepted = %s")
                        params.append(bool(upd.accepted))
                    if upd.note is not None:
                        sets.append("note = %s")
                        params.append((upd.note or "").strip()[:300] or None)
                    if not sets:
                        continue
                    params += [upd.item_id, sell_offer_id]
                    cur.execute(
                        f"UPDATE app.sell_offer_items SET {', '.join(sets)} "
                        "WHERE item_id = %s AND sell_offer_id = %s",
                        params,
                    )
                cur.execute(
                    """
                    UPDATE app.sell_offers
                       SET status = 'countered', awaiting_user_id = %s, buyer_message = COALESCE(%s, buyer_message),
                           updated_at = NOW()
                     WHERE sell_offer_id = %s
                    """,
                    [other_id, message, sell_offer_id],
                )
                notify_event = "countered"
            else:
                new_status = {"accept": "accepted", "decline": "declined",
                              "cancel": "cancelled", "complete": "completed"}[action]
                field = "buyer_message" if is_buyer else "seller_message"
                cur.execute(
                    f"""
                    UPDATE app.sell_offers
                       SET status = %s,
                           awaiting_user_id = NULL,
                           {field} = COALESCE(%s, {field}),
                           accepted_at  = CASE WHEN %s = 'accepted'  THEN NOW() ELSE accepted_at  END,
                           completed_at = CASE WHEN %s = 'completed' THEN NOW() ELSE completed_at END,
                           updated_at = NOW()
                     WHERE sell_offer_id = %s
                    """,
                    [new_status, message, new_status, new_status, sell_offer_id],
                )
                if new_status in ("accepted", "declined"):
                    notify_event = new_status

            cur.execute(_OFFER_BASE_SQL + " WHERE o.sell_offer_id = %s", [sell_offer_id])
            result = _offer_dict(cur, dict(cur.fetchone()))
    if notify_event:
        _notify({**result, "_actor_id": user["user_id"]}, notify_event)
    return result
