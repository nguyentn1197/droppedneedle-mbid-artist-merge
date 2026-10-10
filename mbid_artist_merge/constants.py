"""Fixed identifiers from DroppedNeedle v2 and MusicBrainz (PLAN §7.1)."""

from __future__ import annotations

import re

VARIOUS_ARTISTS_LOCAL_ID = "00000000-0000-4000-8000-000000000001"
UNKNOWN_ARTIST_LOCAL_ID = "00000000-0000-4000-8000-000000000002"
VARIOUS_ARTISTS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"
UNKNOWN_ARTIST_MBID = "125ec42a-7229-4250-afc5-e057484327fe"

RESERVED_ARTIST_IDS = frozenset({VARIOUS_ARTISTS_LOCAL_ID, UNKNOWN_ARTIST_LOCAL_ID})
RESERVED_MBIDS = frozenset({VARIOUS_ARTISTS_MBID, UNKNOWN_ARTIST_MBID})

# Matches after lowercasing.
MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

LOCAL_ONLY = "local_only"
MUSICBRAINZ_LINKED = "musicbrainz_linked"
