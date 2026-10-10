"""PLAN §11.2 integration scenarios for the live merge path (§7.7)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_dn import ORIGIN, FakeDN
from fixture_data import (
    KANADE_ID,
    KANADE_MBID,
    OTONOSE_ID,
    body,
)
from harness import Harness, by_source

from mbid_artist_merge.state import CACHE_KEY

ALIAS_ID = "zz-kanade"
MERGE = ("POST", "/library/artists/merge")
PREVIEW = ("POST", "/library/artists/merge-preview")


def live(**settings: str) -> Harness:
    h = Harness()
    h.settings.update(dry_run="false", **settings)
    return h


def add_second_alias(h: Harness) -> None:
    h.fake.add_artist(ALIAS_ID, "Kanade Otonose", tracks=2, tag_mbid=KANADE_MBID)


async def list_view(fake: FakeDN, scope: str) -> list[tuple[str, str | None, str]]:
    response = await fake.client().get(
        f"{ORIGIN}/api/v1/library/artists",
        params={"scope": scope, "limit": 100, "offset": 0},
        headers={"Authorization": f"Bearer {fake.issue_token()}"},
    )
    return [
        (a["name"], a["musicbrainz_artist_id"], a["artist_identity_state"])
        for a in response.json()["items"]
    ]


# --- scenario 2: live run, then a no-op ---------------------------------------


async def test_live_run_merges_once_keeping_the_link() -> None:
    h = live()
    report = await h.run()

    assert report["status"] == "ok"
    assert report["dry_run"] is False
    assert report["totals"]["merged"] == 1
    assert report["totals"]["would_merge"] == 0
    (merge,) = h.fake.merges
    assert merge["provider_choice"] == "retain_survivor"
    assert merge["source_artist_ids"] == [OTONOSE_ID]
    assert merge["surviving_artist_id"] == KANADE_ID
    assert merge["expected_revisions"] == {OTONOSE_ID: 1, KANADE_ID: 1}
    assert merge["idempotency_key"] == f"mbid-artist-merge:v1:{OTONOSE_ID}:1:{KANADE_ID}:1"

    action = by_source(report)["Otonose Kanade"]
    assert action["decision"] == "merged"
    assert action["target"]["name"] == "音乃瀬奏"
    assert action["reference_counts"] == body("merge_preview")["reference_counts"]

    assert h.fake.retired == {OTONOSE_ID: KANADE_ID}
    assert h.fake.state[KANADE_ID]["musicbrainz_artist_id"] == KANADE_MBID
    assert h.fake.state[KANADE_ID]["artist_identity_state"] == "musicbrainz_linked"
    # preview before merge, survivor re-read after
    keys = [c.key for c in h.fake.calls]
    merge_at = keys.index("POST /library/artists/merge")
    assert keys[merge_at - 1] == "POST /library/artists/merge-preview"
    assert keys[merge_at + 1] == f"GET /library/artists/{KANADE_ID}"


async def test_fake_after_merge_matches_the_real_server() -> None:
    h = live()
    await h.run()
    for scope in ("album", "contributors"):
        captured = body(f"artists_scope_{scope}_after_merge")["items"]
        expected = [
            (a["name"], a["musicbrainz_artist_id"], a["artist_identity_state"]) for a in captured
        ]
        assert await list_view(h.fake, scope) == expected


async def test_second_live_run_is_a_noop() -> None:
    h = live()
    await h.run()
    h.fake.calls.clear()
    report = await h.run()
    assert report["status"] == "ok"
    assert report["totals"]["merged"] == 0
    assert report["totals"]["would_merge"] == 0
    assert report["totals"]["candidates"] == 4
    assert h.fake.calls_to(*MERGE) == []
    assert h.fake.calls_to(*PREVIEW) == []
    assert OTONOSE_ID not in h.state(CACHE_KEY)["artists"]  # pruned with the inventory


async def test_dry_run_then_live_then_dry_run() -> None:
    h = Harness()
    assert (await h.run())["totals"]["would_merge"] == 1
    h.settings["dry_run"] = "off"
    assert (await h.run())["totals"]["merged"] == 1
    h.settings["dry_run"] = "true"
    report = await h.run()
    assert report["totals"]["would_merge"] == 0
    assert len(h.fake.merges) == 1


# --- scenario 3: stale revisions -----------------------------------------------


async def test_revision_moves_before_preview_is_stale() -> None:
    h = live()
    calls = []

    def bump(fake: FakeDN, call: object) -> None:
        calls.append(call)
        if len(calls) == 1:  # after the step-1 re-read, before the preview
            fake.set_artist(OTONOSE_ID, row_revision=2)

    h.fake.before("GET", f"/library/artists/{KANADE_ID}", bump)
    report = await h.run()
    assert report["status"] == "ok"
    action = by_source(report)["Otonose Kanade"]
    assert action["decision"] == "stale"
    assert "STALE_REVISION" in action["detail"]
    assert h.fake.merges == []


async def test_revision_moves_between_preview_and_merge_is_stale() -> None:
    h = live()
    h.fake.before(*MERGE, lambda fake, call: fake.set_artist(KANADE_ID, row_revision=7))
    report = await h.run()
    assert by_source(report)["Otonose Kanade"]["decision"] == "stale"
    assert h.fake.merges == []
    assert h.fake.retired == {}


async def test_stale_is_retried_next_run() -> None:
    h = live()
    h.fake.fail(*PREVIEW, kind=409)
    assert by_source(await h.run())["Otonose Kanade"]["decision"] == "stale"
    assert (await h.run())["totals"]["merged"] == 1


# --- scenario 4: identity conflicts ---------------------------------------------


@pytest.mark.parametrize("dry_run", ["true", "false"])
async def test_identity_conflict_is_skipped(dry_run: str) -> None:
    h = Harness()
    h.settings["dry_run"] = dry_run
    h.fake.extra_identity_conflicts = ["99999999-9999-4999-8999-999999999999"]
    report = await h.run()
    action = by_source(report)["Otonose Kanade"]
    assert action["decision"] == "identity_conflict"
    assert "99999999" in action["detail"]
    assert h.fake.calls_to(*MERGE) == []


# --- scenario 5: source already retired -----------------------------------------


@pytest.mark.parametrize("follow", [True, False])
async def test_retired_source_is_already_merged(follow: bool) -> None:
    h = Harness(follow_redirects=follow)
    h.settings["dry_run"] = "false"

    def retire(fake: FakeDN, call: object) -> None:
        fake.retired[OTONOSE_ID] = KANADE_ID

    h.fake.before("GET", f"/library/artists/{OTONOSE_ID}", retire)
    report = await h.run()
    action = by_source(report)["Otonose Kanade"]
    assert action["decision"] == "already_merged"
    assert KANADE_ID in action["detail"]
    assert h.fake.calls_to(*PREVIEW) == []


# --- scenario 6: things changed under us -----------------------------------------


async def test_survivor_lost_its_mbid_is_changed() -> None:
    h = live()

    def unlink(fake: FakeDN, call: object) -> None:
        fake.set_artist(KANADE_ID, musicbrainz_artist_id=None, artist_identity_state="local_only")

    h.fake.before("GET", f"/library/artists/{KANADE_ID}", unlink)
    report = await h.run()
    action = by_source(report)["Otonose Kanade"]
    assert action["decision"] == "changed"
    assert h.fake.calls_to(*PREVIEW) == []


async def test_survivor_relinked_to_another_mbid_is_changed() -> None:
    h = live()
    other = "77777777-7777-4777-8777-777777777777"
    h.fake.before(
        "GET",
        f"/library/artists/{KANADE_ID}",
        lambda fake, call: fake.set_artist(KANADE_ID, musicbrainz_artist_id=other),
    )
    assert by_source(await h.run())["Otonose Kanade"]["decision"] == "changed"


async def test_source_got_linked_is_changed() -> None:
    h = live()
    h.fake.before(
        "GET",
        f"/library/artists/{OTONOSE_ID}",
        lambda fake, call: fake.set_artist(OTONOSE_ID, artist_identity_state="musicbrainz_linked"),
    )
    assert by_source(await h.run())["Otonose Kanade"]["decision"] == "changed"


# --- scenario 10: merge cap --------------------------------------------------------


async def test_max_merges_one_defers_the_second() -> None:
    h = live(max_merges_per_run="1")
    add_second_alias(h)
    report = await h.run()
    actions = by_source(report)
    assert actions["Otonose Kanade"]["decision"] == "merged"  # a8a4... sorts first
    assert actions["Kanade Otonose"]["decision"] == "deferred"
    assert "max_merges_per_run=1" in actions["Kanade Otonose"]["detail"]
    assert len(h.fake.merges) == 1
    assert len(h.fake.calls_to(*PREVIEW)) == 1

    report = await h.run()
    assert by_source(report)["Kanade Otonose"]["decision"] == "merged"
    assert len(h.fake.merges) == 2


async def test_max_merges_zero_is_report_only() -> None:
    h = live(max_merges_per_run="0")
    report = await h.run()
    assert by_source(report)["Otonose Kanade"]["decision"] == "deferred"
    assert h.fake.calls_to(*PREVIEW) == []
    assert h.fake.calls_to(*MERGE) == []


# --- verification & stop-on-mismatch ---------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {"kind": "merge_album"},
        {"surviving_artist_id": "someone-else"},
        {"retired_artist_ids": []},
    ],
)
async def test_bad_merge_response_is_an_error_and_stops_merging(patch: dict) -> None:
    h = live()
    add_second_alias(h)
    h.fake.merge_result_patch = patch
    report = await h.run()
    actions = by_source(report)
    assert actions["Otonose Kanade"]["decision"] == "error"
    assert "verification failed" in actions["Otonose Kanade"]["detail"]
    assert actions["Kanade Otonose"]["decision"] == "deferred"
    assert actions["Kanade Otonose"]["detail"] == "merges stopped"
    assert len(h.fake.merges) == 1


async def test_survivor_unlinked_after_merge_is_an_error() -> None:
    h = live()
    add_second_alias(h)

    def unlink_after_merge(fake: FakeDN, call: object) -> None:
        if fake.merges:
            fake.set_artist(KANADE_ID, musicbrainz_artist_id=None)

    h.fake.before("GET", f"/library/artists/{KANADE_ID}", unlink_after_merge)
    report = await h.run()
    actions = by_source(report)
    assert actions["Otonose Kanade"]["decision"] == "error"
    assert "survivor link changed" in actions["Otonose Kanade"]["detail"]
    assert actions["Kanade Otonose"]["decision"] == "deferred"


async def test_preview_failure_stops_merging() -> None:
    h = live()
    add_second_alias(h)
    h.fake.fail(*PREVIEW, kind=400)
    report = await h.run()
    actions = by_source(report)
    assert actions["Otonose Kanade"]["decision"] == "error"
    assert actions["Kanade Otonose"]["decision"] == "deferred"
    assert h.fake.merges == []


async def test_merge_connection_error_is_retried_as_a_replay() -> None:
    h = live()
    h.fake.fail(*MERGE, kind="connect")
    report = await h.run()
    assert report["totals"]["merged"] == 1
    attempts = h.fake.calls_to(*MERGE)
    assert len(attempts) == 2
    assert attempts[0].json == attempts[1].json  # same idempotency key and token


async def test_replayed_idempotency_key_counts_as_merged() -> None:
    h = live()
    first = await h.run()
    assert first["totals"]["merged"] == 1
    # the stored result replays: unretire the source to force a second attempt
    del h.fake.retired[OTONOSE_ID]
    h.fake.scope_items["album"][OTONOSE_ID] = body("artists_scope_album")["items"][3]
    h.store.data.clear()
    report = await h.run()
    assert report["totals"]["merged"] == 1
    assert len(h.fake.merges) == 1  # the second POST replayed, nothing applied


# --- the literal ------------------------------------------------------------------


def test_provider_choice_literal_lives_in_exactly_one_place() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime = [root / "plugin.py", *sorted((root / "mbid_artist_merge").glob("*.py"))]
    code = "".join(path.read_text(encoding="utf-8") for path in runtime)
    assert code.count('"provider_choice":') == 1
    assert code.count('"retain_survivor"') == 1
    assert '"provider_choice": "retain_survivor"' in (root / "mbid_artist_merge/api.py").read_text(
        encoding="utf-8"
    )
