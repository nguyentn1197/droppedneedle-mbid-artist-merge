# Implementation plan: `mbid-artist-merge` DroppedNeedle plugin

**Status:** ready to implement.
**Target:** DroppedNeedle **v2.x**, plugin `api_version = 1`, verified on **v2.15.0**.
**Owner decisions still open:** see §15.

Read [`docs/BACKGROUND.md`](docs/BACKGROUND.md) for why the problem exists.
Read [`docs/API-CONTRACT.md`](docs/API-CONTRACT.md) for the exact endpoints. Every
fact in this plan marked ✅ was checked against a running v2.15.0 server or its
source. Real request/response pairs are in [`tests/fixtures/api-v2.15.0/`](tests/fixtures/api-v2.15.0/).

---

## 1. Goal

A DroppedNeedle plugin that finds **local-only artists whose own audio files say,
through embedded `MUSICBRAINZ_ARTISTID` / `MUSICBRAINZ_ALBUMARTISTID` tags, that
they are the same MusicBrainz artist as an artist DroppedNeedle has already
linked.** It merges each one into the linked artist, keeping the MusicBrainz
link.

Motivating case:

| | Before | After |
|---|---|---|
| "Otonose Kanade" (tags carry MBID *M*) | `local_only`, own artist page | retired; its id redirects (308) to 音乃瀬奏 |
| "音乃瀬奏" (linked to *M*) | `musicbrainz_linked` | `musicbrainz_linked`, now owns all albums, tracks, favorites and history |

Success criteria:

1. On the E2E test library (§11.3), a dry-run reports exactly one `would_merge` (Otonose Kanade → 音乃瀬奏). Every other artist gets the expected decision (§7.6).
2. With dry-run off, that merge happens. 音乃瀬奏 keeps MBID *M*. No other artist changes.
3. A second run is a no-op.
4. The plugin never writes to SQLite or the filesystem outside its own state files, never merges two linked artists, and never sends `provider_choice` other than `retain_survivor`.

## 2. Non-goals (and why)

- **Linking an artist when no linked artist exists for its MBID.** v2 has no public endpoint that sets an artist's MBID (§BACKGROUND). Report these as `no_target`.
- **Renaming.** A merge keeps the survivor's name (the MusicBrainz canonical name, e.g. 音乃瀬奏). The API has no rename.
- **Undo.** Merges are irreversible upstream. Dry-run is the default and the safety story.
- **Fixing DroppedNeedle's matching.** That needs the upstream patch described in BACKGROUND.
- **DroppedNeedle v3** (unreleased Rust rewrite, out-of-process plugins). Keep the code portable (§12), but ship v2 only.
- **Merging two local-only artists that share an MBID.** Report them as `shared_mbid_without_link` instead (§7.6).

## 3. Verified platform facts (v2.15.0)

### 3.1 Plugin runtime ✅
- Plugins are Python modules loaded **in-process** from `<ROOT_APP_DIR>/plugins/<name>/` (Docker: `/app/plugins`, which must be a mounted volume).
- The server runs on Python **3.13**, httpx **0.28.1**.
- **Installing from GitHub** downloads the repo zip of `main` (else `master`) and extracts the **whole repo** into the plugin folder. Caps: ≤32 MiB, ≤2000 entries. **Never commit audio files or `.cache/`.**
- **Entrypoint:** `plugin.py` is executed via `importlib.util.spec_from_file_location` as module `droppedneedle_plugin.<name>.plugin`. Sibling modules are **not** importable by default.
  - Fix (verified with a probe plugin): `plugin.py` inserts its own directory at the front of `sys.path`, then removes any already-imported `mbid_artist_merge*` entries from `sys.modules` before importing, so a plugin reload picks up new code.
- **Constructor:** `MbidArtistMerge(context)`. `context` has:
  - `.settings`: a property returning a **fresh `dict[str, str]`** each access;
  - `.http`: a per-plugin shared `httpx.AsyncClient`;
  - `.logger`: `logging.getLogger("plugin.mbid-artist-merge")`;
  - `.publish(kind, payload)`;
  - `.plugin_name`.
- **`context.http` behaviour:**
  - default timeout 30 s;
  - **`follow_redirects=True`**;
  - **keeps cookies** (login sets `droppedneedle_session` on it; logout clears it);
  - ignores `HTTP(S)_PROXY` env vars (the factory passes an explicit transport, which disables httpx env proxies), so container proxy settings don't affect loopback calls;
  - loopback calls to `http://127.0.0.1:<PORT>` work (verified: login, list, logout from inside a tick).
- **Scheduler:**
  - `async def on_tick(self) -> None`;
  - `[schedule] interval_minutes` is an int in [5, 1440], fixed in the manifest (not a setting);
  - the tick runs under `asyncio.timeout(interval)`;
  - no overlap within one loop; exceptions are logged and the loop continues;
  - the plugin instance is re-resolved every tick.
- **`run_on_load = true` behaviour:**
  - fires when the tick loop starts: server start, or enabling a disabled plugin;
  - **re-saving an already-enabled plugin with the same interval does NOT re-run it** ✅ (verified: save → no tick; disable+enable → tick).
- **Settings:** `[[settings]]` allows only `key`, `label`, `help`, `secret`. Values are **strings**; there are no defaults in the manifest, so defaults live in code.
  - `secret = true` values are encrypted at rest and masked as `plugin****` in the UI and API.
- **State files:** `await get_plugin_host().write_plugin_state_file(name, rel_path, bytes)` / `read_plugin_state_file(...)` (import from `core.dependencies`; the host object is a singleton).
  - `rel_path` must match `^[a-z0-9][a-z0-9/_-]{0,63}$` (**no dots**, so `state/report.json` is rejected; `state/last-report` works ✅).
  - 10 MiB per file. Files live inside the plugin folder, so a reinstall wipes them. Treat state as a cache.
- **Routes** (need the `publisher` capability):
  - served at `/api/v1/plugins/ext/mbid-artist-merge/<path>`;
  - signature `async def handle_route(self, method, subpath, query: dict, body: object) -> PluginRouteResponse` (`from infrastructure.plugins.protocols import PluginRouteResponse`);
  - 5 s timeout; response body ≤1 MiB;
  - status passes through only for 200–299/400/404;
  - `auth = "admin"` is re-checked by the host ✅.
- **Reload:** saving any plugin in Settings re-executes `plugin.py` and builds a new instance. A tick already running on the old instance keeps running.
- **Contract rules we follow:** no SQLite, no direct library writes, no engine handles; blocking work in `asyncio.to_thread` (we have none).

### 3.2 Auth ✅
- `POST /api/v1/auth/login {username, password}` → `{token, user}`. **Local accounts only.**
- `POST /api/v1/auth/logout` revokes the token (204).
- Tokens expire 30 days after issue; use does not extend them.
- Admin-only endpoints answer 403 `FORBIDDEN` for other roles. `GET /auth/me` → `role`.

### 3.3 Catalog API ✅
See `docs/API-CONTRACT.md`. Key points:
- **Artist lists:** `GET /library/artists?scope=album|contributors` (limit ≤100, paginate with `offset`/`total`; `scope=all` is silently treated as `album`). Union both scopes by `id`.
  - `musicbrainz_artist_id` is the link; `artist_identity_state` is `local_only|musicbrainz_linked`; `row_revision` is what merge needs.
- **A local-only artist's files:**
  - `/library/artists/{id}/albums` + `/library/albums/{album_id}/tracks` for album-artist credits;
  - `/library/artists/{id}/appearances` for track-only credits.
- **File tags:** `GET /library/tracks/{id}/tags` (admin) reads the **file** and returns per-credit MBIDs paired by position.
- **Merge:** `merge-preview` (read-only, returns a token) then `merge` (irreversible) with identical selection values, `provider_choice: "retain_survivor"`, a unique `idempotency_key`, within 15 min.
- **Retired ids** answer 308 to the survivor.

## 4. Architecture

```
plugin.toml                 manifest (§5)
plugin.py                   v2 adapter: sys.path + module purge, class MbidArtistMerge
mbid_artist_merge/
  __init__.py               __version__ (keep in sync with plugin.toml)
  config.py                 Settings dataclass + parse(dict[str,str]) with defaults/validation
  folding.py                fold(name): mirrors DroppedNeedle's _fold_value
  api.py                    DroppedNeedleApi: typed async calls over an injected HTTP client
  models.py                 dataclasses: Artist, TrackRef, CreditEvidence, Decision, RunReport
  evidence.py               pure: extract the artist's MBID from one /tags payload
  planner.py                pure: inventory + evidence -> Decision per artist
  runner.py                 orchestrates one run: auth, lease, inventory, sampling, merges, report
  state.py                  StateStore protocol; HostStateStore (v2) and MemoryStateStore (tests)
  report.py                 report building, size capping, log lines
tests/
  fixtures/api-v2.15.0/     real captured payloads (do not hand-edit; re-capture instead)
    library_snapshot.json   every GET response of the split test library (38 entries)
    *.json                  one request/response per file: auth, errors, preview, merge, 308
  fake_dn.py                stateful fake DroppedNeedle built on httpx.MockTransport
  test_*.py                 see §11
scripts/
  e2e/                      real-server harness (verified): run_server.sh, make_library.py,
                            bootstrap.py, snapshot_library.py, capture_fixtures.py,
                            run_e2e.py (to write, M5)
  diag/dn_artist_diag.sh    read-only SQLite diagnostic for a user's real instance
docs/                       BACKGROUND.md, API-CONTRACT.md
```

Dependency rules:

- **Runtime code imports only the standard library.** No `httpx` import: the client is injected, and only `.get`, `.post`, `.status_code`, `.json()` and `.headers` are used. This keeps it portable to v3's `ctx.http`.
- **`core.*` / `infrastructure.*` imports live only in `plugin.py` and `state.py:HostStateStore`**, inside functions (lazy). Unit tests never import them.
- Dev/test dependencies: `pytest`, `pytest-asyncio`, `httpx==0.28.1`, `ruff`.

Data flow of one run:

```
on_tick / POST run
  └─ Runner.run(trigger)
       ├─ load Settings ── invalid? → report error, stop
       ├─ acquire lease (state/lease) ── held by a live run? → skip
       ├─ authenticate (token or login) → GET /auth/me role == admin
       ├─ soft guard: GET /library/artists/reconciliation (busy → skip)
       ├─ inventory: page artists (album + contributors) → linked_by_mbid, candidates
       ├─ for candidate in window(cursor, max_artists_per_run):
       │     sample files → GET /tags × n → evidence.extract → planner.decide
       │     would_merge → re-read both artists → merge-preview (dry-run stops here)
       │                 → merge → verify
       ├─ save cache + cursor + report; release lease
       └─ logout (password mode only)
```

## 5. Manifest (validated ✅ with DroppedNeedle's `load_manifest`)

Create `plugin.toml` at the repo root exactly like this. Only `author` and `homepage` change.

```toml
[plugin]
name = "mbid-artist-merge"
display_name = "MBID Artist Merge"
version = "0.1.0"
api_version = 1
entrypoint = "plugin:MbidArtistMerge"
capabilities = ["scheduler", "publisher"]
description = "Merges local-only artists into the MusicBrainz-linked artist that their files' embedded MBIDs point to (e.g. romanized or translated names). Dry-run by default."
author = "OWNER"
homepage = "https://github.com/OWNER/droppedneedle-mbid-artist-merge"

[schedule]
interval_minutes = 360
run_on_load = true

[[route]]
path = "report"
method = "GET"
auth = "admin"
rate_limit_per_minute = 30

[[route]]
path = "run"
method = "POST"
auth = "admin"
rate_limit_per_minute = 2

[[settings]]
key = "base_url"
label = "Server URL"
help = "How the plugin reaches this server from inside it. Default http://127.0.0.1:8688. Include BASE_PATH if you set one, e.g. http://127.0.0.1:8688/music."

[[settings]]
key = "username"
label = "Admin username"
help = "A local DroppedNeedle admin account. Leave empty when using an API token."

[[settings]]
key = "password"
label = "Admin password"
help = "Password for the admin account above."
secret = true

[[settings]]
key = "api_token"
label = "API token (optional)"
help = "Instead of username/password: a bearer token of an admin session. Tokens expire 30 days after issue."
secret = true

[[settings]]
key = "dry_run"
label = "Dry run"
help = "true (default) only reports what it would merge. Set to false to merge. Merges cannot be undone; back up library.db first."

[[settings]]
key = "max_merges_per_run"
label = "Max merges per run"
help = "Default 10."

[[settings]]
key = "max_artists_per_run"
label = "Max artists checked per run"
help = "Default 200. Larger libraries are covered over several runs."

[[settings]]
key = "samples_per_artist"
label = "Files sampled per artist"
help = "Default 5. Every sampled file that names the artist must carry the same MBID."

[[settings]]
key = "min_agreeing_samples"
label = "Minimum agreeing files"
help = "Default 2, lowered automatically for artists with fewer files."

[[settings]]
key = "request_delay_ms"
label = "Delay between API calls (ms)"
help = "Default 100. Raise it if the server feels busy while the plugin runs."

[[settings]]
key = "time_budget_seconds"
label = "Time budget per run (s)"
help = "Default 900. The run stops cleanly and resumes next time."

[[settings]]
key = "exclude_artist_ids"
label = "Never touch these artist IDs"
help = "Comma-separated local artist IDs, as source or survivor."

[[settings]]
key = "exclude_mbids"
label = "Ignore these MBIDs"
help = "Comma-separated MusicBrainz artist IDs to never act on."
```

Manifest rules to keep in mind:

- Unknown keys anywhere fail the load.
- `name` must match `^[a-z0-9][a-z0-9-]{0,31}$`.
- `[[route]]` requires `publisher`.
- Bump `version` in **both** `plugin.toml` and `mbid_artist_merge/__init__.py` on each release.

## 6. Settings parsing (`config.py`)

`Settings.parse(raw: Mapping[str, str]) -> Settings | ConfigError`. Trim whitespace. Empty string means default.

| Key | Type | Default | Rule |
|---|---|---|---|
| `base_url` | str | `http://127.0.0.1:8688` | must start with `http://` or `https://`; strip trailing `/` |
| `username` / `password` | str | "" | used when `api_token` is empty; both required then |
| `api_token` | str | "" | if set, wins over username/password; never logged |
| `dry_run` | bool | **true** | false only for `false`, `0`, `no`, `off` (case-insensitive); **anything else is true** |
| `max_merges_per_run` | int | 10 | 0–500; 0 means report-only even when `dry_run=false` |
| `max_artists_per_run` | int | 200 | 1–10000 |
| `samples_per_artist` | int | 5 | 1–20 |
| `min_agreeing_samples` | int | 2 | 1–20; effective value = `min(setting, samples_taken)` |
| `request_delay_ms` | int | 100 | 0–5000 |
| `time_budget_seconds` | int | 900 | 30–(interval×60−60) |
| `exclude_artist_ids` | set[str] | ∅ | comma/whitespace separated |
| `exclude_mbids` | set[str] | ∅ | lowercased; each must look like a UUID or the entry is ignored with a warning |

An invalid number makes the run abort with a `config_error` report. Don't silently clamp, so the owner notices.

## 7. Algorithm

### 7.1 Constants
```
VARIOUS_ARTISTS_LOCAL_ID = "00000000-0000-4000-8000-000000000001"   # never source/survivor
UNKNOWN_ARTIST_LOCAL_ID  = "00000000-0000-4000-8000-000000000002"   # never source/survivor
VARIOUS_ARTISTS_MBID     = "89ad4ac3-39f7-470e-963a-56509c546377"   # never act on
UNKNOWN_ARTIST_MBID      = "125ec42a-7229-4250-afc5-e057484327fe"   # never act on
MBID_RE = ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$   (after lowercasing)
```

### 7.2 Name folding (`folding.py`)
Mirror DroppedNeedle's `_fold_value`, so a local artist's `name` and the tag credit it came from fold equal:

```python
def fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.strip())
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return " ".join(without_marks.split())
```

### 7.3 Inventory
1. Page `GET /library/artists?scope=album&limit=100&offset=…` until `offset >= total`. Same for `scope=contributors`. Union by `id`.
   - Use only `id`, `name`, `musicbrainz_artist_id`, `artist_identity_state` and `row_revision`, which are identical in both scopes.
   - Counts and `library_relationship` differ per scope (verified), so don't rely on them.
2. `linked_by_mbid = {a.musicbrainz_artist_id.lower(): a for a in artists if a.musicbrainz_artist_id}`.
3. `candidates = [a for a in artists if a.artist_identity_state == "local_only" and a.id not in RESERVED and a.id not in exclude_artist_ids]`, sorted by `id`.
4. **Window:** start after `cursor` (from state), wrap around, take up to `max_artists_per_run`. Save the new cursor at the end, or when the time budget runs out.

### 7.4 Sampling a candidate's files
Pick up to `samples_per_artist` tracks, spread across albums round-robin (track 1 of every album, then track 2 of every album, …):

1. `GET /library/artists/{id}/albums`, then `GET /library/albums/{album_id}/tracks` for each album (stop fetching albums once enough tracks are queued). **Role = `album_artist`.**
2. If more samples are needed: `GET /library/artists/{id}/appearances?limit=100&offset=0`. Take the tracks listed under each item. **Role = `track_artist`.**
3. For each sampled track: `GET /library/tracks/{track_id}/tags`.
   - 400/404 → record a `sample_error` and continue.
   - 403 → abort the run (`not_admin`).

**Cache shortcut:** if the cache (§8) has this artist with the same `row_revision`, a `checked_at` within 7 days, and a decision in {`no_mbid`, `conflict`, `insufficient_evidence`, `ambiguous`}, reuse it without sampling.

For `no_target` / `shared_mbid_without_link`: if the cached MBID now appears in `linked_by_mbid`, **re-sample** before merging. Otherwise reuse the cached result.

### 7.5 Evidence from one `/tags` payload (`evidence.py`, pure)

Input: the artist's `name`, the sample's `role`, the tag JSON. Output: `CreditEvidence(mbid | None, reason)`.

```python
POOLS = {
    "album_artist": [
        ("album_artists", "musicbrainz_album_artist_ids"),
        ("artists", "musicbrainz_artist_ids"),
    ],
    "track_artist": [
        ("artists", "musicbrainz_artist_ids"),
        ("album_artists", "musicbrainz_album_artist_ids"),
    ],
}


def extract(name: str, role: str, tag: dict) -> CreditEvidence:
    target = fold(name)
    for credits_key, ids_key in POOLS[role]:  # primary pool first, then the other
        pool = tag.get(credits_key) or []
        matches = [
            c
            for c in pool
            if fold(c.get("name") or "") == target or fold(c.get("credited_name") or "") == target
        ]
        if not matches:
            continue
        if len(tag.get(ids_key) or []) > len(pool):  # e.g. ARTIST="A feat. B" + two IDs
            return CreditEvidence(None, "ambiguous", "more_ids_than_credits")
        mbids = {(c.get("musicbrainz_artist_id") or "").strip().lower() for c in matches}
        mbids.discard("")
        if len(mbids) > 1:
            return CreditEvidence(None, "ambiguous", "same_name_different_ids")
        if len(mbids) == 1:
            mbid = mbids.pop()
            if MBID_RE.match(mbid):
                return CreditEvidence(mbid, "credit", credits_key)
        # the matching credit carries no usable MBID in this pool: try the other pool
    return CreditEvidence(None, "no_mbid_in_file", None)
```

Notes:

- Use only the per-credit `musicbrainz_artist_id`, never the top-level scalars. The scalar is just the first id of the list, and it can belong to a different credit.
- The `Conflict Name` fixtures have `album_artists[0].musicbrainz_artist_id = null` but differing track-artist ids. Falling through to the second pool turns them into a `conflict`, not `no_mbid`.

### 7.6 Decision (`planner.py`, pure)

Given the candidate and its list of `CreditEvidence`:

| Decision | Condition | Action |
|---|---|---|
| `no_samples` | the candidate has no readable tracks | report |
| `ambiguous` | any evidence is `ambiguous` | report |
| `conflict` | ≥2 distinct MBIDs across evidence | report (list them) |
| `no_mbid` | no evidence carries an MBID | report quietly (count only) |
| `insufficient_evidence` | samples carrying *M* < `min(min_agreeing_samples, samples_taken)`. Samples whose file has no MBID for the artist don't count against it (partially tagged libraries are normal), but any *other* MBID is a `conflict` | report |
| `excluded_mbid` | MBID in `exclude_mbids`, or VA/Unknown MBID | report |
| `no_target` | no linked artist holds the MBID | report |
| `shared_mbid_without_link` | `no_target`, and ≥2 local-only candidates in this run resolved to the same MBID | report them together |
| `self` | the linked artist is the candidate itself | ignore (cannot happen for `local_only`, guard anyway) |
| `excluded_target` | survivor id in `exclude_artist_ids` or reserved | report |
| `would_merge` | otherwise | dry-run: preview, report. Live: §7.7 |

Expected results on the E2E library (§11.3), **checked by running these rules against `tests/fixtures/api-v2.15.0/library_snapshot.json`**:

- Otonose Kanade → `would_merge` (into 音乃瀬奏)
- Other Artist → `no_target`
- Lonely Alias → `no_target`
- Some Band → `no_mbid`
- Conflict Name → `conflict`
- 音乃瀬奏 → not a candidate (linked)

### 7.7 Merge procedure (live mode, per `would_merge`, until `max_merges_per_run`)

1. `GET /library/artists/{source}`:
   - returned `id` ≠ source (it followed a 308) → `already_merged`; skip.
   - state no longer `local_only` → `changed`; skip.
2. `GET /library/artists/{survivor}`: must be `musicbrainz_linked` with `musicbrainz_artist_id` equal to *M* (case-insensitive) and the same `id`. Else `changed`; skip.
3. `selection = {source_artist_ids: [source], surviving_artist_id: survivor, expected_revisions: {source: rev_s, survivor: rev_v}}` using the revisions just read.
4. `POST /library/artists/merge-preview selection`:
   - 409 `STALE_REVISION` → `stale`; retry next run.
   - 200 with `identity_conflicts != []` → `identity_conflict`; skip.
5. `POST /library/artists/merge {**selection, preview_token, idempotency_key, provider_choice: "retain_survivor"}`.
   - `idempotency_key = f"mbid-artist-merge:v1:{source}:{rev_s}:{survivor}:{rev_v}"` (deterministic, so a retried request is a safe replay).
6. **Verify** the response: `kind == "merge_artist"`, `surviving_artist_id == survivor`, and `source in retired_artist_ids`. Then `GET /library/artists/{survivor}` still holds *M*.
   - Any mismatch → `error` with full context, and **stop further merges in this run**.
7. Record `merged` with the preview's `reference_counts`.

Live-mode outcomes are `merged`, `deferred` (the `max_merges_per_run` cap was reached, including a cap of 0), `stale`, `changed`, `already_merged`, `identity_conflict` and `error`. Together with the §7.6 decisions they are the complete set of `decision` values. The report has **one counter per value**.

In **dry-run**, run steps 1–4 (all read-only) and record `would_merge` with `reference_counts`. Never call step 5.

### 7.8 Error-handling matrix

| Situation | Behaviour |
|---|---|
| `ConfigError` | report `config_error`; no requests |
| login 401 / token 401 at start | report `auth_failed` (token: hint "token expired, 30-day lifetime"); stop. **Do not retry within the run.** |
| `/auth/me` role ≠ admin, or any 403 | report `not_admin`; stop |
| 401 mid-run (token revoked) | stop, report `auth_lost`, save cursor |
| connection error / timeout on a call | retry that call once after 2 s; then stop the run, report `server_unreachable`, save cursor |
| 5xx | same as connection error |
| 409 `STALE_REVISION` on preview/merge | per-artist `stale`; continue |
| 400 on `/tags` | per-sample `sample_error`; continue |
| time budget exceeded | finish the current artist, save state, report `partial: true` |
| `asyncio.CancelledError` (host timeout or disable) | let it propagate; a `finally` saves state best-effort, releases the lease and logs out |
| unexpected exception | catch in `on_tick`, log with traceback, report `internal_error`; never raise into the host |

### 7.9 Pacing
- Sleep `request_delay_ms` between API calls; `asyncio.sleep` is non-blocking.
- Check the time budget before each candidate.
- Soft guard at start: `GET /library/artists/reconciliation`. If `state ∈ {queued, running, paused}`, skip the run (`library_busy`). This only covers DroppedNeedle's backfill job, so the revision checks remain the real protection.

## 8. State (`state.py`)

`StateStore` protocol: `async read(key) -> str | None`, `async write(key, value: str)`.

- **`HostStateStore`** (v2): wraps `get_plugin_host().read_plugin_state_file / write_plugin_state_file` with UTF-8 JSON. A missing file (`PluginStateFileError`) means `None`. Import lazily inside methods.
- **`MemoryStateStore`**: tests.

Keys (each must match `^[a-z0-9][a-z0-9/_-]{0,63}$`, so no dots):

| Key | Content | Notes |
|---|---|---|
| `state/lease` | `{"run_id", "trigger", "started_at", "expires_at"}` | a run skips if an unexpired lease exists; `expires_at = started_at + time_budget + 120 s`; removed (written as `{}`) on exit |
| `state/cursor` | `{"after_id": str}` | inventory window position |
| `state/cache` | `{"version": 1, "artists": {id: {"row_revision", "decision", "mbid", "checked_at"}}}` | prune entries for ids absent from inventory; keep < 5 MiB (drop oldest `checked_at` first) |
| `state/last-report` | report JSON (§9) | capped < 900 KiB so the route can return it |

Writes are best-effort: a failure logs a warning and never fails the run. A wiped plugin folder (reinstall) just means a cold cache.

## 9. Report & observability (`report.py`)

Shape (also the body of `GET …/report`):

```json
{
  "plugin_version": "0.1.0",
  "run_id": "uuid4",
  "trigger": "schedule | load | manual",
  "started_at": "2026-10-09T05:00:00Z",
  "finished_at": "…",
  "dry_run": true,
  "partial": false,
  "status": "ok | config_error | auth_failed | not_admin | auth_lost | server_unreachable | library_busy | lease_held | internal_error",
  "server": {"base_url": "http://127.0.0.1:8688"},
  "totals": {"artists_listed": 6, "linked": 1, "candidates": 5, "examined": 5,
             "skipped_cached": 0, "errors": 0,
             "would_merge": 1, "no_target": 2, "conflict": 1, "no_mbid": 1,
             "…one counter for every other decision value (§7.6, §7.7), zero included": 0},
  "actions": [
    {"decision": "would_merge",
     "source": {"id": "a8a4…", "name": "Otonose Kanade", "row_revision": 1},
     "target": {"id": "eae6…", "name": "音乃瀬奏", "row_revision": 1},
     "mbid": "1111…",
     "evidence": [{"track_id": "…", "album_id": "…", "role": "album_artist",
                   "credit_name": "Otonose Kanade", "mbid": "1111…"}],
     "reference_counts": {"album_credits": 2, "track_credits": 6, "…": 0},
     "at": "…"}
  ],
  "errors": [{"stage": "tags", "track_id": "…", "status": 400, "message": "…"}]
}
```

- **Caps:** `actions` lists every decision except `no_mbid` and cache hits, which are counted only. Max 500 entries, then `"truncated": true`.
- **Never in the report or logs:** the password, the token, or full tag payloads.
- **Log lines** (`context.logger`):
  - one summary line per run: `mbid-artist-merge run=<id> dry_run=true would_merge=1 merged=0 no_target=2 conflict=1 …`;
  - one INFO line per `would_merge` / `merged`;
  - WARNING for errors.
- **Routes:**
  - `GET report` → 200 with the last report from memory, else from `state/last-report`. Answer 404 `{"error":"no run yet"}` when there is none.
  - `POST run` → if a lease is held, 200 `{"started": false, "reason": "running"}`. Else `asyncio.create_task(runner.run(trigger="manual"))` and 200 `{"started": true, "run_id": …}`. Keep a reference to the task on the instance so it isn't garbage-collected. Must return within the 5 s route budget.
  - Both are admin-only. An admin opens `https://<server>/api/v1/plugins/ext/mbid-artist-merge/report` in the browser (the session cookie authenticates it).
- **Optional (M6):** `ctx.publish("plugin_notice", {"title": "...", "body": "..."})` after runs with merges. It only reaches other subscriber plugins; nothing in the UI shows it.

## 10. Security & safety checklist

- [ ] `dry_run` defaults to **true**, and any unparseable value is true.
- [ ] Only `local_only` → `musicbrainz_linked` merges. Never two linked artists, never reserved ids, never VA/Unknown MBIDs.
- [ ] `provider_choice` is the literal `"retain_survivor"` in exactly one place, with a unit test asserting it.
- [ ] Preview is called and `identity_conflicts == []` before every merge; fresh revisions are read right before the preview.
- [ ] `max_merges_per_run` is enforced; stop merging after any verification mismatch.
- [ ] Credentials only come from `secret` settings, are never logged, and are never in the report.
- [ ] Every request sends the `Authorization` header. Never depend on the session cookie that login leaves on the shared client (logout clears it).
- [ ] Password mode logs out in `finally`.
- [ ] No SQLite and no file I/O except the host state helper. No `subprocess`, no network beyond `base_url`.
- [ ] README tells the owner to **back up `/app/cache/library.db` before turning dry-run off**, and that merges cannot be undone.

## 11. Testing

### 11.1 Unit tests (pure, fast; no network)
- `test_folding.py`:
  - NFKD and casefold: `"Ｏｔｏｎｏｓｅ　Ｋａｎａｄｅ"` (full-width) → `"otonose kanade"`;
  - combining marks: `"Beyoncé"` → `"beyonce"`;
  - whitespace collapsing;
  - Japanese names unchanged except casefold.
- `test_config.py`: defaults; dry_run truthiness table; bounds errors; token wins over password; exclusion parsing.
- `test_evidence.py`, using the real fixtures `track_tags_*.json`:
  - romanized → MBID `1111…` (role `album_artist`);
  - collab contributor → `1111…` from position 1 (role `track_artist`);
  - conflict_1/2 → `3333…` / `4444…` via the secondary pool;
  - synthetic: more ids than credits → ambiguous;
  - same name twice with different ids → ambiguous;
  - name absent → `no_mbid_in_file`;
  - invalid MBID string → `no_mbid_in_file`.
- `test_planner.py`: every row of §7.6, including `shared_mbid_without_link`, exclusions and VA MBID.

### 11.2 Integration tests (fake server)
`tests/fake_dn.py`: an `httpx.MockTransport` handler that keeps state. It seeds itself from `library_snapshot.json`: every GET the plugin makes in the split state, keyed by path + query string as captured (`limit=100&offset=0&scope=album`; `limit=100&offset=0` for appearances). Match requests on path + **sorted** query parameters so parameter order never matters. It also uses the individual fixtures for auth, errors and merge shapes, and simulates:

- artists with revisions;
- merge-preview issues tokens bound to the selection;
- merge applies (retire source, 308 for retired ids), honours idempotency keys, and returns 409 on stale revisions or a mismatched token;
- auth tokens with revoke;
- a recorded call log.

Build the runner with `httpx.AsyncClient(transport=MockTransport(fake), base_url=…)` passed as the injected client.

Required scenarios:

1. Dry-run on the six-artist library → one `would_merge`, the expected decisions, **zero** POSTs to `/merge`, a preview called once.
2. Live run → exactly one merge with `provider_choice == "retain_survivor"`; a second run → no merges (Otonose is gone; cache/inventory consistent).
3. Stale revision between inventory and merge → `stale`, no merge, no crash.
4. Preview with `identity_conflicts` → skip.
5. 308/retired source → `already_merged`.
6. Survivor lost its MBID before the merge → `changed`, skip.
7. Login 401 → `auth_failed`, no other calls. Non-admin `/auth/me` → `not_admin`.
8. Token mode → no login/logout calls; 401 → `auth_failed` with the expiry hint.
9. Time budget hit mid-inventory → `partial`, cursor saved, next run resumes.
10. `max_merges_per_run = 1` with two mergeable artists → one `merged`, one `deferred`.
11. Lease held → second concurrent run reports `lease_held`.
12. Connection error → one retry, then `server_unreachable`.
13. Report never contains the password or token (assert on the serialized JSON).
14. `plugin.py` adapter: `on_tick` swallows exceptions; `handle_route` GET report/404; POST run returns within 5 s and starts a task.

### 11.3 End-to-end against a real server (verified harness)
Requires git, Python 3.13 (uv recommended), ffmpeg, curl, about 400 MB disk.

```bash
bash scripts/e2e/run_server.sh                         # clones v2.15.0, installs, starts :18688,
                                                       # copies this plugin into the server's plugins dir
.cache/droppedneedle/.venv/bin/python scripts/e2e/make_library.py .cache/dnroot/music
.cache/droppedneedle/.venv/bin/python scripts/e2e/bootstrap.py
# M5: .cache/droppedneedle/.venv/bin/python scripts/e2e/run_e2e.py
bash scripts/e2e/run_server.sh stop
```

`bootstrap.py` ends in the split state (verified):

- 音乃瀬奏 `musicbrainz_linked`;
- Otonose Kanade, Other Artist, Some Band, Conflict Name, Lonely Alias `local_only`.

It links 音乃瀬奏 by writing one row to SQLite, because MusicBrainz identification needs network. That is test setup only.

`run_e2e.py` (write in M5):

1. login as `admin` / `Passw0rd!Passw0rd`;
2. `PUT /api/v1/plugins/mbid-artist-merge {"enabled": true, "settings": {"base_url": "http://127.0.0.1:18688", "username": "admin", "password": "Passw0rd!Passw0rd"}}` (dry_run unset → true);
3. poll `GET /api/v1/plugins/ext/mbid-artist-merge/report` until a run finishes (`run_on_load`), and assert the §7.6 expected decisions and zero merges;
4. `PUT` again with the same settings plus `"dry_run": "false"` (send `plugin****` as the password to keep the stored value; a save does **not** re-trigger `run_on_load`), then `POST …/run`, and poll the report until `merged == 1`;
5. assert:
   - `GET /library/artists` no longer lists Otonose Kanade;
   - 音乃瀬奏 is `musicbrainz_linked` with `11111111-1111-4111-8111-111111111111`;
   - `GET /library/artists/<otonose id>` answers 308 (use a client with `follow_redirects=False`);
   - every other artist is unchanged;
6. `POST …/run` again and assert `merged == 0`;
7. exit non-zero on any failure, and print the server log tail.

### 11.4 Manifest check
`scripts/check_manifest.sh` (M1) runs DroppedNeedle's own loader from the E2E clone:

```bash
cd .cache/droppedneedle/backend && ../.venv/bin/python -c \
 "from pathlib import Path; from infrastructure.plugins.manifest import load_manifest; print(load_manifest(Path('$REPO_ROOT')))"
```

### 11.5 Manual acceptance on the owner's instance
1. Install from GitHub (Settings > Plugins > paste the repo URL). Enable it with credentials and leave dry-run on.
2. Open the report URL and confirm "Otonose Kanade → 音乃瀬奏" appears, with sensible evidence and reference counts.
3. Optional: run `scripts/diag/dn_artist_diag.sh <MBID>` before and after.
4. Back up `library.db`. Set `dry_run=false`, save, then `POST run` (or disable+enable).
5. Check the artist page and the report.

## 12. v3 portability notes (do not implement now)
- **Runtime:** v3 runs plugins as separate processes over JSON-RPC. `plugin.toml` stays the same, imports come from `droppedneedle_plugin`, and nothing from the server's Python code is importable.
- **Swaps needed:**
  - `HostStateStore` → `ctx.state_get`/`state_set` (string values ≤1 MiB, keys `^[a-z0-9][a-z0-9/_-]{0,63}$`);
  - `PluginRouteResponse` import from `droppedneedle_plugin`;
  - `/api/v1/...` paths → v3 routes. Re-capture fixtures; v3 adds `/api/v3/...` catalog routes, so check their shapes.
- `ctx.http` in v3 offers `get/post/put/delete/head` with `params/headers/json/timeout`, and returns `status_code/headers/content/text/json()`. `api.py` must stay within that subset (no `follow_redirects=` kwarg, no streaming, no `cookies`).
- In-memory state is lost on plugin restart in v3; everything important is already in the state store.

## 13. Milestones (each ends green: `ruff check`, `pytest`)

| # | Deliverable | Acceptance |
|---|---|---|
| **M0** | Already in the kit: `pyproject.toml`, `.gitignore`, `uv.lock`, fixture sanity tests. Add a GitHub Actions workflow (uv, Python 3.13, `ruff check`, `ruff format --check`, `pytest`) | CI green on the first push |
| **M1** | `plugin.toml` (§5), `plugin.py` adapter with sys.path/purge, `config.py`, `folding.py`, `scripts/check_manifest.sh` | manifest check prints the manifest; config & folding tests pass |
| **M2** | `evidence.py`, `planner.py`, `models.py` | §11.1 tests pass using real fixtures |
| **M3** | `api.py`, `state.py`, `runner.py` (dry-run path only), `report.py`, `GET report` route | §11.2 scenarios 1, 7, 8, 9, 11, 12, 13 pass |
| **M4** | live merge path (§7.7), `POST run`, verification & stop-on-mismatch | §11.2 scenarios 2–6, 10, 14 pass |
| **M5** | `scripts/e2e/run_e2e.py`; README (install, settings, safety, report URL, uninstall) | E2E passes locally against v2.15.0 |
| **M6** | polish: optional `plugin_notice`, CHANGELOG, tag `v0.1.0` | owner's dry-run on the real library looks right |

## 14. Coding conventions
- Python 3.13, `from __future__ import annotations`, dataclasses (frozen where possible), and type hints everywhere.
- Async only where I/O happens. Pure modules (`folding`, `evidence`, `planner`, `report`) have no async and no I/O.
- No global mutable state except the instance's last report and background task reference.
- `ruff` with default rules + `I` (imports) + `B` (bugbear), line length 100.
- Logs: lowercase key=value style, matching DroppedNeedle's own.
- Every bug found in E2E gets a unit or integration test first.

## 15. Open decisions for the owner
1. **Interval:** 360 min default (manifest-fixed). Change it before release if you want hourly or daily runs.
2. **`run_on_load = true`:** a run happens right after enabling (good for dry-run feedback). Keep it?
3. **`shared_mbid_without_link`:** report only (planned), or later add an opt-in to merge local-only duplicates with each other?
4. **Repo name / license:** suggested name `droppedneedle-mbid-artist-merge`. Pick a license; for reference, DroppedNeedle itself is AGPL-3.0 and the plugin runs in-process importing a few host types.

## 16. References
- [`docs/BACKGROUND.md`](docs/BACKGROUND.md): root cause with pinned upstream links
- [`docs/API-CONTRACT.md`](docs/API-CONTRACT.md): endpoint contract
- Upstream plugin docs at the tested commit: [PLUGINS.md](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/PLUGINS.md), [docs/PLUGIN-CREATION.md](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/docs/PLUGIN-CREATION.md)
- Plugin host source: [infrastructure/plugins/host.py](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/infrastructure/plugins/host.py), [protocols.py](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/infrastructure/plugins/protocols.py), [manifest.py](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/infrastructure/plugins/manifest.py)
