"""
ingest_orb_descriptors.py — Download FaB card images and compute ORB feature descriptors.

Stores descriptors as BYTEA in bronze.card_orb_descriptors.
Resumable: skips image_urls already in the table.
Run once initially, then re-run to pick up new cards after dbt runs.

Usage:
    python ingest_orb_descriptors.py
"""

import os
import io
import time
import sys
import psycopg2
import psycopg2.extras
import requests
import numpy as np
import cv2
from PIL import Image
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

SLEEP       = 0.15   # seconds between downloads
BATCH_SIZE  = 50     # commit every N rows
TIMEOUT     = 10     # request timeout seconds
ORB_FEATURES = 500   # max keypoints per image

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


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bronze.card_orb_descriptors (
                image_url    TEXT PRIMARY KEY,
                descriptors  BYTEA NOT NULL,
                kp_count     INT   NOT NULL,
                computed_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()


def compute_orb(img_pil: Image.Image) -> tuple[np.ndarray | None, int]:
    """
    Convert PIL image to grayscale and compute ORB descriptors.
    Returns (descriptors_bytes_array, keypoint_count).
    Returns (None, 0) if no keypoints found.
    """
    arr = np.array(img_pil.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    # Resize to standard width to keep descriptor sizes consistent
    target_w = 400
    h, w = gray.shape
    if w != target_w:
        scale = target_w / w
        gray = cv2.resize(gray, (target_w, int(h * scale)), interpolation=cv2.INTER_AREA)

    orb = cv2.ORB_create(nfeatures=ORB_FEATURES)
    _, desc = orb.detectAndCompute(gray, None)
    if desc is None or len(desc) == 0:
        return None, 0
    return desc, len(desc)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    conn = get_conn()
    ensure_table(conn)
    cur = conn.cursor()

    # All distinct image URLs not yet processed
    cur.execute("""
        SELECT DISTINCT raw_data->>'image_url' AS image_url
        FROM bronze.fab_printings
        WHERE raw_data->>'image_url' IS NOT NULL
          AND raw_data->>'image_url' NOT IN (
              SELECT image_url FROM bronze.card_orb_descriptors
          )
        ORDER BY 1
    """)
    pending = [r["image_url"] for r in cur.fetchall()]
    total   = len(pending)

    if total == 0:
        print(f"{GREEN}All images already processed -- nothing to do.{RESET}")
        cur.close(); conn.close()
        return

    print(f"{CYAN}Computing ORB descriptors for {total:,} images (skipping already-done){RESET}")
    print(f"{CYAN}Estimated time: ~{total * SLEEP / 60:.0f} min at {SLEEP}s/image{RESET}")
    print()

    done   = 0
    errors = 0
    no_kp  = 0
    batch  = []

    for url in pending:
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            img  = Image.open(io.BytesIO(resp.content))
            desc, kp_count = compute_orb(img)

            if desc is None:
                no_kp += 1
                # Store a placeholder row with empty bytes so we don't retry forever
                batch.append((url, b"", 0))
            else:
                batch.append((url, desc.tobytes(), kp_count))
            done += 1
        except Exception as e:
            errors += 1
            print(f"  {YELLOW}skip{RESET} {url[-60:]}  ({e})")

        if done % 50 == 0 and done > 0:
            pct = done / total * 100
            remaining = (total - done) * SLEEP / 60
            print(f"  {GREEN}{done:,}/{total:,}{RESET}  ({pct:.0f}%)  ~{remaining:.0f} min left")

        if len(batch) >= BATCH_SIZE:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO bronze.card_orb_descriptors (image_url, descriptors, kp_count)
                VALUES %s ON CONFLICT (image_url) DO NOTHING
            """, batch)
            conn.commit()
            batch = []

        time.sleep(SLEEP)

    if batch:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO bronze.card_orb_descriptors (image_url, descriptors, kp_count)
            VALUES %s ON CONFLICT (image_url) DO NOTHING
        """, batch)
        conn.commit()

    cur.execute("SELECT COUNT(*) FROM bronze.card_orb_descriptors")
    total_stored = cur.fetchone()["count"]
    cur.close(); conn.close()

    print()
    print(f"{GREEN}Done. {done:,} processed, {errors} download errors, {no_kp} with no keypoints.")
    print(f"Total in DB: {total_stored:,}{RESET}")


if __name__ == "__main__":
    main()
