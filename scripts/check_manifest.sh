#!/usr/bin/env bash
# Validate plugin.toml with DroppedNeedle's own manifest loader (PLAN §11.4).
#
#   bash scripts/check_manifest.sh
#
# Needs the clone + venv that scripts/e2e/run_server.sh creates.
# Environment: DN_SRC (default .cache/droppedneedle).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DN_SRC="${DN_SRC:-$REPO_ROOT/.cache/droppedneedle}"
PY="$DN_SRC/.venv/bin/python"
[[ -x "$PY" ]] || PY="$DN_SRC/.venv/Scripts/python.exe"  # Windows venv layout
if [[ ! -x "$PY" ]]; then
  echo "no DroppedNeedle venv under $DN_SRC; run scripts/e2e/run_server.sh first" >&2
  exit 1
fi

cd "$DN_SRC/backend"
"$PY" -c '
import sys
from pathlib import Path
from infrastructure.plugins.manifest import load_manifest
print(load_manifest(Path(sys.argv[1])))
' "$REPO_ROOT"
