#!/usr/bin/env bash
# FaB launcher — one entry point to run the data pipeline and/or (re)start the server.
#
# Modes:
#   ./run_pipeline.sh              Full pipeline (ingest + dbt) then serve.
#                                  Skips ingest/dbt if it already ran today.
#   ./run_pipeline.sh --full       Force the full pipeline even if it ran today.
#   ./run_pipeline.sh --restart    Just (re)start the server — skip ingest/dbt.
#   ./run_pipeline.sh --no-serve   Run the pipeline only; don't start the server.
#   ./run_pipeline.sh --local-app  Build/serve the frontend from FastAPI and skip Lovable sync.
#   ./run_pipeline.sh --sync-lovable
#                                  Also commit/push retro-data-display/.env for Lovable.
#                                  This is automatic for daily full pipeline runs.
#   ./run_pipeline.sh --no-sync-lovable
#                                  Skip Lovable sync even after a full pipeline run.
#   ./run_pipeline.sh --new-tunnel (Re)start with a fresh tunnel URL.
#   ./run_pipeline.sh --stop       Stop the persistent Cloudflare tunnel.
#   ./run_pipeline.sh --help
#
# The Cloudflare tunnel is persistent (see start_fab.py): a plain --restart reuses the
# same URL. Normal pipeline runs sync Lovable when needed; quick restarts do not.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
DBT=.venv/bin/dbt
MARKER=tmp/logs/.pipeline_done

# Print the leading comment block (from line 2 to the first non-comment line).
usage() { awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; }

# ── Parse args ────────────────────────────────────────────────────────────────
SERVE=1
PIPELINE=auto          # auto | force | skip
QUICK_RESTART=0
NEW_TUNNEL=0
LOCAL_APP=0
SYNC_LOVABLE=auto      # auto | yes | no
for arg in "$@"; do
  case "$arg" in
    --restart|--serve-only) PIPELINE=skip; QUICK_RESTART=1 ;;
    --no-serve)             SERVE=0; PIPELINE=force ;;
    --local-app)            LOCAL_APP=1 ;;
    --sync-lovable)         SYNC_LOVABLE=yes ;;
    --no-sync-lovable)      SYNC_LOVABLE=no ;;
    --full|--force)         PIPELINE=force ;;
    --new-tunnel)           NEW_TUNNEL=1 ;;
    --stop|--stop-tunnel)   exec "$PY" start_fab.py --stop-tunnel ;;
    -h|--help)              usage; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
  esac
done

# Export .env so dbt's env_var() sees the same values the Python scripts load.
set -a; [ -f .env ] && . ./.env; set +a

[ -x "$PY" ] || { echo "No venv at $PY — create it first (see README)."; exit 1; }

db_is_listening() {
  if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${PG_PORT:-5432} "; then
    return 0
  fi
  "$PY" -c 'import os, socket
host = os.getenv("PG_HOST", "localhost")
port = int(os.getenv("PG_PORT", "5432"))
with socket.create_connection((host, port), timeout=1):
    pass' >/dev/null 2>&1
}

echo "================================================"
echo "  FaB launcher  $(date '+%Y-%m-%d %H:%M')"
echo "================================================"

# Run the pipeline today already? In auto mode, skip the slow ingest/dbt if so.
if [ "$PIPELINE" = "auto" ]; then
  if [ -f "$MARKER" ] && [ "$(cat "$MARKER" 2>/dev/null || true)" = "$(date +%F)" ]; then
    echo "Pipeline already ran today — skipping ingest/dbt (use --full to force)."
    PIPELINE=skip
  else
    PIPELINE=force
  fi
fi

# Postgres is needed for both the pipeline and the API. For a restart, avoid
# Docker/sudo entirely if the DB is already reachable.
if db_is_listening; then
  echo "-> Postgres already reachable — skipping Docker."
else
  echo "-> Postgres (Docker)..."
  if docker info >/dev/null 2>&1; then
    docker compose up -d --wait db
  else
    sudo docker compose up -d --wait db
  fi
fi

if [ "$PIPELINE" = "force" ]; then
  echo "-> Bronze catalogue (cards, sets, Cardmarket)..."
  "$PY" ingest_bronze.py --no-justtcg

  # tcgcsv.com — free, no-rate-limit TCGplayer mirror. Primary source for USD prices
  # and for cards in sets the-fab-cube is missing. (JustTCG is a dormant backup, run
  # by hand if ever needed.)
  echo "-> tcgcsv — cards + USD prices (all sets)..."
  "$PY" ingest_tcgcsv.py || echo "   (tcgcsv fetch failed — continuing with existing data)"

  echo "-> dbt transformations..."
  ( cd fab_dbt && "../$DBT" run --profiles-dir . )

  mkdir -p "$(dirname "$MARKER")"; date +%F > "$MARKER"
  echo "-> Pipeline complete."
fi

# ── Serve ─────────────────────────────────────────────────────────────────────
if [ "$SERVE" = "1" ]; then
  if [ "$SYNC_LOVABLE" = "auto" ]; then
    if [ "$QUICK_RESTART" = "1" ]; then
      SYNC_LOVABLE=no
    else
      SYNC_LOVABLE=yes
    fi
  fi

  export PUSH_LOVABLE=0
  if [ "$SYNC_LOVABLE" = "yes" ]; then
    export PUSH_LOVABLE=1
  fi

  if [ "$LOCAL_APP" = "1" ]; then
    echo "-> Frontend build (same-origin, no Lovable sync)..."
    VITE_API_BASE_URL= npm --prefix retro-data-display run build
    export PUSH_LOVABLE=0
  fi

  if [ "$NEW_TUNNEL" = "1" ]; then
    echo "-> Starting server (API + FRESH tunnel)..."
    if [ "$SYNC_LOVABLE" = "yes" ]; then
      exec "$PY" start_fab.py --new-tunnel --sync-lovable
    else
      exec "$PY" start_fab.py --new-tunnel
    fi
  else
    echo "-> Starting server (API + persistent tunnel)..."
    if [ "$SYNC_LOVABLE" = "yes" ]; then
      exec "$PY" start_fab.py --sync-lovable
    else
      exec "$PY" start_fab.py
    fi
  fi
else
  echo "Done (not serving)."
fi
