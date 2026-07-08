"""User-to-user direct messages. Lightweight chat: threads are just the pair
(from, to); reading a thread marks the incoming half as read. An email nudge
goes out only for the FIRST unread message from a sender (burst guard) so a
back-and-forth chat doesn't spam inboxes."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fab_api.core import get_conn, send_email_async
from fab_api.routers.auth import _current_user, _public_base_url

router = APIRouter()

APP_MESSAGES_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.messages (
    message_id   BIGSERIAL   PRIMARY KEY,
    from_user_id BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    to_user_id   BIGINT      NOT NULL REFERENCES app.users(user_id) ON DELETE CASCADE,
    body         TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    read_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS messages_to   ON app.messages (to_user_id, read_at);
CREATE INDEX IF NOT EXISTS messages_pair ON app.messages (from_user_id, to_user_id, created_at DESC);
"""

_messages_schema_ready = False


def ensure_app_messages_schema():
    global _messages_schema_ready
    if _messages_schema_ready:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(APP_MESSAGES_SCHEMA_SQL)
    _messages_schema_ready = True


class MessageCreate(BaseModel):
    to_user_id: Optional[int] = None
    to_username: Optional[str] = None
    body: str


def _message_dict(r: dict) -> dict:
    return {
        "message_id": r["message_id"],
        "from_user_id": r["from_user_id"],
        "to_user_id": r["to_user_id"],
        "body": r["body"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "read_at": r["read_at"].isoformat() if r.get("read_at") else None,
    }


@router.post("/messages")
def message_send(req: MessageCreate, user: dict = Depends(_current_user)):
    ensure_app_messages_schema()
    body = (req.body or "").strip()[:4000]
    if not body:
        return JSONResponse(status_code=400, content={"error": "Message is empty"})

    with get_conn() as conn:
        with conn.cursor() as cur:
            to_user = None
            if req.to_user_id is not None:
                cur.execute("SELECT user_id, email, username FROM app.users WHERE user_id = %s", [req.to_user_id])
                to_user = cur.fetchone()
            elif (req.to_username or "").strip():
                cur.execute("SELECT user_id, email, username FROM app.users WHERE lower(username) = lower(%s)",
                            [req.to_username.strip()])
                to_user = cur.fetchone()
            if not to_user:
                raise HTTPException(status_code=404, detail="Recipient not found")
            if to_user["user_id"] == user["user_id"]:
                return JSONResponse(status_code=400, content={"error": "That's you"})

            # Burst guard BEFORE inserting: email only when they have nothing
            # unread from me yet (first message of a burst).
            cur.execute(
                "SELECT 1 FROM app.messages WHERE from_user_id = %s AND to_user_id = %s AND read_at IS NULL LIMIT 1",
                [user["user_id"], to_user["user_id"]],
            )
            should_email = cur.fetchone() is None

            cur.execute(
                "INSERT INTO app.messages (from_user_id, to_user_id, body) VALUES (%s, %s, %s) "
                "RETURNING message_id, from_user_id, to_user_id, body, created_at, read_at",
                [user["user_id"], to_user["user_id"], body],
            )
            row = dict(cur.fetchone())

    if should_email:
        base = _public_base_url() or "https://fabmatrix.t4ngo.com"
        sender = user.get("username") or user["email"]
        preview = body if len(body) <= 300 else body[:300] + "…"
        send_email_async(
            to_user["email"],
            f"New message from {sender} on FAB Matrix",
            (
                "<div style='font-family:sans-serif;max-width:520px'>"
                "<h2 style='color:#0891b2'>The FAB Matrix</h2>"
                f"<p><b>{sender}</b> wrote:</p>"
                f"<blockquote style='border-left:3px solid #0891b2;margin:0;padding:6px 12px;color:#333'>{preview}</blockquote>"
                f"<p><a href='{base}/messages' style='display:inline-block;padding:10px 18px;"
                "background:#0891b2;color:#fff;text-decoration:none;border-radius:6px'>Reply</a></p>"
                "</div>"
            ),
        )
    return _message_dict(row)


@router.get("/messages/threads")
def message_threads(user: dict = Depends(_current_user)):
    """One row per counterparty: who, last message, unread count. Newest first."""
    ensure_app_messages_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH mine AS (
                    SELECT m.*,
                           CASE WHEN m.from_user_id = %(me)s THEN m.to_user_id ELSE m.from_user_id END AS other_id
                    FROM app.messages m
                    WHERE m.from_user_id = %(me)s OR m.to_user_id = %(me)s
                ),
                latest AS (
                    SELECT DISTINCT ON (other_id) other_id, message_id, from_user_id, body, created_at
                    FROM mine ORDER BY other_id, created_at DESC, message_id DESC
                )
                SELECT latest.*, u.username AS other_username, u.email AS other_email,
                       (SELECT COUNT(*) FROM app.messages m2
                        WHERE m2.from_user_id = latest.other_id AND m2.to_user_id = %(me)s
                          AND m2.read_at IS NULL) AS unread
                FROM latest
                JOIN app.users u ON u.user_id = latest.other_id
                ORDER BY latest.created_at DESC
                """,
                {"me": user["user_id"]},
            )
            rows = cur.fetchall()
    return [{
        "other_user_id": r["other_id"],
        "other_username": r["other_username"],
        "other_email": r["other_email"],
        "last_body": r["body"],
        "last_from_me": r["from_user_id"] == user["user_id"],
        "last_at": r["created_at"].isoformat() if r["created_at"] else None,
        "unread": int(r["unread"]),
    } for r in rows]


@router.get("/messages/unread-count")
def message_unread_count(user: dict = Depends(_current_user)):
    ensure_app_messages_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM app.messages WHERE to_user_id = %s AND read_at IS NULL",
                [user["user_id"]],
            )
            return {"unread": int(cur.fetchone()["n"])}


@router.get("/messages/with/{other_user_id}")
def message_thread(other_user_id: int, user: dict = Depends(_current_user)):
    """Full thread with one user (last 500), oldest first. Reading it marks
    their messages to me as read."""
    ensure_app_messages_schema()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, username, email FROM app.users WHERE user_id = %s", [other_user_id])
            other = cur.fetchone()
            if not other:
                raise HTTPException(status_code=404, detail="User not found")
            cur.execute(
                "UPDATE app.messages SET read_at = NOW() "
                "WHERE from_user_id = %s AND to_user_id = %s AND read_at IS NULL",
                [other_user_id, user["user_id"]],
            )
            cur.execute(
                """
                SELECT message_id, from_user_id, to_user_id, body, created_at, read_at
                FROM app.messages
                WHERE (from_user_id = %(me)s AND to_user_id = %(other)s)
                   OR (from_user_id = %(other)s AND to_user_id = %(me)s)
                ORDER BY created_at DESC, message_id DESC
                LIMIT 500
                """,
                {"me": user["user_id"], "other": other_user_id},
            )
            rows = [dict(r) for r in cur.fetchall()]
    rows.reverse()
    return {
        "other_user_id": other["user_id"],
        "other_username": other["username"],
        "other_email": other["email"],
        "messages": [_message_dict(r) for r in rows],
    }
