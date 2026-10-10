from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from mbid_artist_merge.models import Artist, CreditEvidence, Decision, Outcome, Sample, TrackRef
from mbid_artist_merge.report import (
    MAX_ACTIONS,
    RunReport,
    capped_json,
    iso,
    notice_for,
    panel_view,
    public_url,
    redact,
    summary_line,
)

AT = datetime(2026, 10, 9, 5, 0, 0, tzinfo=UTC)
SOURCE = Artist("src", "Otonose Kanade", None, "local_only", 1)
TARGET = Artist("dst", "音乃瀬奏", "11111111-1111-4111-8111-111111111111", "musicbrainz_linked", 1)


def new_report() -> RunReport:
    return RunReport(
        run_id="r1",
        trigger="manual",
        started_at=AT,
        version="0.1.0",
        dry_run=True,
        base_url="http://127.0.0.1:8688",
    )


def test_every_outcome_has_a_zero_counter() -> None:
    totals = new_report().totals
    for outcome in Outcome:
        assert totals[outcome.value] == 0
    assert set(totals) >= {"artists_listed", "linked", "candidates", "examined", "errors"}


def test_quiet_and_cached_decisions_are_counted_not_listed() -> None:
    report = new_report()
    report.add_decision(Decision(Outcome.NO_MBID, SOURCE), AT)
    report.add_decision(Decision(Outcome.CONFLICT, SOURCE, cached=True), AT)
    report.add_decision(Decision(Outcome.NO_TARGET, SOURCE, mbid=TARGET.mbid), AT)
    assert report.totals["no_mbid"] == 1
    assert report.totals["conflict"] == 1
    assert report.totals["skipped_cached"] == 1
    assert [a["decision"] for a in report.data["actions"]] == ["no_target"]


def test_action_shape() -> None:
    report = new_report()
    sample = Sample(
        TrackRef("t1", "a1", "album_artist"),
        CreditEvidence(TARGET.mbid, "credit", "album_artists", "Otonose Kanade"),
    )
    report.add_decision(
        Decision(
            Outcome.WOULD_MERGE,
            SOURCE,
            mbid=TARGET.mbid,
            target=TARGET,
            samples=(sample,),
            reference_counts={"album_credits": 2},
        ),
        AT,
    )
    (action,) = report.data["actions"]
    assert action["source"] == {"id": "src", "name": "Otonose Kanade", "row_revision": 1}
    assert action["target"]["name"] == "音乃瀬奏"
    assert action["evidence"][0]["credit_name"] == "Otonose Kanade"
    assert action["reference_counts"] == {"album_credits": 2}
    assert action["at"] == "2026-10-09T05:00:00Z"


def test_actions_are_capped() -> None:
    report = new_report()
    for _ in range(MAX_ACTIONS + 3):
        report.add_decision(Decision(Outcome.NO_TARGET, SOURCE), AT)
    assert len(report.data["actions"]) == MAX_ACTIONS
    assert report.data["truncated"] is True
    assert report.totals["no_target"] == MAX_ACTIONS + 3


def test_finish_validates_status() -> None:
    report = new_report()
    report.finish("ok", AT, partial=True)
    assert (report.status, report.data["partial"]) == ("ok", True)
    with pytest.raises(ValueError):
        report.finish("weird", AT)


def test_capped_json_drops_evidence_then_actions() -> None:
    data = new_report().data
    data["actions"] = [{"decision": "no_target", "evidence": ["x" * 1000]} for _ in range(50)]
    text = capped_json(data, limit=20_000)
    capped = json.loads(text)
    assert len(text.encode()) <= 20_000
    assert capped["truncated"] is True
    assert all("evidence" not in a for a in capped["actions"])
    assert len(capped["actions"]) == 50
    base = len(capped_json({**data, "actions": []}).encode())
    smaller = json.loads(capped_json(data, limit=base + 400))
    assert 0 < len(smaller["actions"]) < 50


def test_redact_deep() -> None:
    value = {"a": ["pw=hunter22"], "hunter22": 1, "n": 5}
    assert redact(value, ["hunter22", ""]) == {"a": ["pw=<redacted>"], "<redacted>": 1, "n": 5}
    assert redact(value, ["ab"]) == value  # too short to redact safely


def test_public_url_strips_credentials() -> None:
    assert public_url("http://u:p@host:8688/music") == "http://host:8688/music"
    assert public_url("http://host") == "http://host"


def test_summary_line() -> None:
    report = new_report()
    report.add_decision(Decision(Outcome.CONFLICT, SOURCE), AT)
    report.finish("ok", AT)
    line = summary_line(report.data)
    assert line.startswith("mbid-artist-merge run=r1 status=ok dry_run=true")
    assert "would_merge=0 merged=0" in line
    assert "conflict=1" in line
    assert "no_target" not in line


def test_iso_is_utc_z() -> None:
    assert iso(AT) == "2026-10-09T05:00:00Z"


def test_notice_only_for_merges() -> None:
    report = new_report()
    report.add_decision(Decision(Outcome.WOULD_MERGE, SOURCE, target=TARGET), AT)
    assert notice_for(report.data) is None
    report.add_decision(Decision(Outcome.MERGED, SOURCE, target=TARGET), AT)
    report.add_decision(Decision(Outcome.MERGED, SOURCE, target=TARGET), AT)
    notice = notice_for(report.data, limit=40)
    assert notice is not None
    assert notice["title"] == "MBID Artist Merge: 2 artists merged"
    assert len(notice["body"]) <= 40
    assert notice["body"].startswith("Otonose Kanade -> 音乃瀬奏")


def test_panel_view_is_compact_and_ordered() -> None:
    report = new_report()
    other = Artist("o", "Other Artist", None, "local_only", 1)
    sample = Sample(
        TrackRef("t1", "a1", "album_artist"),
        CreditEvidence(TARGET.mbid, "credit", "album_artists", "Otonose Kanade"),
    )
    report.add_decision(
        Decision(Outcome.NO_TARGET, other, mbid="22222222-2222-4222-8222-222222222222"), AT
    )
    report.add_decision(
        Decision(Outcome.WOULD_MERGE, SOURCE, mbid=TARGET.mbid, target=TARGET, samples=(sample,)),
        AT,
    )
    report.add_decision(Decision(Outcome.NO_MBID, other), AT)
    report.add_error("tags", "unreadable file", track_id="t9", status=400)
    report.finish("ok", AT)
    view = panel_view(report.data)
    assert view["status"] == "ok"
    assert view["totals"] == {"would_merge": 1, "no_target": 1, "no_mbid": 1, "errors": 1}
    assert [a["decision"] for a in view["actions"]] == ["would_merge", "no_target"]
    first = view["actions"][0]
    assert first == {
        "decision": "would_merge",
        "source": "Otonose Kanade",
        "source_id": "src",
        "target": "音乃瀬奏",
        "mbid": TARGET.mbid,
        "files": 1,
    }
    assert view["errors"] == [{"stage": "tags", "status": 400, "message": "unreadable file"}]
    assert "evidence" not in json.dumps(view)


def test_panel_view_caps_lists() -> None:
    report = new_report()
    for _ in range(300):
        report.add_decision(Decision(Outcome.NO_TARGET, SOURCE), AT)
    for n in range(40):
        report.add_error("tags", f"e{n}")
    view = panel_view(report.data, max_actions=100, max_errors=10)
    assert (len(view["actions"]), view["actions_total"]) == (100, 300)
    assert (len(view["errors"]), view["errors_total"]) == (10, 40)


def test_panel_view_tolerates_old_or_partial_reports() -> None:
    assert panel_view({"status": "config_error"})["actions"] == []
