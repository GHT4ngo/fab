"""
Nightly logical backup of the `app` schema — the ONLY non-regenerable data
(users, sessions, cardlists, scan history). Bronze/silver/gold can always be
rebuilt from sources; app.* cannot.

Writes backups/app_YYYYMMDD_HHMMSS.sql: standard psql script with
TRUNCATE + COPY ... FROM stdin blocks + sequence setvals. Keeps the newest
KEEP_BACKUPS files, deletes older ones.

Run manually:   .venv/bin/python backup_app.py
Cron (installed by this project, daily 03:30):
  30 3 * * * cd /home/tango/Projects/fab && .venv/bin/python backup_app.py >> tmp/logs/backup_app.log 2>&1

Restore (host has no psql client — go through the docker container):
  sudo docker compose exec -T db psql -U <PG_USER> -d fab < backups/app_<stamp>.sql
Schema/tables are recreated by setup_db.py / api startup if missing; the
backup file only carries data (TRUNCATE + COPY), so run setup_db.py first
on a fresh database.
"""

import io
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(dotenv_path=HERE / ".env")

PG_HOST     = os.getenv("PG_HOST", "localhost")
PG_PORT     = int(os.getenv("PG_PORT", 5432))
PG_USER     = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

BACKUP_DIR   = HERE / "backups"
KEEP_BACKUPS = 30

GREEN = "\033[32m"
CYAN  = "\033[36m"
RED   = "\033[31m"
RESET = "\033[0m"

def ok(msg):   print(f"  {GREEN}✔{RESET}  {msg}")
def info(msg): print(f"  {CYAN}→{RESET}  {msg}")
def err(msg):  print(f"  {RED}✘{RESET}  {msg}")


def dump_app_schema(conn, out) -> int:
    """Write a psql-loadable data dump of every table in the app schema."""
    cur = conn.cursor()

    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'app' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)
    tables = [r[0] for r in cur.fetchall()]

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out.write(f"-- app schema data backup · {stamp}\n")
    out.write("-- restore: sudo docker compose exec -T db psql -U <user> -d fab < <this file>\n")
    out.write("BEGIN;\n")
    out.write("SET session_replication_role = replica;  -- defer FK checks during load\n\n")

    # TRUNCATE everything first (single statement so FK order doesn't matter),
    # then COPY each table back in.
    qualified = ", ".join(f"app.{t}" for t in tables)
    out.write(f"TRUNCATE {qualified} CASCADE;\n\n")

    total_rows = 0
    for t in tables:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'app' AND table_name = %s
            ORDER BY ordinal_position
        """, (t,))
        cols = [r[0] for r in cur.fetchall()]
        col_list = ", ".join(f'"{c}"' for c in cols)

        buf = io.StringIO()
        cur.copy_expert(f'COPY app."{t}" ({col_list}) TO STDOUT', buf)
        data = buf.getvalue()
        rows = data.count("\n")
        total_rows += rows

        out.write(f'COPY app."{t}" ({col_list}) FROM stdin;\n')
        out.write(data)
        out.write("\\.\n\n")
        info(f"app.{t}: {rows} rows")

    # Bump sequences past the restored ids (BIGSERIAL pks) — capture the
    # dump-time value as a literal so restore works on a fresh database too.
    cur.execute("""
        SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'app'
    """)
    for seq, last_value in cur.fetchall():
        out.write(f"SELECT setval('app.\"{seq}\"', {int(last_value or 1)}, true);\n")

    out.write("SET session_replication_role = DEFAULT;\n")
    out.write("COMMIT;\n")
    return total_rows


def rotate():
    files = sorted(BACKUP_DIR.glob("app_*.sql"), key=lambda p: p.name, reverse=True)
    for old in files[KEEP_BACKUPS:]:
        old.unlink()
        info(f"rotated out {old.name}")


def main():
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"app_{stamp}.sql"

    try:
        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, database="fab",
                                user=PG_USER, password=PG_PASSWORD)
    except Exception as e:
        err(f"cannot connect to postgres: {e}")
        sys.exit(1)

    try:
        tmp_target = target.with_suffix(".sql.part")
        with open(tmp_target, "w") as out:
            rows = dump_app_schema(conn, out)
        tmp_target.rename(target)   # atomic-ish: never leave a half-written .sql
        ok(f"{target.relative_to(HERE)} · {rows} rows · {target.stat().st_size:,} bytes")
    finally:
        conn.close()

    rotate()


if __name__ == "__main__":
    main()
