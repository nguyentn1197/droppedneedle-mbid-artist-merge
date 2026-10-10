# MBID Artist Merge — a DroppedNeedle plugin

> **Status: planned.** The implementation follows [`PLAN.md`](PLAN.md). This README is replaced in milestone M5.

DroppedNeedle (v2) links local artists to MusicBrainz **by name only**. Files
tagged with a romanized or translated artist name, such as `Otonose Kanade`,
plus the right `MUSICBRAINZ_ARTISTID` therefore end up as two artists:

- a local-only "Otonose Kanade";
- a linked "音乃瀬奏" that DroppedNeedle creates when albums get identified.

See [`docs/BACKGROUND.md`](docs/BACKGROUND.md).

This plugin reads the MBIDs embedded in each local-only artist's files. When
they all agree, and DroppedNeedle already has an artist linked to that MBID, it
merges the local-only artist into the linked one through DroppedNeedle's own
admin API, keeping the MusicBrainz link.

- **Dry-run by default.** Read the report first.
- **Merges cannot be undone.** Back up `/app/cache/library.db` before turning dry-run off.
- **Requires** DroppedNeedle v2 with plugin `api_version = 1` (verified on v2.15.0), a mounted `/app/plugins` volume, and a local admin account (or an admin session token).

Layout:

| Path | What |
|---|---|
| `PLAN.md` | full implementation spec |
| `CLAUDE.md` | instructions for Claude Code |
| `docs/API-CONTRACT.md` | DroppedNeedle endpoints the plugin uses, verified |
| `docs/BACKGROUND.md` | root cause analysis |
| `tests/fixtures/api-v2.15.0/` | real API responses captured from v2.15.0 |
| `scripts/e2e/` | run a real DroppedNeedle locally and reproduce the split |
| `scripts/diag/dn_artist_diag.sh` | read-only check of how your own instance resolved one MBID |
