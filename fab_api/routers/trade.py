"""Phase 4 trading: trade-flagged cardlists become public listings; users send
trade offers (give/want item bundles) valued by gold.trade_value_sek — the
"trend price, or low if it's higher" rule. Accepting an offer records the deal;
the physical swap happens in person (no inventory is moved automatically)."""

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

CREATE TABLE IF NOT EXISTS app.trade_offers (
    offer_id     BIGSERIAL   PRIMARY KEY,
    from_user_id BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    to_user_id   BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    status       TEXT        NOT NULL DEFAULT 'pending',
    message      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS trade_offers_to   ON app.trade_offers (to_user_id, status);
CREATE INDEX IF NOT EXISTS trade_offers_from ON app.trade_offers (from_user_id, status);

CREATE TABLE IF NOT EXISTS app.trade_offer_items (
    item_id            BIGSERIAL PRIMARY KEY,
    offer_id           BIGINT    NOT NULL REFERENCES app.trade_offers(offer_id) ON DELETE CASCADE,
    side               TEXT      NOT NULL,
    printing_unique_id TEXT      NOT NULL,
    qty                INTEGER   NOT NULL DEFAULT 1,
    value_sek          NUMERIC,
    UNIQUE (offer_id, side, printing_unique_id)
);
"""

_trade_schema_ready = False


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


class OfferCreate(BaseModel):
    to_user_id: int
    message: Optional[str] = None
    offer_items: list[OfferItem] = []     # what the sender GIVES
    request_items: list[OfferItem] = []   # what the sender WANTS


class OfferAction(BaseModel):
    action: str   # accept | decline | cancel


# ── Listings ──────────────────────────────────────────────────────────────────

@router.get("/trade/listings")
def trade_listings(
    q:         Optional[str] = Query(None, description="Card name search"),
    set_id:    Optional[str] = Query(None),
    page:      int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """All items in trade-flagged cardlists across ALL users, valued by
    trade_value_sek. Public — this is the marketplace browse view."""
    ensure_app_trade_schema()
    where = ["l.is_trade_list = true"]
    params: list = []
    if q and q.strip():
        where.append("g.name ILIKE %s")
        params.append(f"%{q.strip()}%")
    if set_id:
        where.append("g.set_id = %s")
        params.append(set_id.upper())

    offset = (page - 1) * page_size
    sql = f"""
        SELECT
            i.printing_unique_id, i.qty,
            l.cardlist_id, l.name AS list_name,
            u.user_id AS owner_id, u.email AS owner_email,
            g.name, g.display_id, g.set_id,
            coalesce(s.set_name, g.set_id) AS set_name,
            g.edition, g.foiling, g.is_foil, g.rarity, g.pitch, g.image_url,
            g.trade_value_sek,
            COUNT(*) OVER() AS total_count
        FROM app.cardlist_items i
        JOIN app.cardlists l ON l.cardlist_id = i.cardlist_id
        JOIN app.users u     ON u.user_id = l.user_id
        JOIN gold.gold_cards g ON g.printing_unique_id = i.printing_unique_id
        LEFT JOIN bronze.fab_sets s ON s.set_id = g.set_id AND s.edition = g.edition
        WHERE {' AND '.join(where)}
        ORDER BY g.name, g.set_id, u.email
        LIMIT %s OFFSET %s
    """
    params += [page_size, offset]
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
        items.append(d)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


# ── Offers ────────────────────────────────────────────────────────────────────

def _snapshot_values(cur, items: list[OfferItem]) -> list[tuple[str, int, Optional[float]]]:
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
        val = float(row["trade_value_sek"]) if row["trade_value_sek"] is not None else None
        out.append((pid, qty, val))
    return out


def _offer_dict(cur, offer_row: dict) -> dict:
    """Offer + its items joined to gold for display, plus per-side totals."""
    cur.execute(
        """
        SELECT
            t.side, t.printing_unique_id, t.qty, t.value_sek,
            g.name, g.display_id, g.set_id, g.edition, g.foiling, g.rarity, g.image_url
        FROM app.trade_offer_items t
        LEFT JOIN gold.gold_cards g ON g.printing_unique_id = t.printing_unique_id
        WHERE t.offer_id = %s
        ORDER BY t.side, g.name
        """,
        [offer_row["offer_id"]],
    )
    items = []
    totals = {"offer": 0, "request": 0}
    for r in cur.fetchall():
        d = dict(r)
        d["value_sek"] = float(d["value_sek"]) if d["value_sek"] is not None else None
        if d["value_sek"] is not None:
            totals[d["side"]] += d["value_sek"] * d["qty"]
        items.append(d)
    return {
        "offer_id": offer_row["offer_id"],
        "from_user_id": offer_row["from_user_id"],
        "from_email": offer_row["from_email"],
        "to_user_id": offer_row["to_user_id"],
        "to_email": offer_row["to_email"],
        "status": offer_row["status"],
        "message": offer_row["message"],
        "created_at": offer_row["created_at"].isoformat() if offer_row["created_at"] else None,
        "updated_at": offer_row["updated_at"].isoformat() if offer_row["updated_at"] else None,
        "items": items,
        "offer_total_sek": int(totals["offer"]),
        "request_total_sek": int(totals["request"]),
    }


_OFFER_BASE_SQL = """
    SELECT o.*, fu.email AS from_email, tu.email AS to_email
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
            return "<li style='color:#888'>nothing</li>"
        return "".join(
            f"<li>{i['name']} ×{i['qty']} — {_kr((i['value_sek'] or 0) * i['qty'])}</li>"
            for i in items
        )

    return (
        f"<p><b>{recv_label}</b> ({_kr(offer['offer_total_sek'])}):</p><ul>{rows(recv)}</ul>"
        f"<p><b>{give_label}</b> ({_kr(offer['request_total_sek'])}):</p><ul>{rows(give)}</ul>"
    )


def _notify_offer(offer: dict, event: str) -> None:
    """Email the party who should hear about `event` ('created' → recipient,
    'accepted'/'declined' → sender). Async + best-effort: never blocks or fails
    the API call. Cancel is deliberately silent (noise)."""
    base = _public_base_url() or "https://fabmatrix.t4ngo.com"
    link = f"{base}/trade"
    if event == "created":
        to, subject = offer["to_email"], f"New trade offer from {offer['from_email']}"
        intro = f"<p><b>{offer['from_email']}</b> sent you a trade offer:</p>"
        viewer = "recipient"
    elif event in ("accepted", "declined"):
        to, subject = offer["from_email"], f"Your trade offer was {event}"
        intro = f"<p><b>{offer['to_email']}</b> {event} your trade offer:</p>"
        viewer = "sender"
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
    """Send a trade offer: what you give (offer_items) and/or want (request_items).
    Item values are snapshotted at send time so later price moves don't rewrite
    a standing offer."""
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
                "INSERT INTO app.trade_offers (from_user_id, to_user_id, message) "
                "VALUES (%s, %s, %s) RETURNING offer_id",
                [user["user_id"], req.to_user_id, message],
            )
            offer_id = cur.fetchone()["offer_id"]
            for side, rows in (("offer", give), ("request", want)):
                for pid, qty, val in rows:
                    cur.execute(
                        "INSERT INTO app.trade_offer_items "
                        "(offer_id, side, printing_unique_id, qty, value_sek) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (offer_id, side, printing_unique_id) "
                        "DO UPDATE SET qty = app.trade_offer_items.qty + EXCLUDED.qty",
                        [offer_id, side, pid, qty, val],
                    )
            cur.execute(_OFFER_BASE_SQL + " WHERE o.offer_id = %s", [offer_id])
            result = _offer_dict(cur, dict(cur.fetchone()))
    # Outside the transaction (committed) — notify the recipient, fire-and-forget.
    _notify_offer(result, "created")
    return result


@router.get("/trade/offers")
def trade_offers_list(user: dict = Depends(_current_user)):
    """All offers where I'm sender or recipient, newest first."""
    ensure_app_trade_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _OFFER_BASE_SQL + " WHERE o.from_user_id = %s OR o.to_user_id = %s "
                "ORDER BY o.created_at DESC LIMIT 200",
                [user["user_id"], user["user_id"]],
            )
            offers = [dict(r) for r in cur.fetchall()]
            return [_offer_dict(cur, o) for o in offers]


@router.patch("/trade/offers/{offer_id}")
def trade_offer_act(offer_id: int, req: OfferAction, user: dict = Depends(_current_user)):
    """accept/decline (recipient) or cancel (sender) a pending offer."""
    ensure_app_trade_schema()
    action = (req.action or "").lower()
    if action not in ("accept", "decline", "cancel"):
        return JSONResponse(status_code=400, content={"error": "action must be accept, decline or cancel"})

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_OFFER_BASE_SQL + " WHERE o.offer_id = %s", [offer_id])
            row = cur.fetchone()
            if not row or user["user_id"] not in (row["from_user_id"], row["to_user_id"]):
                raise HTTPException(status_code=404, detail="Offer not found")
            offer = dict(row)
            if offer["status"] != "pending":
                return JSONResponse(status_code=409, content={"error": f"Offer is already {offer['status']}"})

            is_recipient = user["user_id"] == offer["to_user_id"]
            if action in ("accept", "decline") and not is_recipient:
                raise HTTPException(status_code=403, detail="Only the recipient can accept or decline")
            if action == "cancel" and is_recipient:
                raise HTTPException(status_code=403, detail="Only the sender can cancel")

            new_status = {"accept": "accepted", "decline": "declined", "cancel": "cancelled"}[action]
            cur.execute(
                "UPDATE app.trade_offers SET status = %s, updated_at = NOW() WHERE offer_id = %s",
                [new_status, offer_id],
            )
            offer["status"] = new_status
            result = _offer_dict(cur, offer)
    # After commit: tell the sender their offer was accepted/declined (cancel is silent).
    if new_status in ("accepted", "declined"):
        _notify_offer(result, new_status)
    return result
