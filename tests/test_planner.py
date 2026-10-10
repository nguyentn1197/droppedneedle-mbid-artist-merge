from __future__ import annotations

from collections.abc import Sequence

import pytest
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
    snapshot,
)

from mbid_artist_merge.constants import (
    UNKNOWN_ARTIST_MBID,
    VARIOUS_ARTISTS_LOCAL_ID,
    VARIOUS_ARTISTS_MBID,
)
from mbid_artist_merge.evidence import extract
from mbid_artist_merge.models import (
    Artist,
    CreditEvidence,
    Decision,
    Outcome,
    Sample,
    TrackRef,
)
from mbid_artist_merge.planner import (
    Rules,
    build_inventory,
    decide,
    mark_shared,
    round_robin,
    window,
)

OTONOSE = Artist(OTONOSE_ID, "Otonose Kanade", None, "local_only", 1)
KANADE = Artist(KANADE_ID, "音乃瀬奏", KANADE_MBID, "musicbrainz_linked", 1)
LINKED = {KANADE_MBID: KANADE}
RULES = Rules(min_agreeing_samples=2)


def sample(mbid: str | None, reason: str = "credit", n: int = 0) -> Sample:
    detail = "album_artists" if reason == "credit" else None
    ev = CreditEvidence(mbid, reason, detail, "Otonose Kanade")  # type: ignore[arg-type]
    return Sample(TrackRef(f"t{n}", "a1", "album_artist"), ev)


def samples(*mbids: str | None) -> list[Sample]:
    return [
        sample(m, "credit" if m else "no_mbid_in_file", n) for n, m in enumerate(mbids, start=1)
    ]


def run(
    found: Sequence[Sample],
    *,
    candidate: Artist = OTONOSE,
    linked: dict[str, Artist] | None = None,
    rules: Rules = RULES,
) -> Decision:
    return decide(
        candidate, found, linked_by_mbid=LINKED if linked is None else linked, rules=rules
    )


# --- §7.6, one row at a time ----------------------------------------------


def test_no_samples() -> None:
    assert run([]).outcome == Outcome.NO_SAMPLES


def test_ambiguous_beats_everything_else() -> None:
    found = [*samples(KANADE_MBID, OTHER_MBID), sample(None, "ambiguous", 9)]
    d = run(found)
    assert d.outcome == Outcome.AMBIGUOUS
    assert d.mbid is None


def test_conflict_lists_mbids() -> None:
    d = run(samples(KANADE_MBID, OTHER_MBID, KANADE_MBID))
    assert d.outcome == Outcome.CONFLICT
    assert d.conflicting_mbids == (KANADE_MBID, OTHER_MBID)


def test_no_mbid() -> None:
    assert run(samples(None, None)).outcome == Outcome.NO_MBID


def test_insufficient_evidence() -> None:
    d = run(samples(KANADE_MBID, None, None))
    assert d.outcome == Outcome.INSUFFICIENT_EVIDENCE
    assert d.mbid == KANADE_MBID


def test_untagged_files_do_not_count_against() -> None:
    assert run(samples(KANADE_MBID, None, KANADE_MBID, None)).outcome == Outcome.WOULD_MERGE


def test_min_agreeing_lowered_for_few_files() -> None:
    assert run(samples(KANADE_MBID)).outcome == Outcome.WOULD_MERGE
    strict = Rules(min_agreeing_samples=3)
    assert run(samples(KANADE_MBID, KANADE_MBID), rules=strict).outcome == Outcome.WOULD_MERGE
    assert (
        run(samples(KANADE_MBID, KANADE_MBID, None), rules=strict).outcome
        == Outcome.INSUFFICIENT_EVIDENCE
    )


@pytest.mark.parametrize("mbid", [VARIOUS_ARTISTS_MBID, UNKNOWN_ARTIST_MBID])
def test_va_and_unknown_mbids_are_excluded(mbid: str) -> None:
    linked = {mbid: Artist("va", "Various Artists", mbid, "musicbrainz_linked", 1)}
    assert run(samples(mbid, mbid), linked=linked).outcome == Outcome.EXCLUDED_MBID


def test_excluded_mbid_setting() -> None:
    rules = Rules(min_agreeing_samples=2, exclude_mbids=frozenset({KANADE_MBID}))
    assert run(samples(KANADE_MBID, KANADE_MBID), rules=rules).outcome == Outcome.EXCLUDED_MBID


def test_no_target() -> None:
    d = run(samples(OTHER_MBID, OTHER_MBID))
    assert d.outcome == Outcome.NO_TARGET
    assert d.mbid == OTHER_MBID
    assert d.target is None


def test_self_is_guarded() -> None:
    me = Artist(OTONOSE_ID, "Otonose Kanade", KANADE_MBID, "musicbrainz_linked", 1)
    d = run(samples(KANADE_MBID, KANADE_MBID), linked={KANADE_MBID: me})
    assert d.outcome == Outcome.SELF


def test_excluded_target_by_setting() -> None:
    rules = Rules(min_agreeing_samples=2, exclude_artist_ids=frozenset({KANADE_ID}))
    d = run(samples(KANADE_MBID, KANADE_MBID), rules=rules)
    assert d.outcome == Outcome.EXCLUDED_TARGET
    assert d.target == KANADE


def test_excluded_target_reserved_id() -> None:
    va = Artist(VARIOUS_ARTISTS_LOCAL_ID, "Various", KANADE_MBID, "musicbrainz_linked", 1)
    d = run(samples(KANADE_MBID, KANADE_MBID), linked={KANADE_MBID: va})
    assert d.outcome == Outcome.EXCLUDED_TARGET


def test_would_merge() -> None:
    found = samples(KANADE_MBID, KANADE_MBID)
    d = run(found)
    assert d.outcome == Outcome.WOULD_MERGE
    assert (d.source, d.target, d.mbid) == (OTONOSE, KANADE, KANADE_MBID)
    assert d.samples == tuple(found)


def test_shared_mbid_without_link_groups_no_targets() -> None:
    a = Artist("id-a", "Alias A", None, "local_only", 1)
    b = Artist("id-b", "Alias B", None, "local_only", 1)
    c = Artist("id-c", "Other", None, "local_only", 1)
    decisions = [
        run(samples(OTHER_MBID, OTHER_MBID), candidate=a),
        run(samples(OTHER_MBID), candidate=b),
        run(samples(LONELY_MBID), candidate=c),
        run(samples(KANADE_MBID, KANADE_MBID)),
    ]
    marked = mark_shared(decisions)
    assert [d.outcome for d in marked] == [
        Outcome.SHARED_MBID_WITHOUT_LINK,
        Outcome.SHARED_MBID_WITHOUT_LINK,
        Outcome.NO_TARGET,
        Outcome.WOULD_MERGE,
    ]
    assert marked[0].peers == (b,)
    assert marked[1].peers == (a,)


def test_shared_mbid_considers_known_peers_outside_the_window() -> None:
    a = Artist("id-a", "Alias A", None, "local_only", 1)
    b = Artist("id-b", "Alias B", None, "local_only", 1)
    marked = mark_shared([run(samples(OTHER_MBID), candidate=a)], known_peers={OTHER_MBID: [b]})
    assert marked[0].outcome == Outcome.SHARED_MBID_WITHOUT_LINK
    assert marked[0].peers == (b,)


# --- inventory, window, sampling order ------------------------------------


def snapshot_artists() -> list[Artist]:
    snap = snapshot()
    items = []
    for scope in ("album", "contributors"):
        items += snap[f"/library/artists?limit=100&offset=0&scope={scope}"]["body"]["items"]
    return [Artist.from_api(i) for i in items]


def test_inventory_unions_scopes_and_picks_local_only() -> None:
    inv = build_inventory(snapshot_artists(), exclude_artist_ids=frozenset())
    assert inv.artists_listed == 6
    assert set(inv.linked_by_mbid) == {KANADE_MBID}
    assert [c.id for c in inv.candidates] == sorted(
        [OTONOSE_ID, OTHER_ID, LONELY_ID, SOME_BAND_ID, CONFLICT_ID]
    )


def test_inventory_excludes_reserved_and_configured_ids() -> None:
    artists = [
        *snapshot_artists(),
        Artist(VARIOUS_ARTISTS_LOCAL_ID, "Various Artists", None, "local_only", 1),
    ]
    inv = build_inventory(artists, exclude_artist_ids=frozenset({OTHER_ID}))
    ids = {c.id for c in inv.candidates}
    assert VARIOUS_ARTISTS_LOCAL_ID not in ids
    assert OTHER_ID not in ids


def test_inventory_only_linked_artists_are_targets() -> None:
    odd = Artist("odd", "Odd", OTHER_MBID, "something_new", 1)
    inv = build_inventory([odd, KANADE], exclude_artist_ids=frozenset())
    assert set(inv.linked_by_mbid) == {KANADE_MBID}
    assert inv.candidates == []


def test_inventory_mbid_lookup_is_case_insensitive() -> None:
    upper = Artist(KANADE_ID, "音乃瀬奏", KANADE_MBID.upper(), "musicbrainz_linked", 1)
    assert build_inventory([upper], exclude_artist_ids=frozenset()).linked_by_mbid == {
        KANADE_MBID: upper
    }


IDS = [f"id-{n}" for n in range(5)]


@pytest.mark.parametrize(
    ("after", "limit", "expected"),
    [
        (None, 2, ["id-0", "id-1"]),
        ("id-1", 2, ["id-2", "id-3"]),
        ("id-3", 3, ["id-4", "id-0", "id-1"]),
        ("id-4", 2, ["id-0", "id-1"]),
        ("id-2", 99, ["id-3", "id-4", "id-0", "id-1", "id-2"]),
        ("id-25", 2, ["id-3", "id-4"]),  # cursor artist gone: continue after its position
        ("zzz", 1, ["id-0"]),
    ],
)
def test_window(after: str | None, limit: int, expected: list[str]) -> None:
    candidates = [Artist(i, i, None, "local_only", 1) for i in IDS]
    assert [c.id for c in window(candidates, after, limit)] == expected


def test_window_empty() -> None:
    assert window([], "x", 5) == []


def test_round_robin() -> None:
    groups = [["a1", "a2", "a3"], [], ["b1"], ["c1", "c2"]]
    assert round_robin(groups, 4) == ["a1", "b1", "c1", "a2"]
    assert round_robin(groups, 99) == ["a1", "b1", "c1", "a2", "c2", "a3"]
    assert round_robin([], 3) == []
    assert round_robin(groups, 0) == []


# --- the E2E library, decided purely from the captured snapshot -----------


def sample_from_snapshot(artist: Artist, limit: int) -> list[Sample]:
    snap = snapshot()
    albums = snap[f"/library/artists/{artist.id}/albums"]["body"]["items"]
    groups = [
        [
            TrackRef(t["id"], a["id"], "album_artist")
            for t in snap[f"/library/albums/{a['id']}/tracks"]["body"]["items"]
        ]
        for a in albums
    ]
    picked = round_robin(groups, limit)
    if len(picked) < limit:
        key = f"/library/artists/{artist.id}/appearances?limit=100&offset=0"
        apps = snap[key]["body"]["items"]
        groups = [
            [TrackRef(t["id"], i["album"]["id"], "track_artist") for t in i["tracks"]] for i in apps
        ]
        picked += round_robin(groups, limit - len(picked))
    return [
        Sample(
            ref,
            extract(artist.name, ref.role, snap[f"/library/tracks/{ref.track_id}/tags"]["body"]),
        )
        for ref in picked
    ]


def test_expected_decisions_on_the_e2e_library() -> None:
    inv = build_inventory(snapshot_artists(), exclude_artist_ids=frozenset())
    decisions = mark_shared(
        [
            decide(c, sample_from_snapshot(c, 5), linked_by_mbid=inv.linked_by_mbid, rules=RULES)
            for c in inv.candidates
        ]
    )
    by_name = {d.source.name: d for d in decisions}
    assert {name: d.outcome for name, d in by_name.items()} == {
        "Otonose Kanade": Outcome.WOULD_MERGE,
        "Other Artist": Outcome.NO_TARGET,
        "Lonely Alias": Outcome.NO_TARGET,
        "Some Band": Outcome.NO_MBID,
        "Conflict Name": Outcome.CONFLICT,
    }
    otonose = by_name["Otonose Kanade"]
    assert otonose.target is not None and otonose.target.name == "音乃瀬奏"
    assert [s.track.role for s in otonose.samples] == ["album_artist"] * 3 + ["track_artist"]
    assert by_name["Other Artist"].mbid == OTHER_MBID
    assert by_name["Lonely Alias"].mbid == LONELY_MBID
    assert by_name["Conflict Name"].conflicting_mbids == (CONFLICT_MBID_1, CONFLICT_MBID_2)
