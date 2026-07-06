"""Advanced-search tools. /tools/price-gap finds cards whose Cardmarket EUR and
tcgcsv USD prices disagree after converting both to SEK — the arbitrage explorer
behind the web Tools tab. /tools/classes feeds its class/talent filter."""

import re
from typing import Optional

from fastapi import APIRouter, Query, Response

from fab_api.core import get_conn

router = APIRouter()

# Sortable columns — whitelist so sort params can never inject SQL.
_SORT_COLUMNS = {
    "name":       "name",
    "set_id":     "set_id",
    "rarity":     "rarity",
    "eur_sek":    "eur_sek",
    "usd_sek":    "usd_sek",
    "diff_sek":   "diff_sek",
    "diff_pct":   "diff_pct",
    "gap_pct":    "gap_pct",
    "price_eur":  "price_eur",
    "price_usd":  "tcg_price_usd",
}

# Structural type words — everything else in type_text is a class or talent.
_TYPE_STOPWORDS = (
    "Action", "Attack", "Reaction", "Defense", "Instant", "Equipment", "Item",
    "Weapon", "Hero", "Young", "Token", "Ally", "Aura", "Landmark", "Arrow",
    "Resource", "Mentor", "Demi-Hero", "Invocation", "Affliction", "Figment",
    "Block", "Base", "Trap", "Construct", "Evo", "Gem", "Quiver", "Off-Hand",
    "1H", "2H", "(1H)", "(2H)", "Event", "Macro",
)


@router.get("/tools/classes")
def tools_classes(response: Response):
    """Distinct class/talent tokens found in gold type_text, most common first.
    Data-driven so new sets' classes appear without a code change."""
    response.headers["Cache-Control"] = "public, max-age=3600"
    sql = """
        SELECT tok, COUNT(*) AS n
        FROM (
            SELECT unnest(string_to_array(
                translate(split_part(type_text, ' - ', 1), ';,', '  '), ' '
            )) AS tok
            FROM gold.gold_cards
            WHERE type_text IS NOT NULL
        ) t
        WHERE tok <> '' AND tok !~ '[^A-Za-z-]'
        GROUP BY tok
        HAVING COUNT(*) >= 5
        ORDER BY n DESC
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return [r["tok"] for r in rows if r["tok"] not in _TYPE_STOPWORDS]


@router.get("/tools/price-gap")
def tools_price_gap(
    direction: str = Query("any", description="'usd' = USD pricier, 'eur' = EUR pricier, 'any'"),
    min_pct:   float = Query(0, ge=0, description="Minimum gap: pricier side ≥ this % above the cheaper"),
    rarity:    Optional[str] = Query(None),
    set_id:    Optional[str] = Query(None),
    card_class: Optional[str] = Query(None, description="Class/talent word matched in type_text"),
    foil:      Optional[bool] = Query(None, description="true = foil printings only, false = non-foil"),
    sort_by:   str = Query("gap_pct"),
    sort_dir:  str = Query("desc"),
    page:      int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    """Cards priced on BOTH markets whose EUR and USD prices (normalised to SEK)
    diverge. Rows without both prices are excluded by definition. diff_* is signed
    (positive = USD pricier); gap_pct is the absolute divergence used by min_pct."""
    where = ["g.price_eur > 0", "g.tcg_price_usd > 0"]
    params: list = []

    if rarity:
        where.append("g.rarity = %s")
        params.append(rarity.upper())
    if set_id:
        where.append("g.set_id = %s")
        params.append(set_id.upper())
    if card_class:
        # Word-boundary regex on type_text; input reduced to letters/hyphen so it
        # can't alter the pattern.
        cleaned = re.sub(r"[^A-Za-z-]", "", card_class)[:40]
        if cleaned:
            where.append(r"g.type_text ~* ('\m' || %s || '\M')")
            params.append(cleaned)
    if foil is True:
        where.append("g.is_foil = true")
    elif foil is False:
        where.append("g.is_foil = false")

    having = []
    if direction == "usd":
        having.append("diff_sek > 0")
    elif direction == "eur":
        having.append("diff_sek < 0")
    if min_pct > 0:
        having.append("gap_pct >= %s")

    sort_col = _SORT_COLUMNS.get(sort_by, "gap_pct")
    sort_sql = f"{sort_col} {'ASC' if sort_dir.lower() == 'asc' else 'DESC'}"

    offset = (page - 1) * page_size
    sql = f"""
        WITH usd AS (
            SELECT rate_value FROM bronze.exchange_rates
            WHERE series_id = 'USD/SEK'
            ORDER BY rate_date DESC LIMIT 1
        ),
        priced AS (
            SELECT
                g.printing_unique_id, g.display_id, g.set_id, g.edition,
                g.foiling, g.is_foil, g.rarity, g.name, g.pitch, g.type_text,
                g.image_url, g.price_eur, g.tcg_price_usd,
                round(g.price_eur     * g.eur_to_sek_rate, 0)            AS eur_sek,
                round(g.tcg_price_usd * (SELECT rate_value FROM usd), 0) AS usd_sek
            FROM gold.gold_cards g
            WHERE {' AND '.join(where)}
        ),
        gaps AS (
            SELECT
                priced.*,
                usd_sek - eur_sek AS diff_sek,
                round((usd_sek - eur_sek) * 100.0 / nullif(eur_sek, 0), 1) AS diff_pct,
                round((greatest(eur_sek, usd_sek) * 100.0
                       / nullif(least(eur_sek, usd_sek), 0)) - 100, 1)     AS gap_pct
            FROM priced
            WHERE eur_sek > 0 AND usd_sek > 0
        )
        SELECT
            gaps.*,
            coalesce(s.set_name, gaps.set_id) AS set_name,
            COUNT(*) OVER() AS total_count
        FROM gaps
        LEFT JOIN bronze.fab_sets s
            ON s.set_id = gaps.set_id AND s.edition = gaps.edition
        {('WHERE ' + ' AND '.join(having)) if having else ''}
        ORDER BY {sort_sql}, gaps.name ASC
        LIMIT %s OFFSET %s
    """
    if min_pct > 0:
        params.append(min_pct)
    params += [page_size, offset]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    total = rows[0]["total_count"] if rows else 0
    cards = [dict(r) for r in rows]
    for c in cards:
        c.pop("total_count", None)
        c["price_eur"]     = float(c["price_eur"])
        c["tcg_price_usd"] = float(c["tcg_price_usd"])
        c["eur_sek"]       = int(c["eur_sek"])
        c["usd_sek"]       = int(c["usd_sek"])
        c["diff_sek"]      = int(c["diff_sek"])
        c["diff_pct"]      = float(c["diff_pct"]) if c["diff_pct"] is not None else None
        c["gap_pct"]       = float(c["gap_pct"]) if c["gap_pct"] is not None else None

    return {
        "total": total, "page": page, "page_size": page_size,
        "direction": direction, "min_pct": min_pct,
        "sort_by": sort_by if sort_by in _SORT_COLUMNS else "gap_pct",
        "sort_dir": "asc" if sort_dir.lower() == "asc" else "desc",
        "cards": cards,
    }
