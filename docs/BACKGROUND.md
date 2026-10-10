# Background: why DroppedNeedle splits one artist into two

Analysis of DroppedNeedle v2.15.0 (commit
[`cf7278a`](https://github.com/DroppedNeedle/DroppedNeedle/tree/cf7278a11454c2c5e7417e486b027dd98dfc9049)).
Links below are pinned to that commit.

## Symptom

Files tagged with a translated or romanized artist name, for example
`ARTIST=Otonose Kanade` plus `MUSICBRAINZ_ARTISTID=<MBID of 音乃瀬奏>`, end up as
two local artists:

- **"Otonose Kanade"**: `local_only`, no MusicBrainz link. Its artist page is the
  local-only page.
- **"音乃瀬奏"**: created by DroppedNeedle, linked to the MBID. Albums that get
  identified (manually or automatically) move here.

The Artist Identity Desk never shows the pair as duplicates.

## Root cause

1. **The scan keys artists by name only.** Each local artist is
   `uuid5("artist:{tag name}:{sort name}:group")`
   ([library_indexer.py#L540-L557](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/services/native/library_indexer.py#L540-L557)).
   The artist MBID is only stored on the track as `embedded_artist_mbid`
   ([#L656-L657](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/services/native/library_indexer.py#L656-L657)).
2. **After an album is identified, a projection rewrites its artist credits
   from MusicBrainz.** `_provider_artist_for_credit_tx`
   ([native_library_store.py#L16244-L16376](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/infrastructure/persistence/native_library_store.py#L16244-L16376))
   picks, in order:
   - the current owner of the MBID;
   - an unlinked local artist whose name exactly equals MusicBrainz's canonical
     or credited name;
   - otherwise, a **new** artist named after the canonical name.

   Embedded MBIDs are never consulted. MusicBrainz aliases are fetched but
   ignored here
   ([`_raw_credits`](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/services/native/artist_identity_reconciliation_service.py#L98-L118)),
   even though DroppedNeedle uses them when *writing* tags with `translate_names`
   ([canonical_release_metadata_service.py#L113](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/services/native/canonical_release_metadata_service.py#L113)).
3. **One MBID, one owner.** `local_artist_external_identities` has
   `UNIQUE(provider, provider_artist_id)`. Once "音乃瀬奏" owns the MBID,
   linking "Otonose Kanade" only files a `SHARED_PROVIDER_IDENTITY` merge
   candidate
   ([#L9056-L9067](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/infrastructure/persistence/native_library_store.py#L9056-L9067)),
   and nothing in the app reads that table.
4. **Automatic convergence is all-or-nothing.** The tag-named artist is retired
   into the linked one only when **every** album and track still credited to it
   has provider proof for that MBID and the credit count matches MusicBrainz.
   The embedded-MBID convergence path still requires identical display names
   ([#L17289-L17292](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/infrastructure/persistence/native_library_store.py#L17289-L17292)).
5. **The Identity Desk groups by identical folded name only**
   ([#L18211-L18221](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/infrastructure/persistence/native_library_store.py#L18211-L18221)),
   so cross-script duplicates never appear. The unreleased v3 rewrite keeps this
   rule (`server/src/library/reconcile/groups.rs`: "as in v2").
6. **Albums with only artist MBIDs do not auto-identify.** The album-artist gate
   vetoes on name distance
   ([album_evidence_engine.py#L297-L320](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/services/native/album_evidence_engine.py#L297-L320)).

## Why a plugin can only clean up, not prevent

The v2 plugin API is walled off from the catalog:

- no SQLite access;
- subscribers are read-only;
- publish kinds don't touch the catalog;
- no public endpoint sets an artist's MBID.

The one catalog write an admin can make through the public HTTP API is an
**artist merge** (`POST /api/v1/library/artists/merge`). So the plugin does what
an admin would do by hand, using the evidence DroppedNeedle ignores:

> When every sampled file of a `local_only` artist carries the same embedded
> MBID, and another artist already holds that MBID as its MusicBrainz link,
> merge the local-only artist into the linked one, keeping the link.

The real fix belongs upstream: before creating a new artist, the projection
should reuse the tag-named artist when its embedded MBIDs agree, and optionally
match MusicBrainz aliases. See the upstream issue draft the owner prepared. Once
that lands, this plugin becomes unnecessary.

## Related upstream behaviours the plugin must respect

- **Artist merge API defaults are unsafe.** `provider_choice` defaults to
  `detach`, which deletes the survivor's MusicBrainz link
  ([library_operations.py#L321](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/api/v1/schemas/library_operations.py#L321)).
- **A retired artist's own link is deleted, not transferred**
  ([#L16015-L16018](https://github.com/DroppedNeedle/DroppedNeedle/blob/cf7278a11454c2c5e7417e486b027dd98dfc9049/backend/infrastructure/persistence/native_library_store.py#L16015-L16018)).
  So the survivor must always be the linked artist.
- **Merges cannot be undone.**
