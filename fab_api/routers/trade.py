"""Phase 4 trading: trade-flagged cardlists become public listings; users send
trade offers (give/want item bundles) valued by gold.trade_value_sek — the
"trend price, or low if it's higher" rule.

Negotiation flow (2026-07-08): creating an offer RE-CHECKS that every requested
card is still available on the counterparty's trade lists and then LOCKS those
copies (app.trade_locks) so two buyers can't claim the same physical card. The
recipient can accept, decline, or COUNTER (add cards from the other side's
public trade lists — those get availability-checked + locked too), which flips
whose turn it is (awaiting_user_id). Accepting makes the trade "live" — the
swap happens in person (preferably at an LGS); locks are held until the trade
is completed or called off. decline/cancel/complete all release the locks."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fab_api.core import get_conn, send_email_async
from fab_api.routers.auth import _current_user, _public_base_url

router = APIRouter()

APP_TRADE_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS app;

ALTER TABLE app.cardlists ADD COLUMN IF NOT EXISTS is_trade_list BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE app.cardlist_items ADD COLUMN IF NOT EXISTS discount_type TEXT;
ALTER TABLE app.cardlist_items ADD COLUMN IF NOT EXISTS discount_value NUMERIC;

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
    delete_lists_on_accept BOOLEAN NOT NULL DEFAULT FALSE,
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
ALTER TABLE app.trade_offers ADD COLUMN IF NOT EXISTS delete_lists_on_accept BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE app.trade_offers ADD COLUMN IF NOT EXISTS awaiting_user_id BIGINT;
ALTER TABLE app.trade_offers ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ;
ALTER TABLE app.trade_offers ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
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

CREATE TABLE IF NOT EXISTS app.trade_offer_lists (
    id          BIGSERIAL PRIMARY KEY,
    offer_id    BIGINT    NOT NULL REFERENCES app.trade_offers(offer_id) ON DELETE CASCADE,
    side        TEXT      NOT NULL,
    cardlist_id BIGINT,
    name        TEXT      NOT NULL,
    total_sek   NUMERIC
);
CREATE INDEX IF NOT EXISTS trade_offer_lists_offer ON app.trade_offer_lists (offer_id);

CREATE TABLE IF NOT EXISTS app.trade_locks (
    lock_id            BIGSERIAL   PRIMARY KEY,
    offer_id           BIGINT      NOT NULL REFERENCES app.trade_offers(offer_id) ON DELETE CASCADE,
    owner_user_id      BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    printing_unique_id TEXT        NOT NULL,
    qty                INTEGER     NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (offer_id, owner_user_id, printing_unique_id)
);
CREATE INDEX IF NOT EXISTS trade_locks_owner ON app.trade_locks (owner_user_id, printing_unique_id);
"""

_trade_schema_ready = False

# Locks count against availability while the offer is negotiable or live.
ACTIVE_LOCK_STATUSES = ("pending", "accepted")


def ensure_app_trade_schema():
    global _trade_schema_ready
    if _trade_schema_ready:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(APP_TRADE_SCHEMA_SQL)
    _trade_schema_ready = True


class OfferItem(BaseModel):
    printing_unique_id: str
    qty: int = 1
    value_sek: Optional[float] = None
    discount_type: Optional[str] = None
    discount_value: Optional[float] = None


class OfferCreate(BaseModel):
    to_user_id: int
    message: Optional[str] = None
    offer_items: list[OfferItem] = []     # what the sender GIVES
    request_items: list[OfferItem] = []   # what the sender WANTS


class ListOfferCreate(BaseModel):
    # New multi-list form; the old single-id fields are still accepted.
    offer_cardlist_ids: list[int] = []    # sender gives these lists (may be empty = money-only buy)
    request_cardlist_ids: list[int] = []  # sender wants these lists (may be empty = money-only sale)
    offer_cardlist_id: Optional[int] = None
    request_cardlist_id: Optional[int] = None
    to_username: Optional[str] = None
    delete_lists_on_accept: bool = False
    message: Optional[str] = None


class CounterItem(BaseModel):
    side: str                              # 'offer' (sender gives) | 'request' (sender wants)
    printing_unique_id: str
    qty: int = 1


class OfferAction(BaseModel):
    action: str   # accept | decline | cancel | complete | counter
    add_items: list[CounterItem] = []


# ── Availability + locking ────────────────────────────────────────────────────

def _serialize_owner(cur, owner_user_id: int):
    """Advisory xact lock per owner: two offers grabbing the same seller's cards
    queue up instead of racing the availability check."""
    cur.execute("SELECT pg_advisory_xact_lock(874201, %s)", [owner_user_id])


def _availability(cur, owner_user_id: int, printing_ids: list[str],
                  exclude_offer_id: Optional[int] = None) -> dict[str, dict]:
    """For each printing: how many copies the owner has on public trade lists,
    how many are locked by other active offers, and what's left."""
    if not printing_ids:
        return {}
    cur.execute(
        """
        SELECT p.pid,
               COALESCE(listed.qty, 0)  AS listed,
               COALESCE(locked.qty, 0)  AS locked
        FROM unnest(%s::text[]) AS p(pid)
        LEFT JOIN (
            SELECT i.printing_unique_id, SUM(i.qty) AS qty
            FROM app.cardlist_items i
            JOIN app.cardlists l ON l.cardlist_id = i.cardlist_id
            WHERE l.user_id = %s AND l.is_trade_list
            GROUP BY i.printing_unique_id
        ) listed ON listed.printing_unique_id = p.pid
        LEFT JOIN (
            SELECT k.printing_unique_id, SUM(k.qty) AS qty
            FROM app.trade_locks k
            JOIN app.trade_offers o ON o.offer_id = k.offer_id
            WHERE k.owner_user_id = %s AND o.status = ANY(%s)
              AND (%s::bigint IS NULL OR k.offer_id <> %s)
            GROUP BY k.printing_unique_id
        ) locked ON locked.printing_unique_id = p.pid
        """,
        [printing_ids, owner_user_id, owner_user_id, list(ACTIVE_LOCK_STATUSES),
         exclude_offer_id, exclude_offer_id],
    )
    return {
        r["pid"]: {
            "listed": int(r["listed"]),
            "locked": int(r["locked"]),
            "available": max(0, int(r["listed"]) - int(r["locked"])),
        }
        for r in cur.fetchall()
    }


def _check_and_lock(cur, offer_id: int, owner_user_id: int,
                    items: list[tuple[str, int]], *, strict: bool) -> list[dict]:
    """Re-check availability on the owner's trade lists and reserve the copies.
    strict=True → any shortfall aborts with 409 + a per-card breakdown (the
    "cards were sold while you built the trade" guard). strict=False is used
    for the giver's OWN cards: lock what IS publicly listed so it can't be
    double-promised, but don't require unlisted cards to be on a trade list.
    Returns warnings (shortfalls) in non-strict mode."""
    # Aggregate duplicates before checking.
    need: dict[str, int] = {}
    for pid, qty in items:
        need[pid] = need.get(pid, 0) + max(1, qty)
    if not need:
        return []

    _serialize_owner(cur, owner_user_id)
    avail = _availability(cur, owner_user_id, list(need.keys()), exclude_offer_id=offer_id)

    shortfalls = []
    for pid, qty in need.items():
        a = avail.get(pid, {"listed": 0, "locked": 0, "available": 0})
        if a["available"] < qty:
            cur.execute("SELECT name, display_id FROM gold.gold_cards WHERE printing_unique_id = %s LIMIT 1", [pid])
            g = cur.fetchone() or {}
            shortfalls.append({
                "printing_unique_id": pid,
                "name": g.get("name"),
                "display_id": g.get("display_id"),
                "wanted": qty,
                "available": a["available"],
                "locked_elsewhere": a["locked"],
            })
    if shortfalls and strict:
        raise HTTPException(status_code=409, detail={
            "error": "Some cards are no longer available — they may have been traded away or reserved in another offer.",
            "unavailable": shortfalls,
        })

    for pid, qty in need.items():
        a = avail.get(pid, {"available": 0})
        lock_qty = qty if strict else min(qty, a["available"])
        if lock_qty <= 0:
            continue
        cur.execute(
            """
            INSERT INTO app.trade_locks (offer_id, owner_user_id, printing_unique_id, qty)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (offer_id, owner_user_id, printing_unique_id)
            DO UPDATE SET qty = app.trade_locks.qty + EXCLUDED.qty
            """,
            [offer_id, owner_user_id, pid, lock_qty],
        )
    return shortfalls


def _release_locks(cur, offer_id: int):
    cur.execute("DELETE FROM app.trade_locks WHERE offer_id = %s", [offer_id])


# ── Listings ──────────────────────────────────────────────────────────────────

@router.get("/trade/listings")
def trade_listings(
    q:         Optional[str] = Query(None, description="Card name search"),
    set_id:    Optional[str] = Query(None),
    owner_id:  Optional[int] = Query(None, description="Only listings from this user"),
    page:      int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """All items in trade-flagged cardlists across ALL users, valued by
    trade_value_sek. Public — this is the marketplace browse view. Each row
    also reports how many copies are locked in active offers."""
    ensure_app_trade_schema()
    where = ["l.is_trade_list = true"]
    params: list = []
    if q and q.strip():
        where.append("g.name ILIKE %s")
        params.append(f"%{q.strip()}%")
    if set_id:
        where.append("g.set_id = %s")
        params.append(set_id.upper())
    if owner_id is not None:
        where.append("u.user_id = %s")
        params.append(owner_id)

    offset = (page - 1) * page_size
    sql = f"""
        SELECT
            i.printing_unique_id, i.qty,
            l.cardlist_id, l.name AS list_name,
            u.user_id AS owner_id, u.email AS owner_email, u.username AS owner_username,
            g.name, g.display_id, g.set_id,
            coalesce(s.set_name, g.set_id) AS set_name,
            g.edition, g.foiling, g.is_foil, g.rarity, g.pitch, g.image_url,
            GREATEST(0, CASE
                WHEN i.discount_type = 'pct' THEN COALESCE(g.trade_value_sek, 0) * (1 - LEAST(COALESCE(i.discount_value, 0), 100) / 100)
                WHEN i.discount_type = 'sek' THEN COALESCE(g.trade_value_sek, 0) - COALESCE(i.discount_value, 0)
                ELSE COALESCE(g.trade_value_sek, 0)
            END) AS trade_value_sek,
            COALESCE(k.locked, 0) AS locked_qty,
            COUNT(*) OVER() AS total_count
        FROM app.cardlist_items i
        JOIN app.cardlists l ON l.cardlist_id = i.cardlist_id
        JOIN app.users u     ON u.user_id = l.user_id
        JOIN gold.gold_cards g ON g.printing_unique_id = i.printing_unique_id
        LEFT JOIN bronze.fab_sets s ON s.set_id = g.set_id AND s.edition = g.edition
        LEFT JOIN LATERAL (
            SELECT SUM(tk.qty) AS locked
            FROM app.trade_locks tk
            JOIN app.trade_offers o ON o.offer_id = tk.offer_id
            WHERE tk.owner_user_id = u.user_id
              AND tk.printing_unique_id = i.printing_unique_id
              AND o.status = ANY(%s)
        ) k ON true
        WHERE {' AND '.join(where)}
        ORDER BY l.updated_at DESC, g.name, g.set_id, u.email
        LIMIT %s OFFSET %s
    """
    params = [list(ACTIVE_LOCK_STATUSES)] + params + [page_size, offset]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    total = rows[0]["total_count"] if rows else 0
    items = []
    for r in rows:
        d = dict(r)
        d.pop("total_count", None)
        d["trade_value_sek"] = int(d["trade_value_sek"]) if d["trade_value_sek"] is not None else None
        d["locked_qty"] = int(d["locked_qty"])
        d["available_qty"] = max(0, d["qty"] - d["locked_qty"])
        items.append(d)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/trade/traders")
def trade_traders():
    """Users who currently have at least one public trade list — feeds the
    Browse 'filter by trader' dropdown. Public."""
    ensure_app_trade_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.user_id, u.username,
                       COUNT(DISTINCT l.cardlist_id) AS list_count,
                       COALESCE(SUM(i.qty), 0) AS card_count
                FROM app.users u
                JOIN app.cardlists l ON l.user_id = u.user_id AND l.is_trade_list
                LEFT JOIN app.cardlist_items i ON i.cardlist_id = l.cardlist_id
                GROUP BY u.user_id, u.username
                HAVING COALESCE(SUM(i.qty), 0) > 0
                ORDER BY card_count DESC, u.username
                """
            )
            rows = cur.fetchall()
    return [{
        "user_id": r["user_id"],
        "username": r["username"],
        "list_count": int(r["list_count"]),
        "card_count": int(r["card_count"]),
    } for r in rows]


@router.get("/trade/availability/{printing_unique_id}")
def trade_availability(printing_unique_id: str):
    """Who has this printing up for trade, at what value, and how many copies
    are actually still free (not locked by an active offer). Public — drives
    the per-seller panel in the Browse card detail view."""
    ensure_app_trade_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    u.user_id AS owner_id, u.username AS owner_username,
                    SUM(i.qty) AS qty,
                    array_agg(DISTINCT l.name) AS list_names,
                    MIN(GREATEST(0, CASE
                        WHEN i.discount_type = 'pct' THEN COALESCE(g.trade_value_sek, 0) * (1 - LEAST(COALESCE(i.discount_value, 0), 100) / 100)
                        WHEN i.discount_type = 'sek' THEN COALESCE(g.trade_value_sek, 0) - COALESCE(i.discount_value, 0)
                        ELSE COALESCE(g.trade_value_sek, 0)
                    END)) AS trade_value_sek,
                    COALESCE((
                        SELECT SUM(k.qty) FROM app.trade_locks k
                        JOIN app.trade_offers o ON o.offer_id = k.offer_id
                        WHERE k.owner_user_id = u.user_id
                          AND k.printing_unique_id = %s
                          AND o.status = ANY(%s)
                    ), 0) AS locked_qty
                FROM app.cardlist_items i
                JOIN app.cardlists l ON l.cardlist_id = i.cardlist_id AND l.is_trade_list
                JOIN app.users u ON u.user_id = l.user_id
                JOIN gold.gold_cards g ON g.printing_unique_id = i.printing_unique_id
                WHERE i.printing_unique_id = %s
                GROUP BY u.user_id, u.username
                ORDER BY trade_value_sek NULLS LAST, u.username
                """,
                [printing_unique_id, list(ACTIVE_LOCK_STATUSES), printing_unique_id],
            )
            rows = cur.fetchall()
    out = []
    for r in rows:
        qty, locked = int(r["qty"]), int(r["locked_qty"])
        out.append({
            "owner_id": r["owner_id"],
            "owner_username": r["owner_username"],
            "qty": qty,
            "locked_qty": locked,
            "available_qty": max(0, qty - locked),
            "list_names": r["list_names"],
            "trade_value_sek": int(r["trade_value_sek"]) if r["trade_value_sek"] is not None else None,
        })
    return out


@router.get("/trade/lists")
def trade_lists(
    owner_id: Optional[int] = Query(None, description="Only public trade lists from this user"),
    q: Optional[str] = Query(None, description="List name, username, email, or card name search"),
):
    """Public trade-flagged lists, snapshotted by total trade value. Used for
    list-for-list offers; list ids may later be deleted without losing offer history."""
    ensure_app_trade_schema()
    where = ["l.is_trade_list = true"]
    params: list = []
    if owner_id is not None:
        where.append("u.user_id = %s")
        params.append(owner_id)
    if q and q.strip():
        needle = f"%{q.strip()}%"
        where.append(
            "(l.name ILIKE %s OR u.username ILIKE %s OR u.email ILIKE %s "
            "OR EXISTS (SELECT 1 FROM app.cardlist_items si JOIN gold.gold_cards sg "
            "ON sg.printing_unique_id = si.printing_unique_id "
            "WHERE si.cardlist_id = l.cardlist_id AND sg.name ILIKE %s))"
        )
        params += [needle, needle, needle, needle]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    l.cardlist_id, l.name, l.created_at, l.updated_at,
                    u.user_id AS owner_id, u.email AS owner_email, u.username AS owner_username,
                    COALESCE(SUM(i.qty), 0) AS item_count,
                    COALESCE(SUM(i.qty * GREATEST(0, CASE
                        WHEN i.discount_type = 'pct' THEN COALESCE(g.trade_value_sek, 0) * (1 - LEAST(COALESCE(i.discount_value, 0), 100) / 100)
                        WHEN i.discount_type = 'sek' THEN COALESCE(g.trade_value_sek, 0) - COALESCE(i.discount_value, 0)
                        ELSE COALESCE(g.trade_value_sek, 0)
                    END)), 0) AS total_sek
                FROM app.cardlists l
                JOIN app.users u ON u.user_id = l.user_id
                LEFT JOIN app.cardlist_items i ON i.cardlist_id = l.cardlist_id
                LEFT JOIN gold.gold_cards g ON g.printing_unique_id = i.printing_unique_id
                WHERE {' AND '.join(where)}
                GROUP BY l.cardlist_id, u.user_id, u.email, u.username
                ORDER BY l.updated_at DESC, l.created_at DESC
                """,
                params,
            )
            rows = cur.fetchall()
    return [{
        "cardlist_id": r["cardlist_id"],
        "name": r["name"],
        "owner_id": r["owner_id"],
        "owner_email": r["owner_email"],
        "owner_username": r["owner_username"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        "item_count": int(r["item_count"]),
        "total_sek": int(r["total_sek"]),
    } for r in rows]


# ── Offers ────────────────────────────────────────────────────────────────────

def _snapshot_values(cur, items: list[OfferItem]) -> list[tuple[str, int, Optional[float], Optional[float], Optional[str], Optional[float]]]:
    """Validate printings exist and snapshot their trade value (per unit) now."""
    out = []
    for it in items:
        pid = (it.printing_unique_id or "").strip()
        qty = max(1, min(999, it.qty))
        cur.execute(
            "SELECT trade_value_sek FROM gold.gold_cards WHERE printing_unique_id = %s LIMIT 1",
            [pid],
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Unknown printing: {pid}")
        base_val = float(row["trade_value_sek"]) if row["trade_value_sek"] is not None else None
        discount_type = (it.discount_type or "none").lower()
        if discount_type not in ("none", "sek", "pct"):
            discount_type = "none"
        discount_value = max(0.0, float(it.discount_value or 0))
        val = float(it.value_sek) if it.value_sek is not None else base_val
        if val is not None:
            val = max(0.0, val)
        out.append((pid, qty, base_val, val, discount_type, discount_value if discount_type != "none" else 0))
    return out


def _snapshot_cardlist(cur, cardlist_id: int, *, side: str, current_user_id: int) -> dict:
    """Snapshot all items from a cardlist using trade_value_sek. The offered list
    must belong to the current user. The requested list can be your own list or
    another user's public trade list."""
    cur.execute(
        """
        SELECT l.cardlist_id, l.name, l.user_id, l.is_trade_list, u.email AS owner_email, u.username AS owner_username
        FROM app.cardlists l
        JOIN app.users u ON u.user_id = l.user_id
        WHERE l.cardlist_id = %s
        """,
        [cardlist_id],
    )
    lst = cur.fetchone()
    if not lst:
        raise HTTPException(status_code=404, detail=f"Cardlist not found: {cardlist_id}")
    if side == "offer" and lst["user_id"] != current_user_id:
        raise HTTPException(status_code=403, detail="You can only offer one of your own lists")
    if side == "request" and lst["user_id"] != current_user_id and not lst["is_trade_list"]:
        raise HTTPException(status_code=403, detail="You can only request another user's public trade list")

    cur.execute(
        """
        SELECT
            i.printing_unique_id, i.qty, i.discount_type, i.discount_value,
            g.trade_value_sek,
            GREATEST(0, CASE
                WHEN i.discount_type = 'pct' THEN COALESCE(g.trade_value_sek, 0) * (1 - LEAST(COALESCE(i.discount_value, 0), 100) / 100)
                WHEN i.discount_type = 'sek' THEN COALESCE(g.trade_value_sek, 0) - COALESCE(i.discount_value, 0)
                ELSE COALESCE(g.trade_value_sek, 0)
            END) AS effective_value_sek
        FROM app.cardlist_items i
        JOIN gold.gold_cards g ON g.printing_unique_id = i.printing_unique_id
        WHERE i.cardlist_id = %s
        ORDER BY i.added_at
        """,
        [cardlist_id],
    )
    rows = cur.fetchall()
    items = []
    total = 0.0
    for r in rows:
        base_val = float(r["trade_value_sek"]) if r["trade_value_sek"] is not None else None
        val = float(r["effective_value_sek"]) if r["effective_value_sek"] is not None else None
        if val is not None:
            total += val * r["qty"]
        discount_type = (r.get("discount_type") or "none").lower()
        discount_value = float(r["discount_value"]) if r.get("discount_value") is not None else 0
        items.append((r["printing_unique_id"], r["qty"], base_val, val, discount_type, discount_value))
    if not items:
        raise HTTPException(status_code=400, detail=f"List is empty: {lst['name']}")
    return {
        "cardlist_id": lst["cardlist_id"],
        "name": lst["name"],
        "owner_id": lst["user_id"],
        "owner_email": lst["owner_email"],
        "owner_username": lst["owner_username"],
        "items": items,
        "total": total,
    }


def _offer_dict(cur, offer_row: dict) -> dict:
    """Offer + its items joined to gold for display, plus per-side totals."""
    cur.execute(
        """
        SELECT
            t.side, t.printing_unique_id, t.qty, t.base_value_sek, t.value_sek,
            t.discount_type, t.discount_value,
            g.name, g.display_id, g.set_id, g.edition, g.foiling, g.rarity, g.image_url,
            g.trade_value_sek AS current_value_sek
        FROM app.trade_offer_items t
        LEFT JOIN gold.gold_cards g ON g.printing_unique_id = t.printing_unique_id
        WHERE t.offer_id = %s
        ORDER BY t.side, g.name
        """,
        [offer_row["offer_id"]],
    )
    items = []
    totals = {"offer": 0, "request": 0}
    current_totals = {"offer": 0, "request": 0}
    for r in cur.fetchall():
        d = dict(r)
        d["value_sek"] = float(d["value_sek"]) if d["value_sek"] is not None else None
        d["base_value_sek"] = float(d["base_value_sek"]) if d["base_value_sek"] is not None else None
        d["discount_value"] = float(d["discount_value"]) if d["discount_value"] is not None else None
        d["current_value_sek"] = float(d["current_value_sek"]) if d["current_value_sek"] is not None else None
        if d["value_sek"] is not None:
            totals[d["side"]] += d["value_sek"] * d["qty"]
        if d["current_value_sek"] is not None:
            current_totals[d["side"]] += d["current_value_sek"] * d["qty"]
        items.append(d)

    cur.execute(
        "SELECT side, cardlist_id, name, total_sek FROM app.trade_offer_lists WHERE offer_id = %s ORDER BY id",
        [offer_row["offer_id"]],
    )
    lists = {"offer": [], "request": []}
    for r in cur.fetchall():
        lists.setdefault(r["side"], []).append({
            "cardlist_id": r["cardlist_id"],
            "name": r["name"],
            "total_sek": int(r["total_sek"]) if r["total_sek"] is not None else None,
        })

    return {
        "offer_id": offer_row["offer_id"],
        "kind": offer_row.get("kind") or "cards",
        "from_user_id": offer_row["from_user_id"],
        "from_email": offer_row["from_email"],
        "to_user_id": offer_row["to_user_id"],
        "to_email": offer_row["to_email"],
        "from_username": offer_row.get("from_username"),
        "to_username": offer_row.get("to_username"),
        "status": offer_row["status"],
        # Legacy rows predate awaiting_user_id — the recipient was always on turn.
        "awaiting_user_id": offer_row.get("awaiting_user_id") or offer_row["to_user_id"],
        "delete_lists_on_accept": bool(offer_row.get("delete_lists_on_accept")),
        "message": offer_row["message"],
        "created_at": offer_row["created_at"].isoformat() if offer_row["created_at"] else None,
        "updated_at": offer_row["updated_at"].isoformat() if offer_row["updated_at"] else None,
        "accepted_at": offer_row["accepted_at"].isoformat() if offer_row.get("accepted_at") else None,
        "completed_at": offer_row["completed_at"].isoformat() if offer_row.get("completed_at") else None,
        "items": items,
        "offer_total_sek": int(totals["offer"]),
        "request_total_sek": int(totals["request"]),
        "current_offer_total_sek": int(current_totals["offer"]),
        "current_request_total_sek": int(current_totals["request"]),
        "offer_lists": lists["offer"],
        "request_lists": lists["request"],
        "offer_list": {
            "cardlist_id": offer_row.get("offer_list_id"),
            "name": offer_row.get("offer_list_name"),
            "total_sek": int(offer_row["offer_list_total_sek"]) if offer_row.get("offer_list_total_sek") is not None else None,
        } if offer_row.get("offer_list_name") else None,
        "request_list": {
            "cardlist_id": offer_row.get("request_list_id"),
            "name": offer_row.get("request_list_name"),
            "total_sek": int(offer_row["request_list_total_sek"]) if offer_row.get("request_list_total_sek") is not None else None,
        } if offer_row.get("request_list_name") else None,
    }


_OFFER_BASE_SQL = """
    SELECT o.*, fu.email AS from_email, fu.username AS from_username,
           tu.email AS to_email, tu.username AS to_username
    FROM app.trade_offers o
    JOIN app.users fu ON fu.user_id = o.from_user_id
    JOIN app.users tu ON tu.user_id = o.to_user_id
"""


def _kr(v) -> str:
    return f"{int(round(float(v))):,} kr".replace(",", " ") if v is not None else "—"


def _offer_items_html(offer: dict, viewer: str) -> str:
    """Two item lists as simple HTML, labelled from the email recipient's side.
    viewer='recipient' → the offer's 'offer' side is what THEY receive."""
    recv_label, give_label = (
        ("You receive", "You give") if viewer == "recipient" else ("You give", "You receive")
    )
    recv = [i for i in offer["items"] if i["side"] == "offer"]
    give = [i for i in offer["items"] if i["side"] == "request"]

    def rows(items):
        if not items:
            return "<li style='color:#888'>nothing — this side is settled in money</li>"
        return "".join(
            f"<li>{i['name']} ×{i['qty']} — {_kr((i['value_sek'] or 0) * i['qty'])}</li>"
            for i in items
        )

    if offer.get("kind") == "list":
        def names(side_lists, fallback):
            if side_lists:
                return " + ".join(l["name"] for l in side_lists)
            return fallback or "list"
        recv_name = names(offer.get("offer_lists"), (offer.get("offer_list") or {}).get("name")) if viewer == "recipient" \
            else names(offer.get("request_lists"), (offer.get("request_list") or {}).get("name"))
        give_name = names(offer.get("request_lists"), (offer.get("request_list") or {}).get("name")) if viewer == "recipient" \
            else names(offer.get("offer_lists"), (offer.get("offer_list") or {}).get("name"))
        return (
            f"<p><b>{recv_label}</b>: {recv_name} ({_kr(offer['offer_total_sek'] if viewer == 'recipient' else offer['request_total_sek'])})</p>"
            f"<p><b>{give_label}</b>: {give_name} ({_kr(offer['request_total_sek'] if viewer == 'recipient' else offer['offer_total_sek'])})</p>"
        )
    return (
        f"<p><b>{recv_label}</b> ({_kr(offer['offer_total_sek'] if viewer == 'recipient' else offer['request_total_sek'])}):</p><ul>{rows(recv if viewer == 'recipient' else give)}</ul>"
        f"<p><b>{give_label}</b> ({_kr(offer['request_total_sek'] if viewer == 'recipient' else offer['offer_total_sek'])}):</p><ul>{rows(give if viewer == 'recipient' else recv)}</ul>"
    )


def _notify_offer(offer: dict, event: str) -> None:
    """Email the party who should hear about `event`. Async + best-effort:
    never blocks or fails the API call. Cancel/complete are silent (noise)."""
    base = _public_base_url() or "https://fabmatrix.t4ngo.com"
    link = f"{base}/trade"
    if event == "created":
        to, subject = offer["to_email"], f"New trade offer from {offer['from_username'] or offer['from_email']}"
        intro = f"<p><b>{offer['from_username'] or offer['from_email']}</b> sent you a trade offer. The requested cards are now reserved for this trade:</p>"
        viewer = "recipient"
    elif event == "countered":
        # Notify whoever is now on turn.
        awaiting_is_recipient = offer["awaiting_user_id"] == offer["to_user_id"]
        other = offer["from_username"] or offer["from_email"] if awaiting_is_recipient else offer["to_username"] or offer["to_email"]
        to = offer["to_email"] if awaiting_is_recipient else offer["from_email"]
        subject = f"Trade offer updated by {other}"
        intro = f"<p><b>{other}</b> adjusted the trade — please review and accept or decline:</p>"
        viewer = "recipient" if awaiting_is_recipient else "sender"
    elif event in ("accepted", "declined"):
        # Notify the party who was NOT the one acting (the one who was waiting).
        actor_is_recipient = offer["awaiting_user_id"] == offer["to_user_id"]
        to = offer["from_email"] if actor_is_recipient else offer["to_email"]
        viewer = "sender" if actor_is_recipient else "recipient"
        who = (offer["to_username"] or offer["to_email"]) if actor_is_recipient else (offer["from_username"] or offer["from_email"])
        subject = f"Your trade was {event}"
        if event == "accepted":
            intro = (f"<p><b>{who}</b> accepted the trade — it's ON! "
                     "Message each other in the app to decide where to meet (your local game store is a great spot):</p>")
        else:
            intro = f"<p><b>{who}</b> declined the trade:</p>"
    else:
        return
    msg = f"<p style='color:#555'>Message: “{offer['message']}”</p>" if offer.get("message") else ""
    send_email_async(
        to,
        subject,
        (
            "<div style='font-family:sans-serif;max-width:520px'>"
            "<h2 style='color:#0891b2'>The FAB Matrix — Trading Post</h2>"
            f"{intro}{_offer_items_html(offer, viewer)}{msg}"
            f"<p><a href='{link}' style='display:inline-block;padding:10px 18px;"
            "background:#0891b2;color:#fff;text-decoration:none;border-radius:6px'>"
            "Open the Trading Post</a></p>"
            "</div>"
        ),
    )


@router.post("/trade/offers")
def trade_offer_create(req: OfferCreate, user: dict = Depends(_current_user)):
    """Ask for a trade. Every requested card is re-checked against the
    counterparty's live trade lists and LOCKED (409 with a per-card breakdown
    if something was traded away meanwhile). One side may be empty — that's a
    plain buy (you pay money) or sale (you take money). Item values are
    snapshotted at send time so later price moves don't rewrite a standing offer."""
    ensure_app_trade_schema()
    if req.to_user_id == user["user_id"]:
        return JSONResponse(status_code=400, content={"error": "Cannot send an offer to yourself"})
    if not req.offer_items and not req.request_items:
        return JSONResponse(status_code=400, content={"error": "Offer needs at least one item"})
    message = (req.message or "").strip()[:1000] or None

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM app.users WHERE user_id = %s", [req.to_user_id])
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Recipient not found")

            give = _snapshot_values(cur, req.offer_items)
            want = _snapshot_values(cur, req.request_items)

            cur.execute(
                "INSERT INTO app.trade_offers (from_user_id, to_user_id, message, awaiting_user_id) "
                "VALUES (%s, %s, %s, %s) RETURNING offer_id",
                [user["user_id"], req.to_user_id, message, req.to_user_id],
            )
            offer_id = cur.fetchone()["offer_id"]
            for side, rows in (("offer", give), ("request", want)):
                for pid, qty, base_val, val, discount_type, discount_value in rows:
                    cur.execute(
                        "INSERT INTO app.trade_offer_items "
                        "(offer_id, side, printing_unique_id, qty, base_value_sek, value_sek, discount_type, discount_value) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (offer_id, side, printing_unique_id) "
                        "DO UPDATE SET qty = app.trade_offer_items.qty + EXCLUDED.qty",
                        [offer_id, side, pid, qty, base_val, val, discount_type, discount_value],
                    )

            # The failproof step: requested cards must still be free on THEIR
            # trade lists (strict → 409 + rollback if not), and are locked.
            # The sender's own give-cards are soft-locked (whatever of them is
            # publicly listed can't be promised twice).
            _check_and_lock(cur, offer_id, req.to_user_id,
                            [(pid, qty) for pid, qty, *_ in want], strict=True)
            _check_and_lock(cur, offer_id, user["user_id"],
                            [(pid, qty) for pid, qty, *_ in give], strict=False)

            cur.execute(_OFFER_BASE_SQL + " WHERE o.offer_id = %s", [offer_id])
            result = _offer_dict(cur, dict(cur.fetchone()))
    # Outside the transaction (committed) — notify the recipient, fire-and-forget.
    _notify_offer(result, "created")
    return result


@router.post("/trade/list-offers")
def trade_list_offer_create(req: ListOfferCreate, user: dict = Depends(_current_user)):
    """Trade full lists — one or SEVERAL per side, and either side may be empty
    (empty give = you buy the lists for money; empty receive = you sell yours).
    All requested lists must belong to the same user. List names, totals and
    every item/value are snapshotted so the trade stays auditable even if the
    source lists are later deleted; requested cards are availability-checked
    and locked like any other offer."""
    ensure_app_trade_schema()
    message = (req.message or "").strip()[:1000] or None

    offer_ids = list(dict.fromkeys(req.offer_cardlist_ids or ([] if req.offer_cardlist_id is None else [req.offer_cardlist_id])))
    request_ids = list(dict.fromkeys(req.request_cardlist_ids or ([] if req.request_cardlist_id is None else [req.request_cardlist_id])))
    if not offer_ids and not request_ids:
        return JSONResponse(status_code=400, content={"error": "Pick at least one list"})
    overlap = set(offer_ids) & set(request_ids)
    if overlap:
        return JSONResponse(status_code=400, content={"error": "The same list can't be on both sides"})

    with get_conn() as conn:
        with conn.cursor() as cur:
            gives = [_snapshot_cardlist(cur, cid, side="offer", current_user_id=user["user_id"]) for cid in offer_ids]
            wants = [_snapshot_cardlist(cur, cid, side="request", current_user_id=user["user_id"]) for cid in request_ids]

            want_owners = {w["owner_id"] for w in wants}
            if len(want_owners) > 1:
                return JSONResponse(status_code=400, content={"error": "All requested lists must belong to the same user"})

            to_user_id = wants[0]["owner_id"] if wants else None
            to_username = (req.to_username or "").strip()
            if to_username:
                cur.execute(
                    "SELECT user_id FROM app.users WHERE lower(username) = lower(%s)",
                    [to_username],
                )
                target = cur.fetchone()
                if not target:
                    return JSONResponse(status_code=404, content={"error": f"No user named {to_username}"})
                if to_user_id is not None and to_user_id != user["user_id"] and target["user_id"] != to_user_id:
                    return JSONResponse(status_code=400, content={
                        "error": "The requested lists belong to someone else than that username"})
                to_user_id = target["user_id"]
            if to_user_id is None:
                return JSONResponse(status_code=400, content={
                    "error": "Selling lists for money needs a recipient — give their username"})

            give_total = sum(g["total"] for g in gives)
            want_total = sum(w["total"] for w in wants)
            give_name = " + ".join(g["name"] for g in gives) if gives else None
            want_name = " + ".join(w["name"] for w in wants) if wants else None

            cur.execute(
                """
                INSERT INTO app.trade_offers (
                    from_user_id, to_user_id, kind, message,
                    offer_list_id, offer_list_name, offer_list_total_sek,
                    request_list_id, request_list_name, request_list_total_sek,
                    delete_lists_on_accept, awaiting_user_id
                )
                VALUES (%s, %s, 'list', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING offer_id
                """,
                [
                    user["user_id"], to_user_id, message,
                    gives[0]["cardlist_id"] if gives else None, give_name, give_total if gives else None,
                    wants[0]["cardlist_id"] if wants else None, want_name, want_total if wants else None,
                    req.delete_lists_on_accept, to_user_id,
                ],
            )
            offer_id = cur.fetchone()["offer_id"]
            for side, snaps in (("offer", gives), ("request", wants)):
                for snap in snaps:
                    cur.execute(
                        "INSERT INTO app.trade_offer_lists (offer_id, side, cardlist_id, name, total_sek) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        [offer_id, side, snap["cardlist_id"], snap["name"], snap["total"]],
                    )
                    for pid, qty, base_val, val, discount_type, discount_value in snap["items"]:
                        cur.execute(
                            """
                            INSERT INTO app.trade_offer_items
                                (offer_id, side, printing_unique_id, qty, base_value_sek, value_sek, discount_type, discount_value)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (offer_id, side, printing_unique_id)
                            DO UPDATE SET qty = app.trade_offer_items.qty + EXCLUDED.qty
                            """,
                            [offer_id, side, pid, qty, base_val, val, discount_type, discount_value],
                        )

            if to_user_id != user["user_id"]:
                want_items = [(pid, qty) for w in wants for pid, qty, *_ in w["items"]]
                give_items = [(pid, qty) for g in gives for pid, qty, *_ in g["items"]]
                _check_and_lock(cur, offer_id, to_user_id, want_items, strict=True)
                _check_and_lock(cur, offer_id, user["user_id"], give_items, strict=False)

            cur.execute(_OFFER_BASE_SQL + " WHERE o.offer_id = %s", [offer_id])
            result = _offer_dict(cur, dict(cur.fetchone()))
    if result["to_user_id"] != result["from_user_id"]:
        _notify_offer(result, "created")
    return result


@router.get("/trade/offers")
def trade_offers_list(user: dict = Depends(_current_user)):
    """All offers where I'm sender or recipient, newest first."""
    ensure_app_trade_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _OFFER_BASE_SQL + " WHERE (o.from_user_id = %s OR o.to_user_id = %s) AND o.kind <> 'fast' "
                "ORDER BY o.created_at DESC LIMIT 200",
                [user["user_id"], user["user_id"]],
            )
            offers = [dict(r) for r in cur.fetchall()]
            return [_offer_dict(cur, o) for o in offers]


@router.patch("/trade/offers/{offer_id}")
def trade_offer_act(offer_id: int, req: OfferAction, user: dict = Depends(_current_user)):
    """Drive the negotiation:
    - accept / decline / counter — only whoever is on turn (awaiting_user_id),
      pending offers only. counter adds cards (availability-checked + locked)
      and flips the turn, so the other party must accept again.
    - cancel — the party NOT on turn while pending, or either party after
      accept (meetup fell through). Releases all locks.
    - complete — either party once accepted: the swap happened; locks release."""
    ensure_app_trade_schema()
    action = (req.action or "").lower()
    if action not in ("accept", "decline", "cancel", "complete", "counter"):
        return JSONResponse(status_code=400, content={"error": "action must be accept, decline, cancel, complete or counter"})

    notify_event = None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_OFFER_BASE_SQL + " WHERE o.offer_id = %s", [offer_id])
            row = cur.fetchone()
            if not row or user["user_id"] not in (row["from_user_id"], row["to_user_id"]):
                raise HTTPException(status_code=404, detail="Offer not found")
            offer = dict(row)
            awaiting = offer.get("awaiting_user_id") or offer["to_user_id"]
            on_turn = user["user_id"] == awaiting
            other_user_id = offer["from_user_id"] if user["user_id"] == offer["to_user_id"] else offer["to_user_id"]

            if action in ("accept", "decline", "counter"):
                if offer["status"] != "pending":
                    return JSONResponse(status_code=409, content={"error": f"Offer is already {offer['status']}"})
                if not on_turn:
                    raise HTTPException(status_code=403, detail="It's the other trader's turn to respond")
            elif action == "cancel":
                if offer["status"] == "pending" and on_turn:
                    raise HTTPException(status_code=403, detail="It's your turn — accept, decline or counter instead")
                if offer["status"] not in ("pending", "accepted"):
                    return JSONResponse(status_code=409, content={"error": f"Offer is already {offer['status']}"})
            elif action == "complete":
                if offer["status"] != "accepted":
                    return JSONResponse(status_code=409, content={"error": "Only an accepted trade can be completed"})

            if action == "counter":
                if not req.add_items:
                    return JSONResponse(status_code=400, content={"error": "Counter needs at least one added card"})
                lock_by_owner: dict[int, list[tuple[str, int]]] = {}
                for it in req.add_items:
                    side = it.side if it.side in ("offer", "request") else "offer"
                    pid = (it.printing_unique_id or "").strip()
                    qty = max(1, min(999, it.qty))
                    snap = _snapshot_values(cur, [OfferItem(printing_unique_id=pid, qty=qty)])[0]
                    cur.execute(
                        "INSERT INTO app.trade_offer_items "
                        "(offer_id, side, printing_unique_id, qty, base_value_sek, value_sek, discount_type, discount_value) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (offer_id, side, printing_unique_id) "
                        "DO UPDATE SET qty = app.trade_offer_items.qty + EXCLUDED.qty",
                        [offer_id, side, pid, qty, snap[2], snap[3], snap[4], snap[5]],
                    )
                    # 'offer' side is given by the sender, 'request' by the recipient.
                    owner = offer["from_user_id"] if side == "offer" else offer["to_user_id"]
                    lock_by_owner.setdefault(owner, []).append((pid, qty))
                for owner, items in lock_by_owner.items():
                    # Strict when reserving the OTHER party's cards; your own
                    # additions are soft-locked like give-items at create time.
                    _check_and_lock(cur, offer_id, owner, items, strict=(owner != user["user_id"]))
                cur.execute(
                    "UPDATE app.trade_offers SET awaiting_user_id = %s, updated_at = NOW() WHERE offer_id = %s",
                    [other_user_id, offer_id],
                )
                offer["awaiting_user_id"] = other_user_id
                notify_event = "countered"
            else:
                new_status = {"accept": "accepted", "decline": "declined",
                              "cancel": "cancelled", "complete": "completed"}[action]
                cur.execute(
                    "UPDATE app.trade_offers SET status = %s, updated_at = NOW(), "
                    "accepted_at = CASE WHEN %s = 'accepted' THEN NOW() ELSE accepted_at END, "
                    "completed_at = CASE WHEN %s = 'completed' THEN NOW() ELSE completed_at END "
                    "WHERE offer_id = %s",
                    [new_status, new_status, new_status, offer_id],
                )
                if new_status in ("declined", "cancelled", "completed"):
                    _release_locks(cur, offer_id)
                if new_status == "accepted" and offer.get("kind") == "list" and offer.get("delete_lists_on_accept"):
                    cur.execute(
                        "DELETE FROM app.cardlists WHERE cardlist_id IN "
                        "(SELECT cardlist_id FROM app.trade_offer_lists WHERE offer_id = %s AND cardlist_id IS NOT NULL)",
                        [offer_id],
                    )
                    cur.execute(
                        "DELETE FROM app.cardlists WHERE cardlist_id = %s AND user_id = %s",
                        [offer.get("offer_list_id"), offer["from_user_id"]],
                    )
                    cur.execute(
                        "DELETE FROM app.cardlists WHERE cardlist_id = %s AND user_id = %s",
                        [offer.get("request_list_id"), offer["to_user_id"]],
                    )
                offer["status"] = new_status
                if new_status in ("accepted", "declined"):
                    notify_event = new_status
            result = _offer_dict(cur, offer)
    # After commit: fire-and-forget notification (cancel/complete stay silent).
    if notify_event:
        _notify_offer(result, notify_event)
    return result
