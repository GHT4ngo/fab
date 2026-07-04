# AGENTS.md — Codex handoff notes

Read this together with `README.md`, `CLAUDE.md`, and `agent_worklog.md`.

## Current repo shape
- `/home/tango/Projects/fab` is the backend/data workspace, but it is not currently a
  normal git repo from Codex's point of view.
- `retro-data-display/` is the GitHub/Lovable frontend repo:
  `GHT4ngo/retro-data-display`.
- `fab-scanner-android/` is a local Android CameraX scanner MVP. Android Studio/SDK are
  installed enough for Gradle/ADB checks and USB installs.
- Backend/dbt changes must be preserved locally unless the user later puts the root
  project under a real Git remote.

## Git safety
- The frontend working tree has many modified files unrelated to the admin work. Do not
  bulk commit or reset them.
- When pushing frontend work, stage only the intended files.
- Cloudflare tunnel is **persistent** (detached, pidfile `tmp/logs/cloudflared.pid`): it
  survives API restarts. Current URL is in `tmp/logs/tunnel_url.txt`.
- `start_fab.py` no longer lets GitHub/Lovable sync block API startup. Lovable sync is
  explicit in `start_fab.py` (`--sync-lovable` / `PUSH_LOVABLE=1`) and automatic for the
  normal daily `./run_pipeline.sh` path. Quick `./run_pipeline.sh --restart` does NOT push.
- Use `./run_pipeline.sh` for the daily data refresh + serve + Lovable sync. Use
  `./run_pipeline.sh --restart` only to bring the API/tunnel back online quickly.
- `--new-tunnel` forces a fresh URL, `--stop-tunnel` kills it. HTTPS pushes need
  `gh auth setup-git` (done). The parent `fab` repo is on GitHub at GHT4ngo/fab (public).

## Verified commands
```bash
sudo docker compose up -d db
.venv/bin/python ingest_tcgcsv.py
( set -a; . ./.env; set +a; cd fab_dbt && ../.venv/bin/dbt run --profiles-dir . )
( set -a; . ./.env; set +a; cd fab_dbt && ../.venv/bin/dbt test --profiles-dir . )
npm --prefix retro-data-display run build
.venv/bin/python -m py_compile api.py
JAVA_HOME=/snap/android-studio/232/jbr GRADLE_USER_HOME=/home/tango/Projects/fab/fab-scanner-android/.gradle ./gradlew :app:assembleDebug
```

If port 8001 is busy:
```bash
ss -ltnp | rg ':8001'
ps -ef | rg 'start_fab|uvicorn api:app|cloudflared tunnel'
```
Stop a stale `start_fab.py`/uvicorn before restarting — but leave `cloudflared` running
(it's the persistent tunnel; killing it changes the URL and forces a Lovable rebuild).

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
- Scanner page pairing UI was pushed to `retro-data-display` at commit
  `a8e53ba feat: simplify scanner pairing UI`. Lovable may take a minute to redeploy.
- Browser scanner remains as fallback, but native Android scanning is the current direction.
- The tunnel is persistent now (see Handoff notes): `tmp/logs/tunnel_url.txt` holds the
  current URL.
- Admin page expects current backend endpoints:
  `/stats`, `/admin/sets`, `/admin/quality`, `/admin/price-discrepancies`,
  `/admin/unmatched`.

## Native scanner MVP
- Backend has a local `/scan/native` endpoint accepting `full_image`, `footer_crop`,
  `title_crop`, `debug_save`, and optional `session_code`; it records confident scans in
  `app.scanned_cards`.
- Footer code is the primary decision path. Android sends a broad lower footer band and
  backend `_read_footer_code()` searches left/center/wide/lower sub-windows for a real
  `display_id`. It corrects OCR confusions such as `R05` -> `ROS` and `CCRU117` -> `CRU117`.
- Backend now refuses partial collector numbers (e.g. `BET7` no longer snaps to `BET007`)
  and does not accept visual-only guesses. It returns a match when footer code resolves,
  title is very strong, or visual+title agree.
- Debug crops/metadata save under `tmp/scan_debug_samples/`.
- Android MVP files live under `fab-scanner-android/`.
- Android Studio/SDK are now present at least enough for local Gradle/ADB checks:
  `/home/tango/Android/Sdk/platform-tools/adb` and `/snap/android-studio/232/jbr/bin/java`.
- Current Android debug APK has been built and installed via USB at least once. The phone
  UI hides the API URL by default; users enter only a web `Pair phone` code. Advanced mode
  reveals the API URL for recovery.
- Lightweight scanner deps installed in `.venv`: `numpy`, `opencv-python-headless`,
  `pytesseract`. `easyocr` is intentionally not installed because it pulls a huge
  PyTorch/CUDA stack; title OCR fallback may be unavailable unless explicitly installed.
- Trade-session flow is still local/lightweight, not real user auth. Web creates a
  session code with `/scan/session`; phone submits that code; web polls `/scan/records`.
- Wireless debugging gotcha: when phone pairing is lost/stale, the user has found that a
  full computer restart is required before the phone can be paired again. Do not spend time
  repeatedly trying `adb pair` against stale state before rebooting.
- 2026-07-01 latest failed build/install attempt broke the phone pairing immediately. The
  user needs to restart the computer before trying the next Android build.
- `fab-scanner-android/debug_phone.sh` was fixed to launch the debug application id
  `com.fabscanner.app.debug` with activity `com.fabscanner.app.MainActivity`; previous
  package mismatch could cause misleading component/start failures.
