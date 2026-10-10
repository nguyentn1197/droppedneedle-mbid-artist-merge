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
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DN_REF="${DN_REF:-v2.15.0}"
DN_SRC="${DN_SRC:-$REPO_ROOT/.cache/droppedneedle}"
DN_ROOT="${DN_ROOT:-$REPO_ROOT/.cache/dnroot}"
DN_PORT="${DN_PORT:-18688}"
PLUGIN_SRC="${PLUGIN_SRC-$REPO_ROOT}"
VENV="$DN_SRC/.venv"
PIDFILE="$DN_ROOT/server.pid"

stop_server() {
  if [[ -f "$PIDFILE" ]]; then
    pgid="$(cat "$PIDFILE")"
    kill -TERM -- "-$pgid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 -- "-$pgid" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL -- "-$pgid" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "server stopped"
  else
    echo "no server pid file at $PIDFILE"
  fi
}

if [[ "${1:-}" == "stop" ]]; then
  stop_server
  exit 0
fi

if [[ ! -d "$DN_SRC/.git" ]]; then
  git clone --depth 1 --branch "$DN_REF" https://github.com/DroppedNeedle/DroppedNeedle.git "$DN_SRC"
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  if command -v uv >/dev/null; then
    uv venv -q "$VENV" --python 3.13
    VIRTUAL_ENV="$VENV" uv pip install -q -r "$DN_SRC/backend/requirements.txt"
  else
    python3.13 -m venv "$VENV"
    "$VENV/bin/pip" install -q -r "$DN_SRC/backend/requirements.txt"
  fi
fi

mkdir -p "$DN_ROOT"/{cache,config,plugins,music}
if [[ -n "$PLUGIN_SRC" && -f "$PLUGIN_SRC/plugin.toml" ]]; then
  # Plugins are discovered at server start (and on a plugin save), so copy first.
  target="$DN_ROOT/plugins/mbid-artist-merge"
  rm -rf "$target"
  mkdir -p "$target"
  cp "$PLUGIN_SRC/plugin.toml" "$PLUGIN_SRC/plugin.py" "$target/"
  cp -r "$PLUGIN_SRC/mbid_artist_merge" "$target/"
fi

if [[ -f "$PIDFILE" ]] && kill -0 -- "-$(cat "$PIDFILE")" 2>/dev/null; then
  echo "server already running (pgid $(cat "$PIDFILE"))"
else
  (
    cd "$DN_SRC/backend"
    export ROOT_APP_DIR="$DN_ROOT" CACHE_DIR="$DN_ROOT/cache" \
      LIBRARY_DB_PATH="$DN_ROOT/cache/library.db" \
      CONFIG_FILE_PATH="$DN_ROOT/config/config.json" PORT="$DN_PORT"
    setsid "$VENV/bin/python" -m maintenance.automatic_upgrade --start-target \
      >"$DN_ROOT/server.log" 2>&1 < /dev/null &
    echo $! >"$PIDFILE"
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
