# Agent worklog

Running log of what's been done, what's planned, and what was tried but abandoned (and
why). Newest entries on top. Keep this honest — failed attempts are the most valuable
part. See `CLAUDE.md` for architecture and `README.md` for setup.

---

## 2026-06-28 — Admin quality dashboard + tcgcsv composite-product cleanup (DONE)

### Context / why
User wanted a stable foundation and was confused by admin set coverage. The admin page was
showing price coverage, not true set completeness, and some tcgcsv-derived rows made fake
edition-`N` set entries for existing sets.

### Fixes
- **Frontend admin dashboard** (`retro-data-display`, pushed to GitHub):
  - fixed stale frontend contract (`tier1_exact` → `tier1_anchored`);
  - added T4/T5 visibility, `/admin/quality`, and `/admin/price-discrepancies`;
  - pushed commit `9798649 feat: improve admin data quality dashboard`;
  - `start_fab.py` later pushed `.env` tunnel commit `6fccdd9`.
- **API admin endpoints** (`api.py`):
  - `/admin/sets` now includes unique names, missing price, CM/no-CM counts, T1-T5,
    no-match, low-confidence, missing-image, and discrepancy counts.
  - added `/admin/quality` for overall operational counters.
- **tcgcsv set-code parser** (`ingest_tcgcsv.py`):
  - handles split/suffix collector numbers more defensibly (`//`, `/`, `-MV`, `-CF`);
  - normalizes set codes to stable base collector prefixes (`NUU028/NUU029` → `NUU`,
    `IAR145-MV` → `IAR`, etc.).
- **tcgcsv composite products excluded from tier 4** (`silver_cards.sql`):
  - products whose collector `number` contains `/` are TCGplayer composite/pair products,
    not standalone printings (`ARC001 // ARC003`, `NUU028/NUU029`);
  - removing them fixed spurious admin rows for `ARC N`, `WTR N/F`, `ELE N`.
- **Data contracts** (`fab_dbt/models/gold/schema.yml`, `fab_dbt/tests/*.sql`):
  - added identity, enum, non-negative price, malformed set-id, delimited set-id, and
    delimited display-id tests.

### Verified final state
- `dbt run` passes.
- `dbt test` passes: **13/13**.
- `gold.gold_cards`: **17,256 rows**, **16,096 priced**, **93.3%** price coverage.
- `display_id` containing `/`: **0**.
- Admin rows now show:
  - `ARC`: Arcane Rising F/U only.
  - `WTR`: Welcome to Rathe A/U only.
  - `ELE`: Tales of Aria F/U only.
  - `OMN`: 478 rows / 450 priced.
  - `PEN`: 676 rows / 676 priced.

### Operational note
`run_pipeline.sh` rebuilt the DB but initially failed to serve because an old `start_fab.py`
left uvicorn on port 8001. Stopped the stale process and restarted `start_fab.py`.
The live quick-tunnel URL changes on each restart; current URL is stored in
`tmp/logs/tunnel_url.txt` and mirrored to `retro-data-display/.env`.

---

## 2026-06-28 — Fix spurious/duplicate sets from split collector numbers (DONE)

### Problem (user, admin set-coverage tab)
Lots of duplicate sets; "unique card numbers sometimes generate a set." Root cause:
double-faced / paired tcgcsv numbers like `HNT002//HNT055`, `SEA042//SEA244`,
`NUU028/NUU029` confused `set_code_for`, which greedily produced fake codes
(`HNT00`, `SEA04`, `WOD02`, …). One real set ("The Hunted") fractured into HNT / HNT00 /
HNT24 / HNT05 / HNT01 / HNT10 / HNT16. Group abbreviation is NULL for these decks, so the
existing `/`-fallback couldn't help.

### Fix (ingest_tcgcsv.py `set_code_for`)
Take the FIRST `/`-segment and grab its leading letters (keeping a `1H`-style digit
prefix): `SET_CODE_RE = ^([0-9]*[A-Z]+)`. So `HNT002//HNT055`→`HNT`, `SEA//082`→`SEA`,
`1HP141`→`1HP`, `WTR042-C`→`WTR`. Re-ingested tcgcsv + rebuilt gold.

### Result (verified live)
- distinct gold set_id **131 → 122**; **0** digit-ending/malformed codes.
- "The Hunted" merged back to one `HNT` (547 cards). admin/sets reads gold live → already
  visible without an API restart.

### Remaining minor edge cases (legit-ish, not chased)
- `IAR` (9): Omens Marvel-variant cards numbered `IAR###-MV` — really belong to `OMN` but
  tcgcsv numbers them IAR. Fixing needs a group→canonical-code alias (group abbr is null;
  can't group-merge the Promo set which legitimately holds many prefixes).
- `XXX` / `LGD` / `ZENO`: genuine odd tcgcsv promo/deck numbers (e.g. `XXX001//XXX002`,
  `ZENO29`). Small real sets, left as-is.

---

## 2026-06-28 — Normalise tcgcsv card names so the same card shares one name (DONE)

### Problem (user)
tcgcsv bakes extra tokens into product names, so a card split into multiple "names" in the
UI: e.g. "Adaptive Plating" (clean, from the-fab-cube) vs "Adaptive Plating - FAB169"
(tcgcsv promo). Same card must share ONE name; set/number/etc. show when expanded.

### Fix (silver_cards.sql, `tcgcsv_src` / `tcgcsv_missing`)
- Strip a trailing collector number in BOTH forms: `" - FAB169"` and `" (ANQ011)"`
  (`\s*[-(]\s*[A-Z]{2,}[0-9]+\)?\s*$`) → `name_nonum`. Fixed 1,066+ cards; 0 left.
- Extract the pitch colour from ANYWHERE in the name (not just the end — promo names put it
  before a treatment, e.g. "Shred (Blue) (Marvel)"), set `pitch`, strip it from the name.
  0 names now contain (Red/Yellow/Blue).
- set_id/display_id already carry the set + number for the expand view.

### Alt-art treatments → new `variant` column (DONE — user chose "strip + new field")
- New `gold.variant` column (also in `/cards`): the alt-art treatment(s) pulled out of the
  tcgcsv name so the card keeps its base name. Cards can stack treatments
  ("(Left) (Golden)", "(Marvel) (Japanese Alternate Art)") → ALL trailing parentheticals are
  stripped and folded into one comma-joined `variant` (e.g. "Left, Golden").
- `tcgcsv_src.name_np` = name minus collector-number minus pitch; `tcgcsv_missing` then
  splits off the trailing `(...)`-block into `variant` and keeps the base `name`.
- the-fab-cube names are clean (0 trailing parens), so its half sets `variant = null`.
- Result: 0 gold names carry a trailing parenthetical. variant values: Extended Art 92,
  Marvel 91, Golden 38, CC Tag 19, plus grid/position labels (Top left/Center/…) and
  combos. NOTE: bronze.tcgcsv_cards has its own (always-null) `pitch` column — the derived
  one must be aliased (`derived_pitch`) to avoid an ambiguous-column error with `c.*`.
- LIVE STATUS: cleaned names are already served (API reads gold live). The `variant` FIELD
  needs an API restart (running uvicorn predates the api.py change) — deferred to avoid a
  new tunnel URL + Lovable push until wanted.

---

## 2026-06-28 — Match Cardmarket on bare name (drop pitch colour from the key) (DONE)

### Context / why (user)
The pitch colour is NOT a matching key — it's display-only (we already output `pitch`).
Cardmarket is inconsistent about the "(Red/Yellow/Blue)" suffix (cheap normals often omit
it; only the foil carries it), which forced matches onto the wrong/pricey product. And the
**card id/number is the real key**: e.g. the €89 "Spike with Bloodrot (Red)" is a different
card (extended-art rainbow promo) from the regular Spike — they must not be merged.

### What changed (silver_cards.sql)
- `printings.cm_name` (name + colour) → replaced by `name_key` = bare upper(name).
- New `cm_products` CTE strips the trailing "(Red|Yellow|Blue)" from Cardmarket names →
  `base_name`. All matching (anchored pool, tier-2 candidates, tier-3 fallback) now joins
  on the bare name. The tcgcsv USD anchor + the card number do the disambiguation.

### Result (verified) — big win
- Spike fixed: ARA018/OUT021 regular now €0.23 (anchor $0.22); each numbered printing
  (AAC020/ARA018/FAB324/LGS130/OUT021/SAR020) maps to its own card.
- Tier mix: 1 anchored **11,182** · 2 auto 1,331 · 3 fallback 1,366 · 4 tcgcsv 3,305 ·
  5 manual **12** · null **270**.
- vs previous: no-match **2,622 → 270**, manual **688 → 12**, bad divergences **147 → 79**.
- Remaining 79 (all tier 1) = genuine EU/US market gaps (Cardmarket has no product near
  the tcgcsv price for that card); inherent to the data, surfaced by /admin/price-discrepancies.

### Note on same-named cards
Worry was that bare-name matching conflates multi-pitch names (Sink Below Red/Yellow/Blue
are distinct cards). In practice the tcgcsv anchor (per-printing USD via productId) picks
the right-priced product, and identity/number come from the spine, not Cardmarket — so the
displayed card stays correct even if a same-priced wrong-colour product's idproduct is used.
The €89 promo Spike has NO matching high-priced tcgcsv card, so it's left orphaned (correct).

---

## 2026-06-28 — tcgcsv as anchor key; rebuild Cardmarket matching; manual last (DONE)

### Context / why
User: make tcgcsv the primary key for cards, match Cardmarket *through* it, and put the
manual crosswalk LAST (it's the least reliable). Fix the flagged manual mismatches.

### What changed (silver_cards.sql)
Rewrote the Cardmarket EUR matching, now anchored on the tcgcsv USD price. New tiers,
best-linkage-first: **1 anchored → 2 auto → 3 fallback → 4 tcgcsv-missing → 5 manual**.
- **Anchored (tier 1):** for each printing with a tcgcsv USD price, take ALL Cardmarket
  products sharing the card's pitch-name (across every expansion — no expansion constraint)
  and pick the one whose EUR (converted to SEK) is closest to the tcgcsv anchor. The anchor
  price, not the fragile expansion mapping, disambiguates the correct foil/edition/printing.
- **Auto (2):** old foil-pair price-rank + collectors_centre, used only when no anchor.
- **Fallback (3):** name aggregation (unchanged).
- **Manual (5):** the hand crosswalk, used ONLY when nothing above matched (688 rows).
`match_tier` numbers + `price_source`/`price_confidence` updated everywhere (silver, gold,
api `/stats`, `/admin/sets`, `/admin/price-discrepancies` default tier → 0=all).

### Result (verified)
- Tier mix: 1 anchored 8,558 · 2 auto 1,945 · 3 fallback 348 · 4 tcgcsv 3,305 · 5 manual 688
  · null 2,622. Coverage 90.4%.
- Bad divergences (>3×, ≥50 SEK): **~402 → ~147**. Egregious old manual mismatches fixed:
  - Grasp of the Arknight U/R: €984 → €56 (anchor $61) ✓
  - Storm Striders U/R: €1300 → €43 (anchor $47) ✓
  - Flic Flak A/S: 388 SEK (old manual = the foil!) → 3 SEK (anchor $0.25) ✓

### What we tried / found that didn't fully work (and why)
- **Anchoring within the mapped expansion only** (first attempt) — left the worst cases
  unfixed (€984/€1300) because that expansion contained ONLY pricey foil products, so the
  anchor had nothing cheap to pick. Fix: broaden the anchor pool to the card's pitch-name
  across ALL expansions; the anchor price keeps the pick honest. Divergences then dropped.
- **Residual ~147 divergences** are NOT all bugs:
  - genuine EU/US market gaps (legit).
  - **Cardmarket pitch-suffix inconsistency**: e.g. `Spike with Bloodrot` — the cheap
    normal products are named "Spike with Bloodrot" while only the €89 foil carries
    "(Red)". `cm_name` requires the suffix, so the anchor is forced onto the foil. Can't
    just strip the suffix: some names (e.g. Sink Below Red/Yellow/Blue) are DISTINCT cards
    sharing a name across pitches. → guarded follow-up (match base-name only for
    single-pitch names). NOT done yet.
- Use the discrepancy endpoint to triage the rest; manual crosswalk rows that still
  diverge (tier 5) are the only ones genuinely worth hand-fixing.
- **Domain note (user-confirmed): Alpha = 1st Edition.** `tcg_prices` already maps edition
  `A` and `F` both to tcgcsv `'1st Edition …'`, so Alpha anchors correctly — it is NOT a
  residual cause (an earlier writeup wrongly listed it; corrected).

---

## 2026-06-28 — Source reliability model + manual-match audit (DONE, verified)

### Context / why
With tcgcsv in, we now have overlapping price sources. User wanted to rate source
reliability, use the most reliable as primary and others as gap-fillers, and specifically
flagged that the hand-built Cardmarket crosswalk ("my manual work") might not be 100%.

### Decisions (user-confirmed)
- **Headline `price_sek` stays Cardmarket-EUR-primary, tcgcsv-USD-fills.** User chose EU
  market relevance over USD linkage purity. This already matched existing behaviour, so the
  price *ordering* didn't change — we just made it explicit and documented (don't flip it).
- **Every card now carries `price_source` + `price_confidence`** (gold columns, also in
  `/cards`). source ∈ {cardmarket_manual, cardmarket_auto, cardmarket_fallback, tcgcsv_usd};
  confidence: tcgcsv_usd & cardmarket_manual = high, auto = medium, fallback = low.
- **New `GET /admin/price-discrepancies`** to validate the manual crosswalk: normalises EUR
  & USD to SEK and flags cards where they diverge (params: tier [default 1=manual],
  min_ratio [2.0], min_sek [25], paging).

### Finding (actionable)
The audit immediately flagged **~146 tier-1 (manual) matches** diverging ≥3× (≥50 SEK), all
on the early hand-matched sets (WTR/ARC/…), with **EUR consistently far ABOVE USD**. E.g.
`Flic Flak` WTR093 (normal): Cardmarket ⇒ 388 SEK (~€33) vs tcgcsv ⇒ 2 SEK (~$0.20) — a
common normal can't be €33, so that `fab_cm_manual.csv` row is pointing at the wrong
(pricier, probably foil/1st-ed) Cardmarket product. Matches the long-standing code comment
that early sets have swapped printing_unique_ids. NOT yet fixed — left for a focused pass
using the new endpoint.

### What we tried / considered but didn't do
- **Making tcgcsv USD the primary SEK basis** — rejected by user (EU-market EUR preferred).
  Kept tcgcsv as the high-confidence *validator* + gap-filler instead.
- **Auto-correcting the manual crosswalk from tcgcsv** — out of scope for now; the
  divergence report surfaces candidates but fixes should be reviewed by hand (currency/
  market differences mean not every divergence is an error, though >50× clearly is).

### Files
`silver_cards.sql` (+price_source/price_confidence on both union halves),
`gold_cards.sql` (pass-through), `api.py` (new endpoint + price_source in /cards & /stats).

---

## 2026-06-28 — Replace JustTCG with tcgcsv.com (DONE, verified)

### Result (verified end-to-end)
- `ingest_tcgcsv.py` loads 9,770 cards / 15,955 prices across all 97 FaB sets, no rate limit.
- `gold.gold_cards`: 14,163 → **17,468 rows**. USD price coverage **9,705 → 13,869 rows (79%)**;
  overall priced coverage 90.2%.
- The gap sets now resolve fully: `/sets` shows **PEN Compendium of Rathe** (676 cards, all
  priced) and **OMN Omens of the Third Age** (478 cards, 450 priced) with names + release
  dates + images + USD/SEK prices.
- JustTCG dropped from `run_pipeline.sh`; scripts/tables kept as dormant backup.

### Open decision (flag for user)
tier-4 currently folds in **every** tcgcsv card the-fab-cube lacks (3,305 rows) — that
includes the gap booster sets (PEN/OMN) AND promos/decks (e.g. ~1,100 "Promo Cards",
alt-art reprints). If that's too noisy for the card list, easy to restrict tier-4 to
specific set codes or non-promo groups. Left broad for now (max coverage).

---

## 2026-06-28 — Replace JustTCG with tcgcsv.com (original plan)

### Context / why
JustTCG (the USD-price + missing-set source) is capped at ~100 requests/day and ~7s per
request, so the daily pipeline kept stalling on `DAILY_LIMIT_EXCEEDED` and couldn't fully
refresh. Yesterday's session found a better source and agreed on plan "A" but stopped
before any code was written.

### Decision
**tcgcsv.com becomes the primary price + missing-set source; JustTCG goes dormant.**
tcgcsv is a free, no-API-key, **no-rate-limit** mirror of TCGplayer data (FaB = category
62). It provides, for every set including brand-new ones the-fab-cube lacks:
names, set numbers, rarity, full card text, type/class/cost/stats, image URLs, and USD
prices (Normal / Cold Foil / Rainbow Foil variants). It covers **both** jobs JustTCG did:
1. Missing-set cards (PEN/OMN + future gaps) — full card data, in one download.
2. USD prices for existing the-fab-cube cards — joined by `productId = tcgplayer_product_id`.

### Plan (this build) — all done
- [x] `ingest_tcgcsv.py` — fetch FaB groups → products → prices into
      `bronze.tcgcsv_groups` / `tcgcsv_cards` / `tcgcsv_prices` (sends a User-Agent header).
      Also mirrors missing-set names into `bronze.fab_sets` so `/sets` shows them.
- [x] `setup_db.py` — added the three tcgcsv tables.
- [x] `silver_cards.sql` — USD prices sourced from tcgcsv (replaced the JustTCG join);
      missing-set cards (tier 4) sourced from tcgcsv (cards whose `productId` isn't in
      `bronze.fab_printings`).
- [x] `gold_cards.sql` / `api.py` — tier-4 comment + `/stats` & `/admin/sets` now report tier4.
- [x] `run_pipeline.sh` — runs `ingest_tcgcsv.py`; JustTCG steps removed from default flow.
- [x] Verified end-to-end (see Result above).

### Data shape (verified live 2026-06-28)
- `https://tcgcsv.com/tcgplayer/62/groups` → `results[]`: `groupId, name, abbreviation
  (=set code), isSupplemental, publishedOn`. 97 FaB groups; includes Omens of the Third
  Age (24640) and Compendium of Rathe (24532).
- `.../62/<groupId>/products` → `results[]`: `productId, name, cleanName, imageUrl,
  extendedData[]` (Rarity, Number e.g. `OMN001`, Description=card text (HTML),
  CardType, Class, Talent, Intellect, Life, Cost, Pitch, Power, Defense …).
- `.../62/<groupId>/prices` → `results[]`: `productId, lowPrice, midPrice, highPrice,
  marketPrice, directLowPrice, subTypeName` (Normal / Cold Foil / Rainbow Foil).
  `marketPrice` is the USD figure to use; `subTypeName` → our foiling S/C/R.

### What we tried that didn't work (and why)
- **Cardmarket bulk data for card images** — dead end. The product records have no image
  field at all (`idProduct, name, idCategory, idExpansion, idMetacard, dateAdded`).
- **Rendering placeholder images for missing cards** — unnecessary. Real images are
  reachable from a TCGplayer `productId` via the CDN
  (`tcgplayer-cdn.tcgplayer.com/product/<id>_in_1000x1000.jpg`), so no placeholders needed.
- **Relying on the-fab-cube for new sets** — it lags ~2 boosters (skipped Compendium in
  Feb and Omens in Jun 2026). Treat it as unreliable for recent releases.
- **JustTCG as the price/missing-set source** — works but the free tier (~100 req/day,
  ~7s/req) is too tight for a daily full refresh; superseded by tcgcsv. Kept dormant as a
  fallback rather than deleted.
- **`urllib`/default UA against tcgcsv** — returns HTTP 401. tcgcsv requires a
  `User-Agent` header; `requests` with an explicit UA works.
- **Mapping foiling → tcgcsv sub_type with only bare labels** (first attempt at the USD
  join: `foiling 'S' → 'Normal'`, etc.) — silently missed ALL older sets (e.g. WTR got 0
  USD prices). Cause: tcgcsv uses **edition-qualified** sub_types for older sets
  (`1st Edition Normal`, `Unlimited Edition Rainbow Foil`, `1st Edition Cold Foil`) and bare
  labels (`Normal`, `Rainbow Foil`, `Cold Foil`) only for newer sets. Fix: match BOTH forms
  per (edition, foiling), preferring the edition-qualified price; this is essentially the
  same edition+foil logic the old JustTCG join had. After the fix USD coverage went
  9,705 → 13,869 rows and WTR prices its editions correctly (1st Ed Normal $7.74 vs
  Unlimited $0.32). One TCGplayer productId in the-fab-cube maps to all foilings/editions of
  a card, so the (edition, foiling) → sub_type mapping is what disambiguates the price.
- **Deriving set_code from the Number's leading letters** (`^[A-Za-z]+`) — left 162 cards
  with NULL set_code because some collector numbers start with a digit (e.g. `1HB024`).
  Fix: strip the *trailing* digits instead (`\d+$` → ''), so `1HB024` → `1HB`, `OMN001` → `OMN`.

---

## 2026-06-27 — Project resurrection & hosting

- Restored FAB from a Windows drive to `/home/tango/Projects/fab`; stood up the
  bronze/silver/gold + FastAPI + Vite stack.
- Chose Lovable for frontend hosting (own origin → needs absolute API URL), kept FastAPI
  same-origin serving of `dist/` as a fallback. `start_fab.py` syncs the tunnel URL into
  the Lovable repo on each run.
- Postgres via docker-compose; dev exposure via cloudflared quick tunnel (ephemeral URL).
- Added JustTCG missing-set backfill (since superseded — see above).
