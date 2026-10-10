#!/usr/bin/env bash
# Start a throwaway DroppedNeedle server from source for end-to-end testing.
#
#   scripts/e2e/run_server.sh            # start (clones + installs on first run)
#   scripts/e2e/run_server.sh stop       # stop it
#
# Environment (all optional):
#   DN_REF      git tag/branch to run            (default v2.15.0)
#   DN_SRC      where the DroppedNeedle clone lives (default .cache/droppedneedle)
#   DN_ROOT     data root: cache/, config/, plugins/, music/ (default .cache/dnroot)
#   DN_PORT     HTTP port                        (default 18688)
#   PLUGIN_SRC  plugin folder to copy into $DN_ROOT/plugins/mbid-artist-merge
#               before start (default: repo root). Set to "" to skip.
#
# Needs: git, Python 3.13 (uv preferred, else python3.13 -m venv + pip), curl.
# Linux/macOS, or Windows with Git Bash: DroppedNeedle targets Linux, so on
# Windows the venv gets the small shim in scripts/e2e/windows/ plus tzdata.
set -euo pipefail

WINDOWS=0
case "$(uname -s)" in MINGW* | MSYS* | CYGWIN*) WINDOWS=1 ;; esac
native_path() { if ((WINDOWS)); then cygpath -m "$1"; else printf '%s\n' "$1"; fi; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DN_REF="${DN_REF:-v2.15.0}"
DN_SRC="${DN_SRC:-$REPO_ROOT/.cache/droppedneedle}"
DN_ROOT="$(native_path "${DN_ROOT:-$REPO_ROOT/.cache/dnroot}")"
DN_PORT="${DN_PORT:-18688}"
PLUGIN_SRC="${PLUGIN_SRC-$REPO_ROOT}"
VENV="$DN_SRC/.venv"
PIDFILE="$DN_ROOT/server.pid" # Linux: process group id; Windows: Windows pid
if ((WINDOWS)); then PY="$VENV/Scripts/python.exe"; else PY="$VENV/bin/python"; fi

running() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid
  pid="$(cat "$PIDFILE")"
  if ((WINDOWS)); then
    tasklist //FI "PID eq $pid" //NH 2>/dev/null | grep -q "$pid"
  else
    kill -0 -- "-$pid" 2>/dev/null
  fi
}

stop_server() {
  if [[ ! -f "$PIDFILE" ]]; then
    echo "no server pid file at $PIDFILE"
    return
  fi
  local pid
  pid="$(cat "$PIDFILE")"
  if ((WINDOWS)); then
    taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
  else
    kill -TERM -- "-$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 -- "-$pid" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL -- "-$pid" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
  echo "server stopped"
}

if [[ "${1:-}" == "stop" ]]; then
  stop_server
  exit 0
fi

if [[ ! -d "$DN_SRC/.git" ]]; then
  git clone --depth 1 --branch "$DN_REF" https://github.com/DroppedNeedle/DroppedNeedle.git "$DN_SRC"
fi

if [[ ! -x "$PY" ]]; then
  if command -v uv >/dev/null; then
    uv venv -q "$VENV" --python 3.13
    VIRTUAL_ENV="$VENV" uv pip install -q -r "$DN_SRC/backend/requirements.txt"
  else
    python3.13 -m venv "$VENV"
    "$PY" -m pip install -q -r "$DN_SRC/backend/requirements.txt"
  fi
fi
if ((WINDOWS)); then
  site="$VENV/Lib/site-packages"
  cp "$REPO_ROOT/scripts/e2e/windows/sitecustomize.py" "$site/"
  mkdir -p "$site/uvloop"
  cp "$REPO_ROOT/scripts/e2e/windows/uvloop/__init__.py" "$site/uvloop/"
  "$PY" -c "import tzdata" 2>/dev/null || {
    if command -v uv >/dev/null; then VIRTUAL_ENV="$VENV" uv pip install -q tzdata; else "$PY" -m pip install -q tzdata; fi
  }
fi

mkdir -p "$DN_ROOT"/{cache,config,plugins,music}
if ((WINDOWS)) && [[ ! -f "$DN_ROOT/config/config.json" ]]; then
  # The legacy default root "/music" is not an absolute path on Windows. Seed a
  # placeholder root; bootstrap.py replaces it with $DN_ROOT/music.
  mkdir -p "$DN_ROOT/seed-root"
  printf '{"library_settings": {"library_paths": ["%s/seed-root"]}}\n' "$DN_ROOT" \
    >"$DN_ROOT/config/config.json"
fi
if [[ -n "$PLUGIN_SRC" && -f "$PLUGIN_SRC/plugin.toml" ]]; then
  # Plugins are discovered at server start (and on a plugin save), so copy first.
  target="$DN_ROOT/plugins/mbid-artist-merge"
  rm -rf "$target"
  mkdir -p "$target"
  cp "$PLUGIN_SRC/plugin.toml" "$PLUGIN_SRC/plugin.py" "$target/"
  cp -r "$PLUGIN_SRC/mbid_artist_merge" "$PLUGIN_SRC/ui" "$target/"
  rm -rf "$target/mbid_artist_merge/__pycache__"
fi

if running; then
  echo "server already running (pid $(cat "$PIDFILE"))"
else
  (
    cd "$DN_SRC/backend"
    export ROOT_APP_DIR="$DN_ROOT" CACHE_DIR="$DN_ROOT/cache" \
      LIBRARY_DB_PATH="$DN_ROOT/cache/library.db" \
      CONFIG_FILE_PATH="$DN_ROOT/config/config.json" PORT="$DN_PORT" \
      PYTHONUTF8=1 PYTHONIOENCODING=utf-8
    if ((WINDOWS)); then
      "$PY" -m maintenance.automatic_upgrade --start-target \
        >"$DN_ROOT/server.log" 2>&1 </dev/null &
      cat "/proc/$!/winpid" >"$PIDFILE"
    else
      setsid "$PY" -m maintenance.automatic_upgrade --start-target \
        >"$DN_ROOT/server.log" 2>&1 </dev/null &
      echo $! >"$PIDFILE"
    fi
  )
fi

# /health is also answered by a temporary server while the first-start upgrade
# runs, so wait for a real API route instead.
for _ in $(seq 1 120); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 2 "http://127.0.0.1:$DN_PORT/api/v1/auth/setup/status" || true)"
  if [[ "$code" == "200" ]]; then
    echo "server up on http://127.0.0.1:$DN_PORT (log: $DN_ROOT/server.log)"
    exit 0
  fi
  sleep 1
done
echo "server did not start; see $DN_ROOT/server.log" >&2
exit 1
