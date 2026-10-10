# CLAUDE.md

You are implementing a **DroppedNeedle v2 plugin** called `mbid-artist-merge`.
**`PLAN.md` is the spec.** Follow it section by section, milestone by milestone
(§13). If something in the plan turns out to be wrong against the real server,
fix the plan in the same change and say why in the commit message.

## Read first
1. `PLAN.md` — goal, verified platform facts (§3), architecture (§4), manifest (§5), algorithm (§7), tests (§11), milestones (§13).
2. `docs/API-CONTRACT.md` — every endpoint, status code and quirk the plugin depends on.
3. `docs/BACKGROUND.md` — why the plugin exists (context only).
4. `tests/fixtures/api-v2.15.0/` — **real** server responses. Build tests on these; never invent payload shapes. If you need a new shape, capture it with the e2e scripts.

## Hard rules
- **Runtime code is standard library only.** Never import `httpx` (or anything else third-party) in `mbid_artist_merge/` or `plugin.py`; the HTTP client is injected.
- **Lazy host imports only:**
  - `core.*` / `infrastructure.*` may be imported only inside functions in `plugin.py` and `mbid_artist_merge/state.py`;
  - tests must never import them.
- **Never** write SQLite, never touch audio files, never call any URL except `settings.base_url`.
- **Merges:**
  - `provider_choice` is always the literal `"retain_survivor"`;
  - always preview first;
  - only `local_only` → `musicbrainz_linked`;
  - dry-run is the default and any unparseable value means dry-run.
- **No secrets anywhere:** never log or report passwords or tokens.
- **No timestamps in the version;** bump `version` in both `plugin.toml` and `mbid_artist_merge/__init__.py` together.
- **State file keys** must match `^[a-z0-9][a-z0-9/_-]{0,63}$` (no dots).
- **Keep the repo installable from GitHub:** the whole repo is copied into the server's plugin folder (≤2000 files, ≤32 MiB). Never commit audio, `.cache/` or virtualenvs.

## Commands
```bash
uv sync                                   # dev deps (pytest, pytest-asyncio, httpx, ruff)
uv run ruff check . && uv run ruff format --check .
uv run pytest -q                          # unit + integration (no network)

# end-to-end against a real DroppedNeedle v2.15.0 (needs git, python3.13, ffmpeg, curl)
bash scripts/e2e/run_server.sh            # also copies this plugin into the server
.cache/droppedneedle/.venv/bin/python scripts/e2e/make_library.py .cache/dnroot/music
.cache/droppedneedle/.venv/bin/python scripts/e2e/bootstrap.py
.cache/droppedneedle/.venv/bin/python scripts/e2e/run_e2e.py      # written in M5
bash scripts/e2e/run_server.sh stop
```
Re-copying the plugin into the server needs a server restart (`run_server.sh stop` then start). Plugins are discovered at startup.

## Workflow
- Work milestone by milestone. Each milestone ends with ruff + pytest green and a commit (`feat(m2): evidence extraction` style).
- Write the test before the code for every rule in PLAN §7.
- When an e2e run disagrees with a fixture, re-capture with `scripts/e2e/capture_fixtures.py` / `snapshot_library.py` on a fresh `bootstrap.py` state. Don't edit fixtures by hand.
- Keep functions small and pure where the plan says pure (`folding`, `evidence`, `planner`, `report`).
- Ask the owner only about PLAN §15 items. Make every other call yourself and note it in the PR/commit.
