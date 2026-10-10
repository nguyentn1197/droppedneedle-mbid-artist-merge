"""Pure: what one ``GET /library/tracks/{id}/tags`` payload says about an artist (PLAN §7.5).

Only per-credit ``musicbrainz_artist_id`` values are used. The top-level scalars
are just the first id of each list and can belong to a different credit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .folding import fold
from .models import CreditEvidence, Role, normalize_mbid

# (credits key, positional ids key); the role's own pool first, then the other.
POOLS: dict[str, tuple[tuple[str, str], ...]] = {
    "album_artist": (
        ("album_artists", "musicbrainz_album_artist_ids"),
        ("artists", "musicbrainz_artist_ids"),
    ),
    "track_artist": (
        ("artists", "musicbrainz_artist_ids"),
        ("album_artists", "musicbrainz_album_artist_ids"),
    ),
}

NO_MBID = CreditEvidence(None, "no_mbid_in_file")


def extract(name: str, role: Role, tag: Mapping[str, Any]) -> CreditEvidence:
    target = fold(name)
    if not target:
        return NO_MBID
    for credits_key, ids_key in POOLS[role]:
        pool = _list(tag.get(credits_key))
        matches = [c for c in pool if isinstance(c, Mapping) and _names_match(c, target)]
        if not matches:
            continue
        if len(_list(tag.get(ids_key))) > len(pool):  # e.g. ARTIST="A feat. B" + two IDs
            return CreditEvidence(None, "ambiguous", "more_ids_than_credits")
        raw_ids = {_text(c.get("musicbrainz_artist_id")).strip().lower() for c in matches}
        raw_ids.discard("")
        if len(raw_ids) > 1:
            return CreditEvidence(None, "ambiguous", "same_name_different_ids")
        if raw_ids:
            mbid = normalize_mbid(raw_ids.pop())
            if mbid is not None:
                credit = next(
                    c for c in matches if normalize_mbid(c.get("musicbrainz_artist_id")) == mbid
                )
                return CreditEvidence(mbid, "credit", credits_key, _text(credit.get("name")))
        # the matching credit carries no usable MBID in this pool: try the other pool
    return NO_MBID


def _names_match(credit: Mapping[str, Any], target: str) -> bool:
    return (
        fold(_text(credit.get("name"))) == target
        or fold(_text(credit.get("credited_name"))) == target
    )


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
