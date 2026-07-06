# CLAUDE.md — FAB project working notes

Operational focus for agents working in this repo. Pairs with `README.md` (human
setup/run docs), `AGENTS.md` (Codex-specific handoff notes), and `agent_worklog.md`
(running log of decisions + what was tried). Read all four before making changes.

## What this is
A Flesh and Blood TCG card-data app: ingest → PostgreSQL (bronze/silver/gold) →
FastAPI → Vite/React frontend (hosted on Lovable, fallback self-hosted same-origin).
A native Android scanner MVP now also exists under `fab-scanner-android/` for camera
capture experiments; it talks to the same FastAPI backend.

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

## API layout (split 2026-07-05)
`api.py` is thin wiring only (middleware, include_router, static mount) — still run as
`python api.py` / `uvicorn api:app`. Endpoints live in `fab_api/routers/`
(`cards`, `admin`, `scan`, `auth`, `cardlists`, `tools`); shared env + the **pooled**
`get_conn()` context manager (ThreadedConnectionPool, commits on exit, returns conn to
the pool) in `fab_api/core.py`; the OCR/visual scan engine — moved **verbatim**, logic
untouched — in `fab_api/scan_engine.py`. GZip middleware compresses `/cards` (~11×);
`/sets` + `/stats` send Cache-Control. `app.*` is the only non-regenerable data:
`backup_app.py` dumps it nightly (user cron, 03:30) to `backups/` (30 kept, gitignored);
restore via `sudo docker compose exec -T db psql -U <user> -d fab < backups/app_<ts>.sql`.

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
   **Sanity guard (2026-07-06):** if even the closest candidate is ≥20× off the anchor
   AND ≥50 SEK apart, CM has no product for that printing (e.g. HNT055 Cindra token
   $0.08 vs the €5.72 armory-deck hero) → the printing skips tiers 1-3 entirely and
   falls to the tcgcsv USD price (manual tier-5 still overrides). The 3-20× band is
   deliberately untouched — those are genuine EU/US market gaps.
2. auto — `fab_expansions` + Cardmarket by (idExpansion, base_name) + foil heuristic (no anchor)
3. fallback — bare-name aggregation across expansions
4. missing-set card sourced from **tcgcsv** (set absent from the-fab-cube). Since
   2026-07-06 tier-4 rows ALSO get an anchored Cardmarket EUR match (`tcgcsv_cm` CTEs —
   same bare-name pool + closest-to-anchor pick + ≥20×/≥50-SEK guard as tier 1); USD
   falls back `market_price → low_price` (TCGplayer has no market price for low-volume
   deck singles like 1HB/1HD History Pack blitz decks). Anchor-less printings stay
   EUR-less on purpose. `price_source` shows `cardmarket_anchored` when EUR matched.
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
- The frontend working tree often has unrelated generated/scaffold drift. When pushing
  frontend work, stage only the intended files; `.env` URL sync is handled separately.
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
.venv/bin/python start_fab.py         # API :8001 + persistent tunnel; no Git push by default
```
`run_pipeline.sh` is the single launcher (modes, not just `--no-serve`):
- `./run_pipeline.sh` — daily path: ingest if needed, dbt, serve, and sync Lovable/GitHub
  when the public API URL needs updating.
- `./run_pipeline.sh --restart` — quick server restart only; skips ingest/dbt. Reuses the
  tunnel **only if its public URL still responds** (see below); if the tunnel URL changed
  it DOES sync Lovable (a stale frontend URL is the whole point of fixing).
- `--full` force the pipeline; `--no-serve` pipeline only; `--new-tunnel` fresh URL;
  `--stop` kill the tunnel; `--sync-lovable` / `--no-sync-lovable`; `--help`.

The Cloudflare tunnel is now **persistent**: `start_fab.py` runs cloudflared detached
(pidfile `tmp/logs/cloudflared.pid`) so it **survives API restarts** → the URL stays put and
Lovable is **not** rebuilt on every restart. **Reuse is gated on an actual HTTP reachability
check (`tunnel_reachable`), not just a live PID** — trycloudflare quick tunnels frequently
keep the cloudflared process alive while the edge control-stream has died (log:
`control stream encountered a failure` + `Retrying connection`). A live-but-dead tunnel was
the cause of "restarted but the frontend can't connect": the old code reused it by PID. Now
a zombie tunnel is torn down and replaced with a fresh one on the next start. Because that
changes the URL, Lovable **is** synced on that restart (sync fires whenever the URL changed,
even without `--sync-lovable`; a normal same-URL restart still causes no push/rebuild).
`start_fab.py` also publishes every URL to a gist for the native app (`publish_endpoint`,
always, no rebuild). Git timeouts are nonblocking so API startup does not hang on `git push`.
URL changes on reboot, tunnel death (now auto-detected + replaced), or `--new-tunnel`.
`--stop-tunnel` tears it down.

The `fab` repo is on GitHub at **GHT4ngo/fab** (public). Pushing over HTTPS uses the `gh`
token as git's credential helper — if a push prompts for a password, run `gh auth setup-git`
once (GitHub rejects passwords). The `retro-data-display` frontend is a **separate** repo
(its own remote); the parent repo gitignores it.

Latest verified post-cleanup shape (2026-06-28): `gold.gold_cards` has 17,256 rows,
93.3% price coverage, 0 delimited display IDs, and all 13 dbt tests pass.

## Card detail view (Phase 1, DONE + live)
- Frontend `CardDetailModal.tsx`: click a printing (grid or list) → full detail (image,
  stat chips, type, rules text, EUR+USD each in SEK, source/confidence). Wired via
  `onSelect` from `Index.tsx` through CardGrid/CardGroupItem/CardItem + CardListView.
- `/cards` also returns `health`/`intelligence`/`functional_text` and computed
  `price_eur_sek`/`price_usd_sek` (USD/SEK rate from `bronze.exchange_rates`). Grouped card
  art uses the **oldest** printing's image (`groupCards.ts`).
- Manual code entry: `GET /scan/code?code=HVY050` (typo-corrected).

## Accounts + cardlists (Phase 2, DONE + live)
Passwordless magic-link accounts + server-side named cardlists. Tables live in the `app`
schema (`setup_db.py` is canonical; `fab_api/routers/auth.py` self-migrates via
`ensure_app_auth_schema`):
`app.users`, `app.magic_tokens` (15-min TTL), `app.sessions` (30-day bearer token),
`app.cardlists`, `app.cardlist_items` (UNIQUE(list, printing), qty).
- **Auth**: `POST /auth/request-link` (mints token), `GET /auth/verify?token=` (→ session
  token), `GET /auth/me`, `POST /auth/logout`. Session via `Authorization: Bearer <token>`
  resolved by the `_current_user` FastAPI dependency (401 if missing/expired).
- **Cardlists CRUD**: `GET/POST /cardlists`, `GET/PATCH/DELETE /cardlists/{id}`,
  `POST /cardlists/{id}/items` (adds to qty on conflict), `PATCH`/`DELETE
  /cardlists/{id}/items/{printing}`. Ownership enforced everywhere (`_get_owned_cardlist`).
- **EMAIL IS LIVE via Resend (2026-07-06)**: `RESEND_API_KEY` in `.env` → magic link is
  emailed (`emailed: true`, token NOT in the response); without the key it falls back to
  dev mode (`dev_link` returned). Links target the frontend (`/account?token=…`, consumed
  by AuthProvider); `MAGIC_LINK_BASE` env can pin a stable base. **Free-tier limit: only
  delivers to the Resend account owner's own address** — verify a domain in Resend to
  email other users (same domain purchase as the named-tunnel fix).
- Self-hosted frontend has an **SPA fallback** (`SpaStaticFiles` in `api.py`): unknown
  paths serve `index.html`, so deep links like the emailed magic link work on the tunnel.
- Frontend: `src/lib/auth.ts` (client + localStorage token), `src/hooks/useAuth.tsx`
  (AuthProvider), `src/pages/Account.tsx`, `AddToListButton`, `SaveScanToListButton`.

## Trading (Phase 4, DONE + live 2026-07-06)
- **Valuation rule (user-confirmed): `gold.trade_value_sek` = greatest(CM trend, CM low)
  EUR→SEK, tcgcsv USD→SEK fallback** ("trend price, or low if it's higher").
  `gold.cm_low_eur` also exposed. Computed in silver's final select.
- A cardlist with `app.cardlists.is_trade_list = true` is a public trade list
  (toggle on Account; `PATCH /cardlists/{id}` takes `name` and/or `is_trade_list`).
- `fab_api/routers/trade.py`: `GET /trade/listings` (public marketplace browse),
  `POST /trade/offers` (give/want bundles, per-unit `value_sek` snapshotted at send
  time), `GET /trade/offers` (mine, both directions), `PATCH /trade/offers/{id}`
  (accept/decline = recipient only, cancel = sender only, pending-only else 409).
  Accepting records the deal — the swap happens in person; no inventory moves.
- Tables: `app.trade_offers`, `app.trade_offer_items` (setup_db.py canonical;
  trade router self-migrates, incl. the `is_trade_list` ALTER).
- Frontend `/trade` (Trading Post): listings table + search, offer builder (request
  from their list / give from your cardlists, running totals + balance), offers inbox.

## Frontend design system (Phase 3, locked)
`retro-data-display/src/index.css` is the **locked** system (Neurotech/Netrunner: deep
blue-black, cyan primary, magenta accent, Orbitron + Share Tech Mono). Reusable primitives —
prefer these over ad-hoc classes: `.panel`, `.panel-raised`, `.panel-hover`,
`.section-title` (cyan header + accent underline), `.hud-frame` (corner brackets), `.chip`,
`.divider-glow`, plus refined `.text-glow`/`.glow-card`/`.glitch`. Notes: default borders are
intentionally soft (bright cyan is an *accent*, not the whole UI); the page background aura
lives on `body` — do NOT put a solid `bg-background` on a page wrapper (it hides the aura and
makes tabs look inconsistent). The native app mirrors this via a `Theme` palette +
`cyberButton`/`styleInput` helpers and a HUD `CardGuideView`.

## Scanner work
- **Web scan page has NO browser camera anymore** (removed 2026-07-03). It is two paths:
  (1) pair the native app (download `/scanner-apk`, pair code via `/scan/session`, live sync
  via `/scan/records`), and (2) manual code entry via **`GET /scan/code?code=HVY050`**
  (`_parse_code`+`_snap_code`, typo-corrected). The native app is the primary scanner.
- The old browser `POST /scan` (+ `/scan/debug`, `_ocr_claude`, `_ocr_google`) was
  **removed 2026-07-05** in the router split. `/scan/native` keeps `_ocr_easyocr` (title)
  and `_visual_match` (visual fallback) in `fab_api/scan_engine.py` — verified by
  replaying a saved footer crop (identical result before/after the move).
- Browser debug crops showed the footer strip was correctly framed but physically too soft
  for OCR, so footer OCR alone is not viable in browser video (why the native app won).
- Backend has local `/scan/native` for Android/native submissions:
  `full_image`, `footer_crop`, `title_crop`, `debug_save`, `session_code`.
- Lightweight session flow: web creates a short code at `/scan/session`, phone submits
  scans with that code, and web polls `/scan/records`. This is not a real account system.
- Footer code is the primary decision path. Android sends a broad lower footer band, and
  backend searches multiple subwindows for codes near the lower-left/center footer. OCR
  set-code corrections handle reads like `R05 130` → `ROS130`, while partial collector
  reads are refused so `BET7` cannot snap to a random `BET###` card.
- Fusion order: footer OCR exact `display_id`; strong title match; visual+title agreement.
  Visual-only guesses are intentionally not returned.
- Debug crops and metadata save to `tmp/scan_debug_samples/`.
- `fab-scanner-android/` is a CameraX MVP with card guide, footer sharpness gate, refocus,
  torch, zoom, hidden Advanced API URL field, pair-code field, and POST to `/scan/native`.
- Android Studio/SDK are installed enough for local Gradle/ADB checks. Verified build:
  `JAVA_HOME=/snap/android-studio/232/jbr GRADLE_USER_HOME=/home/tango/Projects/fab/fab-scanner-android/.gradle ./gradlew :app:assembleDebug`.
  The debug APK has been installed and launched on the phone at least once.

## Gotchas
- Python 3.14 venv at `.venv`. Scanner deps currently installed: `numpy`,
  `opencv-python-headless`, `pytesseract`. `easyocr` is intentionally not installed because
  it pulls a very large PyTorch/CUDA stack.
- Native app debug package id is `com.fabscanner.app.debug`; activity remains
  `com.fabscanner.app.MainActivity`.
- Wireless debugging can get stuck in stale pairing state. If repeated `adb pair/connect`
  attempts fail while the phone claims it is paired, a full computer restart has been the
  reliable reset.
- Frontend scaffolded with bun; `npm install` needs `--legacy-peer-deps`.
- `start_fab.py` rewrites/pushes `retro-data-display/.env` when the tunnel URL changed
  (with `--sync-lovable`/`PUSH_LOVABLE=1`, or automatically on any restart where the URL
  changed — e.g. a dead tunnel was replaced). A same-URL restart pushes nothing (no rebuild
  churn). Normal `./run_pipeline.sh` is the daily sync path.
- If port 8001 is already in use, check for stale `start_fab.py`/uvicorn processes before
  rerunning (Ctrl+C on `start_fab` now stops only the API; the tunnel stays up — kill it
  with `--stop-tunnel`). The API reads gold live, but code changes require a restart.
- Docker requires sudo in this environment, but `run_pipeline.sh` first checks whether
  Postgres is already reachable and skips Docker startup when it is.
