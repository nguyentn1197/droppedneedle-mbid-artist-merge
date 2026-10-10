"""Pure: inventory + evidence -> one Decision per candidate (PLAN §7.3, §7.4, §7.6)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from .constants import RESERVED_ARTIST_IDS, RESERVED_MBIDS
from .models import Artist, Decision, Outcome, Sample

_UNLINKED = (Outcome.NO_TARGET, Outcome.SHARED_MBID_WITHOUT_LINK)


@dataclass(frozen=True)
class Rules:
    min_agreeing_samples: int = 2
    exclude_mbids: frozenset[str] = frozenset()
    exclude_artist_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Inventory:
    artists: dict[str, Artist] = field(default_factory=dict)
    linked_by_mbid: dict[str, Artist] = field(default_factory=dict)
    candidates: list[Artist] = field(default_factory=list)

    @property
    def artists_listed(self) -> int:
        return len(self.artists)


def build_inventory(artists: Iterable[Artist], *, exclude_artist_ids: frozenset[str]) -> Inventory:
    """Union both scopes by id; index linked artists by MBID; pick local-only candidates.

    Only ``musicbrainz_linked`` artists can be survivors, so an artist that holds an
    MBID in any other state is neither a target nor a candidate.
    """
    by_id: dict[str, Artist] = {}
    for artist in artists:
        by_id.setdefault(artist.id, artist)
    linked: dict[str, Artist] = {}
    for artist in sorted(by_id.values(), key=lambda a: a.id):
        if artist.is_linked and artist.mbid is not None:
            linked.setdefault(artist.mbid, artist)
    candidates = sorted(
        (
            a
            for a in by_id.values()
            if a.is_local_only
            and a.id not in RESERVED_ARTIST_IDS
            and a.id not in exclude_artist_ids
        ),
        key=lambda a: a.id,
    )
    return Inventory(artists=by_id, linked_by_mbid=linked, candidates=candidates)


def window(candidates: Sequence[Artist], after_id: str | None, limit: int) -> list[Artist]:
    """Up to ``limit`` candidates (sorted by id) starting after ``after_id``, wrapping once."""
    if not candidates or limit <= 0:
        return []
    start = 0
    if after_id is not None:
        start = next((i for i, c in enumerate(candidates) if c.id > after_id), 0)
    count = min(limit, len(candidates))
    return [candidates[(start + k) % len(candidates)] for k in range(count)]


def round_robin[T](groups: Sequence[Sequence[T]], limit: int) -> list[T]:
    """Item 1 of every group, then item 2 of every group, ... up to ``limit`` items."""
    picked: list[T] = []
    depth = 0
    while len(picked) < limit:
        layer = [g[depth] for g in groups if depth < len(g)]
        if not layer:
            break
        picked.extend(layer[: limit - len(picked)])
        depth += 1
    return picked


def decide(
    candidate: Artist,
    samples: Sequence[Sample],
    *,
    linked_by_mbid: Mapping[str, Artist],
    rules: Rules,
) -> Decision:
    found = tuple(samples)

    def result(outcome: Outcome, **kwargs: object) -> Decision:
        return Decision(outcome, candidate, samples=found, **kwargs)  # type: ignore[arg-type]

    if not found:
        return result(Outcome.NO_SAMPLES)
    ambiguous = [s for s in found if s.evidence.reason == "ambiguous"]
    if ambiguous:
        return result(Outcome.AMBIGUOUS, detail=ambiguous[0].evidence.detail)
    mbids = sorted({s.evidence.mbid for s in found if s.evidence.mbid})
    if len(mbids) > 1:
        return result(Outcome.CONFLICT, conflicting_mbids=tuple(mbids))
    if not mbids:
        return result(Outcome.NO_MBID)
    mbid = mbids[0]
    agreeing = sum(1 for s in found if s.evidence.mbid == mbid)
    required = min(rules.min_agreeing_samples, len(found))
    if agreeing < required:
        detail = f"{agreeing} of {len(found)} sampled files carry the mbid, need {required}"
        return result(Outcome.INSUFFICIENT_EVIDENCE, mbid=mbid, detail=detail)
    if mbid in RESERVED_MBIDS or mbid in rules.exclude_mbids:
        return result(Outcome.EXCLUDED_MBID, mbid=mbid)
    target = linked_by_mbid.get(mbid)
    if target is None:
        return result(Outcome.NO_TARGET, mbid=mbid)
    if target.id == candidate.id:
        return result(Outcome.SELF, mbid=mbid, target=target)
    if target.id in RESERVED_ARTIST_IDS or target.id in rules.exclude_artist_ids:
        return result(Outcome.EXCLUDED_TARGET, mbid=mbid, target=target)
    return result(Outcome.WOULD_MERGE, mbid=mbid, target=target)


def mark_shared(
    decisions: Sequence[Decision],
    known_peers: Mapping[str, Sequence[Artist]] | None = None,
) -> list[Decision]:
    """Turn ``no_target`` into ``shared_mbid_without_link`` when other local-only
    artists resolved to the same MBID, in this run or (``known_peers``) earlier."""
    groups: dict[str, list[Artist]] = defaultdict(list)
    for d in decisions:
        if d.outcome in _UNLINKED and d.mbid:
            groups[d.mbid].append(d.source)
    for mbid, artists in (known_peers or {}).items():
        groups[mbid].extend(artists)
    marked = []
    for d in decisions:
        if d.outcome in _UNLINKED and d.mbid:
            peers: dict[str, Artist] = {}
            for artist in groups[d.mbid]:
                if artist.id != d.source.id:
                    peers.setdefault(artist.id, artist)
            outcome = Outcome.SHARED_MBID_WITHOUT_LINK if peers else Outcome.NO_TARGET
            d = replace(d, outcome=outcome, peers=tuple(peers.values()))
        marked.append(d)
    return marked
