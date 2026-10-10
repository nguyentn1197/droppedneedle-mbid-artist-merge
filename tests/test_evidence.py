from __future__ import annotations

from typing import Any

import pytest
from fixture_data import (
    CONFLICT_MBID_1,
    CONFLICT_MBID_2,
    KANADE_MBID,
    OTHER_MBID,
    body,
)

from mbid_artist_merge.evidence import extract
from mbid_artist_merge.models import CreditEvidence


def credit(name: str, mbid: str | None, credited_name: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "credited_name": credited_name if credited_name is not None else name,
        "sort_name": None,
        "musicbrainz_artist_id": mbid,
        "join_phrase": "",
    }


def tag(
    artists: list[dict[str, Any]],
    album_artists: list[dict[str, Any]],
    artist_ids: list[str] | None = None,
    album_artist_ids: list[str] | None = None,
) -> dict[str, Any]:
    if artist_ids is None:
        artist_ids = [c["musicbrainz_artist_id"] for c in artists if c["musicbrainz_artist_id"]]
    if album_artist_ids is None:
        album_artist_ids = [
            c["musicbrainz_artist_id"] for c in album_artists if c["musicbrainz_artist_id"]
        ]
    return {
        "artists": artists,
        "album_artists": album_artists,
        "musicbrainz_artist_ids": artist_ids,
        "musicbrainz_album_artist_ids": album_artist_ids,
        # top-level scalars deliberately disagree: they must never be used
        "musicbrainz_artist_id": OTHER_MBID,
        "musicbrainz_album_artist_id": OTHER_MBID,
    }


# --- real fixtures ---------------------------------------------------------


def test_romanized_album_artist() -> None:
    ev = extract("Otonose Kanade", "album_artist", body("track_tags_romanized"))
    assert ev == CreditEvidence(KANADE_MBID, "credit", "album_artists", "Otonose Kanade")


def test_collab_contributor_position_one() -> None:
    ev = extract("Otonose Kanade", "track_artist", body("track_tags_collab_contributor"))
    assert ev.mbid == KANADE_MBID
    assert ev.reason == "credit"
    assert ev.detail == "artists"


def test_collab_album_artist_is_position_zero() -> None:
    ev = extract("Other Artist", "album_artist", body("track_tags_collab_contributor"))
    assert ev.mbid == OTHER_MBID
    assert ev.detail == "album_artists"


@pytest.mark.parametrize(
    ("fixture", "mbid"),
    [("track_tags_conflict_1", CONFLICT_MBID_1), ("track_tags_conflict_2", CONFLICT_MBID_2)],
)
def test_conflict_falls_through_to_secondary_pool(fixture: str, mbid: str) -> None:
    ev = extract("Conflict Name", "album_artist", body(fixture))
    assert ev.mbid == mbid
    assert ev.detail == "artists"


def test_name_matching_is_folded() -> None:
    ev = extract("  OTONOSE   kanade ", "album_artist", body("track_tags_romanized"))
    assert ev.mbid == KANADE_MBID


def test_name_absent_from_real_tags() -> None:
    ev = extract("Some Band", "album_artist", body("track_tags_romanized"))
    assert ev == CreditEvidence(None, "no_mbid_in_file", None, None)


# --- synthetic -------------------------------------------------------------


def test_more_ids_than_credits_is_ambiguous() -> None:
    t = tag(
        [credit("Otonose Kanade", KANADE_MBID)],
        [],
        artist_ids=[KANADE_MBID, OTHER_MBID],
    )
    ev = extract("Otonose Kanade", "track_artist", t)
    assert ev == CreditEvidence(None, "ambiguous", "more_ids_than_credits", None)


def test_same_name_twice_with_different_ids_is_ambiguous() -> None:
    t = tag([credit("Otonose Kanade", KANADE_MBID), credit("Otonose Kanade", OTHER_MBID)], [])
    ev = extract("Otonose Kanade", "track_artist", t)
    assert (ev.reason, ev.detail) == ("ambiguous", "same_name_different_ids")


def test_same_name_twice_with_same_id_is_fine() -> None:
    t = tag([credit("Otonose Kanade", KANADE_MBID), credit("otonose kanade", KANADE_MBID)], [])
    assert extract("Otonose Kanade", "track_artist", t).mbid == KANADE_MBID


def test_same_name_one_id_missing_uses_the_other() -> None:
    t = tag(
        [credit("Otonose Kanade", None), credit("Otonose Kanade", KANADE_MBID)],
        [],
        artist_ids=[KANADE_MBID],
    )
    assert extract("Otonose Kanade", "track_artist", t).mbid == KANADE_MBID


def test_invalid_mbid_string_is_no_mbid() -> None:
    t = tag(
        [credit("Otonose Kanade", "not-a-uuid")],
        [credit("Otonose Kanade", "1111")],
    )
    ev = extract("Otonose Kanade", "album_artist", t)
    assert ev == CreditEvidence(None, "no_mbid_in_file", None, None)


def test_invalid_primary_falls_through_to_valid_secondary() -> None:
    t = tag(
        [credit("Otonose Kanade", KANADE_MBID)],
        [credit("Otonose Kanade", "garbage")],
    )
    ev = extract("Otonose Kanade", "album_artist", t)
    assert (ev.mbid, ev.detail) == (KANADE_MBID, "artists")


def test_uppercase_and_padded_mbid_is_normalised() -> None:
    t = tag([credit("Otonose Kanade", f"  {KANADE_MBID.upper()} ")], [], artist_ids=["x"])
    assert extract("Otonose Kanade", "track_artist", t).mbid == KANADE_MBID


def test_credited_name_matches() -> None:
    t = tag([credit("音乃瀬奏", KANADE_MBID, credited_name="Otonose Kanade")], [])
    ev = extract("Otonose Kanade", "track_artist", t)
    assert ev.mbid == KANADE_MBID
    assert ev.credit_name == "音乃瀬奏"


def test_primary_pool_wins_over_secondary() -> None:
    t = tag([credit("Otonose Kanade", OTHER_MBID)], [credit("Otonose Kanade", KANADE_MBID)])
    assert extract("Otonose Kanade", "album_artist", t).mbid == KANADE_MBID
    assert extract("Otonose Kanade", "track_artist", t).mbid == OTHER_MBID


def test_ambiguity_only_counts_in_the_matching_pool() -> None:
    # extra ids in the album-artist pool don't matter when the name matches in artists
    t = tag(
        [credit("Otonose Kanade", KANADE_MBID)],
        [credit("Somebody", None)],
        album_artist_ids=[OTHER_MBID, CONFLICT_MBID_1],
    )
    assert extract("Otonose Kanade", "album_artist", t).mbid == KANADE_MBID


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"artists": None, "album_artists": None},
        {"artists": [{"name": None, "musicbrainz_artist_id": KANADE_MBID}]},
        {"artists": "not-a-list"},
        {"artists": ["not-a-dict"]},
    ],
)
def test_malformed_payloads_are_no_mbid(payload: dict[str, Any]) -> None:
    ev = extract("Otonose Kanade", "track_artist", payload)
    assert ev == CreditEvidence(None, "no_mbid_in_file", None, None)


def test_empty_artist_name_never_matches() -> None:
    t = tag([credit("", KANADE_MBID)], [])
    assert extract("  ", "track_artist", t).mbid is None
