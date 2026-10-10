# End-to-end harness (verified on DroppedNeedle v2.15.0)

Runs a throwaway DroppedNeedle from source and builds a six-artist library that
reproduces the romanized-name split.

| Script | Does |
|---|---|
| `run_server.sh` | clones DroppedNeedle (`DN_REF`, default `v2.15.0`) into `.cache/droppedneedle`, makes a venv, starts the server on `DN_PORT` (default 18688) with data root `.cache/dnroot`, and copies this plugin into its plugins folder first (plugins are discovered at startup). `run_server.sh stop` stops it. Works on Linux and on Windows with Git Bash (see below). |
| `make_library.py <dir>` | writes 10 tagged two-second FLAC files (needs `ffmpeg`, `mutagen` — both available in the server venv / system) |
| `bootstrap.py` | creates admin `admin` / `MbidArtistMerge-e2e-7c41f9`, swaps the library root to `.cache/dnroot/music` with policy `local_metadata`, scans, then **test-only** links 音乃瀬奏 to the fake MBID `11111111-1111-4111-8111-111111111111` in SQLite (real servers get that link from MusicBrainz, which needs network) |
| `snapshot_library.py [out]` | read-only: saves every GET the plugin makes into `tests/fixtures/api-v2.15.0/library_snapshot.json` |
| `capture_fixtures.py [out-dir]` | records one request/response per file, **including a real irreversible merge** — run only on a throwaway server right after `bootstrap.py` |
| `run_e2e.py` | drives the installed plugin through a dry run (run_on_load), a live run and a no-op rerun, and asserts the outcome (PLAN §11.3); exits non-zero and prints the server log tail on failure |
| `../ui/preview.py` | not e2e: renders the settings panel against a fake bridge (see the main README) |
| `windows/` | shim copied into the server venv on Windows only: POSIX-only `os` flags, `ctypes.CDLL(None)`, a uvloop stand-in |

Typical session:
```bash
bash scripts/e2e/run_server.sh
P=.cache/droppedneedle/.venv/bin/python
$P scripts/e2e/make_library.py .cache/dnroot/music
$P scripts/e2e/bootstrap.py              # prints the split state
$P scripts/e2e/run_e2e.py                # prints "e2e: PASSED"
bash scripts/e2e/run_server.sh stop
rm -rf .cache/dnroot                     # fresh state next time (keeps the clone + venv)
```

Re-capturing fixtures (keeps snapshot and fixtures from the same run, so IDs agree):
```bash
bash scripts/e2e/run_server.sh stop; rm -rf .cache/dnroot; bash scripts/e2e/run_server.sh
$P scripts/e2e/make_library.py .cache/dnroot/music && $P scripts/e2e/bootstrap.py
$P scripts/e2e/snapshot_library.py       # before the merge
$P scripts/e2e/capture_fixtures.py tests/fixtures/api-v2.15.0
```

Notes:

- **IDs:** artist ids are deterministic (derived from the name). Album and track ids change with every fresh data root, because the library root id is random.
- **First start:** it runs an upgrade step behind a temporary `/health` server. `run_server.sh` waits for a real API route.
- **Proxies:** if your shell exports `HTTP(S)_PROXY`, make sure `NO_PROXY` covers `127.0.0.1`.
- **Admin password:** the server checks new passwords against the online
  pwnedpasswords range API (and fails open offline). The harness therefore uses a
  unique password; the captured fixtures still show the older `Passw0rd!Passw0rd`
  from an offline capture.
- **Windows (Git Bash):** the venv Python is `.cache/droppedneedle/.venv/Scripts/python.exe`
  (`P=.cache/droppedneedle/.venv/Scripts/python.exe`), and setting
  `PYTHONIOENCODING=utf-8` keeps the Japanese names printable. `run_server.sh`
  copies `windows/sitecustomize.py` and the uvloop stand-in into the venv, installs
  `tzdata`, and seeds `config.json` with an absolute placeholder library root
  (the legacy default `/music` is not absolute on Windows). Verified: the full
  sequence above passes on Windows 11. DroppedNeedle cannot serve plugin panel
  bundles on Windows (POSIX-only `dir_fd`), so `run_e2e.py` skips that one check
  there; the panel's `search` route is still exercised.
