# Agent worklog

Running log of what's been done, what's planned, and what was tried but abandoned (and
why). Newest entries on top. Keep this honest — failed attempts are the most valuable
part. See `CLAUDE.md` for architecture and `README.md` for setup.

---

## 2026-07-06 (night) — Phase 4: trading platform (valuation, listings, offers)

User confirmed the valuation rule → built the whole Phase 4 slice, live end-to-end
(fab `0ee2d95`/`edd7945`, frontend `00af24b`).

- **Valuation:** `gold.trade_value_sek` = greatest(CM trend, CM low) EUR→SEK, tcgcsv
  USD fallback; `cm_low_eur` surfaced from `latest_prices` via `bm.idproduct` join.
  17,014 valued; 1,047 bumped by low>trend; 0 below trend. 13 dbt tests pass.
- **Backend (`fab_api/routers/trade.py`):** `is_trade_list` flag on cardlists (PATCH
  handles name and/or flag), public `GET /trade/listings` (q/set search, owner email,
  value), offers with per-unit value snapshots at send time, accept/decline/cancel with
  role enforcement (recipient vs sender) + pending-only 409 guard. Accepting only
  records the deal — in-person swap, no inventory movement (deliberate).
- **Frontend `/trade` (Trading Post):** listings table, offer builder (chips to add
  from their trade list / your own cardlists, per-side totals + balance line, message),
  offers inbox with status chips + actions. Account: Trade?/Trading toggle + TRADE chip.
- Verified with two temp users: listing browse → offer 78 kr vs 443 kr → 403
  sender-accept → accept → 409 re-accept. Test users cleaned (cascade removes offers).
- NOT built yet (deliberate): offer notifications (email on incoming offer), Swish
  deep-link/IOU settlement from the roadmap, listing pagination server keys beyond 200
  offers, trade history view beyond the flat inbox.

---

## 2026-07-06 (evening) — Real email via Resend + scanner UI polish + SPA fallback

**Email live (`d1a629c` + frontend `df3e647`):** `_deliver_magic_link` now sends styled
HTML via api.resend.com when `RESEND_API_KEY` is set (10s timeout, logged, graceful
fallback to dev link on any failure). Magic links now target the FRONTEND
(`/account?token=…`) instead of the raw API verify endpoint; `dev_link` only returned
when no email was sent (no token leak). Frontend: `devLogin` returns null on real-email
mode → "Sign-in link sent — check your email" toast. User created a Resend account
(gmail signup), key in `.env` — verified delivered + signed in for real.
GOTCHA: free tier without a verified domain only delivers to the account owner's own
address; domain verification (same purchase as named tunnel) unlocks other users.

**SPA fallback (`10a0186`):** the emailed link 404'd — StaticFiles didn't know
`/account`. `SpaStaticFiles` in api.py serves index.html for unknown paths (API 404s
unaffected — only unmatched paths reach the mount). Closes the long-standing
"deep-link on self-hosted" open item.

**Scanner app UI (`4de96dd`):** header inset below status bar, instruction box
("READS THE CARD CODE" + align hint) flush under header, CODE label over the footer
strip (guide geometry/crops untouched), pair row hidden once paired (Advanced
re-surfaces it), Torch→Light, compact panel. Verified on device via adb screenshots.

---

## 2026-07-06 (later) — Gap-tool search/cardlist filters + anchored-match sanity guard

**Tools search (fab `tbd`, frontend `127feae`):** `/tools/price-gap` gained `q` (name
ILIKE) and `cardlist_id` (+optional bearer auth — `_current_user` called directly, list
ownership enforced, 404 for someone else's list). UI: debounced search box + a Cardlist
dropdown (signed-in only) that restricts the table to one of your lists.

**HNT055 Cindra mismatch → silver guard (`5f9f617`):** user spotted the Cindra token
priced 63 SEK. Root cause: CM has NO product for the plain HNT055 token ($0.08); bare-name
"Cindra" candidates were the CIN armory-deck hero (€5.72, exp 6049) and the HNT Marvel
(€30.11) — anchored picked the least-bad wrong one. Fix in `silver_cards.sql`:
`cm_anchor_rejected` — if even the CLOSEST candidate is ≥20× off the anchor AND ≥50 SEK
apart, the printing skips tiers 1-3 (heuristic/fallback draw from the same wrong pool)
and falls to tcgcsv USD; manual tier-5 still overrides. 18 rows changed: 5 → manual
(WTR080/ARC005 land on their hand-mapped cold-foil products), 13 → tcgcsv_usd (Ash
UPR043, Eloquence FAB154, …). Coverage unchanged (16,560), 13 dbt tests pass. Bounds are
deliberately extreme: the 3-20× band (~70 rows) is documented genuine EU/US market gaps
and keeps the CM-first basis. Verified in the gap tool: the token row disappears (no
longer dual-priced), remaining Cindra rows sane.

---

## 2026-07-06 — Tools tab: price-gap explorer

New web tab (fab `1e32fac`, frontend `f5b5c29`, both pushed; API restarted, same tunnel
URL so no Lovable .env churn — the frontend push itself triggered the rebuild).

- **`GET /tools/price-gap`** (`fab_api/routers/tools.py`): cards priced on BOTH markets
  whose Cardmarket EUR and tcgcsv USD prices (normalised to SEK) diverge. Filters:
  `direction` (usd|eur|any), `min_pct` (gap = pricier/cheaper − 1), `rarity`, `set_id`,
  `card_class` (word-boundary regex on `type_text`, input sanitised to letters/hyphen),
  `foil` (is_foil). Sort via whitelist dict (`_SORT_COLUMNS`) — sort params can't inject.
  Rows missing either price excluded by definition (user requirement: no 0/null rows).
- **`GET /tools/classes`**: distinct class/talent tokens from `type_text` (split on
  ' - ', ';', ','; structural words like Action/Attack/Hero stopped out) — data-driven
  so new sets' classes appear without code changes. 1 h cache header.
- **Frontend `/tools`** (`src/pages/Tools.tsx` + nav tab): filter bar (direction, min %,
  rarity, class, set, foil), clickable sortable column headers, pagination, row click →
  CardDetailModal (row shimmed into `Card` shape via `toCard()`). Green = USD pricier,
  magenta = EUR pricier.
- Verified end-to-end via a same-origin dist build served by a test API instance
  (12,599 dual-priced rows; direction/%/class/foil/set/rarity combos + injection guard).
- Also diagnosed a "tesseract stopped working" toast on the phone: tesseract 5.5.0 is
  fine; the log shows one garbage OCR read from a blurry frame + the known missing
  easyocr/orb-descriptor fallbacks. Transient capture issue, not config.

---

## 2026-07-05 — Foundation hardening: backups, API perf, router split, web polish

Sanity-check session ("are we on the right track?") → verdict: pipeline + product are
sound; fixed the four foundation gaps. All committed + pushed (fab `52d203f`, `3556741`;
frontend `b8d438a` → Lovable rebuild). Tunnel→named-domain fix deliberately DEFERRED
(user will evaluate alternatives).

**1. Nightly app-schema backups (`backup_app.py` + cron).** `app.*` (users, cardlists,
scan history) is the only non-regenerable data and had no backups. Script does a
psycopg2 COPY-based logical dump (TRUNCATE + COPY + literal sequence setvals so it
restores onto a fresh DB) to `backups/app_<ts>.sql`, keeps 30, atomic `.part` rename.
User crontab: `30 3 * * * cd ~/Projects/fab && .venv/bin/python backup_app.py`.
Gotcha hit: first crontab line lacked the `cd` (cron runs from $HOME) — fixed.
Restore: `sudo docker compose exec -T db psql -U <user> -d fab < backups/app_<ts>.sql`.

**2. API perf.** GZip middleware (`/cards?page_size=300`: 248 KB → 22 KB, ~11×);
psycopg2 `ThreadedConnectionPool` (1–12) behind the SAME `get_conn()` context-manager
shape so all 33 call sites were untouched (commits on clean exit, rolls back + drops
broken conns on error); `Cache-Control` on `/sets` (1 h) and `/stats` (10 min).
Pooled `/stats` ≈ 5–6 ms. Write paths verified end-to-end (auth + cardlist round-trip),
test rows cleaned from the shared DB.

**3. Router split + dead-code removal.** `api.py` (2 369 lines) → thin wiring +
`fab_api/` package: `core.py` (env, pool, scan log), `scan_engine.py` (recognition,
moved **verbatim** — scanner logic untouched per standing constraint), and
`routers/{cards,admin,scan,auth,cardlists}.py`. Split done by line-range extraction
script, not retyping. Removed the orphaned browser-scanner path: `POST /scan`,
`POST /scan/debug`, `_ocr_claude`, `_ocr_google`, `ScanRequest`, Google/Anthropic key
consts. KEPT `_rectify_card`/`_find_card_quad`/`_order_corners`/`_ocr_easyocr`/
`_visual_match` — `/scan/native` uses them. Verified: 26 routes respond; replaying a
saved footer crop through `/scan/native` gives the identical result
(`AJV025` → Winter's Bite, 0.99); auth 401s correctly; dead endpoints gone.
`start_fab.py` unchanged (`uvicorn api:app` still the entry).

**4. Web polish (frontend `b8d438a`).** New locked-system primitives `.skeleton`
(shimmer) + `.tile-in` (staggered entrance, reduced-motion safe). Index: skeleton grid
on first load, compact hero (stats folded into the tagline line — was eating ~2× the
viewport), staggered tiles. CardDetailModal: ←/→ keys + on-screen chevrons step through
the browse order. `index.html`: real title/description/OG (Lovable boilerplate gone) +
new cyan-F `favicon.svg`. Stagger note: `CardGroupItem` renders `display:contents`, so
the animation class goes on the inner tile via an `index` prop (a wrapper div would
break the `col-span-full` expanded panel).

**Bash gotcha for future sessions:** `pkill -f "uvicorn api:app --port 8010"` inside a
compound command kills the *current shell* (the pattern matches the shell's own command
line, exit 144). Run pkill in its own Bash call, separate from the start command.

**Still open:** tunnel → named tunnel + domain (deferred by user); real email sender
(`_deliver_magic_link` swap); Phase 4 trading (valuation rule unconfirmed);
off-machine copy of `backups/` would be nice (currently local-only).

---

## 2026-07-04 — Phase 2 frontend + Phase 3 visual overhaul + scanner app revamp

Everything below is committed + pushed (fab → GitHub GHT4ngo/fab; frontend →
GHT4ngo/retro-data-display, which triggers the Lovable rebuild). The self-hosted dist is
rebuilt each time so the tunnel URL shows changes instantly.

### Phase 2 frontend (retro-data-display) — DONE, verified end-to-end on live :8001
- `src/lib/auth.ts` — magic-link auth + cardlist API client; session token in localStorage,
  sent as `Authorization: Bearer`. `devLogin(email)` = request-link then auto-verify the
  returned `dev_link` (dev-mode shortcut; real email later is a one-function swap).
- `src/hooks/useAuth.tsx` — AuthProvider/useAuth: restores session on load, honours a
  `?token=` magic link, dev sign-in. Wrapped around the app in `App.tsx`.
- `src/pages/Account.tsx` — new **Account** tab: email sign-in, create/rename/open/delete
  lists, per-item qty controls.
- `AddToListButton` (card detail modal) + `SaveScanToListButton` (bulk-save a whole scanned
  session) — both nudge signed-out users to the Account tab.

### Scanner app (fab-scanner-android) — two changes
- **Camera on/off toggle** ("Cam On/Cam Off"): `bindCamera`/`stopCamera`/`toggleCamera`
  unbind the camera so NO frames are analyzed or POSTed while off (user: don't spam the API).
- **Professional cyber UI revamp**: glowing FAB SCANNER header, rounded translucent control
  panel with cyan outline, cyber-styled buttons/inputs (`cyberButton`/`styleInput` + a
  `Theme` palette mirroring the web), and a HUD `CardGuideView` (cyan corner brackets + faint
  frame + cyan footer target strip) replacing the plain green rectangle. Verified via adb
  screenshot. `/scanner-apk` now serves `outputs/apk/debug` (was a stale `intermediates` path).

### Phase 3 visual overhaul (roadmap step 3) — locked design system, applied app-wide
- `src/index.css` is now the **locked system**: deep layered aura background, softer default
  borders (bright cyan reserved for accents), tamed/slower scanline + edge-masked grid,
  removed the blanket `button:hover` neon (→ tasteful `:focus-visible` ring). New reusable
  primitives: `.panel`, `.panel-raised`, `.panel-hover`, `.section-title`, `.hud-frame`,
  `.chip`, `.divider-glow` (existing `text-glow`/`glow-card`/`glitch` kept + refined).
- Applied across Nav (uniform `border-b-2` tabs), Scanner (HUD panels, hero title), Browse
  card tiles (hover lift not scale), Account, Admin (`.panel` tiles/tables, `.section-title`
  headers), and the detail modal (raised surface + softer glow).
- **User design feedback applied**: removed the "FAB / Flesh & Blood" header wordmark (looked
  bad); made tabs align uniformly; unified the page background — dropped the flat
  `bg-background` from Browse/Admin so every tab shows the same aura as the Scanner (preferred).

### Still open / next
- Phase 4 (trading): sell/trade lists + offers; NEEDS the valuation rule confirmed
  ("trend price, or low if it's higher").
- Productionize Phase 2: real magic-link email (swap `_deliver_magic_link`).
- Leftover from Phase 1: careful removal of orphaned browser `/scan` dead code (keep
  `/scan/native`'s easyocr+visual). Deep-link SPA fallback on the self-hosted app (Lovable ok).

---

## 2026-07-04 — Phase 2 backend: email accounts (magic-link) + named cardlists

Passwordless account system + server-side cardlists in the `app` schema. Email delivery
is **DEV MODE** (link returned in the response + logged, not emailed) — swap
`_deliver_magic_link()` for a real sender (Resend/SMTP) to go live.

### Schema (setup_db.py canonical + api.py self-migrates via ensure_app_auth_schema)
- `app.users` (email UNIQUE), `app.magic_tokens` (15-min TTL), `app.sessions` (30-day TTL,
  bearer token), `app.cardlists`, `app.cardlist_items` (UNIQUE(list, printing), qty).

### API (all before the `/` static mount)
- `POST /auth/request-link` {email} → mints token, returns `dev_link` (+ logs it).
- `GET /auth/verify?token=` → consumes token, upserts user, returns `session_token`.
- `GET /auth/me`, `POST /auth/logout`. Auth via `Authorization: Bearer <session_token>`
  resolved by the `_current_user` dependency (401 on missing/expired).
- Cardlists CRUD: `GET/POST /cardlists`, `GET/PATCH/DELETE /cardlists/{id}`,
  `POST /cardlists/{id}/items` (adds to qty on conflict), `PATCH`/`DELETE
  /cardlists/{id}/items/{printing}`. Items join `gold.gold_cards` for name/image/price;
  list index carries `item_count` + `total_sek`. Ownership enforced everywhere
  (`_get_owned_cardlist` → 404 for other users).

### Verified end-to-end (throwaway uvicorn on :8010, shared DB, then cleaned up)
- Full flow: request-link → verify → me (+401 without token) → create → add item (qty
  increments on re-add) → unknown printing 404 → detail (joined, total_sek=71) → set qty →
  rename → delete (→404) → logout (→401). Ownership: user2 gets 404 on user1's list + own
  list `[]`. One bug found + fixed: detail query referenced `g.set_name` which doesn't exist
  on `gold.gold_cards` (set_name lives on the `fab_sets s` join other endpoints use).
- NOT live on :8001 yet — needs an API restart. Frontend (login UI + "my lists") is next.

---

## 2026-07-04 — Tunnel self-heal, app endpoint auto-discovery, Phase 1 verified live

### Phase 1 backend — VERIFIED LIVE (after API restart)
- `/scan/code?code=HVY050` resolves to "Miller's Grindstone" + printings; OCR typo
  `HVYO5O` auto-corrects to HVY050. `/cards` returns the fluff + SEK fields
  (`health`, `intelligence`, `functional_text`, `price_eur_sek`, `price_usd_sek`). Done.

### App backend auto-discovery (no more re-pointing the phone after a restart)
- The native app had the trycloudflare URL hardcoded as `DEFAULT_API_BASE`, so every URL
  rotation broke it. Now `start_fab.py:publish_endpoint()` writes the live URL to a public
  gist on every start, and `MainActivity.discoverApiBase()` fetches it at launch and adopts
  it. Gist: `GHT4ngo/84b51c1df1551685fb9b151f684d979d` → raw `endpoint.txt`. A gist edit
  triggers no rebuild, so this runs unconditionally (unlike the Lovable push).
- `/scanner-apk` now serves the canonical `outputs/apk/debug` build (was a stale
  `intermediates/` path). Rebuilt + installed the discovery-capable APK on the phone.

### Tunnel resilience — the "restarted but frontend can't connect" bug
- Root cause: trycloudflare quick tunnels keep the cloudflared PID alive while the edge
  control-stream dies (`control stream encountered a failure` + `Retrying connection`).
  `--restart` reused the dead tunnel by PID → public URL served nothing (HTTP 000).
- Fix: `tunnel_reachable()` HTTP-checks the URL before reuse; a zombie is torn down and
  replaced. Because that changes the URL, Lovable is now synced on that restart too (sync
  fires whenever the URL changed, even without `--sync-lovable`; same-URL restart = no push).
  Verified: dead URL → False, live URL → True.

### Scanner OCR — reverted a change; DO NOT redesign the reader
- Diagnosed a scan failure as an out-of-focus footer capture (crop was pure bokeh) +
  fallbacks disabled (`bronze.card_orb_descriptors` table absent → visual off; `easyocr`
  not installed → title off), NOT broken OCR logic. Raised the phone's OkHttp timeouts
  (10s → 30s) so slow no-match scans don't SocketTimeout. Reverted a `full_footer` fast-mode
  tweak — user: the reader works, don't remake it.

---

## 2026-07-03 — Phase 1: card detail view + scan page rebuild (browser→app + manual code) (NEEDS TESTING)

Native scanner app now works well; pivoted focus to the web app. Agreed a roadmap
(see memory `fab-roadmap.md`): card detail view → email accounts + named cardlists →
graphic overhaul → hybrid app → **card-trading platform**. Started Phase 1.

### Done + pushed to Lovable (frontend, `retro-data-display`)
- **Card detail modal** (`CardDetailModal.tsx`): click a printing (grid or list) → full
  detail: large image, stat chips (cost/attack/defense/life/intellect/pitch, only the
  non-empty ones), type line, rules text (renders `**bold**` + `{r}` tokens), and prices —
  **EUR and USD each with its own SEK** + headline SEK + source/confidence. Replaced the
  list-view side panel with this one shared modal. Wired via `onSelect` through
  CardGrid/CardGroupItem/CardItem and CardListView; state lives in `Index.tsx`.
- **Bigger detail image** (~1.7×, `max-w-4xl` + 380px column) and **oldest-printing art**:
  `groupCards.ts` `best_image` now uses the oldest printing (sorted oldest→newest).
- **Scan page fully rebuilt** (`Scanner.tsx`): removed ALL browser camera/OCR code. Now
  (1) "Scan with the app" — download link (`/scanner-apk`) + pair code (email → `/scan/session`,
  live sync via `/scan/records`), and (2) "Type a code" — manual entry via `/scan/code`.
  Kept cardlist/printing-picker/edit/price-toggle/totals. Bundle shrank; tsc + build pass.

### Done but UNCOMMITTED + NOT YET LIVE (backend, `api.py`) — needs API restart
- `/cards` now returns the fluff fields (`health`, `intelligence`, `functional_text`; cost/
  power/defense/type_text already were) + computed `price_eur_sek` / `price_usd_sek`
  (USD/SEK rate from `bronze.exchange_rates`). Verified via direct SQL.
- New **`GET /scan/code?code=HVY050`** — manual entry: `_parse_code`+`_snap_code`+printings.
  Verified: handles lowercase + OCR-style typos (`HVYO5O`→HVY050), null for garbage.
- **To make Phase 1 testable tomorrow: `./run_pipeline.sh --restart`** (loads new api.py;
  reuses the persistent tunnel, no Lovable rebuild). `api.py` change not committed yet.

### Deliberately NOT done (flagged, do carefully)
- Backend removal of the now-orphaned `/scan` + `/scan/debug` + `_ocr_claude`/`_ocr_google`.
  Reason: `/scan/native` (the working app) still SHARES `_ocr_easyocr` (title OCR) and
  `_visual_match` (visual fallback), so the earlier "remove easyocr" scope conflicts —
  remove only the browser-only bits, keep the app's fallbacks. Also api.py is edited in
  parallel; do this as a focused pass.

### Next session
1. `./run_pipeline.sh --restart`, then TEST: detail modal fields, manual `HVY050` entry,
   app pair flow. Tweak detail layout/fields as needed.
2. Commit `api.py` (fluff + `/scan/code`) to `fab` repo when happy.
3. Careful backend `/scan` dead-code removal (keep `/scan/native` intact).
4. Phase 2: magic-link email accounts + server-side named cardlists (email = portable
   account; expand profile later: username, trade mail, deal history, profit-since-scan).

---

## 2026-07-02 — Pipeline hardening + scanner usability pass

### Pipeline / serving
- Reworked the daily launcher so `./run_pipeline.sh` is the normal data refresh + serve
  path and `./run_pipeline.sh --restart` is a quick API/tunnel restart only.
- `run_pipeline.sh` now checks whether Postgres is already reachable before trying Docker,
  avoiding a pointless sudo/docker stall when the DB is already up.
- `start_fab.py` now keeps Git/Lovable sync opt-in (`--sync-lovable` or `PUSH_LOVABLE=1`)
  outside the daily pipeline path, uses timeouts for git commands, and does not let a
  stuck push block API startup.
- Persistent tunnel handling was hardened: stale/poisoned pidfiles are recovered by
  finding the running cloudflared process, and the pidfile is not written until a URL is
  actually found. Current tunnel remains in `tmp/logs/tunnel_url.txt`.

### Frontend scanner
- Cleaned up `retro-data-display/src/pages/Scanner.tsx` and pushed it to the frontend repo:
  `a8e53ba feat: simplify scanner pairing UI`.
- The scanner page now presents a trade-session flow instead of exposing URL/code fields
  by default: start session, pair phone, optional reveal/copy code, trading name, and phone
  scan polling into the editable list.
- Verified `npm --prefix retro-data-display run build` and confirmed `origin/main` points
  at `a8e53ba6529a6862bc8931d700f7ece2f3727b00`.

### Android scanner
- Updated `fab-scanner-android/app/src/main/java/com/fabscanner/app/MainActivity.kt` so the
  main flow is pair-code first, with the API URL hidden under Advanced.
- Default API base points at the current Cloudflare URL so normal users should not need to
  type a backend URL.
- Widened the footer crop sent from the phone so the backend can search the 2-5 mm footer
  code whether it is centered or left aligned.
- Verified Kotlin compile and debug APK build using Android Studio's JBR:
  `JAVA_HOME=/snap/android-studio/232/jbr GRADLE_USER_HOME=/home/tango/Projects/fab/fab-scanner-android/.gradle ./gradlew :app:assembleDebug`.
- Installed and launched the debug APK via ADB at least once. Wireless debugging can still
  fall into stale pairing state; reboot remains the known reset.

### Backend scanner matching
- `/scan/native` now prioritizes footer-code OCR over visual guessing and stores
  successful session scans in `app.scanned_cards`.
- `_read_footer_code()` searches multiple footer subwindows: lower-left, centered, wide,
  and full footer variants. This matches the physical card layout where the black footer is
  about 2 mm from the bottom and the code sits roughly 2-5 mm from the bottom.
- `_parse_code()` now refuses partial collector numbers, so fragments like `BET7` do not
  snap to random cards. It also corrects common set-code OCR mistakes such as `R05 130` or
  `ENR05130MK` into `ROS130`.
- Fusion now requires footer code, strong title match, or visual+title agreement. Visual-only
  guesses are intentionally suppressed because they produced wrong-card matches.
- Verified `.venv/bin/python -m py_compile api.py`.

### Open / next
- The current phone-to-web flow is a lightweight local trade session, not a real
  account/device system. A real account system still needs persistent users, login,
  device pairing tokens, and ownership/permission rules.
- `easyocr` is intentionally not installed because it tried to pull a huge PyTorch/CUDA
  stack. Footer OCR uses `pytesseract`; title OCR fallback is limited unless a lighter
  dependency plan is chosen.
- Root project is still a local dirty workspace with several uncommitted implementation
  changes. The Android project is local/untracked from the root repo unless that is
  intentionally committed later.

---

## 2026-07-01 — Android scanner pairing/build recovery note

- User ran the pipeline, paired the phone, launched the Android app, then hit a
  component/exception-style failure. The phone pairing closed immediately afterward.
- Local ADB server can start, but `adb devices -l` showed no connected devices after the
  failure, while the phone still claimed it was paired and Android Studio could not find it.
- User-observed recovery rule: when wireless debugging pairing gets into this state, a full
  computer restart is required before the phone can be paired again. Treat repeated pairing
  attempts before reboot as wasted time unless new evidence says otherwise.
- Fixed `fab-scanner-android/debug_phone.sh`: debug builds install as
  `com.fabscanner.app.debug`, while the activity class remains
  `com.fabscanner.app.MainActivity`. The old script launched `com.fabscanner.app`, which
  could produce misleading component/start failures.
- Verified after the script fix: `./gradlew :app:assembleDebug --quiet` passes using
  `/snap/android-studio/232/jbr` and `GRADLE_USER_HOME=/tmp/fab-gradle`.
- Next attempt after reboot: pair/connect the phone again, confirm `adb devices -l` shows
  state `device`, then run `fab-scanner-android/debug_phone.sh` to install, launch, and
  stream `FabScanner`/`AndroidRuntime` logs.

## 2026-07-01 — Native scanner phone-to-web session sync (DONE)

- Added lightweight scanner sessions: `POST /scan/session` creates/joins a short
  alphanumeric `session_code` with optional email/label.
- `/scan/native` now accepts `session_code`/`session_email`; high-confidence stored scans
  are tagged in `app.scanned_cards`.
- `/scan/records` now supports `session_code` + `after_id` and returns printings so the web
  scanner page can poll new phone scans directly into the editable cardlist.
- Android MVP now has a session-code input saved in app preferences and sends the code on
  every native scan request.
- Frontend `/scan` now has a Phone sync panel. Create a code in the browser, enter it on
  the phone, then scans append to My Cardlist where quantity, set/foiling edits, removal,
  and pricing already work.
- Verified: `python -m py_compile api.py`, `npm --prefix retro-data-display run build`,
  local `POST /scan/session`, and session-filtered `/scan/records`.
- Could not validate Android CLI build from Codex shell because `JAVA_HOME`/`java` is not
  on PATH; Android Studio should build/run after syncing `MainActivity.kt`.

---

## 2026-06-29 — Camera scanner (browser → native app), persistent tunnel, git init + GitHub (DONE)

### Context / why
After yesterday's data cleanup the app went live again, but (a) the Lovable frontend was
"not connected to the DB", (b) the camera scanner was the next feature, and (c) restarting
the server kept forcing a Lovable rebuild. Worked through all three; the camera ended the
day pivoting from the browser to a native Android app.

### Frontend "not connected" — root cause + fix
- The `.env` already held the live tunnel URL, but the commit carrying it was **never pushed**
  (`origin/main` still had the previous, dead URL), so Lovable kept building against a dead
  tunnel. The earlier "fix" only addressed the push *failing*, not the URL churn.
- True cause of the failed push: **no git credential helper** → HTTPS push prompted for a
  password, which GitHub rejects. Fixed with `gh auth setup-git` (git now uses the `gh` token).
- Hardened `start_fab.py` `sync_lovable`: `GIT_TERMINAL_PROMPT=0` (fail fast, no hang),
  verify the commit actually reached the remote (`ls-remote` vs local HEAD), loud actionable
  failure pointing at `gh auth setup-git`.

### Camera scanner — code/`display_id` approach (backend kept, browser UI abandoned)
- **Key insight (user):** don't recognise the art — read the bottom-left code (`R EN | HVY050`).
  `HVY050` **is** our `gold.display_id`, so it pins set+number deterministically.
- **Backend `/scan` `code` engine** (`api.py`): OCR the footer with Tesseract (`pytesseract`,
  whitelisted single line), `_parse_code` extracts the `LETTERS+digits` token, `_snap_code`
  snaps it to the **known** display_id vocabulary (122 set codes / ~9.3k ids) with OCR-confusion
  fixup (`O/0 I/1 S/5 B/8 Z/2 G/6`). Returns the exact card + its ≤6 printings. Snap tested
  8/8 on simulated noise incl. set-code corruption (`HVYO5O`→`HVY050`). Added `pytesseract`
  to `requirements-ocr.txt` (system `tesseract-ocr` binary required).
- **Browser capture — TRIED, ABANDONED.** In order: code-crop OCR; sharpness gate + focus
  meter; 4K capture + continuous AF + tap-to-focus; real sensor zoom + manual-focus slider +
  capability diagnostics; WYSIWYG fixes (match preview box to capture aspect, drop double
  zoom); finally native-camera `<input capture>` + draggable bottom band. **Why abandoned:**
  `getUserMedia`/`ImageCapture.takePhoto()` give a crippled camera — the footer stayed too
  soft for OCR even when correctly framed (diagnostics on the user's phone showed full 4K +
  sensor zoom + manual focus available, yet still blurry). The native camera app focuses
  fine, so the browser path is a dead end for tiny footer text.
- **Decision → native Android app.** `fab-scanner-android/` CameraX MVP (card guide, footer
  sharpness gate, refocus, torch, zoom) posting multi-signal to the new **`/scan/native`**
  (`full_image` visual + `footer_crop` exact `display_id` OCR + `title_crop` fuzzy title;
  signals fuse into a confidence). Reuses `_snap_code`/`_ocr_code_tesseract`. Debug crops →
  `tmp/scan_debug_samples/`. Superseded 2026-07-02: Android Studio/SDK are now installed
  enough for Gradle/ADB builds and USB APK installs.

### Persistent Cloudflare tunnel (no more rebuild-on-restart)
- `start_fab.py` now runs cloudflared **detached** (pidfile `tmp/logs/cloudflared.pid`,
  logs to `cloudflared.out`) so it **outlives API restarts**. On start it reuses a live
  tunnel (same URL ⇒ `sync_lovable` no-ops ⇒ no Lovable rebuild); Ctrl+C stops only the API.
  Flags: `--new-tunnel` (force fresh URL), `--stop-tunnel`.
- One-time transition cost: the first `--restart` on the new code couldn't see the old
  (untracked) tunnel, so it minted one new URL + one Lovable push. Verified the new tunnel
  is now tracked (pid 126494) and the **next** restart reuses it.

### Launcher rework — `run_pipeline.sh`
One entry point with modes: default (full pipeline + serve, **skips ingest/dbt if already
run today** via `tmp/logs/.pipeline_done`), `--restart` (serve only — everyday restart),
`--full`, `--no-serve`, `--new-tunnel`, `--stop`, `--help`. `exec`s into `start_fab.py` so
Ctrl+C lands there. Removed hardcoded `[1/4]` labels; JustTCG already gone from the path.

### Git — the parent repo had no history
- Discovered the top-level `fab` repo's `.git/` was **empty** (no commits/refs/remote;
  contradicted the session's "is a git repo"). Surfaced it rather than silently re-init.
- Per user: fresh `git init`, initial commit `0663f27` (37 files), branch renamed `master`→
  `main`. Excluded `.env`, `tmp/`, `.venv/`, dbt artifacts, and the nested `retro-data-display/`
  (its own repo); caught + ignored a stray `*.log`.
- Created **public** GitHub repo **GHT4ngo/fab** and pushed `main`. Scanned tracked files for
  real key formats before going public — none; `.env` returns 404 on GitHub.

### State at end of day
- Backend `/scan` (code engine) + `/scan/native` live; `api.py` modified accordingly.
- Web camera scanner shelved; native app is the path forward.
- Persistent tunnel + `run_pipeline.sh --restart` = restart server without Lovable churn.
- `fab` and `retro-data-display` both version-controlled and backed up on GitHub.

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
Superseded 2026-07-02: the quick tunnel is now persistent across normal API restarts.
Current URL is stored in `tmp/logs/tunnel_url.txt` and mirrored to `retro-data-display/.env`
only when Lovable sync is explicitly requested and needed.

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

## 2026-06-29 — Scanner pivot: browser limits → Android CameraX MVP (IN PROGRESS)

### Context
User wants fast phone scanning for Flesh and Blood cards without per-card button presses or
manual box adjustment. Initial browser scanner attempted to read the footer code such as
`C CRU117`, because that encodes edition/set/card number. Multiple browser changes were
pushed to `retro-data-display`, ending at:
- `bd1f746 feat: add live card scanner frame`
- `b25e0ed fix: scan full footer strip`
- `fba96b7 fix: improve scanner camera focus`

Browser debug mode saved crops to `tmp/scan_debug_samples/`. Inspection showed the crop was
finally on the footer strip, but the actual text pixels were still too soft/blurry for OCR.
Conclusion: the failure is camera acquisition/focus in mobile browser video, not Tesseract
parsing. Commercial apps like Dragon Shield/ManaBox likely use native camera control,
sharpest-frame selection, whole-card visual matching, OCR as secondary signal, and
confidence fusion.

### Backend changes (local, not pushed to remote)
- `api.py` now supports `debug_save` on `/scan`; saved scan images + `.txt` metadata go to
  `tmp/scan_debug_samples/`.
- Added `/scan/native` accepting:
  - `full_image` — full card crop for visual matching,
  - `footer_crop` — footer strip for exact `display_id` OCR,
  - `title_crop` — title area for fuzzy name OCR,
  - `debug_save`.
- Fusion order: footer OCR exact match first; then full-card visual match; then title OCR.
  If visual and title agree, confidence is boosted. Endpoint returns `confidence`,
  `method`, `display_id`, `name`, `matches`, `printings`, `candidates`, and debug paths.
- Guard added so all-letter OCR chunks like `CRUIIT...` no longer silently become `CRU111`.
- Verified: `.venv/bin/python -m py_compile api.py`.

### Android MVP scaffold
Created `fab-scanner-android/`:
- Kotlin + CameraX Android Studio project.
- `MainActivity.kt`: rear camera preview, card guide, tap/refocus, torch toggle, 2x zoom,
  RGBA frame analyzer, footer sharpness gate, full/footer/title crops, POST to
  `/scan/native`.
- `CardGuideView.kt`: card frame + bottom footer strip overlay.
- `README.md`: setup/run notes.
- `.gitignore` updated for Android build/editor artifacts.

### Toolchain status / tomorrow
- This machine has no `java`, no `gradle`, and no Android SDK.
- `sudo snap install android-studio --classic` was attempted, but sudo password prompt was
  not completed; install was cancelled.
- Tomorrow:
  1. Run `sudo snap install android-studio --classic`.
  2. Launch `android-studio`.
  3. Open `/home/tango/Projects/fab/fab-scanner-android`.
  4. Let Android Studio install SDK/Gradle dependencies.
  5. Set `apiBase` in `MainActivity.kt` to a phone-reachable URL (current cloudflared
     tunnel or LAN IP, not emulator-only `10.0.2.2`).
  6. Run on Android phone with USB debugging.
  7. Inspect `tmp/scan_debug_samples/` from native submissions to confirm image quality.

---

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
