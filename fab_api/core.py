"""Shared runtime for the FaB API: env, scan-log paths, and the pg connection pool."""

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=HERE / ".env")

# Scan debug log — appended to on every /scan call so you can read it from the PC
SCAN_LOG = str(HERE / "tmp" / "logs" / "scan_debug.log")
os.makedirs(os.path.dirname(SCAN_LOG), exist_ok=True)
SCAN_DEBUG_DIR = HERE / "tmp" / "scan_debug_samples"
SCAN_DEBUG_DIR.mkdir(parents=True, exist_ok=True)

def _slog(*parts):
    """Append a timestamped line to the scan debug log and also print it."""
    import datetime
    line = datetime.datetime.now().strftime("%H:%M:%S") + "  " + "  ".join(str(p) for p in parts)
    print(line)
    with open(SCAN_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

PG_HOST     = os.getenv("PG_HOST", "localhost")
PG_PORT     = int(os.getenv("PG_PORT", 5432))
PG_USER     = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

# ── Transactional email (Resend, t4ngo.com verified) ─────────────────────────
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM    = os.getenv("RESEND_FROM", "FAB Matrix <onboarding@resend.dev>")


def send_email(to: str, subject: str, html: str) -> bool:
    """Best-effort transactional email via Resend. Returns True when accepted.
    Never raises — callers treat email as a side effect that must not break the
    request (a failed notification is logged, not surfaced)."""
    if not RESEND_API_KEY:
        _slog(f"[EMAIL] no RESEND_API_KEY — skipped '{subject}' to {to}")
        return False
    try:
        import requests
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": RESEND_FROM, "to": [to], "subject": subject, "html": html},
            timeout=10,
        )
        if resp.ok:
            _slog(f"[EMAIL] sent '{subject}' to {to}")
            return True
        _slog(f"[EMAIL] Resend error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        _slog(f"[EMAIL] send failed: {type(e).__name__}: {e}")
    return False


def send_email_async(to: str, subject: str, html: str) -> None:
    """Fire-and-forget send_email on a daemon thread — keeps API responses snappy."""
    threading.Thread(target=send_email, args=(to, subject, html), daemon=True).start()


# Connection pool — a fresh psycopg2.connect per request costs TCP+auth setup
# on every call; the pool keeps warm connections. get_conn() stays a context
# manager so all `with get_conn() as conn:` call sites are unchanged.
_pg_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pg_pool_lock = threading.Lock()


# FastAPI runs sync endpoints on a large threadpool, and one page load fires
# several API calls at once, so short bursts above the pool size are normal.
PG_POOL_MAX = int(os.getenv("PG_POOL_MAX", "32"))
PG_POOL_WAIT = float(os.getenv("PG_POOL_WAIT", "10"))


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pg_pool
    if _pg_pool is None:
        with _pg_pool_lock:
            if _pg_pool is None:
                _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=PG_POOL_MAX,
                    host=PG_HOST, port=PG_PORT,
                    database="fab",
                    user=PG_USER, password=PG_PASSWORD,
                    cursor_factory=psycopg2.extras.RealDictCursor,
                )
    return _pg_pool


def _acquire(pool: psycopg2.pool.ThreadedConnectionPool):
    """Wait briefly for a free connection instead of failing the request.

    psycopg2's getconn() raises PoolError the instant the pool is empty, which
    turned a momentary burst into 500s for whatever else was in flight. Backing
    off and retrying makes a burst queue for a few milliseconds instead."""
    deadline = time.monotonic() + PG_POOL_WAIT
    delay = 0.01
    while True:
        try:
            return pool.getconn()
        except psycopg2.pool.PoolError:
            if time.monotonic() >= deadline:
                _slog("[DB] connection pool exhausted — raise PG_POOL_MAX if this repeats")
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = _acquire(pool)
    broken = False
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            broken = True   # connection itself is dead (e.g. postgres restarted)
        raise
    finally:
        pool.putconn(conn, close=broken or conn.closed)
