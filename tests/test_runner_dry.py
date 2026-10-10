"""PLAN §11.2 integration scenarios for the read-only (dry-run) path."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fake_dn import LISTENER_PASSWORD, LISTENER_USER, FakeDN, Fault
from fixture_data import (
    CONFLICT_ID,
    CONFLICT_MBID_1,
    CONFLICT_MBID_2,
    KANADE_ID,
    KANADE_MBID,
    LONELY_ID,
    LONELY_MBID,
    OTHER_ID,
    OTHER_MBID,
    OTONOSE_ID,
    SOME_BAND_ID,
    body,
)
from harness import EPOCH, Harness, by_source, nonzero

from mbid_artist_merge.report import iso
from mbid_artist_merge.state import CACHE_KEY, CURSOR_KEY, LEASE_KEY, REPORT_KEY

SORTED_CANDIDATES = sorted([OTONOSE_ID, OTHER_ID, LONELY_ID, SOME_BAND_ID, CONFLICT_ID])


# --- scenario 1: dry-run on the six-artist library --------------------------


async def test_dry_run_decisions() -> None:
    h = Harness()
    report = await h.run()

    assert report["status"] == "ok"
    assert report["dry_run"] is True
    assert report["partial"] is False
    assert report["trigger"] == "manual"
    assert nonzero(report) == {
        "artists_listed": 6,
        "linked": 1,
        "candidates": 5,
        "examined": 5,
        "would_merge": 1,
        "no_target": 2,
        "conflict": 1,
        "no_mbid": 1,
    }
    actions = by_source(report)
    assert set(actions) == {"Otonose Kanade", "Other Artist", "Lonely Alias", "Conflict Name"}

    merge = actions["Otonose Kanade"]
    assert merge["decision"] == "would_merge"
    assert merge["target"] == {"id": KANADE_ID, "name": "音乃瀬奏", "row_revision": 1}
    assert merge["mbid"] == KANADE_MBID
    assert merge["reference_counts"] == body("merge_preview")["reference_counts"]
    assert [e["role"] for e in merge["evidence"]] == ["album_artist"] * 3 + ["track_artist"]
    assert {e["mbid"] for e in merge["evidence"]} == {KANADE_MBID}

    assert actions["Other Artist"]["mbid"] == OTHER_MBID
    assert actions["Lonely Alias"]["mbid"] == LONELY_MBID
    assert actions["Conflict Name"]["mbids"] == [CONFLICT_MBID_1, CONFLICT_MBID_2]


async def test_dry_run_never_merges_and_previews_once() -> None:
    h = Harness()
    await h.run()
    assert h.fake.calls_to("POST", "/library/artists/merge") == []
    previews = h.fake.calls_to("POST", "/library/artists/merge-preview")
    assert len(previews) == 1
    assert previews[0].json == {
        "source_artist_ids": [OTONOSE_ID],
        "surviving_artist_id": KANADE_ID,
        "expected_revisions": {OTONOSE_ID: 1, KANADE_ID: 1},
    }
    assert h.fake.merges == []


async def test_every_call_sends_the_bearer_header_and_logs_out() -> None:
    h = Harness()
    await h.run()
    login, *rest = h.fake.calls
    assert login.key == "POST /auth/login"
    assert login.authorization is None
    assert rest[-1].key == "POST /auth/logout"
    assert all(c.authorization and c.authorization.startswith("Bearer ") for c in rest)
    assert h.fake.tokens == {}  # the session was revoked


async def test_state_after_a_dry_run() -> None:
    h = Harness()
    report = await h.run()
    assert h.state(LEASE_KEY) == {}
    assert h.state(CURSOR_KEY) == {"after_id": SORTED_CANDIDATES[-1]}
    assert h.state(REPORT_KEY) == report
    assert h.reports == [report]
    cache = h.state(CACHE_KEY)
    assert cache["version"] == 1
    assert {k: v["decision"] for k, v in cache["artists"].items()} == {
        OTONOSE_ID: "would_merge",
        OTHER_ID: "no_target",
        LONELY_ID: "no_target",
        SOME_BAND_ID: "no_mbid",
        CONFLICT_ID: "conflict",
    }
    assert cache["artists"][CONFLICT_ID]["checked_at"] == iso(h.clock.now())


async def test_pacing_sleeps_between_calls() -> None:
    h = Harness()
    h.settings["request_delay_ms"] = "250"
    await h.run()
    assert h.clock.sleeps == [0.25] * (len(h.fake.calls) - 1)


async def test_second_dry_run_reuses_cache() -> None:
    h = Harness()
    await h.run()
    h.fake.calls.clear()
    report = await h.run()
    assert report["totals"]["skipped_cached"] == 4  # all but would_merge
    assert report["totals"]["would_merge"] == 1
    assert {a["source"]["name"] for a in report["actions"]} == {"Otonose Kanade"}
    tag_reads = [c for c in h.fake.calls if c.path.endswith("/tags")]
    assert {c.path.split("/")[3] for c in tag_reads} == {
        "3e5d06c1-409f-5fab-a37f-880b11eb40aa",
        "9dd04489-8f7b-52ce-8fc3-75b85d2e978e",
        "e2de1064-f27a-55dd-8a39-29105ff82632",
        "38b0df8e-9b5a-532a-99ff-7db15d4fac06",
    }


async def test_cache_expires_after_seven_days_and_on_new_revision() -> None:
    h = Harness()
    await h.run()
    h.fake.set_artist(CONFLICT_ID, row_revision=2)
    report = await h.run()
    assert report["totals"]["skipped_cached"] == 3
    h.clock.advance(7 * 24 * 3600 + 1)
    report = await h.run()
    assert report["totals"]["skipped_cached"] == 0


async def test_cached_no_target_resamples_once_a_target_appears() -> None:
    h = Harness()
    await h.run()
    h.fake.add_artist("linked-5555", "Lonely Canonical", mbid=LONELY_MBID)
    report = await h.run()
    lonely = by_source(report)["Lonely Alias"]
    assert lonely["decision"] == "would_merge"
    assert lonely["target"]["name"] == "Lonely Canonical"


async def test_shared_mbid_without_link() -> None:
    h = Harness()
    h.fake.add_artist("zz-alias", "Other Alias", tracks=2, tag_mbid=OTHER_MBID)
    report = await h.run()
    actions = by_source(report)
    assert actions["Other Artist"]["decision"] == "shared_mbid_without_link"
    assert actions["Other Artist"]["shared_with"] == [{"id": "zz-alias", "name": "Other Alias"}]
    assert actions["Other Alias"]["decision"] == "shared_mbid_without_link"
    assert report["totals"]["no_target"] == 1  # Lonely Alias


async def test_shared_mbid_pairs_across_windows_through_the_cache() -> None:
    h = Harness()
    h.settings["max_artists_per_run"] = "5"
    h.fake.add_artist("zz-alias", "Other Alias", tracks=1, tag_mbid=OTHER_MBID)
    first = await h.run()  # 7b29.. a8a4.. acc3.. fde1.. fec6.. (Other Artist)
    second = await h.run()  # zz-alias first, then wraps
    assert by_source(first)["Other Artist"]["decision"] == "no_target"
    alias = by_source(second)["Other Alias"]
    assert alias["decision"] == "shared_mbid_without_link"
    assert alias["shared_with"] == [{"id": OTHER_ID, "name": "Other Artist"}]


async def test_pagination_covers_more_than_one_page() -> None:
    h = Harness()
    for n in range(150):
        h.fake.add_artist(f"extra-{n:03d}", f"Extra {n:03d}")
    report = await h.run()
    album_pages = [
        c.query
        for c in h.fake.calls
        if c.path == "/library/artists" and c.query["scope"] == "album"
    ]
    assert [q["offset"] for q in album_pages] == ["0", "100"]
    assert all(q["limit"] == "100" for q in album_pages)
    assert report["totals"]["artists_listed"] == 156
    assert report["totals"]["no_samples"] == 150


async def test_window_and_cursor_cover_the_library_over_runs() -> None:
    h = Harness()
    h.settings["max_artists_per_run"] = "2"
    seen: list[str] = []
    for _ in range(3):
        report = await h.run()
        seen += [a["source"]["id"] for a in report["actions"]]
        seen += [SOME_BAND_ID] * report["totals"]["no_mbid"]
        assert report["totals"]["examined"] <= 2
    assert sorted(seen[:5]) == SORTED_CANDIDATES
    assert h.state(CURSOR_KEY) == {"after_id": SORTED_CANDIDATES[0]}


async def test_excluded_artist_ids_and_mbids() -> None:
    h = Harness()
    h.settings["exclude_artist_ids"] = f"{SOME_BAND_ID}, {KANADE_ID}"
    h.settings["exclude_mbids"] = f"{OTHER_MBID} junk"
    report = await h.run()
    actions = by_source(report)
    assert report["totals"]["candidates"] == 4
    assert actions["Otonose Kanade"]["decision"] == "excluded_target"
    assert actions["Other Artist"]["decision"] == "excluded_mbid"
    assert report["warnings"] and "junk" in report["warnings"][0]
    assert h.fake.calls_to("POST", "/library/artists/merge-preview") == []


async def test_base_path_is_honoured() -> None:
    fake = FakeDN(base_path="/music")
    h = Harness(fake=fake)
    h.settings["base_url"] = "http://dn.test/music/"
    report = await h.run()
    assert report["status"] == "ok"
    assert report["totals"]["would_merge"] == 1


async def test_library_busy_skips_the_run() -> None:
    h = Harness()
    h.fake.reconciliation_state = "running"
    report = await h.run()
    assert report["status"] == "library_busy"
    assert not [c for c in h.fake.calls if c.path == "/library/artists"]
    assert h.fake.calls[-1].key == "POST /auth/logout"


async def test_config_error_makes_no_requests() -> None:
    h = Harness()
    h.settings["samples_per_artist"] = "lots"
    report = await h.run()
    assert report["status"] == "config_error"
    assert "samples_per_artist" in report["errors"][0]["message"]
    assert h.fake.calls == []
    assert h.state(REPORT_KEY) == report
    assert h.state(LEASE_KEY) is None


async def test_tags_400_is_a_sample_error() -> None:
    h = Harness()
    h.fake.fail("GET", "/library/tracks/3e5d06c1-409f-5fab-a37f-880b11eb40aa/tags", kind=400)
    report = await h.run()
    assert report["status"] == "ok"
    assert report["totals"]["errors"] == 1
    assert report["errors"][0]["stage"] == "tags"
    assert report["errors"][0]["status"] == 400
    assert by_source(report)["Otonose Kanade"]["decision"] == "would_merge"


async def test_state_store_failures_never_fail_the_run() -> None:
    h = Harness()
    h.store.fail_writes = True
    report = await h.run()
    assert report["status"] == "ok"
    assert report["totals"]["would_merge"] == 1


# --- scenario 7: auth failures ----------------------------------------------


async def test_login_401_is_auth_failed_with_no_other_calls() -> None:
    h = Harness()
    h.settings["password"] = "wrong-password"
    report = await h.run()
    assert report["status"] == "auth_failed"
    assert [c.key for c in h.fake.calls] == ["POST /auth/login"]
    assert "username and password" in report["errors"][0]["message"]


async def test_non_admin_is_not_admin() -> None:
    h = Harness()
    h.settings.update(username=LISTENER_USER, password=LISTENER_PASSWORD)
    report = await h.run()
    assert report["status"] == "not_admin"
    assert [c.key for c in h.fake.calls] == [
        "POST /auth/login",
        "GET /auth/me",
        "POST /auth/logout",
    ]


async def test_403_mid_run_is_not_admin() -> None:
    h = Harness()
    h.fake.fail("GET", "/library/tracks/*", kind=403)
    report = await h.run()
    assert report["status"] == "not_admin"


# --- scenario 8: token mode ---------------------------------------------------


async def test_token_mode_never_logs_in_or_out() -> None:
    h = Harness()
    token = h.fake.issue_token()
    h.settings = {"base_url": "http://dn.test", "api_token": token}
    report = await h.run()
    assert report["status"] == "ok"
    keys = {c.key for c in h.fake.calls}
    assert "POST /auth/login" not in keys
    assert "POST /auth/logout" not in keys
    assert all(c.authorization == f"Bearer {token}" for c in h.fake.calls)
    assert token in h.fake.tokens  # still valid


async def test_expired_token_is_auth_failed_with_hint() -> None:
    h = Harness()
    h.settings = {"base_url": "http://dn.test", "api_token": "expired-token-value"}
    report = await h.run()
    assert report["status"] == "auth_failed"
    assert "30-day" in report["errors"][0]["message"]
    assert [c.key for c in h.fake.calls] == ["GET /auth/me"]


async def test_401_mid_run_is_auth_lost_and_saves_cursor() -> None:
    h = Harness()
    token = h.fake.issue_token()
    h.settings = {"base_url": "http://dn.test", "api_token": token}

    def revoke(fake: FakeDN, call: object) -> None:
        fake.tokens.clear()

    first_tag = "/library/tracks/3e5d06c1-409f-5fab-a37f-880b11eb40aa/tags"  # Otonose
    h.fake.before("GET", first_tag, revoke)
    report = await h.run()
    assert report["status"] == "auth_lost"
    assert report["totals"]["examined"] == 1  # Some Band (7b29...) finished first
    assert h.state(CURSOR_KEY) == {"after_id": SOME_BAND_ID}


# --- scenario 9: time budget --------------------------------------------------


async def test_time_budget_mid_candidates_is_partial_and_resumes() -> None:
    h = Harness()
    h.settings["time_budget_seconds"] = "30"
    jumped = []

    def jump(call: object) -> None:
        if getattr(call, "path", "").endswith("/tags") and not jumped:
            jumped.append(True)
            h.clock.advance(31)

    h.fake.on_request = jump
    report = await h.run()
    assert report["partial"] is True
    assert report["status"] == "ok"
    assert report["totals"]["examined"] == 1
    assert h.state(CURSOR_KEY) == {"after_id": SORTED_CANDIDATES[0]}

    report = await h.run()
    assert report["partial"] is False
    assert report["totals"]["examined"] == 5
    assert report["totals"]["skipped_cached"] == 1
    assert report["totals"]["would_merge"] == 1


async def test_time_budget_mid_inventory_is_partial_and_keeps_cursor() -> None:
    h = Harness()
    h.store.data[CURSOR_KEY] = json.dumps({"after_id": OTONOSE_ID})
    h.settings["time_budget_seconds"] = "30"

    def jump(call: object) -> None:
        if getattr(call, "query", {}).get("scope") == "album":
            h.clock.advance(31)

    h.fake.on_request = jump
    report = await h.run()
    assert report["partial"] is True
    assert report["totals"]["examined"] == 0
    assert not [c for c in h.fake.calls if c.query.get("scope") == "contributors"]
    assert h.state(CURSOR_KEY) == {"after_id": OTONOSE_ID}
    assert h.state(CACHE_KEY) is None


# --- scenario 11: lease ---------------------------------------------------------


async def test_held_lease_skips_the_run() -> None:
    h = Harness()
    lease = {
        "run_id": "other",
        "trigger": "schedule",
        "started_at": iso(EPOCH),
        "expires_at": iso(h.clock.now().replace(year=2027)),
    }
    h.store.data[LEASE_KEY] = json.dumps(lease)
    h.store.data[REPORT_KEY] = json.dumps({"run_id": "previous"})
    report = await h.run()
    assert report["status"] == "lease_held"
    assert h.fake.calls == []
    assert h.state(LEASE_KEY) == lease
    assert h.state(REPORT_KEY) == {"run_id": "previous"}  # not clobbered


async def test_expired_lease_is_taken_over() -> None:
    h = Harness()
    lease = {"run_id": "dead", "expires_at": iso(EPOCH.replace(year=2025))}
    h.store.data[LEASE_KEY] = json.dumps(lease)
    report = await h.run()
    assert report["status"] == "ok"
    assert h.state(LEASE_KEY) == {}


async def test_concurrent_runs_one_is_lease_held() -> None:
    h = Harness()
    first, second = await asyncio.gather(h.run("schedule"), h.run("manual"))
    assert sorted([first["status"], second["status"]]) == ["lease_held", "ok"]
    assert len(h.fake.calls_to("POST", "/auth/login")) == 1


# --- scenario 12: connection errors ---------------------------------------------


async def test_one_connection_error_is_retried() -> None:
    h = Harness()
    h.fake.fail("GET", "/library/artists/reconciliation", kind="connect")
    report = await h.run()
    assert report["status"] == "ok"
    assert len(h.fake.calls_to("GET", "/library/artists/reconciliation")) == 2
    assert 2.0 in h.clock.sleeps


@pytest.mark.parametrize("kind", ["connect", "timeout", 503])
async def test_second_failure_is_server_unreachable(kind: str | int) -> None:
    h = Harness()
    h.fake.fail("GET", "/library/artists/reconciliation", times=2, kind=kind)
    report = await h.run()
    assert report["status"] == "server_unreachable"
    assert len(h.fake.calls_to("GET", "/library/artists/reconciliation")) == 2
    assert h.state(LEASE_KEY) == {}
    assert h.fake.calls[-1].key == "POST /auth/logout"


async def test_unreachable_mid_run_saves_cursor() -> None:
    h = Harness()
    h.fake.fail("GET", f"/library/artists/{OTONOSE_ID}/albums", times=2, kind="connect")
    report = await h.run()
    assert report["status"] == "server_unreachable"
    assert h.state(CURSOR_KEY) == {"after_id": SOME_BAND_ID}


async def test_server_down_from_the_start() -> None:
    h = Harness()
    h.fake.fail("POST", "/auth/login", times=2, kind="connect")
    report = await h.run()
    assert report["status"] == "server_unreachable"
    assert len(h.fake.calls) == 2


# --- scenario 13: secrets ----------------------------------------------------------


async def test_report_and_logs_never_contain_secrets(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    h = Harness()
    password = h.settings["password"]
    # a server error message that echoes the password must still be scrubbed
    h.fake.faults.append(Fault("GET", "/library/tracks/*", 1, 400, f"bad file near {password}"))
    report = await h.run()
    tokens_seen = {c.authorization.removeprefix("Bearer ") for c in h.fake.calls if c.authorization}
    blob = json.dumps(report) + json.dumps(h.store.data) + caplog.text
    assert tokens_seen
    for secret in (password, *tokens_seen):
        assert secret not in blob
    assert "<redacted>" in json.dumps(report)


async def test_token_never_in_report() -> None:
    h = Harness()
    token = h.fake.issue_token()
    h.settings = {"base_url": "http://dn.test", "api_token": token}
    report = await h.run()
    assert report["status"] == "ok"
    assert token not in json.dumps(report) + json.dumps(h.store.data)


async def test_credentials_in_base_url_never_reach_the_report() -> None:
    h = Harness()
    h.settings["base_url"] = "http://user:pw-in-url@dn.test"
    report = await h.run()
    assert report["status"] == "config_error"
    assert "pw-in-url" not in json.dumps(report)
    assert h.fake.calls == []


async def test_config_error_report_never_contains_secrets() -> None:
    h = Harness()
    h.settings["max_merges_per_run"] = h.settings["password"]
    report = await h.run()
    assert report["status"] == "config_error"
    assert h.settings["password"] not in json.dumps(report)
