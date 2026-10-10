"""Pure: the run report (PLAN §9), its size cap, and log lines."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .models import Decision, Outcome

MAX_ACTIONS = 500
MAX_REPORT_BYTES = 900 * 1024

STATUSES = (
    "ok",
    "config_error",
    "auth_failed",
    "not_admin",
    "auth_lost",
    "server_unreachable",
    "library_busy",
    "lease_held",
    "internal_error",
)
RUN_COUNTERS = ("artists_listed", "linked", "candidates", "examined", "skipped_cached", "errors")
QUIET = frozenset({Outcome.NO_MBID})
REDACTED = "<redacted>"


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def public_url(url: str) -> str:
    """``url`` without any ``user:password@`` part."""
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


class RunReport:
    def __init__(
        self,
        *,
        run_id: str,
        trigger: str,
        started_at: datetime,
        version: str,
        dry_run: bool,
        base_url: str | None,
    ) -> None:
        self.data: dict[str, Any] = {
            "plugin_version": version,
            "run_id": run_id,
            "trigger": trigger,
            "started_at": iso(started_at),
            "finished_at": None,
            "dry_run": dry_run,
            "partial": False,
            "status": "ok",
            "server": {"base_url": public_url(base_url) if base_url else None},
            "totals": {key: 0 for key in (*RUN_COUNTERS, *(o.value for o in Outcome))},
            "actions": [],
            "errors": [],
            "warnings": [],
        }

    @property
    def totals(self) -> dict[str, int]:
        return self.data["totals"]

    @property
    def status(self) -> str:
        return self.data["status"]

    def set_dry_run(self, dry_run: bool) -> None:
        self.data["dry_run"] = dry_run

    def add_warning(self, message: str) -> None:
        self.data["warnings"].append(message)

    def add_error(self, stage: str, message: str, **fields: Any) -> None:
        self.data["errors"].append({"stage": stage, **fields, "message": message})
        self.totals["errors"] = len(self.data["errors"])

    def add_decision(self, decision: Decision, at: datetime) -> None:
        self.totals[decision.outcome.value] += 1
        if decision.cached:
            self.totals["skipped_cached"] += 1
        if decision.cached or decision.outcome in QUIET:
            return
        actions = self.data["actions"]
        if len(actions) >= MAX_ACTIONS:
            self.data["truncated"] = True
            return
        actions.append(action_for(decision, at))

    def finish(self, status: str, finished_at: datetime, *, partial: bool = False) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        self.data["status"] = status
        self.data["partial"] = partial
        self.data["finished_at"] = iso(finished_at)

    def to_json(self, secrets: Iterable[str] = ()) -> str:
        """Serialized report, secrets scrubbed, under :data:`MAX_REPORT_BYTES`."""
        return capped_json(redact(self.data, secrets))


def action_for(decision: Decision, at: datetime) -> dict[str, Any]:
    action: dict[str, Any] = {"decision": decision.outcome.value, "source": decision.source.ref()}
    if decision.target is not None:
        action["target"] = decision.target.ref()
    if decision.mbid:
        action["mbid"] = decision.mbid
    if decision.conflicting_mbids:
        action["mbids"] = list(decision.conflicting_mbids)
    if decision.peers:
        action["shared_with"] = [{"id": p.id, "name": p.name} for p in decision.peers]
    if decision.detail:
        action["detail"] = decision.detail
    action["evidence"] = [s.to_report() for s in decision.samples]
    if decision.reference_counts is not None:
        action["reference_counts"] = dict(decision.reference_counts)
    action["at"] = iso(at)
    return action


def redact(value: Any, secrets: Iterable[str]) -> Any:
    """Deep copy of ``value`` with every secret substring replaced."""
    needles = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)
    if not needles:
        return json.loads(json.dumps(value))

    def scrub(item: Any) -> Any:
        if isinstance(item, str):
            for needle in needles:
                item = item.replace(needle, REDACTED)
            return item
        if isinstance(item, dict):
            return {scrub(k): scrub(v) for k, v in item.items()}
        if isinstance(item, list):
            return [scrub(v) for v in item]
        return item

    return scrub(value)


def capped_json(data: dict[str, Any], limit: int = MAX_REPORT_BYTES) -> str:
    """Drop per-action evidence, then actions, then errors until the JSON fits."""

    def dump(d: dict[str, Any]) -> str:
        return json.dumps(d, ensure_ascii=False)

    text = dump(data)
    if len(text.encode("utf-8")) <= limit:
        return text
    data = {**data, "truncated": True}
    data["actions"] = [{k: v for k, v in a.items() if k != "evidence"} for a in data["actions"]]
    for key in ("actions", "errors"):
        while len((text := dump(data)).encode("utf-8")) > limit and data[key]:
            data[key] = data[key][: len(data[key]) // 2]
    return text


def summary_line(data: dict[str, Any]) -> str:
    totals = data["totals"]
    always = ("would_merge", "merged")
    counts = [f"{k}={totals[k]}" for k in always]
    counts += [
        f"{o.value}={totals[o.value]}"
        for o in Outcome
        if o.value not in always and totals.get(o.value)
    ]
    head = (
        f"mbid-artist-merge run={data['run_id']} status={data['status']} "
        f"dry_run={str(data['dry_run']).lower()} partial={str(data['partial']).lower()} "
        f"examined={totals['examined']} skipped_cached={totals['skipped_cached']} "
        f"errors={totals['errors']}"
    )
    return " ".join([head, *counts])


def decision_line(decision: Decision) -> str:
    target = decision.target
    return (
        f"mbid-artist-merge decision={decision.outcome.value} "
        f"source={decision.source.id} source_name={decision.source.name!r} "
        f"target={target.id if target else None} "
        f"target_name={target.name if target else None!r} mbid={decision.mbid}"
    )
