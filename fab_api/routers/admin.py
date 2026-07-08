"""Admin/QA endpoints: unmatched, price discrepancies, per-set + overall quality."""

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fab_api.core import get_conn
from fab_api.routers.auth import _clean_username, _current_user, ensure_app_auth_schema

router = APIRouter()


BUG_REPORT_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.bug_reports (
    bug_id      BIGSERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES app.users(user_id) ON DELETE SET NULL,
    email       TEXT,
    username    TEXT,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS bug_reports_status ON app.bug_reports (status, created_at DESC);
"""


class AdminUserUpdate(BaseModel):
    username: Optional[str] = None
    is_dev: Optional[bool] = None


class BugReportCreate(BaseModel):
    message: str


class BugReportUpdate(BaseModel):
    status: str


def ensure_admin_schema():
    ensure_app_auth_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(BUG_REPORT_SCHEMA_SQL)


def _require_dev(user: dict = Depends(_current_user)) -> dict:
    if not user.get("is_dev"):
        raise HTTPException(status_code=403, detail="Dev account required")
    return user


def _optional_user(authorization: Optional[str] = Header(None)) -> Optional[dict]:
    if not authorization:
        return None
    try:
        return _current_user(authorization)
    except HTTPException:
        return None


@router.post("/bug-reports")
def create_bug_report(req: BugReportCreate, user: Optional[dict] = Depends(_optional_user)):
    ensure_admin_schema()
    message = (req.message or "").strip()[:3000]
    if len(message) < 3:
        return JSONResponse(status_code=400, content={"error": "Write a short bug description"})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.bug_reports (user_id, email, username, message)
                VALUES (%s, %s, %s, %s)
                RETURNING bug_id, created_at
                """,
                [
                    user.get("user_id") if user else None,
                    user.get("email") if user else None,
                    user.get("username") if user else None,
                    message,
                ],
            )
            row = cur.fetchone()
    return {"ok": True, "bug_id": row["bug_id"], "created_at": row["created_at"].isoformat()}


@router.get("/admin/users", dependencies=[Depends(_require_dev)])
def admin_users(q: Optional[str] = Query(None), page_size: int = Query(100, ge=1, le=300)):
    ensure_admin_schema()
    where = []
    params: list = []
    if q and q.strip():
        where.append("(email ILIKE %s OR username ILIKE %s)")
        needle = f"%{q.strip()}%"
        params += [needle, needle]
    sql = """
        SELECT user_id, email, username, is_dev, created_at, last_login_at
        FROM app.users
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(page_size)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [{
        "user_id": r["user_id"],
        "email": r["email"],
        "username": r["username"],
        "is_dev": bool(r["is_dev"]),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "last_login_at": r["last_login_at"].isoformat() if r["last_login_at"] else None,
    } for r in rows]


@router.patch("/admin/users/{user_id}", dependencies=[Depends(_require_dev)])
def admin_update_user(user_id: int, req: AdminUserUpdate):
    ensure_admin_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            if req.username is not None:
                username = _clean_username(req.username)
                if not username:
                    return JSONResponse(status_code=400, content={"error": "Username must be 3-32 letters, numbers, _ or -"})
                cur.execute(
                    "SELECT 1 FROM app.users WHERE lower(username) = lower(%s) AND user_id <> %s",
                    [username, user_id],
                )
                if cur.fetchone():
                    return JSONResponse(status_code=409, content={"error": "Username is already taken"})
                cur.execute("UPDATE app.users SET username = %s WHERE user_id = %s", [username, user_id])
            if req.is_dev is not None:
                cur.execute("UPDATE app.users SET is_dev = %s WHERE user_id = %s", [req.is_dev, user_id])
            cur.execute(
                "SELECT user_id, email, username, is_dev, created_at, last_login_at FROM app.users WHERE user_id = %s",
                [user_id],
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="User not found")
    return {
        "user_id": row["user_id"],
        "email": row["email"],
        "username": row["username"],
        "is_dev": bool(row["is_dev"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_login_at": row["last_login_at"].isoformat() if row["last_login_at"] else None,
    }


@router.get("/admin/bug-reports", dependencies=[Depends(_require_dev)])
def admin_bug_reports(status: Optional[str] = Query(None), page_size: int = Query(100, ge=1, le=300)):
    ensure_admin_schema()
    where = []
    params: list = []
    if status and status != "all":
        where.append("status = %s")
        params.append(status)
    sql = "SELECT * FROM app.bug_reports"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(page_size)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [{
        "bug_id": r["bug_id"],
        "user_id": r["user_id"],
        "email": r["email"],
        "username": r["username"],
        "message": r["message"],
        "status": r["status"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    } for r in rows]


@router.patch("/admin/bug-reports/{bug_id}", dependencies=[Depends(_require_dev)])
def admin_update_bug_report(bug_id: int, req: BugReportUpdate):
    ensure_admin_schema()
    status = (req.status or "").strip().lower()
    if status not in ("open", "reviewing", "closed"):
        return JSONResponse(status_code=400, content={"error": "status must be open, reviewing or closed"})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.bug_reports SET status = %s, updated_at = NOW() WHERE bug_id = %s RETURNING bug_id, status",
                [status, bug_id],
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Bug report not found")
    return {"bug_id": row["bug_id"], "status": row["status"]}


# ── /admin/unmatched ──────────────────────────────────────────────────────────

@router.get("/admin/unmatched", dependencies=[Depends(_require_dev)])
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

@router.get("/admin/price-discrepancies", dependencies=[Depends(_require_dev)])
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

@router.get("/admin/sets", dependencies=[Depends(_require_dev)])
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


@router.get("/admin/quality", dependencies=[Depends(_require_dev)])
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
