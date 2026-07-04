# FAB — Flesh and Blood card data app

A data pipeline + API + web frontend for Flesh and Blood TCG card data and prices.

- **Pipeline** (`ingest_bronze.py`, `ingest_tcgcsv.py`) downloads card data
  (the-fab-cube), prices (Cardmarket + tcgcsv/TCGplayer), and exchange rates into
  PostgreSQL `bronze.*`.
- **Transform** (`fab_dbt/`, a dbt project) builds `silver.silver_cards` →
  `gold.gold_cards`.
- **API** (`api.py`, FastAPI) serves `gold.gold_cards` at `/cards`, `/sets`, `/stats`,
  admin quality endpoints, and card-scanning endpoints (`/scan`, `/scan/native`). It also
  serves the built frontend at `/`.
- **Frontend** (`retro-data-display/`, Vite + React + TS) — a retro card search UI and
  scanner session page, hosted by **Lovable** from the `retro-data-display` GitHub repo.
  It reaches the API over the persistent cloudflared tunnel.
- **Native scanner MVP** (`fab-scanner-android/`) — Android CameraX companion app for
  sharper phone scanning, pair-code sessions, and footer-code recognition.

## Layout

```
api.py                 FastAPI app (also serves the frontend from dist/)
ingest_bronze.py       Bronze-layer ingestion (downloads -> Postgres)
ingest_image_hashes.py, ingest_orb_descriptors.py, load_collectors_centre.py
setup_db.py            One-time DB/schema creation
start_fab.py           Launch API + persistent Cloudflare tunnel (public URL)
run_pipeline.sh        daily ingest/dbt/serve launcher
docker-compose.yml     Local Postgres (db service)
fab_dbt/               dbt project (profile fab_shop, profiles.yml included)
retro-data-display/    frontend (build output -> dist/, served by the API)
fab-scanner-android/   Android CameraX scanner MVP (open in Android Studio)
tmp/                   gitignored: download cache, DB backups, logs, pgdata, cloudflared
```

`tmp/` holds everything throwaway/regenerable: `tmp/data/` (download cache),
`tmp/backup/` (DB snapshots + legacy files), `tmp/logs/`, `tmp/pgdata/` (Postgres
volume), `tmp/bin/cloudflared`.

## First-time setup

```bash
# 1. Secrets
cp .env.example .env        # then fill in PG_PASSWORD + API keys

# 2. Python env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# optional (card scanning / image ingest, heavy):
# .venv/bin/pip install -r requirements-ocr.txt

# 3. Postgres (Docker)
sudo docker compose up -d db     # Postgres on localhost:5432, data in tmp/pgdata/

# 4. Create schema + load data + transform
.venv/bin/python setup_db.py
.venv/bin/python ingest_bronze.py --no-justtcg
.venv/bin/python ingest_tcgcsv.py
( set -a; . ./.env; set +a; cd fab_dbt && ../.venv/bin/dbt run --profiles-dir . )
( set -a; . ./.env; set +a; cd fab_dbt && ../.venv/bin/dbt test --profiles-dir . )

# 5. Build the frontend (served by the API at /)
cd retro-data-display
npm install --legacy-peer-deps     # or: bun install
npm run build                      # or: bun run build
cd ..
```

## Run

Local only:
```bash
.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8001
# http://localhost:8001/  -> frontend ;  /cards /sets /stats -> API
```

Public (share over the internet while the PC is on):
```bash
# one-time: get the Linux cloudflared binary
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
     -o tmp/bin/cloudflared && chmod +x tmp/bin/cloudflared

.venv/bin/python start_fab.py
```
`start_fab.py` exposes the API at a `https://<random>.trycloudflare.com` URL and saves it to
`tmp/logs/tunnel_url.txt`. The Cloudflare quick tunnel is persistent and detached, so normal
API restarts reuse the same public URL. Direct `start_fab.py` does **not** push Git/Lovable
unless called with `--sync-lovable` or `PUSH_LOVABLE=1`.

Frontend hosting — two options:
- **Lovable (current):** the app is hosted by Lovable from the `retro-data-display` GitHub
  repo. It runs on its own origin, so it needs the absolute API URL. The normal daily
  `./run_pipeline.sh` path syncs `retro-data-display/.env` and pushes only when needed.
- **Self-hosted (fallback):** FastAPI also serves `retro-data-display/dist` at `/`, so the
  app is reachable on the same tunnel/origin as the API without Lovable.

Daily commands:
```bash
./run_pipeline.sh                    # daily data refresh + serve + Lovable sync if needed
./run_pipeline.sh --restart          # quick API/tunnel restart, no ingest/dbt, no push
./run_pipeline.sh --restart --sync-lovable  # force a frontend API URL sync
```

## Android scanner MVP

The browser scanner proved unreliable for tiny footer text: debug crops showed the footer
region was in frame but too soft for OCR. The current scanner path is the native Android
CameraX app, paired to the web scanner page with a short trade-session code.

Native project:
```text
fab-scanner-android/
```

Backend endpoint:
```text
POST /scan/native
POST /scan/session
GET  /scan/records
```

Request signals:
- `full_image` — full card guide crop, used for visual matching.
- `footer_crop` — broad lower footer band, used for exact `display_id` OCR.
- `title_crop` — top title strip, used for fuzzy title OCR.
- `debug_save` — saves submitted crops and metadata.
- `session_code` — lets the web page poll scans made from the phone.

Debug output:
```text
tmp/scan_debug_samples/
```

Current flow:
1. Open the web scanner page and start a trade session.
2. Click **Pair phone** and use the short code in the Android app.
3. Scan cards on the phone; the web page polls `/scan/records` and adds matches.

The phone app hides the API URL under **Advanced**. Users should only need the pair code.
This is a lightweight local session system, not real login/account/device auth.

Build/install:
```bash
cd fab-scanner-android
JAVA_HOME=/snap/android-studio/232/jbr \
GRADLE_USER_HOME=/home/tango/Projects/fab/fab-scanner-android/.gradle \
./gradlew :app:assembleDebug

/home/tango/Android/Sdk/platform-tools/adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Matching rules:
- Footer code is the primary decision path. The backend searches several subwindows in the
  broad footer crop because the code may be centered or left aligned.
- OCR corrections handle common set-code mistakes, for example `R05 130` → `ROS130`.
- Partial collector-number reads are refused so a fragment like `BET7` cannot snap to a
  random `BET###` card.
- Visual-only guesses are intentionally not returned; the scanner needs a footer code,
  strong title match, or visual+title agreement.

## Notes

- JustTCG is dormant backup. The normal pipeline uses tcgcsv.com for USD prices and
  missing-set cards because it is fast and does not require an API key.
- dbt tests live under `fab_dbt/tests/` and should pass after every pipeline change.
  Current guardrails check identity fields, accepted edition/foil/tier values, non-negative
  prices, and malformed set/display IDs.
- The admin page shows price coverage and quality signals, not official set checklist
  completeness. Important endpoints: `/admin/sets`, `/admin/quality`,
  `/admin/price-discrepancies`.
- If `run_pipeline.sh` fails at serve time with `address already in use` on port `8001`,
  an old `start_fab.py`/uvicorn process is still running. Stop it before restarting the API.
- If Android wireless debugging gets stuck after stale pairing, a full computer restart has
  been the reliable reset.
- The frontend was scaffolded with `bun`; `npm` needs `--legacy-peer-deps` because
  `vite@8` is newer than `@vitejs/plugin-react-swc@3.11`'s declared peer range (the build
  works regardless).

## Roadmap: the "hub"

The end goal is a self-contained install for an always-on / paid server. `docker-compose.yml`
already runs Postgres; extend it with an `api` service (FastAPI + built frontend) and a
one-shot `pipeline` job so the whole stack comes up with `docker compose up`. At that
point swap the quick tunnel for a named Cloudflare Tunnel (stable domain) or the server's
own reverse proxy.
