"""Passwordless magic-link auth. _current_user is the bearer-token dependency
used by the cardlists router too."""

import os
import secrets
from typing import Optional

import requests as http_requests

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fab_api.core import HERE, get_conn, _slog

router = APIRouter()


# ── Phase 2: email accounts (magic-link) + named cardlists ──────────────────
# Portable account = email. Passwordless: POST /auth/request-link mints a short-lived
# magic token; GET /auth/verify?token=… consumes it and returns a long-lived session
# token the client sends as `Authorization: Bearer <token>`. Cardlists belong to a user
# and hold printings (printing_unique_id → gold.gold_cards) with quantities.
#
# EMAIL DELIVERY IS DEV-MODE: the magic link is returned in the response + logged, not
# emailed. Swap _deliver_magic_link() for a real sender (Resend/SMTP) to go live.
APP_AUTH_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.users (
    user_id        BIGSERIAL   PRIMARY KEY,
    email          TEXT        NOT NULL UNIQUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS app.magic_tokens (
    token       TEXT        PRIMARY KEY,
    email       TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS magic_tokens_email ON app.magic_tokens (email);

CREATE TABLE IF NOT EXISTS app.sessions (
    session_token TEXT        PRIMARY KEY,
    user_id       BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS sessions_user ON app.sessions (user_id);

CREATE TABLE IF NOT EXISTS app.cardlists (
    cardlist_id BIGSERIAL   PRIMARY KEY,
    user_id     BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS cardlists_user ON app.cardlists (user_id);

CREATE TABLE IF NOT EXISTS app.cardlist_items (
    item_id            BIGSERIAL   PRIMARY KEY,
    cardlist_id        BIGINT      NOT NULL REFERENCES app.cardlists(cardlist_id) ON DELETE CASCADE,
    printing_unique_id TEXT        NOT NULL,
    qty                INTEGER     NOT NULL DEFAULT 1,
    added_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cardlist_id, printing_unique_id)
);
CREATE INDEX IF NOT EXISTS cardlist_items_list ON app.cardlist_items (cardlist_id);
"""

MAGIC_TOKEN_TTL = "15 minutes"   # how long a magic link is valid
SESSION_TTL     = "30 days"      # how long a login session lasts


def ensure_app_auth_schema():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(APP_AUTH_SCHEMA_SQL)


def _normalise_email(raw: str | None) -> str | None:
    email = (raw or "").strip().lower()
    # Deliberately light validation — dev mode. A real sender will bounce bad addresses.
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 240:
        return None
    return email


def _public_base_url() -> str:
    """Best-effort public origin for building the magic link (the live tunnel URL)."""
    try:
        u = (HERE / "tmp" / "logs" / "tunnel_url.txt").read_text().strip()
        if u:
            return u.rstrip("/")
    except Exception:
        pass
    return os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM    = os.getenv("RESEND_FROM", "FAB Matrix <onboarding@resend.dev>")


def _deliver_magic_link(email: str, link: str) -> bool:
    """Email the magic link via Resend; returns True when actually sent.

    Without RESEND_API_KEY in .env this stays in dev mode: the link is only
    logged (and returned as dev_link by /auth/request-link). NOTE: Resend's
    free tier without a verified domain only delivers to the account owner's
    own address — verify a domain in Resend to email other users.
    """
    _slog(f"[AUTH] magic link for {email}: {link}")
    if not RESEND_API_KEY:
        return False
    try:
        resp = http_requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": RESEND_FROM,
                "to": [email],
                "subject": "Your FAB Matrix sign-in link",
                "html": (
                    "<div style='font-family:sans-serif;max-width:480px'>"
                    "<h2 style='color:#0891b2'>The FAB Matrix</h2>"
                    "<p>Click to sign in — the link is valid for 15 minutes:</p>"
                    f"<p><a href='{link}' style='display:inline-block;padding:10px 18px;"
                    "background:#0891b2;color:#fff;text-decoration:none;border-radius:6px'>"
                    "Sign in</a></p>"
                    f"<p style='color:#888;font-size:12px'>Or open: {link}</p>"
                    "<p style='color:#888;font-size:12px'>If you didn't request this, ignore it.</p>"
                    "</div>"
                ),
            },
            timeout=10,
        )
        if resp.ok:
            _slog(f"[AUTH] magic link emailed to {email} via Resend")
            return True
        _slog(f"[AUTH] Resend error {resp.status_code}: {resp.text[:200]} — falling back to dev link")
    except Exception as e:
        _slog(f"[AUTH] Resend send failed: {type(e).__name__}: {e} — falling back to dev link")
    return False


def _current_user(authorization: Optional[str] = Header(None)) -> dict:
    """Resolve `Authorization: Bearer <session_token>` to a user, or 401. Touches
    last_seen_at so active sessions stay warm. Expired sessions are rejected."""
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.sessions
                   SET last_seen_at = NOW()
                 WHERE session_token = %s AND expires_at > NOW()
             RETURNING user_id
                """,
                [token],
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=401, detail="Session expired or invalid")
            cur.execute(
                "SELECT user_id, email, created_at, last_login_at FROM app.users WHERE user_id = %s",
                [row["user_id"]],
            )
            user = cur.fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return dict(user)


def _get_owned_cardlist(cur, user_id: int, cardlist_id: int) -> dict:
    """Fetch a cardlist owned by user_id, or raise 404. Enforces ownership everywhere."""
    cur.execute(
        "SELECT cardlist_id, user_id, name, created_at, updated_at "
        "FROM app.cardlists WHERE cardlist_id = %s AND user_id = %s",
        [cardlist_id, user_id],
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Cardlist not found")
    return dict(row)


class AuthRequest(BaseModel):
    email: str


@router.post("/auth/request-link")
def auth_request_link(req: AuthRequest):
    """Start passwordless login: mint a magic token for this email and 'send' the link.
    Dev mode returns the link directly; production would only email it."""
    ensure_app_auth_schema()
    email = _normalise_email(req.email)
    if not email:
        return JSONResponse(status_code=400, content={"error": "Enter a valid email address"})

    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.magic_tokens (token, email, expires_at) "
                "VALUES (%s, %s, NOW() + INTERVAL %s)",
                [token, email, MAGIC_TOKEN_TTL],
            )

    # The link targets the FRONTEND (/account?token=…) — the AuthProvider picks up
    # ?token= on load, verifies it, and cleans the URL. MAGIC_LINK_BASE overrides the
    # tunnel URL once a stable domain/Lovable URL should be used instead.
    base = os.getenv("MAGIC_LINK_BASE", "").rstrip("/") or _public_base_url()
    link = f"{base}/account?token={token}" if base else f"/account?token={token}"
    emailed = _deliver_magic_link(email, link)
    resp = {
        "sent": True,
        "email": email,
        "emailed": emailed,
        "expires_in": MAGIC_TOKEN_TTL,
    }
    if not emailed:
        # Dev fallback only — never expose the token when a real email went out.
        resp["dev_link"] = link
    return resp


@router.get("/auth/verify")
def auth_verify(token: str = Query(...)):
    """Consume a magic token: mark it used, upsert the user, mint a session token."""
    ensure_app_auth_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.magic_tokens SET used_at = NOW() "
                "WHERE token = %s AND used_at IS NULL AND expires_at > NOW() "
                "RETURNING email",
                [token],
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="Link is invalid, used, or expired")
            email = row["email"]

            cur.execute(
                "INSERT INTO app.users (email, last_login_at) VALUES (%s, NOW()) "
                "ON CONFLICT (email) DO UPDATE SET last_login_at = NOW() "
                "RETURNING user_id, email",
                [email],
            )
            user = cur.fetchone()

            session_token = secrets.token_urlsafe(32)
            cur.execute(
                "INSERT INTO app.sessions (session_token, user_id, expires_at) "
                "VALUES (%s, %s, NOW() + INTERVAL %s)",
                [session_token, user["user_id"], SESSION_TTL],
            )
    return {
        "session_token": session_token,
        "user_id": user["user_id"],
        "email": user["email"],
        "expires_in": SESSION_TTL,
    }


@router.get("/auth/me")
def auth_me(user: dict = Depends(_current_user)):
    """Who am I — validates the session token and returns the account."""
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "created_at": user["created_at"].isoformat() if user.get("created_at") else None,
        "last_login_at": user["last_login_at"].isoformat() if user.get("last_login_at") else None,
    }


@router.post("/auth/logout")
def auth_logout(authorization: Optional[str] = Header(None)):
    """Invalidate the current session token (idempotent)."""
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if token:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.sessions WHERE session_token = %s", [token])
    return {"ok": True}
