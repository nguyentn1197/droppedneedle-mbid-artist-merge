# Changelog

## 0.2.1 — 2026-10-10

- Settings panel fits its fixed-height frame (DroppedNeedle sizes it at ~192 px
  and offers no way to resize it): a one-line status bar, then two scrolling
  columns. Left: the counts, clickable to filter. Right: one line per artist,
  full details on hover. Narrow frames show the counts as a scrolling strip.
- Decisions that are only counted (no MBID in files, results reused from the
  cache) explain why when selected, instead of an empty list.

## 0.2.0 — 2026-10-10

- Settings panel (Settings → Plugins → Plugin panel): dry-run/live mode, last
  run status, counts, the artists that need a look (most important first),
  problems and settings errors, and a **Run now** button (two-step confirm in live
  mode). It polls while a run is in progress.
- New admin route `GET …/search`, the panel's only bridge: returns the panel
  state, or starts a run when given the one-time code from that state (single
  use, 10 minutes), so a cross-site link cannot start runs.
- `scripts/ui/preview.py` renders the panel against a fake bridge for review.

## 0.1.0 — 2026-10-10

First release, for DroppedNeedle v2 (plugin `api_version = 1`), tested on v2.15.0.

- Finds local-only artists whose files' embedded `MUSICBRAINZ_ARTISTID` /
  `MUSICBRAINZ_ALBUMARTISTID` tags point at an artist DroppedNeedle has already
  linked to MusicBrainz, and merges them into it, keeping the link.
- Dry-run by default; any value other than `false`/`0`/`no`/`off` is dry-run.
- Merges only `local_only` → `musicbrainz_linked`, always previewed first, with
  fresh revisions, `provider_choice: "retain_survivor"` and a deterministic
  idempotency key; each merge is verified and any mismatch stops further merges.
- Samples up to `samples_per_artist` files per artist across its albums and
  appearances; reports `conflict`, `ambiguous`, `no_target`,
  `shared_mbid_without_link` and the other decisions instead of guessing.
- Runs every 6 hours and when enabled; `POST …/run` starts a run on demand;
  `GET …/report` shows the last report. Runs are paced, time-boxed, resumable
  (cursor) and cached (7 days), and never overlap (lease).
- Publishes a `plugin_notice` for other plugins after a run that merged.
- Standard library only; never writes SQLite or audio files, never calls any URL
  but `base_url`, never logs or reports credentials.
