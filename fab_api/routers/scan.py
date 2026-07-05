"""Scanner endpoints: /scan/native (Android app), /scan/code (manual entry),
scan sessions/records, and the APK download. Recognition logic lives in
fab_api.scan_engine and is unchanged."""

import json
import re
import secrets
import string
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from fab_api.core import HERE, get_conn, _slog
from fab_api.scan_engine import (
    _crop_name_region,
    _decode_b64_image,
    _fuzzy_match,
    _ocr_easyocr,
    _parse_code,
    _printings_for_display_id,
    _read_footer_code,
    _rectify_card,
    _save_scan_debug_image,
    _snap_code,
    _visual_match,
)
from fab_api.routers.cards import card_printings

router = APIRouter()


APP_SCAN_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.scanned_cards (
    scan_id             BIGSERIAL   PRIMARY KEY,
    scanned_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_code        TEXT,
    session_email       TEXT,
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

CREATE TABLE IF NOT EXISTS app.scan_sessions (
    session_code TEXT PRIMARY KEY,
    email        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE app.scanned_cards ADD COLUMN IF NOT EXISTS session_code TEXT;
ALTER TABLE app.scanned_cards ADD COLUMN IF NOT EXISTS session_email TEXT;

CREATE INDEX IF NOT EXISTS scanned_cards_scanned_at ON app.scanned_cards (scanned_at DESC);
CREATE INDEX IF NOT EXISTS scanned_cards_printing   ON app.scanned_cards (printing_unique_id);
CREATE INDEX IF NOT EXISTS scanned_cards_display_id ON app.scanned_cards (display_id);
CREATE INDEX IF NOT EXISTS scanned_cards_session    ON app.scanned_cards (session_code, scan_id DESC);
"""


def ensure_app_scan_schema():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(APP_SCAN_SCHEMA_SQL)


def _pick_printing_for_scan(display_id: str | None, name: str | None) -> dict | None:
    if display_id:
        printings = _printings_for_display_id(display_id)
        if printings:
            return printings[0]
    if name:
        printings = card_printings(name)
        if printings:
            return printings[0]
    return None


def _clean_session_code(code: str | None) -> str | None:
    if not code:
        return None
    cleaned = re.sub(r"[^A-Z0-9]", "", code.upper())
    return cleaned[:12] or None


def _record_scan_result(
    response: dict,
    scanner: str = "native_android",
    session_code: str | None = None,
    session_email: str | None = None,
) -> int | None:
    if not response.get("name"):
        return None
    confidence = float(response.get("confidence") or 0.0)
    if confidence < 0.90:
        return None

    printing = _pick_printing_for_scan(response.get("display_id"), response.get("name"))
    printing_unique_id = printing.get("printing_unique_id") if printing else None
    display_id = response.get("display_id")
    if not display_id and printing:
        set_id = printing.get("set_id")
        # Prefer the recognized display_id when OCR provided it. For visual/name
        # fallbacks there may be many printings, so leave display_id unset rather
        # than inventing one from a partial printing row.
        display_id = None if set_id else None

    ensure_app_scan_schema()
    session_code = _clean_session_code(session_code)
    session_email = (session_email or "").strip().lower()[:240] or None
    with get_conn() as conn:
        with conn.cursor() as cur:
            if session_code:
                cur.execute(
                    """
                    INSERT INTO app.scan_sessions (session_code, email)
                    VALUES (%s, %s)
                    ON CONFLICT (session_code) DO UPDATE
                    SET email = coalesce(EXCLUDED.email, app.scan_sessions.email),
                        last_seen_at = NOW()
                    """,
                    [session_code, session_email],
                )
            cur.execute(
                """
                SELECT scan_id
                FROM app.scanned_cards
                WHERE scanner = %s
                  AND session_code IS NOT DISTINCT FROM %s
                  AND scanned_at >= NOW() - INTERVAL '3 seconds'
                  AND (
                    (printing_unique_id IS NOT NULL AND printing_unique_id = %s)
                    OR (
                        printing_unique_id IS NULL
                        AND display_id IS NOT DISTINCT FROM %s
                        AND name = %s
                    )
                  )
                ORDER BY scanned_at DESC
                LIMIT 1
                """,
                [scanner, session_code, printing_unique_id, display_id, response.get("name")],
            )
            existing = cur.fetchone()
            if existing:
                return int(existing["scan_id"])

            cur.execute(
                """
                INSERT INTO app.scanned_cards (
                    session_code, session_email, scanner,
                    printing_unique_id, display_id, name, method,
                    confidence, raw_text, debug_paths, response
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING scan_id
                """,
                [
                    session_code,
                    session_email,
                    scanner,
                    printing_unique_id,
                    display_id,
                    response.get("name"),
                    response.get("method"),
                    confidence,
                    response.get("raw_text"),
                    response.get("debug_paths") or [],
                    json.dumps(response),
                ],
            )
            row = cur.fetchone()
            return int(row["scan_id"]) if row else None


class NativeScanRequest(BaseModel):
    full_image: str | None = None
    footer_crop: str | None = None
    title_crop: str | None = None
    debug_save: bool = False
    session_code: str | None = None
    session_email: str | None = None

class ScanSessionRequest(BaseModel):
    email: str | None = None
    session_code: str | None = None


@router.post("/scan/native")
def scan_native(req: NativeScanRequest):
    """
    Native scanner fusion endpoint.

    Signals, strongest first:
      1. footer_crop OCR -> display_id exact set+number
      2. full_image visual match -> card name candidate
      3. title_crop OCR -> fuzzy card-name candidate
    """
    try:
        debug_paths = []
        candidates: list[dict] = []
        footer_raw = ""
        footer_parsed = None
        display_id = None
        printings = []
        name = None

        footer_img = _decode_b64_image(req.footer_crop) if req.footer_crop else None
        full_img = _decode_b64_image(req.full_image) if req.full_image else None
        title_img = _decode_b64_image(req.title_crop) if req.title_crop else None

        if footer_img is not None:
            footer_read = _read_footer_code(footer_img)
            footer_raw = footer_read["raw_text"]
            footer_parsed = footer_read["parsed"]
            display_id = footer_read["display_id"]
            printings = _printings_for_display_id(display_id) if display_id else []
            if printings:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT name FROM gold.gold_cards WHERE display_id=%s LIMIT 1",
                                    [display_id])
                        row = cur.fetchone()
                        name = row["name"] if row else None
            if req.debug_save:
                debug_paths.append(_save_scan_debug_image(
                    footer_img,
                    f"native_footer_{footer_read.get('window') or 'none'}",
                    footer_raw,
                    footer_parsed,
                    display_id,
                ))

        if name:
            _slog(f"[SCAN_NATIVE] footer display_id={display_id} name={name!r}")
            resp = {
                "confidence": 0.99,
                "method": "footer",
                "display_id": display_id,
                "name": name,
                "raw_text": footer_raw,
                "matches": [{"name": name, "score": 1.0}],
                "printings": printings,
                "candidates": [{"source": "footer", "name": name, "score": 1.0}],
                "debug_paths": debug_paths,
            }
            scan_id = _record_scan_result(
                resp,
                session_code=req.session_code,
                session_email=req.session_email,
            )
            resp["stored"] = scan_id is not None
            resp["scan_record_id"] = scan_id
            return resp

        visual_matches = []
        if full_img is not None:
            try:
                proc_img, rectify_status = _rectify_card(full_img)
                visual_matches = _visual_match(proc_img, top_n=3, min_rarity="C")
                for m in visual_matches:
                    candidates.append({"source": "visual", **m})
                if req.debug_save:
                    debug_paths.append(_save_scan_debug_image(
                        proc_img, f"native_full_{rectify_status}", "", None, None
                    ))
            except Exception as e:
                _slog(f"[SCAN_NATIVE] visual_error={type(e).__name__}: {e}")

        title_raw = ""
        title_matches = []
        if title_img is not None:
            try:
                title_raw = _ocr_easyocr(_crop_name_region(title_img))
                title_matches = _fuzzy_match(title_raw, top_n=3, cutoff=55)
                for m in title_matches:
                    candidates.append({"source": "title", **m})
                if req.debug_save:
                    debug_paths.append(_save_scan_debug_image(
                        title_img, "native_title", title_raw, None, None
                    ))
            except Exception as e:
                _slog(f"[SCAN_NATIVE] title_error={type(e).__name__}: {e}")

        scores: dict[str, float] = {}
        for m in title_matches:
            if float(m["score"]) >= 0.85:
                scores[m["name"]] = max(scores.get(m["name"], 0.0), float(m["score"]) * 0.90)
        for vm in visual_matches:
            for tm in title_matches:
                if vm["name"] == tm["name"]:
                    scores[vm["name"]] = max(scores.get(vm["name"], 0.0), 0.92)

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if ranked:
            name, confidence = ranked[0]
            printings = card_printings(name)
            _slog(f"[SCAN_NATIVE] method=fusion name={name!r} confidence={confidence:.2f} "
                  f"footer_raw={footer_raw!r} title_raw={title_raw!r}")
            resp = {
                "confidence": round(confidence, 3),
                "method": "fusion",
                "display_id": None,
                "name": name,
                "raw_text": footer_raw or title_raw,
                "matches": [{"name": name, "score": round(confidence, 3)}],
                "printings": printings,
                "candidates": candidates,
                "debug_paths": debug_paths,
            }
            scan_id = _record_scan_result(
                resp,
                session_code=req.session_code,
                session_email=req.session_email,
            )
            resp["stored"] = scan_id is not None
            resp["scan_record_id"] = scan_id
            return resp

        _slog(f"[SCAN_NATIVE] no_match footer_raw={footer_raw!r} title_raw={title_raw!r}")
        return {
            "confidence": 0.0,
            "method": "none",
            "display_id": None,
            "name": None,
            "raw_text": footer_raw or title_raw,
            "matches": [],
            "printings": [],
            "candidates": candidates,
            "debug_paths": debug_paths,
            "stored": False,
            "scan_record_id": None,
        }
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/scan/code")
def scan_code(code: str):
    """Resolve a manually-typed set code (e.g. 'HVY050') to a card + its printings.
    The no-app entry path: same parse+snap logic as the scanner, but from text so it
    corrects common typos against the known display_id list (O/0, I/1, S/5, ...)."""
    parsed = _parse_code(code or "")
    display_id = _snap_code(*parsed) if parsed else None
    printings = _printings_for_display_id(display_id) if display_id else []
    name = None
    if printings:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM gold.gold_cards WHERE display_id=%s LIMIT 1",
                            [display_id])
                row = cur.fetchone()
                name = row["name"] if row else None
    return {
        "query": code,
        "display_id": display_id,
        "name": name,
        "printings": printings,
        "matches": [{"name": name, "score": 1.0}] if name else [],
    }


@router.post("/scan/session")
def scan_session(req: ScanSessionRequest):
    """Create or join a lightweight scanner session shared by web + phone."""
    ensure_app_scan_schema()
    session_code = _clean_session_code(req.session_code)
    alphabet = string.ascii_uppercase + string.digits
    email = (req.email or "").strip().lower()[:240] or None

    with get_conn() as conn:
        with conn.cursor() as cur:
            if not session_code:
                for _ in range(20):
                    candidate = "".join(secrets.choice(alphabet) for _ in range(6))
                    cur.execute(
                        "SELECT 1 FROM app.scan_sessions WHERE session_code = %s",
                        [candidate],
                    )
                    if not cur.fetchone():
                        session_code = candidate
                        break
            if not session_code:
                return JSONResponse(status_code=500, content={"error": "Could not create session"})

            cur.execute(
                """
                INSERT INTO app.scan_sessions (session_code, email)
                VALUES (%s, %s)
                ON CONFLICT (session_code) DO UPDATE
                SET email = coalesce(EXCLUDED.email, app.scan_sessions.email),
                    last_seen_at = NOW()
                RETURNING session_code, email, created_at, last_seen_at
                """,
                [session_code, email],
            )
            row = dict(cur.fetchone())

    return {
        "session_code": row["session_code"],
        "email": row["email"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
    }


@router.get("/scan/records")
def scan_records(
    limit: int = Query(50, ge=1, le=500),
    session_code: Optional[str] = Query(None),
    after_id: int = Query(0, ge=0),
):
    """Most recent cards added by the native scanner."""
    ensure_app_scan_schema()
    where = ["s.scan_id > %s"]
    params: list = [after_id]
    clean_code = _clean_session_code(session_code)
    if clean_code:
        where.append("s.session_code = %s")
        params.append(clean_code)

    sql = f"""
        SELECT
            s.scan_id,
            s.scanned_at,
            s.session_code,
            s.session_email,
            s.scanner,
            s.printing_unique_id,
            s.display_id,
            s.name,
            s.method,
            s.confidence,
            s.raw_text,
            s.debug_paths,
            g.set_id,
            g.edition,
            g.foiling,
            g.rarity,
            g.image_url,
            g.price_sek
        FROM app.scanned_cards s
        LEFT JOIN gold.gold_cards g
            ON g.printing_unique_id = s.printing_unique_id
        WHERE {' AND '.join(where)}
        ORDER BY s.scan_id DESC
        LIMIT %s
    """
    params.append(limit)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d["scanned_at"] = d["scanned_at"].isoformat() if d["scanned_at"] else None
        d["confidence"] = float(d["confidence"]) if d["confidence"] is not None else None
        d["price_sek"] = int(d["price_sek"]) if d["price_sek"] is not None else None
        if d["display_id"]:
            d["printings"] = _printings_for_display_id(d["display_id"])
        elif d["name"]:
            d["printings"] = card_printings(d["name"])
        else:
            d["printings"] = []
        result.append(d)
    return result


@router.get("/scan/records/stats")
def scan_records_stats():
    """Small operational summary for the native scanner database writes."""
    ensure_app_scan_schema()
    sql = """
        SELECT
            COUNT(*) AS total_scans,
            COUNT(*) FILTER (WHERE scanned_at >= NOW() - INTERVAL '1 day') AS scans_24h,
            MAX(scanned_at) AS last_scan_at
        FROM app.scanned_cards
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = dict(cur.fetchone())
    row["last_scan_at"] = row["last_scan_at"].isoformat() if row["last_scan_at"] else None
    return row


@router.get("/scanner-apk")
def scanner_apk():
    """Download the latest local Android scanner debug APK."""
    # `outputs/apk/debug` is the canonical `assembleDebug` output (the newest signed APK);
    # the older `intermediates/apk/debug` path can lag or go missing between builds.
    apk_dir = HERE / "fab-scanner-android" / "app" / "build"
    candidates = [
        apk_dir / "outputs" / "apk" / "debug" / "app-debug.apk",
        apk_dir / "intermediates" / "apk" / "debug" / "app-debug.apk",
    ]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        return JSONResponse(status_code=404, content={"error": "Scanner APK has not been built yet"})
    apk = max(existing, key=lambda p: p.stat().st_mtime)
    return FileResponse(
        apk,
        media_type="application/vnd.android.package-archive",
        filename="fab-scanner-debug.apk",
    )
