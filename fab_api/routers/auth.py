"""Email auth. _current_user is the bearer-token dependency used by the
cardlists router too.

Magic links remain supported, and password login is intentionally lightweight:
the product goal is easy personal collection access, not high-security custody.
"""

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fab_api.core import HERE, get_conn, _slog, send_email

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
    username       TEXT        UNIQUE,
    is_dev         BOOLEAN     NOT NULL DEFAULT FALSE,
    password_hash  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at  TIMESTAMPTZ
);
ALTER TABLE app.users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE app.users ADD COLUMN IF NOT EXISTS is_dev BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE app.users ADD COLUMN IF NOT EXISTS password_hash TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS users_username_unique
    ON app.users (lower(username)) WHERE username IS NOT NULL;
UPDATE app.users
   SET is_dev = TRUE
 WHERE lower(coalesce(username, '')) = 't4ngo'
    OR lower(email) IN ('t4ngo', 't4ngo@t4ngo.com', 'tango.christofer@gmail.com');

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
PASSWORD_ITERATIONS = 160_000


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


def _valid_password(raw: str | None) -> str | None:
    password = raw or ""
    if len(password) < 4 or len(password) > 200:
        return None
    return password


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def _verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _mint_session(cur, user_id: int) -> str:
    session_token = secrets.token_urlsafe(32)
    cur.execute(
        "INSERT INTO app.sessions (session_token, user_id, expires_at) "
        "VALUES (%s, %s, NOW() + INTERVAL %s)",
        [session_token, user_id, SESSION_TTL],
    )
    return session_token


def _clean_username(raw: str | None) -> str | None:
    username = (raw or "").strip()
    if len(username) < 3 or len(username) > 32:
        return None
    if not all(ch.isalnum() or ch in "_-" for ch in username):
        return None
    return username


def _ensure_username(cur, user_id: int) -> str:
    cur.execute("SELECT username FROM app.users WHERE user_id = %s", [user_id])
    row = cur.fetchone()
    if row and row.get("username"):
        return row["username"]
    for candidate in [f"User{user_id}"] + [f"User{user_id}-{i}" for i in range(2, 20)]:
        cur.execute("SELECT 1 FROM app.users WHERE lower(username) = lower(%s)", [candidate])
        if cur.fetchone():
            continue
        cur.execute("UPDATE app.users SET username = %s WHERE user_id = %s", [candidate, user_id])
        return candidate
    raise HTTPException(status_code=500, detail="Could not assign username")


def _public_base_url() -> str:
    """Best-effort public origin for building the magic link (the live tunnel URL)."""
    try:
        u = (HERE / "tmp" / "logs" / "tunnel_url.txt").read_text().strip()
        if u:
            return u.rstrip("/")
    except Exception:
        pass
    return os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


def _deliver_magic_link(email: str, link: str) -> bool:
    """Email the magic link (core.send_email → Resend, noreply@t4ngo.com); returns
    True when actually sent. Without RESEND_API_KEY this stays in dev mode: the
    link is only logged (and returned as dev_link by /auth/request-link).
    Sent SYNCHRONOUSLY on purpose — the response's `emailed` flag must be truthful
    (it decides whether the frontend says 'check your email' or auto-signs-in)."""
    _slog(f"[AUTH] magic link for {email}: {link}")
    return send_email(
        email,
        "Your FAB Matrix sign-in link",
        (
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
    )


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
                "SELECT user_id, email, username, is_dev, created_at, last_login_at FROM app.users WHERE user_id = %s",
                [row["user_id"]],
            )
            user = cur.fetchone()
            if user and not user.get("username"):
                _ensure_username(cur, user["user_id"])
                cur.execute(
                    "SELECT user_id, email, username, is_dev, created_at, last_login_at FROM app.users WHERE user_id = %s",
                    [row["user_id"]],
                )
                user = cur.fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return dict(user)


def _get_owned_cardlist(cur, user_id: int, cardlist_id: int) -> dict:
    """Fetch a cardlist owned by user_id, or raise 404. Enforces ownership everywhere."""
    cur.execute(
        "SELECT cardlist_id, user_id, name, is_trade_list, created_at, updated_at "
        "FROM app.cardlists WHERE cardlist_id = %s AND user_id = %s",
        [cardlist_id, user_id],
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Cardlist not found")
    return dict(row)


class AuthRequest(BaseModel):
    email: str


class PasswordLoginRequest(BaseModel):
    email: str
    password: str


class SetPasswordRequest(BaseModel):
    password: str


class UsernameRequest(BaseModel):
    username: str


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
                "RETURNING user_id, email, username, is_dev",
                [email],
            )
            user = cur.fetchone()
            username = _ensure_username(cur, user["user_id"])

            session_token = _mint_session(cur, user["user_id"])
    return {
        "session_token": session_token,
        "user_id": user["user_id"],
        "email": user["email"],
        "username": username,
        "is_dev": bool(user.get("is_dev")),
        "expires_in": SESSION_TTL,
    }


@router.post("/auth/password")
def auth_password(req: PasswordLoginRequest):
    """Sign in with email + password.

    Usability rule: if the email has no account yet, create it. If the account
    came from a magic link and has no password yet, set this password.
    """
    ensure_app_auth_schema()
    email = _normalise_email(req.email)
    password = _valid_password(req.password)
    if not email:
        return JSONResponse(status_code=400, content={"error": "Enter a valid email address"})
    if not password:
        return JSONResponse(status_code=400, content={"error": "Password must be 4-200 characters"})

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, email, password_hash FROM app.users WHERE email = %s",
                [email],
            )
            user = cur.fetchone()
            password_hash = _hash_password(password)
            created = False
            password_set = False

            if not user:
                cur.execute(
                    "INSERT INTO app.users (email, password_hash, last_login_at) "
                    "VALUES (%s, %s, NOW()) RETURNING user_id, email, username, is_dev",
                    [email, password_hash],
                )
                user = cur.fetchone()
                created = True
                password_set = True
            elif not user.get("password_hash"):
                cur.execute(
                    "UPDATE app.users SET password_hash = %s, last_login_at = NOW() "
                    "WHERE user_id = %s RETURNING user_id, email, username, is_dev",
                    [password_hash, user["user_id"]],
                )
                user = cur.fetchone()
                password_set = True
            else:
                if not _verify_password(password, user["password_hash"]):
                    raise HTTPException(status_code=401, detail="Email or password is incorrect")
                cur.execute(
                    "UPDATE app.users SET last_login_at = NOW() "
                    "WHERE user_id = %s RETURNING user_id, email, username, is_dev",
                    [user["user_id"]],
                )
                user = cur.fetchone()

            username = _ensure_username(cur, user["user_id"])
            session_token = _mint_session(cur, user["user_id"])

    return {
        "session_token": session_token,
        "user_id": user["user_id"],
        "email": user["email"],
        "username": username,
        "is_dev": bool(user.get("is_dev")),
        "expires_in": SESSION_TTL,
        "created": created,
        "password_set": password_set,
    }


@router.post("/auth/set-password")
def auth_set_password(req: SetPasswordRequest, user: dict = Depends(_current_user)):
    """Set or replace the current user's password."""
    ensure_app_auth_schema()
    password = _valid_password(req.password)
    if not password:
        return JSONResponse(status_code=400, content={"error": "Password must be 4-200 characters"})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.users SET password_hash = %s WHERE user_id = %s",
                [_hash_password(password), user["user_id"]],
            )
    return {"ok": True}


@router.post("/auth/username")
def auth_set_username(req: UsernameRequest, user: dict = Depends(_current_user)):
    ensure_app_auth_schema()
    username = _clean_username(req.username)
    if not username:
        return JSONResponse(status_code=400, content={"error": "Username must be 3-32 letters, numbers, _ or -"})
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM app.users WHERE lower(username) = lower(%s) AND user_id <> %s",
                [username, user["user_id"]],
            )
            if cur.fetchone():
                return JSONResponse(status_code=409, content={"error": "Username is already taken"})
            cur.execute(
                "UPDATE app.users SET username = %s WHERE user_id = %s RETURNING user_id, email, username, is_dev",
                [username, user["user_id"]],
            )
            row = cur.fetchone()
    return {"user_id": row["user_id"], "email": row["email"], "username": row["username"], "is_dev": bool(row.get("is_dev"))}


@router.get("/auth/me")
def auth_me(user: dict = Depends(_current_user)):
    """Who am I — validates the session token and returns the account."""
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "username": user.get("username"),
        "is_dev": bool(user.get("is_dev")),
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
