"""Plain data passed between the pure modules and the runner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from .constants import LOCAL_ONLY, MBID_RE, MUSICBRAINZ_LINKED

Role = Literal["album_artist", "track_artist"]


class Outcome(StrEnum):
    """Every value a report ``decision`` can take (PLAN §7.6 and §7.7)."""

    # planner decisions (§7.6)
    NO_SAMPLES = "no_samples"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    NO_MBID = "no_mbid"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    EXCLUDED_MBID = "excluded_mbid"
    NO_TARGET = "no_target"
    SHARED_MBID_WITHOUT_LINK = "shared_mbid_without_link"
    SELF = "self"
    EXCLUDED_TARGET = "excluded_target"
    WOULD_MERGE = "would_merge"
    # merge outcomes (§7.7)
    MERGED = "merged"
    DEFERRED = "deferred"
    STALE = "stale"
    CHANGED = "changed"
    ALREADY_MERGED = "already_merged"
    IDENTITY_CONFLICT = "identity_conflict"
    ERROR = "error"


def normalize_mbid(value: object) -> str | None:
    """Lowercased MBID, or None when the value is not a well-formed MBID."""
    if not isinstance(value, str):
        return None
    mbid = value.strip().lower()
    return mbid if MBID_RE.match(mbid) else None


@dataclass(frozen=True)
class Artist:
    """The fields of a ``TargetNativeArtist`` that are identical in every scope."""

    id: str
    name: str
    musicbrainz_artist_id: str | None
    artist_identity_state: str
    row_revision: int

    @classmethod
    def from_api(cls, item: Mapping[str, Any]) -> Artist:
        return cls(
            id=str(item["id"]),
            name=str(item.get("name") or ""),
            musicbrainz_artist_id=item.get("musicbrainz_artist_id") or None,
            artist_identity_state=str(item.get("artist_identity_state") or ""),
            row_revision=int(item["row_revision"]),
        )

    @property
    def mbid(self) -> str | None:
        return normalize_mbid(self.musicbrainz_artist_id)

    @property
    def is_local_only(self) -> bool:
        return self.artist_identity_state == LOCAL_ONLY

    @property
    def is_linked(self) -> bool:
        return self.artist_identity_state == MUSICBRAINZ_LINKED and self.mbid is not None

    def ref(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "row_revision": self.row_revision}


@dataclass(frozen=True)
class TrackRef:
    track_id: str
    album_id: str | None
    role: Role


@dataclass(frozen=True)
class CreditEvidence:
    """What one file's tags say about the artist.

    ``reason`` is ``credit`` (``mbid`` set, ``detail`` names the pool),
    ``ambiguous`` (``detail`` says why) or ``no_mbid_in_file``.
    """

    mbid: str | None
    reason: Literal["credit", "ambiguous", "no_mbid_in_file"]
    detail: str | None = None
    credit_name: str | None = None


@dataclass(frozen=True)
class Sample:
    track: TrackRef
    evidence: CreditEvidence

    def to_report(self) -> dict[str, Any]:
        return {
            "track_id": self.track.track_id,
            "album_id": self.track.album_id,
            "role": self.track.role,
            "credit_name": self.evidence.credit_name,
            "mbid": self.evidence.mbid,
            "reason": self.evidence.reason,
            "detail": self.evidence.detail,
        }


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    source: Artist
    mbid: str | None = None
    target: Artist | None = None
    samples: tuple[Sample, ...] = ()
    conflicting_mbids: tuple[str, ...] = ()
    peers: tuple[Artist, ...] = ()
    detail: str | None = None
