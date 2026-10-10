# MBID Artist Merge — a DroppedNeedle plugin

DroppedNeedle v2 links local artists to MusicBrainz **by name only**. Files tagged
with a romanized or translated artist name plus the right `MUSICBRAINZ_ARTISTID`
(for example `ARTIST=Otonose Kanade` with the MBID of 音乃瀬奏) therefore end up
as two artists:

- a local-only "Otonose Kanade", with its own artist page;
- a MusicBrainz-linked "音乃瀬奏", which DroppedNeedle creates when albums get identified.

This plugin reads the MBIDs embedded in each local-only artist's files. When the
sampled files agree, and DroppedNeedle already has an artist linked to that MBID,
it merges the local-only artist into the linked one through DroppedNeedle's own
admin API, **keeping the MusicBrainz link**. The linked artist keeps its name and
gains the albums, tracks, favorites and play history; the old artist id redirects
to it. See [`docs/BACKGROUND.md`](docs/BACKGROUND.md) for why the split happens.

## Safety first

- **Dry-run is the default.** Until you set `dry_run` to `false`, the plugin only
  reports what it would merge. Any value other than `false`, `0`, `no` or `off`
  counts as dry-run.
- **Merges cannot be undone.** Before turning dry-run off, back up the library
  database (`/app/cache/library.db` in Docker) while DroppedNeedle is stopped.
- It only ever merges a **local-only** artist into a **MusicBrainz-linked**
  artist, never two linked artists, never "Various Artists" / "Unknown Artist".
- Before every merge it re-reads both artists, previews the merge, and requires
  the preview to report no identity conflicts. It always sends
  `provider_choice: "retain_survivor"`, so the survivor keeps its MusicBrainz link.
  After the merge it checks the survivor still holds the MBID; any surprise stops
  all further merges in that run.
- It never writes to the database or touches audio files. It talks only to the
  server URL you configure, and never logs or reports your password or token.

## Requirements

- DroppedNeedle **v2** with plugin `api_version = 1` (tested on **v2.15.0**).
- A plugins volume: in Docker, mount `/app/plugins`.
- A **local** DroppedNeedle admin account (SSO, Plex and Jellyfin logins cannot
  sign in through the API), or a bearer token of an admin session.

## Install

**From GitHub:** in DroppedNeedle, open **Settings → Plugins**, paste
`https://github.com/deadmanproqn/droppedneedle-mbid-artist-merge` and install.

**By hand:** copy `plugin.toml`, `plugin.py` and the `mbid_artist_merge/` folder
into `/app/plugins/mbid-artist-merge/`, then restart DroppedNeedle (plugins are
discovered at startup).

Then edit the plugin in **Settings → Plugins**, fill in the settings below and
enable it. Enabling starts a dry run straight away; after that it runs every
6 hours.

## Settings

All values are text. Leave a field empty to use the default.

| Setting | Default | Meaning |
|---|---|---|
| `base_url` | `http://127.0.0.1:8688` | How the plugin reaches this server from inside it. Add your `BASE_PATH` if you set one, e.g. `http://127.0.0.1:8688/music`. |
| `username` / `password` | — | A local admin account. The plugin logs in at the start of each run and logs out at the end. |
| `api_token` | — | Instead of username/password: an admin session's bearer token. Tokens expire 30 days after they were issued. |
| `dry_run` | `true` | `false` to actually merge. |
| `max_merges_per_run` | `10` | 0–500. `0` means report-only even when `dry_run` is `false`. |
| `max_artists_per_run` | `200` | 1–10000. Bigger libraries are covered over several runs. |
| `samples_per_artist` | `5` | 1–20 files read per artist, spread across its albums. |
| `min_agreeing_samples` | `2` | 1–20 files that must carry the MBID; lowered automatically for artists with fewer files. |
| `request_delay_ms` | `100` | 0–5000 ms pause between API calls. Raise it if the server feels busy during a run. |
| `time_budget_seconds` | `900` | 30–21540. A run stops cleanly when it runs out and continues next time. |
| `exclude_artist_ids` | — | Local artist ids (comma or space separated) never to touch, as source or survivor. |
| `exclude_mbids` | — | MusicBrainz artist ids never to act on. |

A number out of range is not clamped: the run stops with `status: config_error`
so you notice.

## Reading the report

While signed in as an admin, open

```
https://<your-server>/api/v1/plugins/ext/mbid-artist-merge/report
```

It shows the last run: `status`, `dry_run`, `totals` (one counter per decision)
and `actions` (one entry per artist, with the sampled files as evidence and, for
merges, the preview's reference counts). Artists without any MBID in their files
(`no_mbid`) and results reused from the cache are only counted.

| Decision | Meaning |
|---|---|
| `would_merge` | Dry-run: this artist would be merged into `target`. |
| `merged` | Merged into `target`. |
| `deferred` | Would merge, but `max_merges_per_run` was reached (or merges stopped after an error). Next run. |
| `no_target` | The files agree on an MBID, but no artist is linked to it yet. Identify one of its albums in DroppedNeedle, then the next run can merge. |
| `shared_mbid_without_link` | Like `no_target`, and other local-only artists carry the same MBID (`shared_with`). |
| `conflict` | The files carry different MBIDs for this name (listed in `mbids`). Fix the tags. |
| `ambiguous` | A file credits the name twice with different MBIDs, or has more MBIDs than credited names. |
| `insufficient_evidence` | Too few files carry the MBID. |
| `no_mbid` | No file carries an MBID for this name. |
| `no_samples` | No readable files. |
| `excluded_mbid` / `excluded_target` | Blocked by your exclusions (or a Various/Unknown Artist id). |
| `stale` / `changed` / `already_merged` / `identity_conflict` | The library changed between check and merge; nothing was done. Usually fine next run. |
| `error` | Something unexpected; details are in the entry and the server log. Further merges in that run were stopped. |

Run `status` values other than `ok`: `config_error`, `auth_failed` (wrong
password, non-local account or expired token), `not_admin`, `auth_lost`,
`server_unreachable` (check `base_url`), `library_busy` (DroppedNeedle's own
artist reconciliation is running; retried next time), `lease_held` (another run
is in progress), `internal_error`. `partial: true` means the time budget ran out;
the next run continues where this one stopped.

The server log has one summary line per run
(`mbid-artist-merge run=… status=ok dry_run=true would_merge=1 merged=0 …`) and one
line per merge.

## Going live

1. Read the dry-run report and check every `would_merge` pair.
2. Stop DroppedNeedle, back up `library.db`, start it again.
3. Set `dry_run` to `false` and save. (Saving does not start a run.)
4. Either wait for the next scheduled run, disable and re-enable the plugin, or
   start one now:
   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" \
     https://<your-server>/api/v1/plugins/ext/mbid-artist-merge/run
   ```
5. Check the report and the artist pages.

Optionally, [`scripts/diag/dn_artist_diag.sh`](scripts/diag/dn_artist_diag.sh)
shows (read-only) how your database resolved one MBID, before and after.

## Uninstall

Remove the plugin in **Settings → Plugins** (or delete
`/app/plugins/mbid-artist-merge/` and restart). Its state — cache, cursor, last
report — lives in that folder and goes with it. Merges already done stay done.

## Development

The implementation spec is [`PLAN.md`](PLAN.md); the endpoints it relies on are in
[`docs/API-CONTRACT.md`](docs/API-CONTRACT.md). Runtime code is standard library
only; the HTTP client is injected by the host.

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -q                    # unit + integration against a fake server
bash scripts/check_manifest.sh      # DroppedNeedle's own manifest loader
```

End-to-end against a real DroppedNeedle v2.15.0 built from source (Linux, or
Windows with Git Bash): see [`scripts/e2e/README.md`](scripts/e2e/README.md).
