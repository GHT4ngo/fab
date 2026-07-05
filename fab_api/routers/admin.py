"""Admin/QA endpoints: unmatched, price discrepancies, per-set + overall quality."""

from typing import Optional

from fastapi import APIRouter, Query

from fab_api.core import get_conn

router = APIRouter()


# ── /admin/unmatched ──────────────────────────────────────────────────────────

@router.get("/admin/unmatched")
def get_unmatched(
    reason: Optional[str] = Query(None, description="'no_price' or 'no_match'"),
    page:   int           = Query(1, ge=1),
    page_size: int        = Query(50, ge=1, le=200),
):
    """Cards without a price or without a Cardmarket product match."""
    where = []
    if reason == "no_price":
        where.append("g.has_price = false")
    elif reason == "no_match":
        where.append("g.cm_idproduct IS NULL")
    else:
        where.append("(g.has_price = false OR g.cm_idproduct IS NULL)")

    offset = (page - 1) * page_size
    sql = f"""
        SELECT
            g.printing_unique_id,
            g.display_id,
            g.set_id,
            coalesce(s.set_name, g.set_id) as set_name,
            g.edition,
            g.foiling,
            g.rarity,
            g.name,
            g.match_tier,
            g.cm_idproduct,
            g.price_eur,
            g.price_sek,
            g.has_price,
            COUNT(*) OVER() AS total_count
        FROM gold.gold_cards g
        LEFT JOIN bronze.fab_sets s
            ON s.set_id = g.set_id AND s.edition = g.edition
        WHERE {' AND '.join(where)}
        ORDER BY g.set_id, g.name, g.foiling
        LIMIT %s OFFSET %s
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [page_size, offset])
            rows = cur.fetchall()

    total = rows[0]["total_count"] if rows else 0
    cards = [dict(r) for r in rows]
    for c in cards:
        c.pop("total_count", None)
        c["price_eur"] = float(c["price_eur"]) if c["price_eur"] else None
        c["price_sek"] = int(c["price_sek"]) if c["price_sek"] else None

    return {"total": total, "page": page, "page_size": page_size, "cards": cards}


# ── /admin/price-discrepancies ────────────────────────────────────────────────

@router.get("/admin/price-discrepancies")
def get_price_discrepancies(
    tier:      int   = Query(0, ge=0, le=5,
                             description="Cardmarket match tier to audit "
                                         "(1=anchored, 2=auto, 3=fallback, 5=manual; "
                                         "0=all tiers)"),
    min_ratio: float = Query(2.0, ge=1.0,
                             description="Flag when the two prices differ by ≥ this factor"),
    min_sek:   int   = Query(25, ge=0,
                             description="Ignore cards cheaper than this (SEK) to cut noise"),
    page:      int   = Query(1, ge=1),
    page_size: int   = Query(50, ge=1, le=200),
):
    """Cards where the Cardmarket EUR price and the tcgcsv USD price disagree a lot.

    Both prices are normalised to SEK and compared. Tier 1 (anchored) should rarely
    diverge now that the tcgcsv price drives the Cardmarket pick; remaining gaps are
    EU/US market differences or Alpha-edition cards (no exact tcgcsv price). A gap on a
    tier-5 (manual) card flags a hand-crosswalk row still worth reviewing. Defaults to
    all tiers, worst-first."""
    where = ["g.price_eur IS NOT NULL", "g.tcg_price_usd IS NOT NULL"]
    params: list = []
    if tier:
        where.append("g.match_tier = %s")
        params.append(tier)

    offset = (page - 1) * page_size
    sql = f"""
        WITH usd AS (
            SELECT rate_value FROM bronze.exchange_rates
            WHERE series_id = 'USD/SEK'
            ORDER BY rate_date DESC LIMIT 1
        ),
        cmp AS (
            SELECT
                g.printing_unique_id, g.display_id, g.set_id, g.edition, g.foiling,
                g.rarity, g.name, g.match_tier, g.price_source, g.cm_idproduct,
                g.price_eur, g.tcg_price_usd,
                round(g.price_eur     * g.eur_to_sek_rate, 0)             AS eur_sek,
                round(g.tcg_price_usd * (SELECT rate_value FROM usd), 0)  AS usd_sek
            FROM gold.gold_cards g
            WHERE {' AND '.join(where)}
        )
        SELECT
            cmp.*,
            coalesce(s.set_name, cmp.set_id) AS set_name,
            round(greatest(eur_sek / nullif(usd_sek, 0),
                           usd_sek / nullif(eur_sek, 0)), 2) AS divergence_x,
            COUNT(*) OVER() AS total_count
        FROM cmp
        LEFT JOIN bronze.fab_sets s
            ON s.set_id = cmp.set_id AND s.edition = cmp.edition
        WHERE greatest(eur_sek, usd_sek) >= %s
          AND greatest(eur_sek / nullif(usd_sek, 0),
                       usd_sek / nullif(eur_sek, 0)) >= %s
        ORDER BY divergence_x DESC NULLS LAST, eur_sek DESC
        LIMIT %s OFFSET %s
    """
    params += [min_sek, min_ratio, page_size, offset]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    total = rows[0]["total_count"] if rows else 0
    cards = [dict(r) for r in rows]
    for c in cards:
        c.pop("total_count", None)
        c["price_eur"]     = float(c["price_eur"])     if c["price_eur"]     else None
        c["tcg_price_usd"] = float(c["tcg_price_usd"]) if c["tcg_price_usd"] else None
        c["eur_sek"]       = int(c["eur_sek"])         if c["eur_sek"]       else None
        c["usd_sek"]       = int(c["usd_sek"])         if c["usd_sek"]       else None
        c["divergence_x"]  = float(c["divergence_x"])  if c["divergence_x"]  else None

    return {"total": total, "page": page, "page_size": page_size,
            "tier": tier, "min_ratio": min_ratio, "min_sek": min_sek, "cards": cards}


# ── /admin/sets ───────────────────────────────────────────────────────────────

@router.get("/admin/sets")
def get_admin_sets():
    """Per-set price and data-quality breakdown for the admin panel."""
    sql = """
        WITH usd AS (
            SELECT rate_value
            FROM bronze.exchange_rates
            WHERE series_id = 'USD/SEK'
            ORDER BY rate_date DESC
            LIMIT 1
        ),
        discrepancies AS (
            SELECT
                g.set_id,
                g.edition,
                COUNT(*) AS discrepancy_count
            FROM gold.gold_cards g
            CROSS JOIN usd
            WHERE g.price_eur IS NOT NULL
              AND g.tcg_price_usd IS NOT NULL
              AND greatest(
                    g.price_eur * g.eur_to_sek_rate,
                    g.tcg_price_usd * usd.rate_value
                  ) >= 50
              AND greatest(
                    (g.price_eur * g.eur_to_sek_rate)
                        / nullif(g.tcg_price_usd * usd.rate_value, 0),
                    (g.tcg_price_usd * usd.rate_value)
                        / nullif(g.price_eur * g.eur_to_sek_rate, 0)
                  ) >= 3
            GROUP BY g.set_id, g.edition
        )
        SELECT
            g.set_id,
            coalesce(s.set_name, g.set_id)              as set_name,
            g.edition,
            COUNT(*)                                     as total,
            COUNT(DISTINCT g.name)                       as unique_names,
            COUNT(*) FILTER (WHERE g.has_price)          as priced,
            COUNT(*) FILTER (WHERE NOT g.has_price)      as missing_price,
            COUNT(*) FILTER (WHERE g.cm_idproduct IS NOT NULL) as cm_matched,
            COUNT(*) FILTER (WHERE g.cm_idproduct IS NULL)     as no_cm_match,
            COUNT(*) FILTER (WHERE g.match_tier = 1)     as tier1,
            COUNT(*) FILTER (WHERE g.match_tier = 2)     as tier2,
            COUNT(*) FILTER (WHERE g.match_tier = 3)     as tier3,
            COUNT(*) FILTER (WHERE g.match_tier = 4)     as tier4,
            COUNT(*) FILTER (WHERE g.match_tier = 5)     as tier5,
            COUNT(*) FILTER (WHERE g.match_tier IS NULL) as no_match,
            COUNT(*) FILTER (WHERE g.price_confidence = 'low') as low_confidence,
            COUNT(*) FILTER (
                WHERE g.image_url IS NULL OR trim(g.image_url) = ''
            )                                            as missing_image,
            coalesce(max(d.discrepancy_count), 0)        as discrepancies,
            round(
                COUNT(*) FILTER (WHERE g.has_price) * 100.0 / COUNT(*), 1
            )                                            as coverage_pct
        FROM gold.gold_cards g
        LEFT JOIN bronze.fab_sets s
            ON s.set_id = g.set_id AND s.edition = g.edition
        LEFT JOIN discrepancies d
            ON d.set_id = g.set_id AND d.edition = g.edition
        GROUP BY g.set_id, g.edition, s.set_name
        ORDER BY coverage_pct ASC, missing_price DESC, g.set_id
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    result = [dict(r) for r in rows]
    for r in result:
        r["coverage_pct"] = float(r["coverage_pct"]) if r["coverage_pct"] else 0
    return result


@router.get("/admin/quality")
def get_admin_quality():
    """Overall data-quality counters for the admin panel."""
    sql = """
        WITH usd AS (
            SELECT rate_value
            FROM bronze.exchange_rates
            WHERE series_id = 'USD/SEK'
            ORDER BY rate_date DESC
            LIMIT 1
        ),
        discrepancies AS (
            SELECT COUNT(*) AS total
            FROM gold.gold_cards g
            CROSS JOIN usd
            WHERE g.price_eur IS NOT NULL
              AND g.tcg_price_usd IS NOT NULL
              AND greatest(
                    g.price_eur * g.eur_to_sek_rate,
                    g.tcg_price_usd * usd.rate_value
                  ) >= 50
              AND greatest(
                    (g.price_eur * g.eur_to_sek_rate)
                        / nullif(g.tcg_price_usd * usd.rate_value, 0),
                    (g.tcg_price_usd * usd.rate_value)
                        / nullif(g.price_eur * g.eur_to_sek_rate, 0)
                  ) >= 3
        )
        SELECT
            COUNT(*) AS total_printings,
            COUNT(DISTINCT name) AS unique_names,
            COUNT(DISTINCT set_id) AS set_count,
            COUNT(*) FILTER (WHERE has_price) AS priced,
            COUNT(*) FILTER (WHERE NOT has_price) AS missing_price,
            COUNT(*) FILTER (WHERE cm_idproduct IS NOT NULL) AS cm_matched,
            COUNT(*) FILTER (WHERE cm_idproduct IS NULL) AS no_cm_match,
            COUNT(*) FILTER (WHERE match_tier = 4) AS tcgcsv_only,
            COUNT(*) FILTER (WHERE match_tier IS NULL) AS no_match,
            COUNT(*) FILTER (WHERE price_confidence = 'low') AS low_confidence,
            COUNT(*) FILTER (
                WHERE image_url IS NULL OR trim(image_url) = ''
            ) AS missing_image,
            COUNT(*) FILTER (
                WHERE set_id LIKE '%//%'
            ) AS malformed_set_id,
            (SELECT total FROM discrepancies) AS price_discrepancies
        FROM gold.gold_cards
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    return dict(row)

