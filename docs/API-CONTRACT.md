# DroppedNeedle API contract used by the plugin

Verified against **DroppedNeedle v2.15.0** (commit `cf7278a`, plugin `api_version = 1`)
by running the server from source and calling every endpoint below. The raw
request/response pairs are in [`tests/fixtures/api-v2.15.0/`](../tests/fixtures/api-v2.15.0/);
each file holds `request`, `status`, `note`, `body` and, where relevant,
`request_body` and `location`. Re-capture them with
[`scripts/e2e/capture_fixtures.py`](../scripts/e2e/capture_fixtures.py).

All paths are relative to `{base_url}/api/v1`, where `base_url` is the plugin's
`base_url` setting (default `http://127.0.0.1:8688`; add the `BASE_PATH` prefix
when the server runs under one). JSON keys are snake_case exactly as shown.

## General rules

| Topic | Behaviour |
|---|---|
| Auth | `Authorization: Bearer <token>` on every call. A session cookie (`droppedneedle_session`) is also accepted, and `POST /auth/login` sets one on the client that called it. Always send the header; never rely on the cookie. |
| Error envelope | `{"error": {"code": "<CODE>", "message": "<text>", "details": null}}` |
| 401 | `UNAUTHORIZED` — no or invalid/expired token (`unauthenticated.json`, `after_logout.json`). Bad login password also returns 401 but with code `INTERNAL_ERROR` (`auth_login_bad_password.json`). |
| 403 | `FORBIDDEN`, message `Admin access required` — every endpoint marked **admin** below (`forbidden_non_admin_*.json`). |
| 404 | `NOT_FOUND` (`track_tags_not_found.json`). |
| 409 | `STALE_REVISION` — a revision moved, or a merge preview token is invalid/expired (`merge_preview_stale_revision.json`, `merge_apply_bad_token.json`). Also `CONFLICT` for other conflicts. |
| 400 | `VALIDATION_ERROR` (e.g. merge with no source artists). |
| Redirects | Retired (merged-away) artist/album ids answer **308** with `Location` pointing at the survivor (`artist_detail_retired_redirect.json`). The host's shared plugin HTTP client follows redirects, so detect a retired id by comparing the returned `id` with the requested one. |
| Pagination | `GET /library/artists`: `limit` 1–100 (clamped), `offset` ≥ 0, `total` in the body. Other list endpoints used here return everything in one page. |
| Background load | Every `/api/*` request marks the server as "interactive", which pauses background library work while it is in flight. Pace requests (the plugin defaults to 100 ms between calls). |

## Endpoints

### `POST /auth/login` — public
Request: `{"username": "...", "password": "..."}` (local accounts only — SSO/Plex/Jellyfin users cannot log in here).
Response 200: `{"token": "<opaque ~44 chars>", "user": UserResponse}`. Also sets the session cookie.
Tokens expire **30 days** after issue; using a token does not extend it.

### `POST /auth/logout` — any authenticated user
Revokes the bearer token (and clears the cookie). 204, empty body. A later call with that token gets 401.

### `GET /auth/me` — any authenticated user
`UserResponse`: `id, display_name, role ("admin" | "trusted" | "user"), email, avatar_url, username, username_display, providers, musicbrainz_source`.
The plugin requires `role == "admin"`.

### `GET /library/artists/reconciliation` — admin
Progress of DroppedNeedle's own artist-reconciliation backfill:
`{state, completed_count, expected_count, automatically_resolved_count, waiting_for_identity_count, genuine_review_count, provider_conflict_count, ambiguous_credit_structure_count, same_name_only_count, operation_job_id}`.
`state` is the backfill job state (`queued | running | paused | ready | succeeded | failed | cancelled | stopped`) or `idle` when no job exists.

### `GET /library/artists` — any user
Query: `limit` (≤100), `offset`, `sort_by` (`name` default), `sort_order` (`asc`), `q` (substring of the folded name), `scope` = `album` (default) or `contributors`. Any other `scope` (including `all`) silently becomes `album`.

- `album` = artists credited as album artist on at least one album with indexed tracks.
- `contributors` = artists credited only on tracks of albums where they are not an album artist.
- An artist can appear in both lists — union them by `id`.

Response: `{items: TargetNativeArtist[], total, album_artist_total, contributor_total}`.

`TargetNativeArtist`:
```json
{"id": "a8a4098f-...", "name": "Otonose Kanade", "musicbrainz_artist_id": null,
 "artist_identity_state": "local_only", "album_count": 1, "track_count": 3,
 "appearance_release_count": 0, "appearance_track_count": 0,
 "library_relationship": "album_artist", "date_added": 1791521352.15, "row_revision": 1}
```
- `id` is the **local** artist id (despite an internal column name, it is never an MBID).
- `musicbrainz_artist_id` is the MusicBrainz link DroppedNeedle holds for this artist (null when `local_only`). At most one active artist holds a given MBID (database UNIQUE constraint).
- `row_revision` is the value the merge endpoints expect.
- `name` is the artist's display name; for scanner-created artists it is exactly the tag credit name.

### `GET /library/artists/{artist_id}` — any user
One `TargetNativeArtist` (fresh `row_revision`; `library_relationship` can be `both`). Retired id → 308 to the survivor.

### `GET /library/artists/{artist_id}/albums` — any user
`{items: TargetNativeAlbum[], total}` — albums where the artist is the album artist.
`TargetNativeAlbum` keys: `id, title, artist_name, artist_id, musicbrainz_release_group_id, musicbrainz_release_id, musicbrainz_artist_id, album_identity_state, track_count, total_duration_seconds, total_size_bytes, format, year, is_compilation, cover_available, date_added, sort_name, original_release_date, contribution_id, contribution_state`.

### `GET /library/artists/{artist_id}/appearances` — any user
Query: `limit` 1–100 (default 20), `offset`. Albums where the artist is credited on tracks but is not the album artist:
`{items: [{album: TargetNativeAlbum, tracks: TargetNativeTrack[]}], total, total_tracks, offset, limit}`.

### `GET /library/albums/{album_id}/tracks` — any user
`{items: TargetNativeTrack[], total, offset: 0, limit: total}`.
`TargetNativeTrack` keys: `id, title, album_id, album_title, artist_id, artist_name, album_artist_id, album_artist_name, musicbrainz_recording_id, musicbrainz_release_group_id, musicbrainz_artist_id, musicbrainz_album_artist_id, disc_number, track_number, year, genre, duration_seconds, format, bit_rate, sample_rate, bit_depth, channels, file_size_bytes, date_added, cover_available, current_tier, below_cutoff`.
- `artist_id` is the **position-0** track artist only (falls back to the album artist).
- The `musicbrainz_*` fields here are DroppedNeedle's **links**, not the file's tags. Use `/tags` for tag MBIDs.

### `GET /library/tracks/{track_id}/tags` — admin
Reads the audio file **from disk** and returns its tags (`AudioTag`). Relevant keys:
```json
{"artist": "Other Artist & Otonose Kanade", "album_artist": "Other Artist",
 "musicbrainz_artist_id": "2222...", "musicbrainz_album_artist_id": "2222...",
 "musicbrainz_artist_ids": ["2222...", "1111..."],
 "musicbrainz_album_artist_ids": ["2222..."],
 "artists": [
   {"name": "Other Artist", "credited_name": "Other Artist", "sort_name": null,
    "musicbrainz_artist_id": "2222...", "join_phrase": ""},
   {"name": "Otonose Kanade", "credited_name": "Otonose Kanade", "sort_name": null,
    "musicbrainz_artist_id": "1111...", "join_phrase": ""}],
 "album_artists": [{"name": "Other Artist", "musicbrainz_artist_id": "2222...", "...": "..."}]}
```
- `artists` come from `ARTISTS` (multi-value) or else `ARTIST`; `album_artists` from `ALBUMARTISTS` or else `ALBUMARTIST`.
- Each credit's `musicbrainz_artist_id` is paired **by position** with `MUSICBRAINZ_ARTISTID` / `MUSICBRAINZ_ALBUMARTISTID` values; it is `null` when there are fewer IDs than names.
- When there are more IDs than names (e.g. `ARTIST="A feat. B"` with two IDs), the extra IDs exist only in the `*_ids` lists — treat that as ambiguous.
- `musicbrainz_artist_id` / `musicbrainz_album_artist_id` scalars are the first ID of each list.
- Unreadable file → 400 `VALIDATION_ERROR`; unknown track → 404.

### `POST /library/artists/merge-preview` — admin (read-only)
Request:
```json
{"source_artist_ids": ["<retire me>"], "surviving_artist_id": "<keep me>",
 "expected_revisions": {"<retire me>": 1, "<keep me>": 1}}
```
`expected_revisions` must hold the current `row_revision` of **every** artist named, else 409 `STALE_REVISION`. Response 200:
```json
{"preview_token": "1791521384.1722c1...", "source_album_ids": [],
 "target_album_id": "<surviving artist id>", "track_ids": [],
 "identity_conflicts": [], "aliases": ["<retire me>"], "automatic_groups": [],
 "reference_counts": {"album_credits": 2, "track_credits": 6, "primary_albums": 2,
   "migration_references": 0, "favorites": 0, "playlist_snapshots": 0,
   "history": 0, "compatibility_ids": 0}}
```
- `target_album_id` carries the surviving **artist** id (a reused field).
- `identity_conflicts` lists MBIDs when the artists hold more than one distinct MusicBrainz link. The plugin must see `[]`.
- `reference_counts` are summed over all named artists.
- Writes nothing, so it is safe in dry-run.

### `POST /library/artists/merge` — admin (IRREVERSIBLE)
Request = the preview request **with identical values** plus:
```json
{"preview_token": "<from preview>", "idempotency_key": "<unique per intended merge>",
 "provider_choice": "retain_survivor"}
```
- The token is valid for **15 minutes** and is bound to the exact `source_artist_ids`, `surviving_artist_id` and `expected_revisions` sent to preview. Any difference → 409 `STALE_REVISION` "The catalog selection changed after preview." Expired/garbled → 409 "The catalog preview expired…".
- `provider_choice` **defaults to `"detach"`, which deletes the survivor's MusicBrainz link**. Always send `"retain_survivor"`.
- The retired artist's own MusicBrainz link (if any) is deleted, never moved. The plugin only retires `local_only` artists, so nothing is lost.
- Response 200: `{"kind": "merge_artist", "surviving_artist_id": "...", "retired_artist_ids": ["..."], "catalog_revision": 10, "track_ids": [], "source_album_ids": [], "target_album_id": null}`.
- Replaying the same `idempotency_key` returns the stored result with 200 and changes nothing (`merge_apply_replay_same_key.json`).
- Effects:
  - every album credit, track credit, album-artist pointer, favorite, play-history row, playlist entry and compat id of the retired artist moves to the survivor;
  - the retired id becomes an alias (308);
  - the survivor keeps its name and MusicBrainz link, and its `row_revision` is unchanged.
- **Merges cannot be undone** (stated in the upstream v3 README; v2 has no undo path either).

### `GET|POST|DELETE /plugins/ext/{plugin_name}/{subpath}` — per `[[route]]`
Host-mounted plugin routes (`auth` from the manifest; `admin` re-checked at runtime). Handler timeout 5 s, response body capped at 1 MiB, only 200–299/400/404 pass through.

### Plugin admin endpoints (used by the E2E harness, not by the plugin)
- `GET /plugins` → `{plugins: PluginInfo[]}` with `name, enabled, error, active_capabilities, settings_values` (secrets masked as `plugin****`).
- `PUT /plugins/{name}` with `{"enabled": bool, "settings": {key: str}}` saves and reloads all plugins. Sending `plugin****` for a secret keeps the stored value.
- Plugins are discovered at server start and on any plugin save; a brand-new plugin folder needs a restart unless another plugin already exists.
