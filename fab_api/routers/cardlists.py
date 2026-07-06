"""Named cardlists CRUD — ownership enforced via auth._get_owned_cardlist."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fab_api.core import get_conn
from fab_api.routers.auth import _current_user, _get_owned_cardlist
from fab_api.routers.trade import ensure_app_trade_schema

router = APIRouter()


class CardlistCreate(BaseModel):
    name: str


class CardlistUpdate(BaseModel):
    name: str | None = None
    is_trade_list: bool | None = None


class CardlistItemAdd(BaseModel):
    printing_unique_id: str
    qty: int = 1


class CardlistItemQty(BaseModel):
    qty: int



@router.get("/cardlists")
def cardlists_list(user: dict = Depends(_current_user)):
    """All of the current user's cardlists with item count + total SEK value."""
    ensure_app_trade_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    l.cardlist_id, l.name, l.is_trade_list, l.created_at, l.updated_at,
                    COALESCE(SUM(i.qty), 0)                        AS item_count,
                    COALESCE(SUM(i.qty * COALESCE(g.price_sek, 0)), 0) AS total_sek
                FROM app.cardlists l
                LEFT JOIN app.cardlist_items i ON i.cardlist_id = l.cardlist_id
                LEFT JOIN gold.gold_cards g    ON g.printing_unique_id = i.printing_unique_id
                WHERE l.user_id = %s
                GROUP BY l.cardlist_id
                ORDER BY l.updated_at DESC
                """,
                [user["user_id"]],
            )
            rows = cur.fetchall()
    return [{
        "cardlist_id": r["cardlist_id"],
        "name": r["name"],
        "is_trade_list": r["is_trade_list"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        "item_count": int(r["item_count"]),
        "total_sek": int(r["total_sek"]),
    } for r in rows]


@router.post("/cardlists")
def cardlists_create(req: CardlistCreate, user: dict = Depends(_current_user)):
    """Create a new named cardlist."""
    name = (req.name or "").strip()[:120]
    if not name:
        return JSONResponse(status_code=400, content={"error": "Name is required"})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.cardlists (user_id, name) VALUES (%s, %s) "
                "RETURNING cardlist_id, name, created_at, updated_at",
                [user["user_id"], name],
            )
            r = cur.fetchone()
    return {
        "cardlist_id": r["cardlist_id"],
        "name": r["name"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        "item_count": 0,
        "total_sek": 0,
    }


@router.get("/cardlists/{cardlist_id}")
def cardlists_get(cardlist_id: int, user: dict = Depends(_current_user)):
    """A cardlist with its items joined to gold.gold_cards for display."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            lst = _get_owned_cardlist(cur, user["user_id"], cardlist_id)
            cur.execute(
                """
                SELECT
                    i.printing_unique_id, i.qty, i.added_at,
                    g.name, g.set_id, g.edition, g.foiling,
                    g.rarity, g.image_url, g.price_sek
                FROM app.cardlist_items i
                LEFT JOIN gold.gold_cards g ON g.printing_unique_id = i.printing_unique_id
                WHERE i.cardlist_id = %s
                ORDER BY i.added_at DESC
                """,
                [cardlist_id],
            )
            items = cur.fetchall()

    out_items = []
    total = 0
    for it in items:
        price = int(it["price_sek"]) if it["price_sek"] is not None else None
        total += (price or 0) * it["qty"]
        out_items.append({
            "printing_unique_id": it["printing_unique_id"],
            "qty": it["qty"],
            "added_at": it["added_at"].isoformat() if it["added_at"] else None,
            "name": it["name"],
            "set_id": it["set_id"],
            "edition": it["edition"],
            "foiling": it["foiling"],
            "rarity": it["rarity"],
            "image_url": it["image_url"],
            "price_sek": price,
        })
    return {
        "cardlist_id": lst["cardlist_id"],
        "name": lst["name"],
        "is_trade_list": lst.get("is_trade_list", False),
        "created_at": lst["created_at"].isoformat() if lst["created_at"] else None,
        "updated_at": lst["updated_at"].isoformat() if lst["updated_at"] else None,
        "item_count": sum(i["qty"] for i in out_items),
        "total_sek": total,
        "items": out_items,
    }


@router.patch("/cardlists/{cardlist_id}")
def cardlists_update(cardlist_id: int, req: CardlistUpdate, user: dict = Depends(_current_user)):
    """Rename a cardlist and/or flip its trade-list flag (Phase 4: a trade-flagged
    list's items show up in the public /trade/listings marketplace)."""
    ensure_app_trade_schema()
    name = (req.name or "").strip()[:120] if req.name is not None else None
    if req.name is not None and not name:
        return JSONResponse(status_code=400, content={"error": "Name is required"})
    if name is None and req.is_trade_list is None:
        return JSONResponse(status_code=400, content={"error": "Nothing to update"})
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_owned_cardlist(cur, user["user_id"], cardlist_id)
            if name is not None:
                cur.execute(
                    "UPDATE app.cardlists SET name = %s, updated_at = NOW() WHERE cardlist_id = %s",
                    [name, cardlist_id],
                )
            if req.is_trade_list is not None:
                cur.execute(
                    "UPDATE app.cardlists SET is_trade_list = %s, updated_at = NOW() WHERE cardlist_id = %s",
                    [req.is_trade_list, cardlist_id],
                )
            cur.execute(
                "SELECT cardlist_id, name, is_trade_list FROM app.cardlists WHERE cardlist_id = %s",
                [cardlist_id],
            )
            r = dict(cur.fetchone())
    return r


@router.delete("/cardlists/{cardlist_id}")
def cardlists_delete(cardlist_id: int, user: dict = Depends(_current_user)):
    """Delete a cardlist and its items (cascade)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_owned_cardlist(cur, user["user_id"], cardlist_id)
            cur.execute("DELETE FROM app.cardlists WHERE cardlist_id = %s", [cardlist_id])
    return {"ok": True, "cardlist_id": cardlist_id}


@router.post("/cardlists/{cardlist_id}/items")
def cardlists_add_item(cardlist_id: int, req: CardlistItemAdd, user: dict = Depends(_current_user)):
    """Add a printing to a cardlist (adds to qty if it's already there)."""
    printing_id = (req.printing_unique_id or "").strip()
    qty = max(1, min(9999, req.qty))
    if not printing_id:
        return JSONResponse(status_code=400, content={"error": "printing_unique_id is required"})
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_owned_cardlist(cur, user["user_id"], cardlist_id)
            cur.execute("SELECT 1 FROM gold.gold_cards WHERE printing_unique_id = %s", [printing_id])
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Unknown printing_unique_id")
            cur.execute(
                """
                INSERT INTO app.cardlist_items (cardlist_id, printing_unique_id, qty)
                VALUES (%s, %s, %s)
                ON CONFLICT (cardlist_id, printing_unique_id)
                DO UPDATE SET qty = app.cardlist_items.qty + EXCLUDED.qty
                RETURNING printing_unique_id, qty
                """,
                [cardlist_id, printing_id, qty],
            )
            r = cur.fetchone()
            cur.execute("UPDATE app.cardlists SET updated_at = NOW() WHERE cardlist_id = %s", [cardlist_id])
    return {"printing_unique_id": r["printing_unique_id"], "qty": r["qty"]}


@router.patch("/cardlists/{cardlist_id}/items/{printing_unique_id}")
def cardlists_set_item_qty(cardlist_id: int, printing_unique_id: str, req: CardlistItemQty,
                           user: dict = Depends(_current_user)):
    """Set an item's quantity. qty <= 0 removes it."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_owned_cardlist(cur, user["user_id"], cardlist_id)
            if req.qty <= 0:
                cur.execute(
                    "DELETE FROM app.cardlist_items WHERE cardlist_id = %s AND printing_unique_id = %s",
                    [cardlist_id, printing_unique_id],
                )
                removed = True
            else:
                cur.execute(
                    "UPDATE app.cardlist_items SET qty = %s "
                    "WHERE cardlist_id = %s AND printing_unique_id = %s RETURNING qty",
                    [min(9999, req.qty), cardlist_id, printing_unique_id],
                )
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Item not in cardlist")
                removed = False
            cur.execute("UPDATE app.cardlists SET updated_at = NOW() WHERE cardlist_id = %s", [cardlist_id])
    return {"printing_unique_id": printing_unique_id, "qty": 0 if removed else min(9999, req.qty), "removed": removed}


@router.delete("/cardlists/{cardlist_id}/items/{printing_unique_id}")
def cardlists_remove_item(cardlist_id: int, printing_unique_id: str, user: dict = Depends(_current_user)):
    """Remove a printing from a cardlist."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            _get_owned_cardlist(cur, user["user_id"], cardlist_id)
            cur.execute(
                "DELETE FROM app.cardlist_items WHERE cardlist_id = %s AND printing_unique_id = %s",
                [cardlist_id, printing_unique_id],
            )
            cur.execute("UPDATE app.cardlists SET updated_at = NOW() WHERE cardlist_id = %s", [cardlist_id])
    return {"ok": True, "printing_unique_id": printing_unique_id}
