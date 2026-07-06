"""
FAB API + Cloudflare quick-tunnel launcher (Linux).

Starts:
  1. The FAB API via uvicorn on port 8001 (it also serves a locally-built copy of
     the frontend from retro-data-display/dist, if present, for same-origin access).
  2. A Cloudflare quick tunnel (no account/domain needed) and prints the public
     https://<random>.trycloudflare.com URL.

It records the live tunnel URL in tmp/logs/tunnel_url.txt. Lovable/GitHub sync is
explicitly opt-in: pass --sync-lovable or set PUSH_LOVABLE=1 to rewrite
retro-data-display/.env, commit it, and push so Lovable redeploys.

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
  python start_fab.py --sync-lovable # also push retro-data-display/.env for Lovable
  python start_fab.py --new-tunnel   # force a fresh tunnel URL
  python start_fab.py --stop-tunnel  # kill the persistent tunnel
"""

import os
import re
import sys
import time
import shutil
import signal
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_PORT = int(os.getenv("API_PORT", "8001"))

# Lovable frontend sync: the Lovable-hosted app (GitHub repo retro-data-display)
# reads VITE_API_BASE_URL from its .env at build time. Because it runs on a
# different origin it needs the absolute tunnel URL. On each new URL we rewrite that
# one line and push so Lovable redeploys against the live API. Only .env is staged —
# the working tree may carry unrelated drift we must not commit. This is opt-in
# because startup must not depend on GitHub auth or Lovable deploy state.
FRONTEND_DIR = HERE / "retro-data-display"
FRONTEND_ENV = FRONTEND_DIR / ".env"
FRONTEND_DIST = FRONTEND_DIR / "dist"
URL_FILE     = HERE / "tmp" / "logs" / "tunnel_url.txt"
PUSH_LOVABLE = os.getenv("PUSH_LOVABLE", "0") == "1"
GIT_TIMEOUT = int(os.getenv("GIT_TIMEOUT", "30"))
GIT_PUSH_TIMEOUT = int(os.getenv("GIT_PUSH_TIMEOUT", "60"))

# Native scanner endpoint discovery: the Android app has no way to know the current
# ephemeral trycloudflare URL, so on every start we publish the live URL to a public
# GitHub gist and the app fetches it at launch. Unlike the Lovable push, a gist edit
# triggers no rebuild, so this runs on EVERY start (not gated by PUSH_LOVABLE) and the
# app never needs to be re-pointed. Raw (always-latest) URL the app polls:
#   https://gist.githubusercontent.com/GHT4ngo/<id>/raw/endpoint.txt
ENDPOINT_GIST_ID = os.getenv("ENDPOINT_GIST_ID", "84b51c1df1551685fb9b151f684d979d")

# The Cloudflare quick-tunnel runs as its own DETACHED, long-lived process so it
# survives API restarts — its URL then stays stable and we don't re-push to Lovable
# every time the server restarts. We track it by pidfile + log it to a file (not a
# pipe, so it can't deadlock when we stop reading).
CF_PIDFILE = HERE / "tmp" / "logs" / "cloudflared.pid"
CF_LOGFILE = HERE / "tmp" / "logs" / "cloudflared.out"

# Named tunnel (2026-07-06): with ~/.cloudflared/config.yml present (tunnel `fab`,
# created via `cloudflared tunnel login` + `tunnel create fab` + `tunnel route dns`),
# the public URL is PERMANENT — no more trycloudflare URL churn, Lovable resyncs, or
# zombie-URL replacement. The quick-tunnel path below remains as a fallback if the
# named config is ever missing.
NAMED_TUNNEL      = os.getenv("NAMED_TUNNEL", "fab")
NAMED_TUNNEL_URL  = os.getenv("NAMED_TUNNEL_URL", "https://fabmatrix.t4ngo.com")
NAMED_TUNNEL_CONF = Path.home() / ".cloudflared" / "config.yml"

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


def _find_cloudflared_tunnel_pid():
    needles = (
        f"cloudflared tunnel --url http://localhost:{API_PORT}",  # quick tunnel
        f"tunnel run {NAMED_TUNNEL}",                              # named tunnel
    )
    try:
        proc = subprocess.run(
            ["pgrep", "-af", "cloudflared"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in proc.stdout.splitlines():
        if any(n in line for n in needles):
            try:
                return int(line.split(None, 1)[0])
            except (ValueError, IndexError):
                continue
    return None


def tunnel_reachable(url, timeout=8):
    """True if the public tunnel URL actually serves traffic — not just that the
    cloudflared PID is alive. Quick tunnels routinely keep their process running while
    the edge 'control stream' has died, so a live PID does NOT mean a working tunnel.
    Any HTTP response (even a 4xx/5xx) proves the edge is up; only a connection-level
    failure (refused/timeout/DNS) means the tunnel is dead."""
    if not url:
        return False
    try:
        with urllib.request.urlopen(url + "/stats", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True   # edge answered with a status code → tunnel itself is alive
    except Exception:
        return False


def tunnel_status():
    """Return (pid, url) for a still-running persistent tunnel, else (None, None)."""
    try:
        url = URL_FILE.read_text().strip() or None
    except FileNotFoundError:
        url = None

    try:
        pid = int(CF_PIDFILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        pid = None
    if pid and _pid_alive(pid):
        return pid, url

    recovered_pid = _find_cloudflared_tunnel_pid()
    if recovered_pid and url:
        CF_PIDFILE.write_text(str(recovered_pid))
        return recovered_pid, url
    if pid:
        CF_PIDFILE.unlink(missing_ok=True)
    return None, None


def start_persistent_tunnel(cloudflared):
    """Spawn cloudflared DETACHED (survives this script + API restarts), wait for its
    URL, and record pid + URL. Returns the URL, or None on failure.

    Named-tunnel mode (config.yml present): runs `tunnel run <name>` and the URL is
    the fixed NAMED_TUNNEL_URL — verified reachable before being accepted."""
    CF_LOGFILE.parent.mkdir(parents=True, exist_ok=True)
    logf = open(CF_LOGFILE, "w")

    if NAMED_TUNNEL_CONF.exists():
        proc = subprocess.Popen(
            [cloudflared, "tunnel", "run", NAMED_TUNNEL],
            stdout=logf, stderr=subprocess.STDOUT, cwd=HERE,
            start_new_session=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"  {RED}x{RESET}  Named tunnel died on start — see {CF_LOGFILE}")
                return None
            if tunnel_reachable(NAMED_TUNNEL_URL, timeout=4):
                CF_PIDFILE.write_text(str(proc.pid))
                URL_FILE.write_text(NAMED_TUNNEL_URL + "\n")
                return NAMED_TUNNEL_URL
            time.sleep(1)
        try:
            proc.terminate()
        except OSError:
            pass
        print(f"  {RED}x{RESET}  Named tunnel never became reachable at {NAMED_TUNNEL_URL}")
        return None

    proc = subprocess.Popen(
        [cloudflared, "tunnel", "--url", f"http://localhost:{API_PORT}", "--no-autoupdate"],
        stdout=logf, stderr=subprocess.STDOUT, cwd=HERE,
        start_new_session=True,          # detach: own session, immune to our Ctrl+C / exit
    )

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
        CF_PIDFILE.write_text(str(proc.pid))
        URL_FILE.write_text(url + "\n")
    else:
        try:
            proc.terminate()
        except OSError:
            pass
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


def _port_pids(port):
    """Return PIDs listening on a TCP port, using ss when available."""
    try:
        proc = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    pids = set()
    for line in proc.stdout.splitlines():
        if f":{port} " not in line:
            continue
        for pid in re.findall(r"pid=(\d+)", line):
            pids.add(int(pid))
    return sorted(pids)


def _cmdline(pid):
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def stop_stale_api_on_port():
    """Stop only the uvicorn api:app process that owns API_PORT; leave cloudflared alone."""
    pids = _port_pids(API_PORT)
    if not pids:
        return

    unknown = []
    targets = []
    for pid in pids:
        cmd = _cmdline(pid)
        if "uvicorn" in cmd and "api:app" in cmd:
            targets.append(pid)
        else:
            unknown.append((pid, cmd or "<unknown>"))

    if unknown:
        print(f"  {RED}x{RESET}  Port {API_PORT} is busy, but not by this FAB API:")
        for pid, cmd in unknown:
            print(f"       PID {pid}: {cmd}")
        print(f"  {YELLOW}->{RESET}  Stop that process or set API_PORT before restarting.")
        sys.exit(1)

    for pid in targets:
        print(f"  {CYAN}->{RESET}  Stopping stale FAB API on port {API_PORT} (PID {pid})...")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue

    deadline = time.time() + 6
    while time.time() < deadline:
        if not any(_pid_alive(pid) for pid in targets):
            print(f"  {GREEN}ok{RESET}  Cleared stale API process")
            return
        time.sleep(0.25)

    for pid in targets:
        if _pid_alive(pid):
            print(f"  {YELLOW}!!{RESET}  Stale API PID {pid} did not stop; killing it.")
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


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


def _git(*args, timeout=None):
    # GIT_TERMINAL_PROMPT=0 makes auth failures fail FAST instead of blocking on an
    # interactive username/password prompt (GitHub rejects passwords anyway — pushing
    # over HTTPS needs `gh auth setup-git` so git uses the gh token as a credential helper).
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    timeout = GIT_TIMEOUT if timeout is None else timeout
    try:
        return subprocess.run(["git", *args], cwd=FRONTEND_DIR,
                              capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", *args], 124, "", f"timed out after {timeout}s"
        )


def publish_endpoint(tunnel_url):
    """Publish the live tunnel URL to a public gist so the native scanner app can
    auto-discover the backend (no rebuild, no re-typing the URL). Best-effort — a
    failure here never blocks startup; the app just keeps its last known URL."""
    if not ENDPOINT_GIST_ID:
        return
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(tunnel_url + "\n")
            tmp_path = f.name
        res = subprocess.run(
            ["gh", "gist", "edit", ENDPOINT_GIST_ID, "-f", "endpoint.txt", tmp_path],
            capture_output=True, text=True, timeout=GIT_PUSH_TIMEOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if res.returncode == 0:
            print(f"  {GREEN}ok{RESET}  App endpoint published → gist (app auto-discovers this URL)")
        else:
            err = (res.stderr or res.stdout or "").strip()
            print(f"  {YELLOW}!!{RESET}  Could not update app endpoint gist — app may keep its old URL.")
            if err:
                print(f"       gh said: {err[:200]}")
    except FileNotFoundError:
        print(f"  {YELLOW}!!{RESET}  gh CLI not found — skipped app endpoint publish.")
    except subprocess.TimeoutExpired:
        print(f"  {YELLOW}!!{RESET}  Timed out publishing app endpoint gist.")
    except Exception:
        pass
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def sync_lovable(tunnel_url):
    """Record the live API URL; optionally push it for Lovable when explicitly enabled."""
    try:
        URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        URL_FILE.write_text(tunnel_url + "\n")
    except Exception:
        pass

    # Push to Lovable when explicitly requested OR whenever the URL actually changed —
    # a changed URL means the Lovable frontend is now pointed at a dead tunnel, which is
    # the exact "restarted but frontend can't connect" failure. This still causes no
    # rebuild churn on a normal same-URL restart (url_changed is False → we return).
    url_changed = FRONTEND_ENV.exists() and _read_env_url() != tunnel_url
    if not PUSH_LOVABLE and not url_changed:
        print(f"  {GREEN}ok{RESET}  Lovable .env already matches this URL — no sync needed")
        return
    if not PUSH_LOVABLE and url_changed:
        print(f"  {CYAN}->{RESET}  Tunnel URL changed — syncing Lovable so the frontend isn't left stale.")

    if not FRONTEND_ENV.exists():
        print(f"  {YELLOW}!!{RESET}  Lovable sync requested, but retro-data-display/.env is missing.")
        return
    if _read_env_url() == tunnel_url:
        print(f"  {GREEN}ok{RESET}  Lovable .env already points at this URL")
        return

    _write_env_url(tunnel_url)
    print(f"  {GREEN}ok{RESET}  Updated retro-data-display/.env → {tunnel_url}")

    if _git("add", ".env").returncode != 0:
        print(f"  {YELLOW}!!{RESET}  git add .env failed — push .env manually to deploy.")
        return
    if _git("diff", "--cached", "--quiet", "--", ".env").returncode == 0:
        print(f"  {GREEN}ok{RESET}  Lovable .env is staged but unchanged")
        return

    commit = _git("commit", "-m", f"chore: point API at {tunnel_url}")
    if commit.returncode != 0:
        err = (commit.stderr or commit.stdout or "").strip()
        print(f"  {YELLOW}!!{RESET}  Could not commit Lovable .env; API is still running.")
        if err:
            print(f"       git said: {err[:240]}")
        print(f"  {YELLOW}->{RESET}  Manual deploy: cd retro-data-display && git add .env && git commit -m 'chore: point API at {tunnel_url}' && git push origin main")
        return

    push = _git("push", "origin", "main", timeout=GIT_PUSH_TIMEOUT)

    # Verify the commit actually reached the remote — a push can "succeed" partially or
    # the tunnel restart can interrupt the run, leaving Lovable on a dead URL. Compare the
    # local commit to what the remote reports for main.
    local = _git("rev-parse", "HEAD").stdout.strip()
    remote = _git("ls-remote", "origin", "-h", "refs/heads/main", timeout=GIT_TIMEOUT).stdout.split()
    pushed = push.returncode == 0 and remote and remote[0] == local

    if pushed:
        print(f"  {GREEN}ok{RESET}  Pushed to GitHub — Lovable will redeploy (click Sync/Deploy if needed)")
        return

    # Loud, actionable failure — this is the difference between a working and a dead frontend.
    err = (push.stderr or "").strip()
    print(f"  {RED}!!{RESET}  PUSH FAILED — the API is online, but Lovable may still point at the old tunnel URL.")
    if err:
        print(f"       git said: {err[:240]}")
    if "Authentication" in err or "could not read" in err or "terminal prompts disabled" in err:
        print(f"  {YELLOW}->{RESET}  Auth not set up. GitHub rejects passwords; run this ONCE:")
        print(f"           gh auth setup-git")
    print(f"  {YELLOW}->{RESET}  Manual deploy: update Lovable to {tunnel_url}, or run:")
    print(f"           cd retro-data-display && git push origin main")


def main():
    args = set(sys.argv[1:])
    global PUSH_LOVABLE
    if "--sync-lovable" in args:
        PUSH_LOVABLE = True

    # `--stop-tunnel` tears down the persistent tunnel and exits (next start gets a new URL).
    if "--stop-tunnel" in args:
        stop_tunnel()
        return

    print(f"\n{CYAN}{BOLD}  FAB Matrix - Starting services{RESET}")
    print(f"{CYAN}{SEP}{RESET}\n")

    # -- 1. Start the API (uvicorn) --
    stop_stale_api_on_port()
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
    if existing and tunnel_reachable(existing):
        tunnel_url = existing
        print(f"  {GREEN}ok{RESET}  Reusing live tunnel (PID {pid}) — URL unchanged, no Lovable rebuild")
    else:
        if existing:
            # PID alive but the URL doesn't serve — a stale/zombie quick tunnel. Reusing
            # it is exactly what leaves the frontend "connected to nothing" after a
            # restart. Tear it down so we spawn a fresh, working one (new URL → synced).
            print(f"  {YELLOW}!!{RESET}  Tunnel process is alive but not serving (dead edge) — replacing it.")
            stop_tunnel()
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
    if FRONTEND_DIST.is_dir():
        print(f"  {GREEN}ok{RESET}  Web app:     {BOLD}{tunnel_url}/{RESET}")
        print(f"  {GREEN}ok{RESET}  Scanner:     {BOLD}{tunnel_url}/scan{RESET}")
        print(f"  {GREEN}ok{RESET}  Admin:       {BOLD}{tunnel_url}/admin{RESET}")
    print(f"  {CYAN}->{RESET}  Tunnel persists across restarts (stop it with: "
          f"python start_fab.py --stop-tunnel)")
    if not PUSH_LOVABLE:
        print(f"  {CYAN}->{RESET}  Local app mode: Lovable/GitHub sync is disabled.")
    print()

    # Publish the live URL for the native scanner app to auto-discover (every start —
    # a gist edit triggers no rebuild, so the phone never needs re-pointing).
    publish_endpoint(tunnel_url)

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
