# FAB — Flesh and Blood card data app

A data pipeline + API + web frontend for Flesh and Blood TCG card data and prices.

- **Pipeline** (`ingest_bronze.py`, `ingest_tcgcsv.py`) downloads card data
  (the-fab-cube), prices (Cardmarket + tcgcsv/TCGplayer), and exchange rates into
  PostgreSQL `bronze.*`.
- **Transform** (`fab_dbt/`, a dbt project) builds `silver.silver_cards` →
  `gold.gold_cards`.
- **API** (`api.py`, FastAPI) serves `gold.gold_cards` at `/cards`, `/sets`, `/stats`,
  admin quality endpoints, and a card-scanning `/scan` endpoint. It also serves the built
  frontend at `/`.
- **Frontend** (`retro-data-display/`, Vite + React + TS) — a retro card search UI,
  hosted by **Lovable** from the `retro-data-display` GitHub repo. It reaches the API over
  the cloudflared tunnel; `start_fab.py` keeps its `VITE_API_BASE_URL` in sync.

## Layout

```
api.py                 FastAPI app (also serves the frontend from dist/)
ingest_bronze.py       Bronze-layer ingestion (downloads -> Postgres)
ingest_image_hashes.py, ingest_orb_descriptors.py, load_collectors_centre.py
setup_db.py            One-time DB/schema creation
start_fab.py           Launch API + Cloudflare quick tunnel (public URL)
run_pipeline.sh        ingest -> dbt -> serve
docker-compose.yml     Local Postgres (db service)
fab_dbt/               dbt project (profile fab_shop, profiles.yml included)
retro-data-display/    frontend (build output -> dist/, served by the API)
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
`start_fab.py` exposes the API at a `https://<random>.trycloudflare.com` URL, then **syncs
the Lovable frontend**: it writes that URL into `retro-data-display/.env`
(`VITE_API_BASE_URL`) and `git push`es it (staging **only** `.env`) so Lovable redeploys
against the live API. The URL is also saved to `tmp/logs/tunnel_url.txt`.

Frontend hosting — two options:
- **Lovable (current):** the app is hosted by Lovable from the `retro-data-display` GitHub
  repo. It runs on its own origin, so it needs the absolute API URL — kept current
  automatically by `start_fab.py`. Set `PUSH_LOVABLE=0` to update `.env` without pushing.
- **Self-hosted (fallback):** FastAPI also serves `retro-data-display/dist` at `/`, so the
  app is reachable on the same tunnel/origin as the API without Lovable.

> ⚠️ The quick-tunnel URL changes on every restart, so Lovable is re-pushed each run. For a
> **stable** URL that never needs re-syncing, use a named Cloudflare tunnel (needs a domain).

Full daily refresh + serve: `./run_pipeline.sh`

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
- The frontend was scaffolded with `bun`; `npm` needs `--legacy-peer-deps` because
  `vite@8` is newer than `@vitejs/plugin-react-swc@3.11`'s declared peer range (the build
  works regardless).

## Roadmap: the "hub"

The end goal is a self-contained install for an always-on / paid server. `docker-compose.yml`
already runs Postgres; extend it with an `api` service (FastAPI + built frontend) and a
one-shot `pipeline` job so the whole stack comes up with `docker compose up`. At that
point swap the quick tunnel for a named Cloudflare Tunnel (stable domain) or the server's
own reverse proxy.
