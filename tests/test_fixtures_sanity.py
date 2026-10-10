"""Baseline checks that the captured DroppedNeedle fixtures are complete and coherent.

These pass before any plugin code exists, giving milestone M0 a green suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "api-v2.15.0"
KANADE_MBID = "11111111-1111-4111-8111-111111111111"

EXPECTED_FILES = {
    "after_logout": 401,
    "album_tracks": 200,
    "artist_albums": 200,
    "artist_appearances": 200,
    "artist_detail_linked": 200,
    "artist_detail_local_only": 200,
    "artist_detail_retired_redirect": 308,
    "artist_detail_survivor_after_merge": 200,
    "artists_scope_album": 200,
    "artists_scope_album_after_merge": 200,
    "artists_scope_all_rejected": 200,
    "artists_scope_contributors": 200,
    "artists_scope_contributors_after_merge": 200,
    "auth_login": 200,
    "auth_login_bad_password": 401,
    "auth_logout": 204,
    "auth_me": 200,
    "forbidden_non_admin_merge_preview": 403,
    "forbidden_non_admin_tags": 403,
    "merge_apply": 200,
    "merge_apply_bad_token": 409,
    "merge_apply_replay_same_key": 200,
    "merge_preview": 200,
    "merge_preview_stale_revision": 409,
    "reconciliation_progress": 200,
    "track_tags_collab_contributor": 200,
    "track_tags_conflict_1": 200,
    "track_tags_conflict_2": 200,
    "track_tags_not_found": 404,
    "track_tags_romanized": 200,
    "unauthenticated": 401,
}


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(("name", "status"), sorted(EXPECTED_FILES.items()))
def test_fixture_present_with_status(name: str, status: int) -> None:
    record = load(name)
    assert record["status"] == status
    assert record["request"].split(" ", 1)[0] in {"GET", "POST", "PUT", "DELETE"}


def test_error_envelopes_have_code_and_message() -> None:
    for name, status in EXPECTED_FILES.items():
        if status >= 400:
            body = load(name)["body"]
            assert set(body["error"]) >= {"code", "message"}, name


def test_split_state_before_merge() -> None:
    items = {a["name"]: a for a in load("artists_scope_album")["body"]["items"]}
    assert items["Otonose Kanade"]["artist_identity_state"] == "local_only"
    assert items["音乃瀬奏"]["artist_identity_state"] == "musicbrainz_linked"
    assert items["音乃瀬奏"]["musicbrainz_artist_id"] == KANADE_MBID


def test_merge_keeps_link_and_retires_source() -> None:
    applied = load("merge_apply")
    assert applied["request_body"]["provider_choice"] == "retain_survivor"
    body = applied["body"]
    assert body["kind"] == "merge_artist"
    source = applied["request_body"]["source_artist_ids"][0]
    assert body["retired_artist_ids"] == [source]
    after = {a["name"]: a for a in load("artists_scope_album_after_merge")["body"]["items"]}
    assert "Otonose Kanade" not in after
    assert after["音乃瀬奏"]["musicbrainz_artist_id"] == KANADE_MBID
    redirect = load("artist_detail_retired_redirect")
    assert redirect["location"].endswith(body["surviving_artist_id"])


def _session_tokens(value: object) -> list[object]:
    if isinstance(value, dict):
        found = [v for k, v in value.items() if k == "token"]
        return found + [t for v in value.values() for t in _session_tokens(v)]
    if isinstance(value, list):
        return [t for item in value for t in _session_tokens(item)]
    return []


def test_session_tokens_are_redacted() -> None:
    for path in FIXTURES.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert all(t == "<redacted-session-token>" for t in _session_tokens(data)), path.name


def test_snapshot_covers_every_track_tag() -> None:
    snapshot = json.loads((FIXTURES / "library_snapshot.json").read_text(encoding="utf-8"))[
        "responses"
    ]
    tags = [key for key in snapshot if key.endswith("/tags")]
    assert len(tags) == 10
    assert all(entry["status"] == 200 for entry in snapshot.values())
    assert "/library/artists?limit=100&offset=0&scope=album" in snapshot
    assert "/library/artists?limit=100&offset=0&scope=contributors" in snapshot
