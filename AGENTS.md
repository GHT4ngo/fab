# AGENTS.md — Codex handoff notes

Read this together with `README.md`, `CLAUDE.md`, and `agent_worklog.md`.

## Current repo shape
- `/home/tango/Projects/fab` is the backend/data workspace, but it is not currently a
  normal git repo from Codex's point of view.
- `retro-data-display/` is the GitHub/Lovable frontend repo:
  `GHT4ngo/retro-data-display`.
- Backend/dbt changes must be preserved locally unless the user later puts the root
  project under a real Git remote.

## Git safety
- The frontend working tree has many modified files unrelated to the admin work. Do not
  bulk commit or reset them.
- When pushing frontend work, stage only the intended files.
- `start_fab.py` intentionally stages and pushes only `retro-data-display/.env` to update
  Lovable with the current quick-tunnel URL.

## Verified commands
```bash
sudo docker compose up -d db
.venv/bin/python ingest_tcgcsv.py
( set -a; . ./.env; set +a; cd fab_dbt && ../.venv/bin/dbt run --profiles-dir . )
( set -a; . ./.env; set +a; cd fab_dbt && ../.venv/bin/dbt test --profiles-dir . )
npm --prefix retro-data-display run build
```

If port 8001 is busy:
```bash
ss -ltnp | rg ':8001'
ps -ef | rg 'start_fab|uvicorn api:app|cloudflared tunnel'
```
Stop stale `start_fab.py`/uvicorn/cloudflared processes before restarting.

## Current verified data state
After the 2026-06-28 cleanup:
- `gold.gold_cards`: 17,256 rows.
- priced rows: 16,096.
- price coverage: 93.3%.
- dbt tests: 13/13 passing.
- delimited `display_id` rows: 0.
- fake admin rows for `ARC N`, `WTR N/F`, and `ELE N` are gone.

## Important data rules
- Tier 4 is tcgcsv-only missing data.
- Exclude tcgcsv composite/pair products from tier 4 when `bronze.tcgcsv_cards.number`
  contains `/`. They are TCGplayer product bundles, not standalone card printings.
- Keep real split-card names when the collector number is a normal single id.
- `price_sek` remains Cardmarket EUR first, tcgcsv USD fallback.
- Pitch colour is display-only, not a Cardmarket match key.

## Frontend/admin
- Admin dashboard changes are pushed to GitHub at commit
  `9798649 feat: improve admin data quality dashboard`.
- The latest tunnel `.env` push at handoff was commit `6fccdd9`, but quick-tunnel URLs are
  ephemeral; trust `tmp/logs/tunnel_url.txt` for the current local run.
- Admin page expects current backend endpoints:
  `/stats`, `/admin/sets`, `/admin/quality`, `/admin/price-discrepancies`,
  `/admin/unmatched`.
