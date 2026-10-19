# CLAUDE.md — FAB project working notes

Operational focus for agents working in this repo. Pairs with `README.md` (public
overview + setup) and `docs/DEVLOG.md` (running log of decisions + what was tried;
add an entry, newest on top, for non-trivial changes). `AGENTS.md` just points here.

## What this is
A Flesh and Blood TCG card-data app: ingest → PostgreSQL (bronze/silver/gold) →
FastAPI → Vite/React frontend. The API serves the built frontend same-origin from
`retro-data-display/dist` on https://fabmatrix.t4ngo.com (Lovable can still build the
frontend repo, but the domain is what users hit — rebuild `dist` to ship frontend changes).
A native Android scanner (`fab-scanner-android/`) talks to the same FastAPI backend.

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
(`cards`, `admin`, `scan`, `auth`, `cardlists`, `tools`, `trade`, `messages`, `buylist`); shared env + the **pooled**
`get_conn()` context manager (ThreadedConnectionPool, commits on exit, returns conn to
the pool) in `fab_api/core.py`; the OCR scan engine — moved **verbatim**, logic
untouched — in `fab_api/scan_engine.py`. GZip middleware compresses `/cards` (~11×);
`/sets` + `/stats` send Cache-Control. `app.*` is the only non-regenerable data:
`backup_app.py` dumps it to `backups/` (30 kept, gitignored) via the systemd user timer
`fab-backup.timer` (03:30, `Persistent=true` so a run missed while the laptop was off
fires at next boot — the old cron job never ran once; units in `deploy/`). Restore via `sudo docker compose exec -T db psql -U <user> -d fab < backups/app_<ts>.sql`.

## Data sources
| Source | Gives | Notes |
|---|---|---|
| **the-fab-cube** (GitHub JSON) | card catalogue spine: names, stats, text, images, set list | **Lags ~2 boosters** — unreliable for brand-new sets. |
| **Cardmarket** (S3 bulk) | EUR prices, product catalogue | **No image field.** EUR only. |
| **tcgcsv.com** | USD prices, names, set #, rarity, card text, images, for ALL sets incl. new | **Primary price/missing-set source.** Free, no key, no rate limit. Category **62** = FaB. Send a `User-Agent` header (401 otherwise). |
| **Riksbank** | EUR/SEK, USD/SEK rates | drives `price_sek`. |
| **collectors_centre** (scraped fabtcg.com) | foil technique per printing | validates tier-2 foil matches. |

`price_sek` = CM EUR → SEK if available, else tcgcsv USD → SEK.

Retired (2026-10-19): **JustTCG** (USD API, 100 req/day — superseded by tcgcsv) and the
perceptual-hash scanner ingest; ORB visual matching followed the same day (below).
Their scripts were deleted and `setup_db.py` drops the old tables; the history is in `docs/DEVLOG.md`. Hand-maintained
inputs live in `data/` (`fab_cm_manual.csv`, `collectors_centre_scrape.md`).

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
.venv/bin/python ingest_bronze.py     # catalogue + CM (network)
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

**PERMANENT URL (2026-07-06): `https://fabmatrix.t4ngo.com`** — a **named** Cloudflare
tunnel (`fab`) on the user's own domain t4ngo.com (bought at Cloudflare Registrar).
`~/.cloudflared/config.yml` routes it to :8001; `start_fab.py` auto-detects the config
and runs `cloudflared tunnel run fab`. The URL never changes → no Lovable resyncs, no
gist churn, no zombie-URL replacement (that machinery remains as fallback if the config
is ever missing). Frontend `.env`, the endpoint gist and the Android app's
`DEFAULT_API_BASE` all point at the domain. ~3-7× lower latency than trycloudflare.

Fallback only (no `~/.cloudflared/config.yml`): `start_fab.py` runs a detached
trycloudflare quick tunnel (pidfile `tmp/logs/cloudflared.pid`) that survives API
restarts. Reuse is gated on an HTTP reachability check (`tunnel_reachable`), not a live
PID — quick tunnels often keep the process alive after the edge stream dies. A dead one is
replaced, and because that changes the URL, the frontend `.env` is synced and the URL is
published to the gist (`publish_endpoint`) that the Android app reads at launch.

## Uptime: the API runs as a systemd USER service (2026-08-09)
The host is a **laptop** (Ubuntu 26.04), so the two things that took the site down were
lid-close (logind default = suspend, instant) and idle-suspend (GNOME: 2 h on AC, 1 h on
battery) — plus closing the terminal, since it used to run in the foreground.
- Unit: `~/.config/systemd/user/fab-api.service`, copy kept at `deploy/fab-api.service`.
  `ExecStart` wraps `start_fab.py` in
  `systemd-inhibit --mode=block --what=sleep:idle:handle-lid-switch`, so **the laptop
  cannot idle-sleep or lid-sleep while the API is running**, and sleeps normally again the
  moment the service is stopped. `Restart=always`, `StartLimitIntervalSec=0` (never give up).
- `loginctl enable-linger tango` is ON → it starts at boot without anyone logging in.
- Manage it with `systemctl --user {status,restart,stop,start} fab-api`,
  logs with `journalctl --user -u fab-api -f`. `run_pipeline.sh` detects the enabled
  service and serves via `systemctl --user restart fab-api` (2026-10-19). Before that it
  launched its own `start_fab.py`, and the two copies killed each other as "stale" over
  port 8001. Never run `start_fab.py` by hand while the service is enabled.
- Verified: inhibitor held (`sleep:idle:handle-lid-switch`, block), `kill -9` on uvicorn →
  service auto-restarted and the site was back inside 20 s.
- A manual suspend from the GNOME menu still works — the inhibitor only blocks *automatic*
  sleep. Nothing here stops the tunnel dropping while the machine is genuinely asleep.

The `fab` repo is on GitHub at **GHT4ngo/fab** (public). Pushing over HTTPS uses the `gh`
token as git's credential helper — if a push prompts for a password, run `gh auth setup-git`
once (GitHub rejects passwords). The `retro-data-display` frontend is a **separate** repo
(its own remote); the parent repo gitignores it.

Latest verified shape (2026-10-19 ingest): `gold.gold_cards` has 18,605 rows, **99.1%
price coverage**, all 13 dbt tests pass. The 169 unpriced rows
are data-absent on both markets (tier-4 with no TCGplayer prices at all, or no CM
product under any matching name) — do NOT chase them with looser matching. Key coverage
rules: a CM match with neither trend nor low is EXCLUDED from tiers 2/3/5 so the cascade
continues (a priceless match must never block a priced lower tier); tier-2/5 price from
trend, else the matched product's own low.

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
- **EMAIL IS LIVE via Resend, ANY recipient (2026-07-07)**: t4ngo.com is verified in
  Resend; sender is `RESEND_FROM="FAB Matrix <noreply@t4ngo.com>"` (`.env` — value MUST
  stay quoted: `run_pipeline.sh` sources `.env` as shell and an unquoted `<` aborts the
  restart). `RESEND_API_KEY` set → magic link emailed (`emailed: true`, token NOT in the
  response); without the key it falls back to dev mode (`dev_link` returned). Links
  target the frontend (`/account?token=…`, consumed by AuthProvider). Verified: delivery
  to a non-owner address works → real multi-user accounts/trading are unlocked.
- Self-hosted frontend has an **SPA fallback** (`SpaStaticFiles` in `api.py`): unknown
  paths serve `index.html`, so deep links like the emailed magic link work on the tunnel.
- Frontend: `src/lib/auth.ts` (client + localStorage token), `src/hooks/useAuth.ts`
  (context + `useAuth`), `src/components/AuthProvider.tsx` (the provider),
  `src/pages/Account.tsx`, `AddToListButton`, `SaveScanToListButton`.

## Trading (Phase 4, live 2026-07-06; negotiation/locking rework 2026-07-08)
- **Valuation rule (user-confirmed): `gold.trade_value_sek` = greatest(CM trend, CM low)
  EUR→SEK, tcgcsv USD→SEK fallback** ("trend price, or low if it's higher").
  `gold.cm_low_eur` also exposed. Computed in silver's final select.
- A cardlist with `app.cardlists.is_trade_list = true` is a public trade list
  (toggle on Account; `PATCH /cardlists/{id}` takes `name` and/or `is_trade_list`).
- **Card locking (2026-07-08)**: creating an offer re-checks every requested card against
  the counterparty's live trade lists and reserves the copies in `app.trade_locks`
  (available = listed − locks from pending/accepted offers; race-guarded with
  `pg_advisory_xact_lock(874201, owner_id)`). Shortfall → 409 with a per-card
  `{unavailable:[…]}` breakdown (UI shows it verbatim). The giver's own listed cards are
  soft-locked (give-cards do NOT have to be on a trade list). Locks release on
  decline/cancel/complete and persist through accept (trade live = cards spoken for).
- **Negotiation**: `awaiting_user_id` = whose turn. `PATCH /trade/offers/{id}` actions:
  accept/decline/counter (on-turn user only, pending only), cancel (the not-on-turn party
  while pending; either party after accept), complete (accepted only). `counter` takes
  `add_items[{side,printing_unique_id,qty}]`, strict-locks the other party's cards and
  flips the turn → the other side must accept again. Emails: created → recipient,
  countered → new on-turn party, accepted/declined → the waiting party (accepted mail
  suggests meeting at an LGS); cancel/complete silent.
- **Money-only + multi-list**: one offer side may be empty (= cash buy/sale).
  `POST /trade/list-offers` takes `offer_cardlist_ids[]`/`request_cardlist_ids[]`
  (several per side; request lists must all share one owner; empty receive = sale and
  needs `to_username`); snapshots into `app.trade_offer_lists` (legacy single-list
  columns still filled, names joined "A + B").
- Public endpoints: `GET /trade/listings` (+`locked_qty`/`available_qty` per row),
  `GET /trade/traders`, `GET /trade/availability/{printing}` (per-seller
  qty/locked/available/value), `GET /trade/lists`.
- **Browse integration**: `/cards` returns `trade_qty`/`trade_sellers` + filters
  `on_trade=true` and `trade_owner=<username>`. Frontend: ⇄ badges on tiles, "For trade"
  toggle + trader dropdown in FilterBar, per-seller availability panel in the card
  detail modal with add-to-cart + message buttons.
- **Per-seller trade carts**: `retro-data-display/src/lib/tradeCart.ts` (localStorage;
  one cart per seller, so a cart can only hold that user's cards). /trade shows the
  carts; "Request trade" → request-only offer through the lock machinery.
- Tables: `app.trade_offers` (+`awaiting_user_id`, `accepted_at`, `completed_at`),
  `app.trade_offer_items`, `app.trade_offer_lists`, `app.trade_locks` (setup_db.py
  canonical; trade router self-migrates, incl. the `is_trade_list` ALTER).
- Frontend `/trade`: carts panel, multi-select list-trade builder, listings with
  available/locked (reserved rows disabled), offer builder, trades inbox with
  your-turn/waiting chips + filters (All/Your turn/Waiting/Live/Closed), CounterPanel,
  accepted → meet-at-LGS banner + Mark completed / Call off, Message buttons.

## Buylist — "Sell to t4ngo" (2026-07-30)
Users sell cards **to the store**; the store is one designated account —
`lower(username)='t4ngo'`, override with `BUYLIST_STORE_USERNAME`, else the oldest
`is_dev` user. Router `fab_api/routers/buylist.py`, tables in `setup_db.py` (canonical)
+ router self-migration.
- **Rate card** (`app.buylist_settings`, one row per store): `enabled`, `base_pct`
  (% of market), `min_card_sek`, `condition_pct` JSONB, `language_pct` JSONB, `notes`.
  Defaults merge in at read time (NM 100 / LP 90 / MP 75 / HP 55 / DMG 30; EN 100,
  others 70-90) so untouched keys still price sensibly.
- **Rules** (`app.buylist_rules`, UNIQUE(user, field, value)): `field` ∈
  {rarity, foiling, edition, set_id, condition, language}, `action` ∈ {block, pct}.
  `block` = never buy (the user's "no commons, no gold foil, nothing from GEM/LGS");
  `pct` multiplies the base rate for matching cards. Blocks always beat rates.
  `max_value_sek` (2026-08-04) scopes a rule to cheap copies only — it applies only when
  market ≤ that amount (inclusive), null = always. **Why it exists: rarity does not
  predict value once foiling is involved.** A blanket "don't buy Commons" rejected the
  Monarch Cold Foils (Halo of Illumination, rarity Common, 208 kr) — 40% of a real
  collection's value. The live t4ngo policy is now `Common ≤5 kr` / `Rare ≤5 kr`.
  `gold.rarity` mixes short codes and long words for the same rarity → `_norm_rarity`
  collapses them (`C`/`Common` → `common`) so ONE rule catches both spellings.
- **Price** = `greatest(trade_value_sek, price_sek)` × base% × condition% × language%
  × each matching pct rule, rounded to whole kr. Blocked / under-minimum / priceless
  cards quote 0 with a printed reason. Same valuation basis as the trading post.
- **Flow**: `POST /buylist/quote` (public, non-binding, `binding:false` + `notice`) →
  `POST /buylist/offers` (auth; server RE-QUOTES, never trusts the client, and
  snapshots the whole policy into `policy_snapshot`) → the store `counter`s
  (per-line `offered_sek`/`qty`/`accepted`/`note` + message) or accepts.
  `app.sell_offers.awaiting_user_id` is the turn, exactly like `trade_offers`:
  accept/decline/counter = on-turn only; cancel = the NOT-on-turn party while open or
  either party once accepted; complete = accepted only. Counter flips the turn so the
  seller must accept again. Emails on submitted/countered/accepted/declined.
- Blocked cards are **kept on the offer**, flagged `accepted=false` at 0 kr — the seller
  sees what happened instead of cards silently vanishing.
- One line = one printing × condition × language (`app.sell_offer_items`), so the same
  card in NM and MP are separate priced lines. Duplicate identical lines merge on submit.
- **Editing a line** (2026-08-04): `components/PrintingEditor.tsx` swaps a line onto any
  sibling printing — cascading set → number → edition → finish → alt-art pickers that only
  offer existing combinations, then artwork+price tiles when the catalogue still can't
  separate two rows (see the duplicate-printing hazard above). Used in the seller's draft
  AND in the store's counter panel. `PATCH /buylist/offers/{id}` `counter` takes
  `printing_unique_id` / `condition` / `language` / `remove` per item plus `add_items`;
  changing the CARD re-quotes that line against the offer's `policy_snapshot`, not today's
  rates. A counter that would empty the list is refused (decline instead).
- **Printing** (2026-08-04): `/sell/print/:id` (`pages/PrintOffer.tsx`) renders a plain
  black-on-white A4 document — print or Save as PDF, no server-side PDF dependency. Drafts
  print via `/sell/print/draft` (payload handed over in `sessionStorage`). App chrome is
  hidden with `.no-print` on the nav + bug reporter; print CSS is scoped inside the page so
  the locked `index.css` design system is untouched.
- Frontend: `/sell` (`pages/Sell.tsx`) — store terms + full rate card, draft builder
  (localStorage `lib/sellDraft.ts`, import from a cardlist, per-line condition/language),
  live quote with a loud "estimate, not a deal" banner, offer inbox with the store's
  re-price panel. `components/PrintingPicker.tsx` + `lib/cardLabels.ts` guarantee one
  exact printing is picked (set, collector no., edition, foiling, rarity, variant, pitch,
  price, thumbnail — plus a `#id` suffix when two rows share every visible attribute).
  `components/BuylistSettings.tsx` is the owner panel on /account (renders only for the
  store account); `SellToStoreButton` shows the store's price inside the card detail modal.

## Paste-a-list importer (2026-08-04)
`POST /cards/parse-list` (public, read-only) turns a pasted text collection into
resolved printings. Logic in `fab_api/list_parser.py`; UI in
`components/PasteListImport.tsx`, used from the /sell draft builder.
- Reads: section headings (`*Majestic Foil*`), `3x Name` / `Name x3`, treatments in
  parens (`(Regular)`/`(Rainbow Foil)`/`(Cold Foil)`/`(Extended Art Rainbow Foil)`),
  pitch colours `(Red/Yellow/Blue)`, collector codes (`(MON124)`, `- MON006`), bullets,
  trailing commas. Repeated identical lines fold into a quantity.
- Auto-detects set + edition from a heading ("Flesh and Blood Monarch First Edition"
  → MON / F). **Set detection resolves against `gold.gold_cards`** — `bronze.fab_sets`
  still carries the composite rows (`MON105 // MON`) dbt keeps out of gold, and they
  otherwise win the name match.
- **Never guesses on money.** `gold` holds printings that share every displayed
  attribute and differ wildly in price (MON034 1st-Ed Rainbow Foil at 7 kr AND 873 kr;
  251 of 1,377 duplicate signatures have a >25% + >20 kr spread — the pairs differ only
  by `image_url`, e.g. `MON124.width-450.png` vs `MON124-RF.png`). Statuses:
  `matched` (one fit) · `auto_picked` (several, but within 1.25× or 20 kr — takes the
  cheapest, says so) · `ambiguous` (several, price genuinely differs → the UI makes a
  human pick, showing artwork + price) · `unmatched` (difflib near-misses offered).
- A name absent from the chosen set is retried across ALL sets before being called
  missing (pastes mix sets). Headings/greetings that match nothing are moved to
  `skipped_lines` and shown, not silently dropped.
- One catalogue query per request (scoped by set = ~15 ms; unscoped ~170 ms), matching
  in Python — a 200-line paste must not become 200 round trips.
- **No LLM involved and none needed**: on the real 46-card Monarch list this was tested
  against, 32/33 lines resolved deterministically; the one flagged was the genuinely
  ambiguous Extended Art foil. An LLM would guess there, which is exactly the failure
  that costs money.

## Direct messages (2026-07-08)
- `app.messages` + `fab_api/routers/messages.py`: `POST /messages` (to_user_id or
  to_username), `GET /messages/threads`, `GET /messages/with/{id}` (marks incoming as
  read), `GET /messages/unread-count`. Email nudge only for the FIRST unread message
  from a sender (burst guard, checked before insert).
- Frontend `/messages`: thread list + chat (10-20s polling), deep link
  `/messages?to=<user_id>&name=<username>` (used from trade offers + the availability
  panel), nav Messages item with unread badge (30s poll, signed-in only).

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

## Frontend build + code health (2026-10-19)
- **Shipping = `npm run build`.** The API serves `retro-data-display/dist` live, so a build
  IS a deploy (no restart needed). Verify before building, not after.
- **Lockfile: `package-lock.json` only** (bun lockfiles deleted). Install with
  `npm install --legacy-peer-deps`. Package is named `fab-matrix-web`.
- **Unused Lovable/shadcn scaffold was removed** (43 files, 33 packages, found with
  `npx knip`). Only the `components/ui/*` primitives actually imported remain; add new ones
  with the shadcn CLI when needed rather than restoring the old set. Re-run knip after
  big removals to catch newly-dead code.
- **Code splitting** (`App.tsx`): every page except Browse (`Index`, the landing page) is
  `lazyPage(() => import(...))` inside one `<Suspense fallback={<PageLoading/>}>`;
  `vite.config.ts` `manualChunks.vendor` = react, react-dom, react-router-dom,
  @tanstack/react-query (long-cached across deploys). Largest chunk 231 kB, first load
  ~143 kB gzipped (was one 613 kB bundle). New pages: add them via `lazyPage`, not a static
  import. `lazyPage` reloads ONCE (sessionStorage flag `fab:chunk-reload`) when a chunk
  import fails — an open tab from before a deploy asks for old hashed names that `dist` no
  longer has. Verified by 404-ing the Trade chunk in headless Chromium.
- **React Router 7** (`react-router-dom` 7.18.4; v6 has no fix for the open-redirect
  advisory GHSA-wrjc-x8rr-h8h6). App uses only `BrowserRouter`, flat `Routes`,
  `Link`/`NavLink` and the basic hooks — no data routers/loaders.
- **ESLint is clean (0 errors, 0 warnings) — keep it that way.** `react-refresh/
  only-export-components`: a `.tsx` file exports components only; constants/hooks/context
  go in `.ts` files. Hence `hooks/useAuth.ts` + `components/AuthProvider.tsx`, and
  `lib/cardDisplay.ts`.
- **Two label modules, on purpose:** `lib/cardDisplay.ts` = compact grid/table/filter
  labels + rarity colours ("1st Ed", "Standard"); `lib/cardLabels.ts` = exact wording that
  identifies ONE printing in pickers, offers and print sheets ("1st Edition", "Non-foil").
  Don't merge them — it would change UI text.
- `npm audit`: 10 left, all dev/build-time only (Vite 5 dev-server esbuild, Tailwind 3's
  braces/postcss-selector-parser). Clearing them needs Vite 6+ / Tailwind 4 (the latter
  rewrites the config + `index.css` layer syntax) — deliberately deferred.
- Quick live check without a test suite: playwright-core + the cached Chromium in
  `~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome` — deep-load every route, click
  the nav (`aria-current="page"` marks the active link), round-trip `/?q=`, back button.

## Scanner work
- **Web scan page has NO browser camera anymore** (removed 2026-07-03). It is two paths:
  (1) pair the native app (download `/scanner-apk`, pair code via `/scan/session`, live sync
  via `/scan/records`), and (2) manual code entry via **`GET /scan/code?code=HVY050`**
  (`_parse_code`+`_snap_code`, typo-corrected). The native app is the primary scanner.
- The old browser `POST /scan` (+ `/scan/debug`, `_ocr_claude`, `_ocr_google`) was
  **removed 2026-07-05** in the router split. `/scan/native` keeps `_ocr_easyocr` (title)
  in `fab_api/scan_engine.py` — verified by replaying a saved footer crop (identical
  result before/after the move).
- **ORB visual matching REMOVED 2026-10-19.** `_visual_match` read
  `bronze.card_orb_descriptors`, a table that never existed in this Postgres (the ingest
  last ran in April against the old DB) — the `try/except` in `scan.py` swallowed the
  error, so the fallback silently returned nothing for months. Removed with
  `ingest_orb_descriptors.py`, `_rectify_card` and OpenCV. `full_image` is still accepted
  (the app sends it) but only saved as a debug sample.
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
- Decision order: footer OCR exact `display_id`; then a strong (≥0.85) title match.
  Title OCR needs `easyocr`, which is NOT installed (see Gotchas), so in practice the
  footer code is the only live signal — and it is what makes the scanner work well.
- Debug crops and metadata save to `tmp/scan_debug_samples/`.
- `fab-scanner-android/` is a CameraX MVP with card guide, footer sharpness gate, refocus,
  torch, zoom, hidden Advanced API URL field, pair-code field, and POST to `/scan/native`.
- Android Studio/SDK are installed enough for local Gradle/ADB checks. Verified build:
  `JAVA_HOME=/snap/android-studio/current/jbr GRADLE_USER_HOME=/home/tango/Projects/fab/fab-scanner-android/.gradle ./gradlew :app:assembleDebug`.
  (`current` is the snap's stable symlink; numbered paths like `232/` vanish on update.)
  **`GET /scanner-apk` serves the APK straight from `app/build/outputs/apk/debug/`** — never
  delete that folder without rebuilding, or the live download breaks.

## Gotchas
- Python 3.14 venv at `.venv`. Scanner deps currently installed: `numpy`,
  `pytesseract` (OpenCV is no longer needed). `easyocr` is intentionally not installed because
  it pulls a very large PyTorch/CUDA stack.
- Native app debug package id is `com.fabscanner.app.debug`; activity remains
  `com.fabscanner.app.MainActivity`.
- Wireless debugging can get stuck in stale pairing state. If repeated `adb pair/connect`
  attempts fail while the phone claims it is paired, a full computer restart has been the
  reliable reset.
- Frontend uses npm (`package-lock.json`); `npm install` needs `--legacy-peer-deps`.
- `start_fab.py` rewrites/pushes `retro-data-display/.env` when the tunnel URL changed
  (with `--sync-lovable`/`PUSH_LOVABLE=1`, or automatically on any restart where the URL
  changed — e.g. a dead tunnel was replaced). A same-URL restart pushes nothing (no rebuild
  churn). Normal `./run_pipeline.sh` is the daily sync path.
- If port 8001 is already in use, check for stale `start_fab.py`/uvicorn processes before
  rerunning (Ctrl+C on `start_fab` now stops only the API; the tunnel stays up — kill it
  with `--stop-tunnel`). The API reads gold live, but code changes require a restart.
- Docker requires sudo in this environment, but `run_pipeline.sh` first checks whether
  Postgres is already reachable and skips Docker startup when it is.
