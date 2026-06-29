"""
ingest_image_hashes.py — Download FaB card images and compute perceptual hashes.

Stores dhash + phash per image_url in bronze.card_image_hashes.
Resumable: skips URLs already in the table.
Run once initially, then re-run to pick up new cards after dbt runs.

Usage:
    python ingest_image_hashes.py
"""

import os
import io
import time
import sys
import psycopg2
import psycopg2.extras
import requests
import imagehash
from PIL import Image
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

SLEEP       = 0.15   # seconds between downloads — polite to CDN
BATCH_SIZE  = 100    # commit every N rows
TIMEOUT     = 10     # request timeout seconds

GREEN  = "\033[32m"
CYAN   = "\033[36m"
YELLOW = "\033[33m"
RED    = "\033[31m"
RESET  = "\033[0m"

def get_conn():
    return psycopg2.connect(
        host=os.environ["PG_HOST"], port=os.environ["PG_PORT"],
        dbname=os.environ["PG_DATABASE"],
        user=os.environ["PG_USER"], password=os.environ["PG_PASSWORD"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    conn = get_conn()
    cur  = conn.cursor()

    # All distinct image URLs not yet hashed
    cur.execute("""
        SELECT DISTINCT raw_data->>'image_url' AS image_url
        FROM bronze.fab_printings
        WHERE raw_data->>'image_url' IS NOT NULL
          AND raw_data->>'image_url' NOT IN (
              SELECT image_url FROM bronze.card_image_hashes
          )
        ORDER BY 1
    """)
    pending = [r["image_url"] for r in cur.fetchall()]
    total   = len(pending)

    if total == 0:
        print(f"{GREEN}All images already hashed — nothing to do.{RESET}")
        cur.close(); conn.close()
        return

    print(f"{CYAN}Hashing {total:,} images  (skipping already-done){RESET}")
    print(f"{CYAN}Estimated time: ~{total * SLEEP / 60:.0f} min at {SLEEP}s/image{RESET}")
    print()

    done = 0
    errors = 0
    batch = []

    for url in pending:
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            img  = Image.open(io.BytesIO(resp.content)).convert("RGB")
            dh   = str(imagehash.dhash(img))
            ph   = str(imagehash.phash(img))
            batch.append((url, dh, ph))
            done += 1
        except Exception as e:
            errors += 1
            print(f"  {YELLOW}skip{RESET} {url[-60:]}  ({e})")

        # Progress every 50
        if done % 50 == 0:
            pct = done / total * 100
            remaining = (total - done) * SLEEP / 60
            print(f"  {GREEN}{done:,}/{total:,}{RESET}  ({pct:.0f}%)  ~{remaining:.0f} min left")

        # Commit in batches
        if len(batch) >= BATCH_SIZE:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO bronze.card_image_hashes (image_url, dhash, phash)
                VALUES %s ON CONFLICT (image_url) DO NOTHING
            """, batch)
            conn.commit()
            batch = []

        time.sleep(SLEEP)

    # Final commit
    if batch:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO bronze.card_image_hashes (image_url, dhash, phash)
            VALUES %s ON CONFLICT (image_url) DO NOTHING
        """, batch)
        conn.commit()

    cur.execute("SELECT COUNT(*) FROM bronze.card_image_hashes")
    total_stored = cur.fetchone()["count"]
    cur.close(); conn.close()

    print()
    print(f"{GREEN}Done. {done:,} hashed, {errors} errors. Total in DB: {total_stored:,}{RESET}")


if __name__ == "__main__":
    main()
