"""
FAB API + Cloudflare quick-tunnel launcher (Linux).

Starts:
  1. The FAB API via uvicorn on port 8001 (it also serves a locally-built copy of
     the frontend from retro-data-display/dist, if present, for same-origin access).
  2. A Cloudflare quick tunnel (no account/domain needed) and prints the public
     https://<random>.trycloudflare.com URL.

Then it syncs the **Lovable** frontend: the app hosted by Lovable (GitHub repo
retro-data-display) runs on its own origin, so it needs the absolute API URL. We write
the live tunnel URL into retro-data-display/.env (VITE_API_BASE_URL) and push so Lovable
redeploys against the current API. The URL is also saved to tmp/logs/tunnel_url.txt.
Set PUSH_LOVABLE=0 to update .env locally without pushing.

The tunnel is PERSISTENT: cloudflared runs detached and outlives this script, so a
plain restart reuses the same URL (no Lovable push/rebuild). The URL only changes when
the tunnel itself is restarted. For a URL that never changes at all, use a named
Cloudflare tunnel (needs a domain).

cloudflared lookup order: ./tmp/bin/cloudflared, then `cloudflared` on PATH.
Get the Linux binary once with:
  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
       -o tmp/bin/cloudflared && chmod +x tmp/bin/cloudflared

Usage:
  python start_fab.py                # start/restart the API, reuse the live tunnel
  python start_fab.py --new-tunnel   # force a fresh tunnel URL (re-syncs Lovable once)
  python start_fab.py --stop-tunnel  # kill the persistent tunnel
"""

import os
import re
import sys
import time
import shutil
import threading
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_PORT = int(os.getenv("API_PORT", "8001"))

# Lovable frontend sync: the Lovable-hosted app (GitHub repo retro-data-display)
# reads VITE_API_BASE_URL from its .env at build time. Because it runs on a
# different origin it needs the absolute tunnel URL. On each new URL we rewrite that
# one line and push so Lovable redeploys against the live API. Only .env is staged —
# the working tree may carry unrelated drift we must not commit. Set PUSH_LOVABLE=0
# to update .env locally without pushing.
FRONTEND_DIR = HERE / "retro-data-display"
FRONTEND_ENV = FRONTEND_DIR / ".env"
URL_FILE     = HERE / "tmp" / "logs" / "tunnel_url.txt"
PUSH_LOVABLE = os.getenv("PUSH_LOVABLE", "1") != "0"

# The Cloudflare quick-tunnel runs as its own DETACHED, long-lived process so it
# survives API restarts — its URL then stays stable and we don't re-push to Lovable
# every time the server restarts. We track it by pidfile + log it to a file (not a
# pipe, so it can't deadlock when we stop reading).
CF_PIDFILE = HERE / "tmp" / "logs" / "cloudflared.pid"
CF_LOGFILE = HERE / "tmp" / "logs" / "cloudflared.out"

GREEN, CYAN, YELLOW, RED, BOLD, RESET = (
    "\033[32m", "\033[36m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
)
SEP = "-" * 50


def find_cloudflared():
    local = HERE / "tmp" / "bin" / "cloudflared"
    if local.exists():
        return str(local)
    return shutil.which("cloudflared")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def tunnel_status():
    """Return (pid, url) for a still-running persistent tunnel, else (None, None)."""
    try:
        pid = int(CF_PIDFILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None, None
    if not _pid_alive(pid):
        return None, None
    try:
        url = URL_FILE.read_text().strip() or None
    except FileNotFoundError:
        url = None
    return pid, url


def start_persistent_tunnel(cloudflared):
    """Spawn cloudflared DETACHED (survives this script + API restarts), wait for its
    URL, and record pid + URL. Returns the URL, or None on failure."""
    CF_LOGFILE.parent.mkdir(parents=True, exist_ok=True)
    logf = open(CF_LOGFILE, "w")
    proc = subprocess.Popen(
        [cloudflared, "tunnel", "--url", f"http://localhost:{API_PORT}", "--no-autoupdate"],
        stdout=logf, stderr=subprocess.STDOUT, cwd=HERE,
        start_new_session=True,          # detach: own session, immune to our Ctrl+C / exit
    )
    CF_PIDFILE.write_text(str(proc.pid))

    url, deadline = None, time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:      # died early
            break
        try:
            m = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", CF_LOGFILE.read_text())
            if m:
                url = m.group(0)
                break
        except FileNotFoundError:
            pass
        time.sleep(0.3)

    if url:
        URL_FILE.write_text(url + "\n")
    return url


def stop_tunnel():
    """Kill the persistent tunnel and clear its tracking files."""
    pid, _ = tunnel_status()
    if pid:
        try:
            os.kill(pid, 15)
            print(f"  {GREEN}ok{RESET}  Stopped tunnel (PID {pid})")
        except OSError as e:
            print(f"  {YELLOW}!!{RESET}  Could not stop tunnel PID {pid}: {e}")
    else:
        print(f"  {CYAN}->{RESET}  No running tunnel to stop.")
    CF_PIDFILE.unlink(missing_ok=True)


def stream_output(proc, prefix, suppress_re=None):
    for line in iter(proc.stdout.readline, b""):
        text = line.decode("utf-8", errors="replace").rstrip()
        if suppress_re and re.search(suppress_re, text):
            continue
        print(f"  {prefix} {text}")


def _read_env_url():
    try:
        for line in FRONTEND_ENV.read_text().splitlines():
            if line.startswith("VITE_API_BASE_URL="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return None


def _write_env_url(url):
    lines = FRONTEND_ENV.read_text().splitlines() if FRONTEND_ENV.exists() else []
    out, found = [], False
    for line in lines:
        if line.startswith("VITE_API_BASE_URL="):
            out.append(f"VITE_API_BASE_URL={url}"); found = True
        else:
            out.append(line)
    if not found:
        out.append(f"VITE_API_BASE_URL={url}")
    FRONTEND_ENV.write_text("\n".join(out) + "\n")


def _git(*args):
    # GIT_TERMINAL_PROMPT=0 makes auth failures fail FAST instead of blocking on an
    # interactive username/password prompt (GitHub rejects passwords anyway — pushing
    # over HTTPS needs `gh auth setup-git` so git uses the gh token as a credential helper).
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=FRONTEND_DIR,
                          capture_output=True, text=True, env=env)


def sync_lovable(tunnel_url):
    """Point the Lovable frontend at the live API URL and push so it redeploys.
    Records the URL to tmp/logs/tunnel_url.txt and stages ONLY .env (the working
    tree may carry unrelated changes we must not commit)."""
    try:
        URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        URL_FILE.write_text(tunnel_url + "\n")
    except Exception:
        pass

    if not FRONTEND_ENV.exists():
        return
    if _read_env_url() == tunnel_url:
        print(f"  {GREEN}ok{RESET}  Lovable .env already points at this URL")
        return

    _write_env_url(tunnel_url)
    print(f"  {GREEN}ok{RESET}  Updated retro-data-display/.env → {tunnel_url}")

    if not PUSH_LOVABLE:
        print(f"  {YELLOW}!!{RESET}  PUSH_LOVABLE=0 — not pushing; commit .env yourself to deploy.")
        return

    if _git("add", ".env").returncode != 0:
        print(f"  {YELLOW}!!{RESET}  git add .env failed — push .env manually to deploy.")
        return
    _git("commit", "-m", f"chore: point API at {tunnel_url}")
    push = _git("push", "origin", "main")

    # Verify the commit actually reached the remote — a push can "succeed" partially or
    # the tunnel restart can interrupt the run, leaving Lovable on a dead URL. Compare the
    # local commit to what the remote reports for main.
    local = _git("rev-parse", "HEAD").stdout.strip()
    remote = _git("ls-remote", "origin", "-h", "refs/heads/main").stdout.split()
    pushed = push.returncode == 0 and remote and remote[0] == local

    if pushed:
        print(f"  {GREEN}ok{RESET}  Pushed to GitHub — Lovable will redeploy (click Sync/Deploy if needed)")
        return

    # Loud, actionable failure — this is the difference between a working and a dead frontend.
    err = (push.stderr or "").strip()
    print(f"  {RED}!!{RESET}  PUSH FAILED — the Lovable frontend is still pointing at a DEAD tunnel URL.")
    if err:
        print(f"       git said: {err[:200]}")
    if "Authentication" in err or "could not read" in err or "terminal prompts disabled" in err:
        print(f"  {YELLOW}->{RESET}  Auth not set up. GitHub rejects passwords; run this ONCE:")
        print(f"           gh auth setup-git")
    print(f"  {YELLOW}->{RESET}  Then push manually: cd retro-data-display && git push origin main")


def main():
    args = set(sys.argv[1:])

    # `--stop-tunnel` tears down the persistent tunnel and exits (next start gets a new URL).
    if "--stop-tunnel" in args:
        stop_tunnel()
        return

    print(f"\n{CYAN}{BOLD}  FAB Matrix - Starting services{RESET}")
    print(f"{CYAN}{SEP}{RESET}\n")

    # -- 1. Start the API (uvicorn) --
    print(f"  {CYAN}->{RESET}  Starting FAB API on port {API_PORT}...")
    api_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app",
         "--host", "0.0.0.0", "--port", str(API_PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=HERE,
    )
    threading.Thread(
        target=stream_output, args=(api_proc, f"{GREEN}[API]{RESET}"), daemon=True
    ).start()
    time.sleep(2)
    if api_proc.poll() is not None:
        print(f"  {RED}x{RESET}  API failed to start. Check api.py for errors.")
        sys.exit(1)
    print(f"  {GREEN}ok{RESET}  API running (PID {api_proc.pid})")

    # -- 2. Reuse the persistent tunnel if it's still up; else start a fresh one --
    # The tunnel is detached and outlives this script, so a plain restart keeps the
    # SAME URL — sync_lovable then sees an unchanged .env and skips the push (no rebuild).
    # Force a new URL with `--new-tunnel`.
    if "--new-tunnel" in args:
        stop_tunnel()

    tunnel_url = None
    pid, existing = tunnel_status()
    if existing:
        tunnel_url = existing
        print(f"  {GREEN}ok{RESET}  Reusing live tunnel (PID {pid}) — URL unchanged, no Lovable rebuild")
    else:
        cloudflared = find_cloudflared()
        if not cloudflared:
            print(f"  {RED}x{RESET}  cloudflared not found. See the header of this file "
                  f"for the one-line download command.")
            api_proc.terminate()
            sys.exit(1)
        print(f"  {CYAN}->{RESET}  Starting Cloudflare tunnel (persistent)...")
        tunnel_url = start_persistent_tunnel(cloudflared)
        if not tunnel_url:
            print(f"  {RED}x{RESET}  Could not read tunnel URL. See {CF_LOGFILE}")
            api_proc.terminate()
            sys.exit(1)

    print()
    print(f"  {GREEN}ok{RESET}  Public URL: {BOLD}{tunnel_url}{RESET}")
    print(f"  {CYAN}->{RESET}  Tunnel persists across restarts (stop it with: "
          f"python start_fab.py --stop-tunnel)")
    print()

    # Point the Lovable-hosted frontend at this URL and push. No-ops when the URL is
    # unchanged (the common restart case) — so no redeploy churn.
    sync_lovable(tunnel_url)

    print()
    print(f"{CYAN}{SEP}{RESET}")
    print(f"  {GREEN}{BOLD}API running. Ctrl+C stops the API; the tunnel stays up.{RESET}")
    print(f"{CYAN}{SEP}{RESET}\n")

    try:
        while True:
            if api_proc.poll() is not None:
                print(f"\n  {RED}x{RESET}  API process exited unexpectedly")
                break
            if tunnel_status()[0] is None:
                # Tunnel died — warn but DON'T kill the API; next start re-establishes it.
                print(f"\n  {YELLOW}!!{RESET}  Tunnel went down — restart to re-establish it.")
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print(f"\n  {CYAN}->{RESET}  Stopping API (tunnel left running)...")
    finally:
        api_proc.terminate()
        print(f"  {GREEN}ok{RESET}  API stopped. Tunnel still up — "
              f"`--stop-tunnel` to kill it.")


if __name__ == "__main__":
    main()
