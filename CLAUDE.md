# CLAUDE.md — FAB project working notes

Operational focus for agents working in this repo. Pairs with `README.md` (human
setup/run docs), `AGENTS.md` (Codex-specific handoff notes), and `agent_worklog.md`
(running log of decisions + what was tried). Read all four before making changes.

## What this is
A Flesh and Blood TCG card-data app: ingest → PostgreSQL (bronze/silver/gold) →
FastAPI → Vite/React frontend (hosted on Lovable, fallback self-hosted same-origin).

```
sources ──▶ ingest_*.py ──▶ bronze.*  ──(dbt)──▶ silver.silver_cards ──▶ gold.gold_cards ──▶ api.py ──▶ frontend
```

## Architecture (medallion)
- **bronze** — raw, source-shaped tables, one ingest script per source. Truncate-and-
  reload or upsert; keep a `raw_data JSONB` column where the source is rich so silver
  can read new fields without re-ingesting.
- **silver** — `fab_dbt/models/silver/silver_cards.sql`. One row per printing
  (set × edition × foiling). This is where all the matching/business logic lives
  (price tiers, foil resolution, source unioning). It is the hard part of the repo.
- **gold** — `fab_dbt/models/gold/gold_cards.sql`. Thin, API-ready projection of silver
  (adds `is_foil`, `has_price`). The API only reads gold.

## Data sources
| Source | Gives | Notes |
|---|---|---|
| **the-fab-cube** (GitHub JSON) | card catalogue spine: names, stats, text, images, set list | **Lags ~2 boosters** — unreliable for brand-new sets. |
| **Cardmarket** (S3 bulk) | EUR prices, product catalogue | **No image field.** EUR only. |
| **tcgcsv.com** | USD prices, names, set #, rarity, card text, images, for ALL sets incl. new | **Primary price/missing-set source.** Free, no key, no rate limit. Category **62** = FaB. Send a `User-Agent` header (401 otherwise). |
| **JustTCG** (API) | USD prices + missing-set cards | **Dormant backup** — superseded by tcgcsv (was 100 req/day, ~7s/req). Scripts/tables kept but out of the default pipeline. |
| **Riksbank** | EUR/SEK, USD/SEK rates | drives `price_sek`. |
| **collectors_centre** (scraped fabtcg.com) | foil technique per printing | validates tier-2 foil matches. |

`price_sek` = CM EUR → SEK if available, else tcgcsv USD → SEK.

## Price match tiers (in `silver_cards`, surfaced as `gold.match_tier`)
tcgcsv `productId` (= `tcgplayer_product_id`) is the **anchor key**: the tcgcsv USD price
per printing drives how Cardmarket EUR is matched. Matching is on the **bare card name**
(pitch colour is display-only, NOT a key — Cardmarket is inconsistent about the
`(Red/Yellow/Blue)` suffix; `cm_products.base_name` strips it). Tiers, best linkage first:
1. **anchored** — Cardmarket product picked by closest EUR (in SEK) to the tcgcsv USD
   anchor, from ALL products sharing the bare name (no expansion constraint). The anchor
   price + the card id/number separate same-named cards (e.g. promo alt-arts).
2. auto — `fab_expansions` + Cardmarket by (idExpansion, base_name) + foil heuristic (no anchor)
3. fallback — bare-name aggregation across expansions
4. missing-set card sourced from **tcgcsv** (set absent from the-fab-cube)
5. **manual** — `fab_cm_manual` crosswalk, **LAST resort** (least trusted; ~12 rows now)
- `null` — no price matched

Tier-4 deliberately excludes tcgcsv composite products whose collector `number` contains
`/` (for example `ARC001 // ARC003`, `NUU028/NUU029`). Those are TCGplayer pair products
such as hero+weapon or token bundles, not standalone card printings. Keeping them created
bogus edition-`N` admin rows for existing sets (`ARC`, `WTR`, `ELE`). This is guarded by
dbt tests for delimited set/display IDs.

## Source reliability model (decided 2026-06-28)
`match_tier` = the linkage method; the headline price also carries explicit
**source + confidence**:
- `price_sek` basis = **Cardmarket EUR first** (EU market), **tcgcsv USD fills the gap**.
  Chosen deliberately — do NOT silently flip it.
- `gold.price_source` ∈ {`cardmarket_anchored`, `cardmarket_auto`, `cardmarket_fallback`,
  `cardmarket_manual`, `tcgcsv_usd`}; `gold.price_confidence` ∈ {high, medium, low}.
- Confidence: anchored & tcgcsv_usd = high, auto = medium, fallback & manual = low.
- **`GET /admin/price-discrepancies`** normalises EUR & USD to SEK and flags divergences
  (params: tier [0=all], min_ratio [2.0], min_sek [25]). Use it to QA matches. After the
  anchored rewrite + bare-name matching, bad divergences (>3×, ≥50 SEK) fell ~402 → ~79
  (all tier 1) and no-match fell 2,622 → 270. The residual ~79 are genuine EU/US market
  gaps — Cardmarket has no product near the tcgcsv price for that card (data-inherent).
- NOTE: in FaB, **Alpha = 1st Edition** — `tcg_prices` maps edition `A` and `F` both to
  tcgcsv's `'1st Edition …'` price, so Alpha printings anchor correctly (not a residual).
- NOTE: pitch colour is **display-only**; matching never uses it. Same-named cards
  (e.g. Sink Below Red/Yellow/Blue, or regular vs extended-art promo) are kept distinct by
  the card id/number in the spine + the per-printing tcgcsv anchor price.

## Card name normalisation (tcgcsv)
tcgcsv bakes extra tokens into product names; we normalise so the **same card shares one
name** (the card id/number is the key; details show on expand). In `silver_cards`
`tcgcsv_src`/`tcgcsv_missing`, from the tcgcsv name we strip, in order:
1. trailing collector number — both `" - FAB169"` and `" (ANQ011)"` forms,
2. the pitch colour `(Red/Yellow/Blue)` anywhere → `pitch` field,
3. all trailing `(...)` alt-art treatments → folded into the new `gold.variant` column
   (comma-joined when stacked, e.g. `Cintari Saber` + `variant="Left, Golden"`).
the-fab-cube names are already clean (`variant = null`). `variant` is exposed in `/cards`
for the expand view. Gotcha: `bronze.tcgcsv_cards` has its own always-null `pitch` column,
so the derived pitch is aliased `derived_pitch` to avoid an ambiguous-column clash with `c.*`.

## Conventions
- Python: stdlib + `requests` + `psycopg2` + `python-dotenv`. Each ingest script:
  loads `.env`, builds `DB_CONFIG` from `PG_*`, prints with the shared ANSI
  `ok/info/warn/err`/`section` helpers. Match the existing visual style.
- DB creds come from `.env` as `PG_HOST/PG_PORT/PG_USER/PG_PASSWORD/PG_DATABASE`
  (NOT the standard `PGHOST`/`PGPASSWORD`). DB name is `fab`.
- New bronze table → add it to `setup_db.py` `CREATE_TABLES_SQL` (idempotent
  `CREATE TABLE IF NOT EXISTS`) AND an `ok()` line; setup_db is the single source of schema.
- Don't commit the frontend working tree — only `.env` is safe to push (see start_fab.py).
- `tmp/` is throwaway/regenerable (download cache, pgdata, logs, cloudflared). Never rely
  on it for source-of-truth.

## Run / verify
```bash
# DB must be up first (already on :5432 if the container is running):
sudo docker compose up -d db          # docker needs sudo here (non-interactive will fail)
.venv/bin/python setup_db.py          # idempotent schema
.venv/bin/python ingest_bronze.py --no-justtcg   # catalogue + CM (network)
.venv/bin/python ingest_tcgcsv.py     # USD prices + missing sets (fast, no rate limit)
( set -a; . ./.env; set +a; cd fab_dbt && ../.venv/bin/dbt run --profiles-dir . )
( set -a; . ./.env; set +a; cd fab_dbt && ../.venv/bin/dbt test --profiles-dir . )
.venv/bin/python start_fab.py         # API :8001 + Cloudflare tunnel + Lovable sync
```
Full pipeline: `./run_pipeline.sh` (`--no-serve` to skip API/tunnel).

Latest verified post-cleanup shape (2026-06-28): `gold.gold_cards` has 17,256 rows,
93.3% price coverage, 0 delimited display IDs, and all 13 dbt tests pass.

## Gotchas
- Python 3.14 venv at `.venv`. OCR deps are heavy → `requirements-ocr.txt` (separate).
- Frontend scaffolded with bun; `npm install` needs `--legacy-peer-deps`.
- `start_fab.py` rewrites `retro-data-display/.env` + pushes each run (ephemeral tunnel
  URL). Use `PUSH_LOVABLE=0` when testing so you don't push a dead URL.
- If port 8001 is already in use, check for stale `start_fab.py`/uvicorn/cloudflared
  processes before rerunning. The API reads gold live, but code changes require a restart.
- Docker requires sudo in this environment; `run_pipeline.sh` falls back to `sudo docker`
  which fails non-interactively. Start the DB container separately or add user to `docker` group.
