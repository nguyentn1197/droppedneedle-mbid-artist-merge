#!/usr/bin/env bash
# Read-only check of how DroppedNeedle has resolved one MusicBrainz artist.
# Usage: ./dn_artist_diag.sh <artist-mbid> [container-name]
# Example: ./dn_artist_diag.sh 1234abcd-... droppedneedle
set -euo pipefail
MBID="${1:?pass the artist MBID from your MUSICBRAINZ_ARTISTID tag}"
CONTAINER="${2:-droppedneedle}"

docker exec -i -e MBID="$MBID" "$CONTAINER" python3 - <<'PY'
import os, sqlite3
db = sqlite3.connect("file:/app/cache/library.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row
m = os.environ["MBID"].strip().lower()

def show(title, sql):
    print(f"\n== {title}")
    rows = db.execute(sql, {"m": m}).fetchall()
    if not rows:
        print("  (none)")
    for row in rows:
        print("  " + " | ".join(f"{k}={row[k]}" for k in row.keys()))

show("Local artists tied to this MBID (by link, or by embedded tag on their tracks)", """
SELECT a.id, a.display_name, a.sort_name, a.row_revision, i.provider_artist_id, i.decision_source
FROM local_artists a
LEFT JOIN local_artist_external_identities i ON i.local_artist_id = a.id AND i.provider = 'musicbrainz'
WHERE a.retired_into_artist_id IS NULL AND (
  i.provider_artist_id = :m OR a.id IN (
    SELECT ta.local_artist_id FROM local_track_artists ta
    JOIN local_tracks t ON t.id = ta.local_track_id
    WHERE lower(t.embedded_artist_mbid) = :m OR lower(t.embedded_album_artist_mbid) = :m))
""")

show("Tracks carrying this MBID in their tags, by the local artist they are credited to", """
SELECT ar.display_name AS local_artist, i.provider_artist_id AS linked_mbid,
       t.embedded_release_group_mbid IS NOT NULL AS has_release_group_tag,
       ie.release_group_mbid IS NOT NULL AS album_identified, COUNT(*) AS tracks
FROM local_tracks t
JOIN local_track_artists ta ON ta.local_track_id = t.id AND ta.position = 0
JOIN local_artists ar ON ar.id = ta.local_artist_id
LEFT JOIN local_artist_external_identities i ON i.local_artist_id = ar.id AND i.provider = 'musicbrainz'
LEFT JOIN local_album_external_identities ie ON ie.local_album_id = t.local_album_id
WHERE t.availability = 'indexed'
  AND (lower(t.embedded_artist_mbid) = :m OR lower(t.embedded_album_artist_mbid) = :m)
GROUP BY 1, 2, 3, 4
""")

show("Albums with this MBID in their tags: identity and artist-reconciliation state", """
SELECT al.title, ar.display_name AS album_artist, ie.decision_source, rs.state, rs.reason_code
FROM local_albums al
JOIN local_artists ar ON ar.id = al.album_artist_id
LEFT JOIN local_album_external_identities ie ON ie.local_album_id = al.id
LEFT JOIN library_artist_reconciliation_state rs ON rs.local_album_id = al.id
WHERE al.retired_into_album_id IS NULL AND al.id IN (
  SELECT local_album_id FROM local_tracks
  WHERE lower(embedded_artist_mbid) = :m OR lower(embedded_album_artist_mbid) = :m)
ORDER BY 2, 1
""")
PY
