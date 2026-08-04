"""Browse endpoints: /cards, /sets, /stats, /card-printings, /cards/parse-list."""

from typing import Optional

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from fab_api import list_parser
from fab_api.core import get_conn

router = APIRouter()


# ── /cards/parse-list ────────────────────────────────────────────────────────

class ParseListRequest(BaseModel):
    text: str
    set_id: Optional[str] = None
    edition: Optional[str] = None


@router.post("/cards/parse-list")
def parse_list(req: ParseListRequest):
    """Turn a pasted text collection into resolved printings.

    Public and read-only. Deliberately conservative: lines that could be two
    printings at very different prices come back as `ambiguous` with every
    candidate, because the catalogue holds same-looking printings that differ
    10× in value. Callers are expected to make a human resolve those."""
    parsed = list_parser.parse_text(req.text or "")
    set_id = (req.set_id or "").strip().upper() or None
    edition = (req.edition or "").strip().upper() or None

    with get_conn() as conn:
        with conn.cursor() as cur:
            detected_set = list_parser.detect_set(cur, parsed["set_text"])
            if not set_id and detected_set:
                set_id = detected_set["set_id"]
            if not edition and parsed["edition_hint"]:
                edition = parsed["edition_hint"]
            entries = list_parser.match_entries(cur, parsed["entries"], set_id, edition)

    # A title line ("Flesh and Blood Monarch First Edition") parses as a card and
    # then matches nothing. Once we know it named the set/edition, retire it to
    # the skipped list instead of reporting a phantom missing card.
    kept = []
    for e in entries:
        if e["status"] == "unmatched" and not e["candidates"] and (
            list_parser.looks_like_heading(e["name"], detected_set["set_name"] if detected_set else None)
            or list_parser.looks_like_noise(e["name"])
        ):
            parsed["skipped_lines"].append(e["name"])
            continue
        kept.append(e)
    entries = kept

    counts = {"matched": 0, "auto_picked": 0, "ambiguous": 0, "unmatched": 0}
    total_qty = 0
    est_sek = 0
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
        total_qty += e["qty"]
        if e["chosen"] and e["chosen"]["value_sek"]:
            est_sek += e["chosen"]["value_sek"] * e["qty"]

    return {
        "set_id": set_id,
        "edition": edition,
        "detected_set": detected_set,
        "detected_edition": parsed["edition_hint"],
        "entries": entries,
        "skipped_lines": parsed["skipped_lines"],
        "counts": counts,
        "total_qty": total_qty,
        "est_market_sek": est_sek,
        "needs_review": counts["ambiguous"] + counts["unmatched"],
    }


# ── /cards ────────────────────────────────────────────────────────────────────

@router.get("/cards")
def get_cards(
    q:        Optional[str]   = Query(None, description="Name search"),
    set_id:   Optional[str]   = Query(None, description="Set code filter"),
    rarity:   Optional[str]   = Query(None, description="Rarity filter"),
    foiling:  Optional[str]   = Query(None, description="Foiling: S, R, C, G"),
    has_price: Optional[bool] = Query(None, description="Only cards with price"),
    on_trade: Optional[bool]  = Query(None, description="Only cards on someone's trade list"),
    trade_owner: Optional[str] = Query(None, description="Only cards on this username's trade lists"),
    page:     int             = Query(1,    ge=1),
    page_size: int            = Query(40,   ge=1, le=500),
):
    """
    Browse cards. Returns paginated results with price in EUR, USD, SEK, plus
    trade availability (copies on public trade lists + number of traders).
    """
    where = []
    params = []

    if q:
        where.append("g.name ILIKE %s")
        params.append(f"%{q}%")
    if set_id:
        where.append("g.set_id = %s")
        params.append(set_id.upper())
    if rarity:
        where.append("g.rarity ILIKE %s")
        params.append(rarity)
    if foiling:
        where.append("g.foiling = %s")
        params.append(foiling.upper())
    if has_price is True:
        where.append("g.has_price = true")
    elif has_price is False:
        where.append("g.has_price = false")
    if on_trade is True:
        where.append("COALESCE(tr.trade_qty, 0) > 0")
    if trade_owner and trade_owner.strip():
        where.append(
            "EXISTS (SELECT 1 FROM app.cardlist_items ti "
            "JOIN app.cardlists tl ON tl.cardlist_id = ti.cardlist_id AND tl.is_trade_list "
            "JOIN app.users tu ON tu.user_id = tl.user_id "
            "WHERE ti.printing_unique_id = g.printing_unique_id AND lower(tu.username) = lower(%s))"
        )
        params.append(trade_owner.strip())

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * page_size

    sql = f"""
        WITH usd AS (
            SELECT rate_value FROM bronze.exchange_rates
            WHERE series_id = 'USD/SEK'
            ORDER BY rate_date DESC LIMIT 1
        )
        SELECT
            g.printing_unique_id,
            g.display_id,
            g.set_id,
            coalesce(s.set_name, g.set_id) as set_name,
            g.edition,
            g.foiling,
            g.rarity,
            g.name,
            g.variant,
            g.pitch,
            g.cost,
            g.power,
            g.defense,
            g.health,
            g.intelligence,
            g.type_text,
            g.functional_text,
            g.image_url,
            g.cm_idproduct,
            g.match_tier,
            g.cc_technique,
            g.price_eur,
            g.tcg_price_usd,
            g.tcg_fetched_at,
            g.price_sek,
            round(g.price_eur     * g.eur_to_sek_rate, 0)            AS price_eur_sek,
            round(g.tcg_price_usd * (SELECT rate_value FROM usd), 0) AS price_usd_sek,
            g.price_source,
            g.price_confidence,
            g.is_foil,
            g.has_price,
            COALESCE(tr.trade_qty, 0)     AS trade_qty,
            COALESCE(tr.trade_sellers, 0) AS trade_sellers,
            COUNT(*) OVER() AS total_count
        FROM gold.gold_cards g
        LEFT JOIN bronze.fab_sets s
            ON s.set_id = g.set_id AND s.edition = g.edition
        LEFT JOIN (
            SELECT i.printing_unique_id,
                   SUM(i.qty)               AS trade_qty,
                   COUNT(DISTINCT l.user_id) AS trade_sellers
            FROM app.cardlist_items i
            JOIN app.cardlists l ON l.cardlist_id = i.cardlist_id AND l.is_trade_list
            GROUP BY i.printing_unique_id
        ) tr ON tr.printing_unique_id = g.printing_unique_id
        {where_sql}
        ORDER BY g.name, g.set_id, g.edition, g.foiling
        LIMIT %s OFFSET %s
    """
    params += [page_size, offset]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    total = rows[0]["total_count"] if rows else 0
    cards = [dict(r) for r in rows]
    for c in cards:
        c.pop("total_count", None)
        c["price_eur"]      = float(c["price_eur"])     if c["price_eur"]     else None
        c["tcg_price_usd"]  = float(c["tcg_price_usd"]) if c["tcg_price_usd"] else None
        c["price_sek"]      = int(c["price_sek"])        if c["price_sek"]     else None
        c["price_eur_sek"]  = int(c["price_eur_sek"])    if c["price_eur_sek"] else None
        c["price_usd_sek"]  = int(c["price_usd_sek"])    if c["price_usd_sek"] else None
        c["tcg_fetched_at"] = c["tcg_fetched_at"].isoformat() if c["tcg_fetched_at"] else None
        c["trade_qty"]      = int(c["trade_qty"])
        c["trade_sellers"]  = int(c["trade_sellers"])

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "cards": cards,
    }


# ── /sets ─────────────────────────────────────────────────────────────────────

@router.get("/sets")
def get_sets(response: Response):
    """List all sets that have cards in gold_cards."""
    # Changes at most once a day (after the pipeline) — let browsers cache it.
    response.headers["Cache-Control"] = "public, max-age=3600"
    sql = """
        SELECT
            g.set_id,
            coalesce(s.set_name, g.set_id)      as set_name,
            g.edition,
            s.initial_release_date::text         as release_date,
            COUNT(*)                             as card_count,
            COUNT(*) FILTER (WHERE g.has_price)  as priced_count
        FROM gold.gold_cards g
        LEFT JOIN bronze.fab_sets s
            ON s.set_id = g.set_id AND s.edition = g.edition
        GROUP BY g.set_id, g.edition, s.set_name, s.initial_release_date
        ORDER BY s.initial_release_date NULLS LAST, s.set_name, g.edition
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


# ── /stats ────────────────────────────────────────────────────────────────────

@router.get("/stats")
def get_stats(response: Response):
    """Overall price coverage and match tier breakdown."""
    response.headers["Cache-Control"] = "public, max-age=600"
    sql = """
        SELECT
            COUNT(*)                                    as total_cards,
            COUNT(*) FILTER (WHERE has_price)           as priced_cards,
            COUNT(*) FILTER (WHERE match_tier = 1)      as tier1_anchored,
            COUNT(*) FILTER (WHERE match_tier = 2)      as tier2_auto,
            COUNT(*) FILTER (WHERE match_tier = 3)      as tier3_fallback,
            COUNT(*) FILTER (WHERE match_tier = 4)      as tier4_tcgcsv,
            COUNT(*) FILTER (WHERE match_tier = 5)      as tier5_manual,
            COUNT(*) FILTER (WHERE match_tier IS NULL)  as no_match,
            round(
                COUNT(*) FILTER (WHERE has_price) * 100.0 / COUNT(*), 1
            )                                           as coverage_pct
        FROM gold.gold_cards
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    result = dict(row)
    for k, v in result.items():
        result[k] = float(v) if v is not None else 0
    return result


@router.get("/card-printings")
def card_printings(name: str):
    """
    All printings for a given card name — used by the scanner My Cardlist.
    Returns set_id, set_name, edition, foiling, rarity, image_url, price_eur,
    tcg_price_usd, price_sek, cm_idproduct, printing_unique_id.
    """
    sql = """
        SELECT
            g.printing_unique_id,
            g.display_id,
            g.set_id,
            coalesce(s.set_name, g.set_id) AS set_name,
            g.edition,
            g.foiling,
            g.rarity,
            g.variant,
            g.pitch,
            g.image_url,
            g.price_eur,
            g.tcg_price_usd,
            g.price_sek,
            g.trade_value_sek,
            g.cm_idproduct
        FROM gold.gold_cards g
        LEFT JOIN bronze.fab_sets s
            ON s.set_id = g.set_id AND s.edition = g.edition
        WHERE g.name ILIKE %s
        ORDER BY g.set_id, g.edition, g.foiling
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [name])
            rows = cur.fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d["price_eur"]     = float(d["price_eur"])     if d["price_eur"]     else None
        d["tcg_price_usd"] = float(d["tcg_price_usd"]) if d["tcg_price_usd"] else None
        d["price_sek"]     = int(d["price_sek"])        if d["price_sek"]     else None
        d["trade_value_sek"] = int(d["trade_value_sek"]) if d["trade_value_sek"] else None
        result.append(d)
    return result
