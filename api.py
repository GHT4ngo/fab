"""
FaB Store API — FastAPI server exposing gold.gold_cards to the frontend.
Run with: python api.py
"""

import os
import re
import base64
import io
import unicodedata
from pathlib import Path
from rapidfuzz import process, fuzz
import requests as http_requests
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from PIL import Image

HERE = Path(__file__).resolve().parent
load_dotenv(dotenv_path=HERE / ".env")

GOOGLE_VISION_KEY  = os.getenv("GOOGLE_VISION_KEY", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

# Scan debug log — appended to on every /scan call so you can read it from the PC
SCAN_LOG = str(HERE / "tmp" / "logs" / "scan_debug.log")
os.makedirs(os.path.dirname(SCAN_LOG), exist_ok=True)

def _slog(*parts):
    """Append a timestamped line to the scan debug log and also print it."""
    import datetime
    line = datetime.datetime.now().strftime("%H:%M:%S") + "  " + "  ".join(str(p) for p in parts)
    print(line)
    with open(SCAN_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# easyocr reader — loaded lazily on first /scan request (takes ~5s first time)
_easyocr_reader = None

def _get_easyocr():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr
        _easyocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _easyocr_reader

# Card name list for fuzzy matching — refreshed on first /scan request
_card_names: list[str] = []

def _get_card_names():
    global _card_names
    if not _card_names:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT name FROM gold.gold_cards ORDER BY name")
                _card_names = [r["name"] for r in cur.fetchall()]
    return _card_names

PG_HOST     = os.getenv("PG_HOST", "localhost")
PG_PORT     = int(os.getenv("PG_PORT", 5432))
PG_USER     = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

app = FastAPI(title="FaB Store API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT,
        database="fab",
        user=PG_USER, password=PG_PASSWORD,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


# ── /cards ────────────────────────────────────────────────────────────────────

@app.get("/cards")
def get_cards(
    q:        Optional[str]   = Query(None, description="Name search"),
    set_id:   Optional[str]   = Query(None, description="Set code filter"),
    rarity:   Optional[str]   = Query(None, description="Rarity filter"),
    foiling:  Optional[str]   = Query(None, description="Foiling: S, R, C, G"),
    has_price: Optional[bool] = Query(None, description="Only cards with price"),
    page:     int             = Query(1,    ge=1),
    page_size: int            = Query(40,   ge=1, le=500),
):
    """
    Browse cards. Returns paginated results with price in EUR, USD, SEK.
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

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
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
            g.variant,
            g.pitch,
            g.cost,
            g.power,
            g.defense,
            g.type_text,
            g.image_url,
            g.cm_idproduct,
            g.match_tier,
            g.cc_technique,
            g.price_eur,
            g.tcg_price_usd,
            g.tcg_fetched_at,
            g.price_sek,
            g.price_source,
            g.price_confidence,
            g.is_foil,
            g.has_price,
            COUNT(*) OVER() AS total_count
        FROM gold.gold_cards g
        LEFT JOIN bronze.fab_sets s
            ON s.set_id = g.set_id AND s.edition = g.edition
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
        c["tcg_fetched_at"] = c["tcg_fetched_at"].isoformat() if c["tcg_fetched_at"] else None

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "cards": cards,
    }


# ── /sets ─────────────────────────────────────────────────────────────────────

@app.get("/sets")
def get_sets():
    """List all sets that have cards in gold_cards."""
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

@app.get("/stats")
def get_stats():
    """Overall price coverage and match tier breakdown."""
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


# ── /admin/unmatched ──────────────────────────────────────────────────────────

@app.get("/admin/unmatched")
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

@app.get("/admin/price-discrepancies")
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

@app.get("/admin/sets")
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


@app.get("/admin/quality")
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


# ── /scan ─────────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    image: str          # base64-encoded JPEG/PNG frame from the camera
    engine: str = "claude"    # "claude" | "easyocr" | "google" | "visual"
    # Smart mode filters (only used when engine == "visual")
    no_decks:    bool = True   # exclude sets with < 50 unique card names (blitz/hero decks)
    one_per_set: bool = True   # keep one edition per (name, set_id) — prefer Unlimited
    min_rarity:  str  = "M"   # "C"=all  "R"=rare+  "M"=majestic+
    show_preview: bool = False # return the processed image so the UI can display it

def _crop_name_region(img: Image.Image) -> Image.Image:
    """
    The frontend already crops + 2× scales the name region before sending,
    so we receive a pre-cropped image. Apply sharpening for easyocr.
    """
    from PIL import ImageFilter, ImageEnhance
    img = img.convert("RGB")
    img = ImageEnhance.Sharpness(img).enhance(2.0)
    img = ImageEnhance.Contrast(img).enhance(1.3)
    return img

def _ocr_easyocr(img: Image.Image) -> str:
    import numpy as np
    reader = _get_easyocr()
    arr = np.array(img.convert("RGB"))
    results = reader.readtext(arr, detail=0, paragraph=True)
    return " ".join(results).strip()

def _ocr_google(img: Image.Image) -> str:
    if not GOOGLE_VISION_KEY:
        raise ValueError("GOOGLE_VISION_KEY not set in .env")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_KEY}"
    body = {"requests": [{"image": {"content": b64},
                          "features": [{"type": "TEXT_DETECTION", "maxResults": 1}]}]}
    resp = http_requests.post(url, json=body, timeout=10)
    resp.raise_for_status()
    annotations = resp.json()["responses"][0].get("textAnnotations", [])
    return annotations[0]["description"].splitlines()[0].strip() if annotations else ""

def _find_card_quad(edges_map, arr_shape, min_frac=0.10, max_frac=0.97):
    """
    Given a binary edge map, find the largest quadrilateral that looks like a card.
    Returns ordered corner points (float32 4×2) or None.
    """
    import numpy as np
    import cv2

    h, w   = arr_shape[:2]
    img_area = h * w

    contours, _ = cv2.findContours(edges_map, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:10]:
        area = cv2.contourArea(cnt)
        if area < img_area * min_frac or area > img_area * max_frac:
            continue

        # Convex hull first — cleans up jagged contours caused by card art edges
        hull  = cv2.convexHull(cnt)
        peri  = cv2.arcLength(hull, True)
        for eps in (0.02, 0.03, 0.04, 0.05):   # try looser fits if strict fails
            approx = cv2.approxPolyDP(hull, eps * peri, True)
            if len(approx) == 4:
                pts = approx.reshape(4, 2).astype(np.float32)
                # Sanity check: polygon should be roughly card-shaped (portrait or landscape)
                xs, ys  = pts[:, 0], pts[:, 1]
                bw, bh  = float(np.max(xs) - np.min(xs)), float(np.max(ys) - np.min(ys))
                ratio   = min(bw, bh) / max(bw, bh) if max(bw, bh) > 0 else 0
                if 0.45 < ratio < 0.90:
                    return pts
    return None


def _order_corners(pts):
    import numpy as np
    s    = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).ravel()
    return np.array([
        pts[np.argmin(s)],     # TL
        pts[np.argmin(diff)],  # TR
        pts[np.argmax(s)],     # BR
        pts[np.argmax(diff)],  # BL
    ], dtype=np.float32)


def _rectify_card(img: Image.Image) -> tuple:
    """
    Detect the card's four corners and apply a perspective warp to 400×560.

    Tries three preprocessing pipelines in order:
      1. Bilateral filter + double Canny   (handles most backgrounds)
      2. CLAHE equalisation + Canny        (handles dark/uneven lighting)
      3. Adaptive threshold                (handles flat, low-contrast cards)

    Fallback: if no quad found, crop to the center 80% of the frame
    (where the UI overlay guides the user to place the card) — this still
    gives Claude a much better image than the raw full frame.

    Returns (image, "rectified" | "cropped" | "raw").
    """
    import numpy as np
    import cv2

    arr  = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    kernel5 = np.ones((5, 5), np.uint8)

    def _try(edge_map):
        dilated = cv2.dilate(edge_map, kernel5, iterations=2)
        return _find_card_quad(dilated, arr.shape)

    # Pipeline 1: bilateral + double Canny
    bil    = cv2.bilateralFilter(gray, 9, 75, 75)
    e1 = cv2.bitwise_or(cv2.Canny(bil, 20, 60), cv2.Canny(bil, 60, 180))
    pts = _try(e1)

    # Pipeline 2: CLAHE + Canny (improves low-light / dark card backs)
    if pts is None:
        clahe  = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        eq     = clahe.apply(gray)
        e2     = cv2.Canny(cv2.GaussianBlur(eq, (5, 5), 0), 30, 90)
        pts    = _try(e2)

    # Pipeline 3: adaptive threshold (handles uniform backgrounds very well)
    if pts is None:
        thresh = cv2.adaptiveThreshold(gray, 255,
                                       cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY_INV, 21, 4)
        pts    = _try(thresh)

    if pts is not None:
        ordered = _order_corners(pts)
        W, H    = 400, 560
        dst     = np.array([[0, 0], [W, 0], [W, H], [0, H]], dtype=np.float32)
        M       = cv2.getPerspectiveTransform(ordered, dst)
        warped  = cv2.warpPerspective(arr, M, (W, H))
        return Image.fromarray(warped), "rectified"

    # Fallback: crop to 80% centre region matching the UI overlay
    pad_x = int(w * 0.10)
    pad_y = int(h * 0.10)
    cropped = arr[pad_y: h - pad_y, pad_x: w - pad_x]
    return Image.fromarray(cropped), "cropped"


def _ocr_claude(img: Image.Image) -> str:
    """
    Use Claude Haiku vision to read the card name from a pre-cropped card image.
    Applies sharpening to help with slightly blurry physical card photos.
    """
    from PIL import ImageEnhance

    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set in .env")

    # Sharpen before encoding — helps with out-of-focus physical card photos
    img = img.convert("RGB")
    img = ImageEnhance.Sharpness(img).enhance(2.5)
    img = ImageEnhance.Contrast(img).enhance(1.15)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    b64 = base64.b64encode(buf.getvalue()).decode()

    headers = {
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    body = {
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 32,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type":   "image",
                    "source": {
                        "type":       "base64",
                        "media_type": "image/jpeg",
                        "data":       b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "This is a Flesh and Blood trading card. "
                        "The card name is in bold text at the very top of the card. "
                        "You MUST give your best guess — do not say you cannot read it. "
                        "Reply with ONLY the card name, 1-5 words, nothing else."
                    ),
                },
            ],
        }],
    }
    resp = http_requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


def _normalize(text: str) -> str:
    """Strip diacritics and lowercase — so ō→o, é→e, etc. OCR reads plain ASCII."""
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode().lower()

def _fuzzy_match(raw_text: str, top_n: int = 5, cutoff: int = 40) -> list[dict]:
    """
    Match OCR/Claude text against known card names using rapidfuzz WRatio.
    Returns empty list when no match exceeds the cutoff — callers can then
    show "not in database" rather than a wrong card.
    """
    names = _get_card_names()
    norm_query = _normalize(raw_text)
    norm_names = {_normalize(n): n for n in names}

    hits = process.extract(
        norm_query,
        list(norm_names.keys()),
        scorer=fuzz.WRatio,
        limit=top_n,
        score_cutoff=cutoff,
    )

    results = []
    for norm, score, _ in hits:
        original = norm_names[norm]
        results.append({"name": original, "score": round(score / 100, 3)})

    results.sort(key=lambda x: -x["score"])
    return results[:top_n]

# ── Card-code (display_id) scanning ───────────────────────────────────────────
# The bottom-left of every FaB card carries a code like "R EN · HVY050". The
# trailing token (HVY050) IS our gold.display_id, so reading it pins the exact
# set+number deterministically — far more reliable than OCRing the stylised name.
# Because EVERY valid code is known, we snap a noisy OCR read to the nearest real
# display_id: structured snap (set code, then number) auto-corrects the common
# OCR confusions (O/0, I/1, S/5, B/8, Z/2, G/6) without needing a trained model.

_code_index: dict | None = None

# Per-character confusions on the small, plain corner font. Applied to the NUMBER
# part only (digits ↔ look-alike letters); letters are snapped via the known set list.
_OCR_DIGIT_FIX = str.maketrans({
    "O": "0", "Q": "0", "D": "0", "U": "0",
    "I": "1", "L": "1", "|": "1", "T": "1", "/": "1",
    "Z": "2", "S": "5", "G": "6", "B": "8",
})

def _get_code_index():
    """Build the display_id snap index from gold (cached for the process).

    Returns {set_codes: set[str], by_set_num: {(set_code, int): display_id}}.
    The number is the digit run of display_id after the set-code prefix, so we can
    snap a parsed (letters, number) pair straight back to a real printing's code.
    """
    global _code_index
    if _code_index is None:
        set_codes: set[str] = set()
        by_set_num: dict[tuple[str, int], str] = {}
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT display_id, set_id FROM gold.gold_cards "
                    "WHERE display_id IS NOT NULL AND set_id IS NOT NULL"
                )
                for r in cur.fetchall():
                    did = (r["display_id"] or "").upper().strip()
                    sid = (r["set_id"] or "").upper().strip()
                    if not did or not sid:
                        continue
                    set_codes.add(sid)
                    tail = did[len(sid):] if did.startswith(sid) else did
                    m = re.search(r"\d+", tail)
                    if m:
                        by_set_num[(sid, int(m.group()))] = did
        _code_index = {"set_codes": set_codes, "by_set_num": by_set_num}
    return _code_index

def _parse_code(raw: str):
    """Pull the (letters, number) set-code token out of an OCR string.

    'R EN · HVY050' / 'R EN | HVY 050' / 'HVY050' all yield ('HVY', '050').
    Takes the LAST set+number token — the code sits at the end after the rarity
    ('R'/'C'/...) and language ('EN') tokens, which never carry trailing digits.
    """
    s = re.sub(r"[^A-Z0-9 ]+", " ", (raw or "").upper())
    best = None
    # optional leading digit (e.g. '1HP'), 2-4 letters, then 1-3 digit-ish chars
    for m in re.finditer(r"([0-9]?[A-Z]{2,4})\s*([0-9OISBZGLDUQT|/]{1,3})\b", s):
        best = (m.group(1), m.group(2))
    return best

def _snap_code(letters: str, num_raw: str):
    """Snap a parsed (letters, number) to a real display_id, or None.

    1. Snap letters to the nearest known set code (exact, else fuzzy ≥50).
    2. Snap the number (after digit-confusion fixup) to the nearest real number
       in that set, accepting only if within 3 (guards against wild misreads).
    """
    idx = _get_code_index()
    letters = letters.upper()
    digits = re.sub(r"\D", "", num_raw.upper().translate(_OCR_DIGIT_FIX))
    if not digits:
        return None
    n = int(digits)

    if letters in idx["set_codes"]:
        set_code = letters
    else:
        cand = process.extractOne(letters, list(idx["set_codes"]),
                                  scorer=fuzz.ratio, score_cutoff=50)
        set_code = cand[0] if cand else None
    if not set_code:
        return None

    if (set_code, n) in idx["by_set_num"]:
        return idx["by_set_num"][(set_code, n)]
    nums = [k[1] for k in idx["by_set_num"] if k[0] == set_code]
    if not nums:
        return None
    nearest = min(nums, key=lambda x: abs(x - n))
    if abs(nearest - n) <= 3:
        return idx["by_set_num"][(set_code, nearest)]
    return None

def _ocr_code_tesseract(img: Image.Image) -> str:
    """OCR the (already bottom-left-cropped) code strip with Tesseract.

    Restricts to the code alphabet and treats it as a single line — far more
    accurate and ~30-80ms vs a general-purpose model on this tiny fixed region.
    """
    import pytesseract
    from PIL import ImageOps, ImageEnhance
    g = ImageOps.grayscale(img.convert("RGB"))
    g = ImageOps.autocontrast(g)
    g = ImageEnhance.Sharpness(g).enhance(2.0)
    cfg = ("--psm 7 --oem 1 "
           "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    try:
        return pytesseract.image_to_string(g, config=cfg).strip()
    except pytesseract.TesseractNotFoundError:
        raise ValueError(
            "Tesseract OCR is not installed. Run: sudo apt-get install -y tesseract-ocr"
        )

def _printings_for_display_id(display_id: str) -> list[dict]:
    """Gold rows for one display_id (set+number) — same shape as /card-printings."""
    sql = """
        SELECT
            g.printing_unique_id, g.set_id,
            coalesce(s.set_name, g.set_id) AS set_name,
            g.edition, g.foiling, g.rarity, g.pitch, g.image_url,
            g.price_eur, g.tcg_price_usd, g.price_sek, g.cm_idproduct
        FROM gold.gold_cards g
        LEFT JOIN bronze.fab_sets s
            ON s.set_id = g.set_id AND s.edition = g.edition
        WHERE g.display_id = %s
        ORDER BY g.edition, g.foiling
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [display_id])
            rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["price_eur"]     = float(d["price_eur"])     if d["price_eur"]     else None
        d["tcg_price_usd"] = float(d["tcg_price_usd"]) if d["tcg_price_usd"] else None
        d["price_sek"]     = int(d["price_sek"])       if d["price_sek"]     else None
        out.append(d)
    return out

# ORB descriptor cache — loaded once per process, keyed by filter params
_orb_cache: dict | None = None
_orb_cache_key: tuple | None = None


def _load_orb_candidates(no_decks: bool, one_per_set: bool, min_rarity: str) -> list[dict]:
    """
    Load ORB descriptors from DB filtered by smart mode options.
    Each row: {name, set_id, edition, is_foil, desc: np.ndarray}
    """
    import numpy as np

    # Rarity sets: C=all, R=rare and above, M=majestic and above
    # T=Token, P=Promo, B=Bonus, S=Special excluded from visual matching —
    # their non-standard layouts create disproportionate keypoint hits
    rarity_map = {
        "C": ("C", "R", "M", "L", "F", "V"),
        "R": ("R", "M", "L", "F", "V"),
        "M": ("M", "L", "F", "V"),
    }
    allowed_rarities = rarity_map.get(min_rarity, rarity_map["C"])

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH deck_sets AS (
                    SELECT set_id FROM gold.gold_cards
                    GROUP BY set_id HAVING COUNT(DISTINCT name) < 50
                ),
                ranked AS (
                    SELECT
                        g.name,
                        g.set_id,
                        g.edition,
                        g.is_foil,
                        p.raw_data->>'image_url' AS image_url,
                        ROW_NUMBER() OVER (
                            PARTITION BY g.name, g.set_id
                            ORDER BY
                                CASE g.edition
                                    WHEN 'U' THEN 1
                                    WHEN 'N' THEN 2
                                    WHEN 'A' THEN 3
                                    WHEN 'F' THEN 4
                                    ELSE 5
                                END,
                                CASE g.is_foil WHEN false THEN 1 ELSE 2 END
                        ) AS rn
                    FROM gold.gold_cards g
                    JOIN bronze.fab_printings p
                      ON p.printing_unique_id = g.printing_unique_id
                    WHERE p.raw_data->>'image_url' IS NOT NULL
                      AND g.rarity = ANY(%(rarities)s)
                      AND (NOT %(no_decks)s
                           OR g.set_id NOT IN (SELECT set_id FROM deck_sets))
                )
                SELECT r.name, r.set_id, r.edition, r.is_foil,
                       d.descriptors, d.kp_count
                FROM ranked r
                JOIN bronze.card_orb_descriptors d ON d.image_url = r.image_url
                WHERE (%(one_per_set)s = false OR r.rn = 1)
                  AND d.kp_count > 0
            """, {
                "rarities": list(allowed_rarities),
                "no_decks": no_decks,
                "one_per_set": one_per_set,
            })
            rows = cur.fetchall()

    candidates = []
    for row in rows:
        raw = bytes(row["descriptors"])
        desc = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 32)
        candidates.append({
            "name":     row["name"],
            "set_id":   row["set_id"],
            "edition":  row["edition"],
            "is_foil":  row["is_foil"],
            "desc":     desc,
        })
    return candidates


def _visual_match(img: Image.Image, top_n: int = 5,
                  no_decks: bool = True, one_per_set: bool = True,
                  min_rarity: str = "M") -> list[dict]:
    """
    ORB feature matching: compare camera frame against pre-computed card descriptors.
    Score = number of good keypoint matches (Hamming distance < 60).
    Smart mode filters reduce the candidate set to speed up matching.
    """
    import numpy as np
    import cv2

    global _orb_cache, _orb_cache_key

    cache_key = (no_decks, one_per_set, min_rarity)
    if _orb_cache is None or _orb_cache_key != cache_key:
        _orb_cache = _load_orb_candidates(no_decks, one_per_set, min_rarity)
        _orb_cache_key = cache_key

    candidates = _orb_cache
    if not candidates:
        return []

    # Compute ORB on the incoming camera frame
    arr  = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    target_w = 400
    h, w = gray.shape
    if w != target_w:
        scale = target_w / w
        gray = cv2.resize(gray, (target_w, int(h * scale)), interpolation=cv2.INTER_AREA)

    orb = cv2.ORB_create(nfeatures=500)
    _, query_desc = orb.detectAndCompute(gray, None)

    if query_desc is None or len(query_desc) == 0:
        return []

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    # Score each candidate
    name_scores: dict[str, int] = {}
    for c in candidates:
        matches = matcher.match(query_desc, c["desc"])
        good    = sum(1 for m in matches if m.distance < 60)
        name    = c["name"]
        if good > name_scores.get(name, 0):
            name_scores[name] = good

    if not name_scores:
        return []

    max_good = max(name_scores.values())

    # Require a meaningful number of good matches — avoid noise matches
    if max_good < 12:
        return []

    # Only return cards with >= 70% of the best match count (clear winner zone)
    threshold = max(12, max_good * 0.70)
    scored = [
        {"name": n, "score": round(cnt / max_good, 3)}
        for n, cnt in name_scores.items()
        if cnt >= threshold
    ]
    scored.sort(key=lambda x: (-x["score"], x["name"]))
    return scored[:top_n]


@app.post("/scan")
def scan_card(req: ScanRequest):
    """
    Receive a base64 camera frame and return matching card names.

    engine: "claude"   — Claude Haiku vision (most accurate, needs ANTHROPIC_API_KEY)
            "google"   — Cloud Vision OCR (accurate, needs GOOGLE_VISION_KEY)
            "easyocr"  — Local OCR on name strip (free, lower accuracy)
            "visual"   — ORB feature matching on full card image
    """
    try:
        img_bytes = base64.b64decode(req.image)
        img       = Image.open(io.BytesIO(img_bytes))
        w, h      = img.size

        _slog(f"[SCAN] engine={req.engine}  frame={w}x{h}  preview={req.show_preview}")

        # Code engine: read the bottom-left set code (= display_id) and snap it to a
        # real printing. The frontend sends a pre-cropped corner strip, so we skip the
        # whole-card perspective warp and OCR the strip directly.
        if req.engine == "code":
            raw_text = _ocr_code_tesseract(img)
            parsed   = _parse_code(raw_text)
            display_id = _snap_code(*parsed) if parsed else None
            printings  = _printings_for_display_id(display_id) if display_id else []
            name = None
            if printings:
                # all printings of one display_id share a name; fetch it cheaply
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT name FROM gold.gold_cards WHERE display_id=%s LIMIT 1",
                                    [display_id])
                        row = cur.fetchone()
                        name = row["name"] if row else None
            matches = [{"name": name, "score": 1.0}] if name else []
            _slog(f"[SCAN] code_read={repr(raw_text)} parsed={parsed} "
                  f"display_id={display_id} name={name!r}")
            resp = {"engine": "code", "raw_text": raw_text, "matches": matches,
                    "display_id": display_id, "printings": printings}
            if req.show_preview:
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=80)
                resp["preview_image"] = base64.b64encode(buf.getvalue()).decode()
            return resp

        # Perspective correction — straighten card before recognition
        # status: "rectified" | "cropped" (fallback)
        proc_img, rectify_status = _rectify_card(img)
        pw, ph = proc_img.size
        _slog(f"[SCAN] rectify={rectify_status}  processed={pw}x{ph}")

        def _preview_b64(pil_img: Image.Image) -> str:
            buf = io.BytesIO()
            pil_img.convert("RGB").save(buf, format="JPEG", quality=80)
            return base64.b64encode(buf.getvalue()).decode()

        if req.engine == "visual":
            matches = _visual_match(proc_img,
                                    no_decks=req.no_decks,
                                    one_per_set=req.one_per_set,
                                    min_rarity=req.min_rarity)
            candidate_count = len(_orb_cache) if _orb_cache else 0
            raw_text = f"ORB vs {candidate_count} ({rectify_status})"
            _slog(f"[SCAN] ORB candidates={candidate_count}  matches={[m['name'] for m in matches[:3]]}")
            resp = {"engine": req.engine, "raw_text": raw_text,
                    "matches": matches, "rectified": rectify_status}
            if req.show_preview:
                resp["preview_image"] = _preview_b64(proc_img)
            return resp

        if req.engine == "claude":
            raw_text = _ocr_claude(proc_img)
            # Higher cutoff for Claude — it reads accurately so low scores mean
            # the card genuinely isn't in the database yet (new set, not ingested)
            matches  = _fuzzy_match(raw_text, cutoff=65)
            _slog(f"[SCAN] claude_read={repr(raw_text)}  top_matches={[m['name'] for m in matches[:3]]}")
            resp = {"engine": req.engine, "raw_text": raw_text,
                    "matches": matches, "rectified": rectify_status}
            if req.show_preview:
                resp["preview_image"] = _preview_b64(proc_img)
            return resp

        cropped  = _crop_name_region(img)
        raw_text = _ocr_google(cropped) if req.engine == "google" else _ocr_easyocr(cropped)
        matches  = _fuzzy_match(raw_text)
        return {"engine": req.engine, "raw_text": raw_text, "matches": matches}

    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/card-printings")
def card_printings(name: str):
    """
    All printings for a given card name — used by the scanner My Cardlist.
    Returns set_id, set_name, edition, foiling, rarity, image_url, price_eur,
    tcg_price_usd, price_sek, cm_idproduct, printing_unique_id.
    """
    sql = """
        SELECT
            g.printing_unique_id,
            g.set_id,
            coalesce(s.set_name, g.set_id) AS set_name,
            g.edition,
            g.foiling,
            g.rarity,
            g.pitch,
            g.image_url,
            g.price_eur,
            g.tcg_price_usd,
            g.price_sek,
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
        result.append(d)
    return result


@app.post("/scan/debug")
def scan_debug(req: ScanRequest):
    """
    Returns base64 images of each processing stage so you can see
    what the edge detector is finding. Use from browser devtools or curl.
    """
    import numpy as np
    import cv2

    img_bytes = base64.b64decode(req.image)
    img       = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    arr       = np.array(img)
    gray      = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    def _to_b64(frame_arr):
        ok, buf = cv2.imencode(".jpg", frame_arr)
        return base64.b64encode(buf.tobytes()).decode() if ok else ""

    # Pipeline 1: bilateral + Canny
    bil  = cv2.bilateralFilter(gray, 9, 75, 75)
    e1   = cv2.bitwise_or(cv2.Canny(bil, 20, 60), cv2.Canny(bil, 60, 180))
    e1d  = cv2.dilate(e1, np.ones((5, 5), np.uint8), iterations=2)

    # Pipeline 2: CLAHE + Canny
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    eq    = clahe.apply(gray)
    e2    = cv2.Canny(cv2.GaussianBlur(eq, (5, 5), 0), 30, 90)
    e2d   = cv2.dilate(e2, np.ones((5, 5), np.uint8), iterations=2)

    # Pipeline 3: adaptive threshold
    e3 = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 21, 4)
    e3d = cv2.dilate(e3, np.ones((5, 5), np.uint8), iterations=2)

    # Processed result
    proc_img, status = _rectify_card(img)
    proc_arr = np.array(proc_img.convert("RGB"))
    proc_bgr = cv2.cvtColor(proc_arr, cv2.COLOR_RGB2BGR)

    return {
        "status":   status,
        "edges_bilateral":  _to_b64(e1d),
        "edges_clahe":      _to_b64(e2d),
        "edges_adaptive":   _to_b64(e3d),
        "processed":        _to_b64(proc_bgr),
    }


# ── Serve the built frontend ────────────────────────────────────────────────
# Mounted last so it only catches paths not handled by an API route above.
# Same-origin means the frontend uses relative URLs (VITE_API_BASE_URL=""),
# so the public tunnel URL can change without ever rebuilding the frontend.
_FRONTEND_DIST = HERE / "retro-data-display" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
