"""One run, end to end (PLAN §4 data flow, §7, §8).

``Runner.run`` never raises except ``asyncio.CancelledError``; every failure
becomes the report ``status``. State is saved, the lease released and the
session logged out in ``finally``, best-effort.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from . import __version__
from .api import (
    ApiError,
    Conflict,
    DroppedNeedleApi,
    Forbidden,
    HttpClient,
    NotFound,
    Retired,
    Unauthorized,
    Unreachable,
)
from .config import INTERVAL_MINUTES, ConfigError, Settings, secret_values
from .evidence import extract
from .models import Artist, Decision, Outcome, Sample, TrackRef, normalize_mbid
from .planner import Inventory, Rules, build_inventory, decide, mark_shared, round_robin, window
from .report import RunReport, decision_line, iso, summary_line
from .state import CACHE_KEY, CURSOR_KEY, LEASE_KEY, REPORT_KEY, JsonState, StateStore, dumps

BUSY_STATES = frozenset({"queued", "running", "paused"})
CACHE_VERSION = 1
CACHE_TTL = timedelta(days=7)
CACHE_MAX_BYTES = 5 * 1024 * 1024
LEASE_GRACE_S = 120
REUSABLE = frozenset(
    {Outcome.NO_MBID, Outcome.CONFLICT, Outcome.INSUFFICIENT_EVIDENCE, Outcome.AMBIGUOUS}
)
UNLINKED = frozenset({Outcome.NO_TARGET, Outcome.SHARED_MBID_WITHOUT_LINK})
TOKEN_HINT = "api_token rejected: token expired (30-day lifetime) or revoked; create a new one"
LOGIN_HINT = "login rejected: check username and password (local accounts only)"
LOAD_TRIGGER_WINDOW_S = 60
# These end the run; every other ApiError is handled per artist or per sample.
FATAL = (Unreachable, Unauthorized, Forbidden)


class StopRun(Exception):
    def __init__(self, run_status: str, stage: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.status = run_status
        self.stage = stage
        self.message = message
        self.fields = fields


class OutOfTime(Exception):
    """The time budget ran out; stop cleanly and resume next run."""


@dataclass
class _Run:
    settings: Settings
    api: DroppedNeedleApi
    report: RunReport
    deadline: float
    rules: Rules
    authenticated: bool = False
    logged_in: bool = False
    inventory: Inventory | None = None
    cache: dict[str, Any] | None = None
    cursor: str | None = None
    cursor_loaded: bool = False
    decisions: list[tuple[Decision, datetime]] = field(default_factory=list)
    merges_done: int = 0
    merges_stopped: bool = False
    partial: bool = False


@dataclass(frozen=True)
class _Ready:
    """Steps 1-4 of PLAN §7.7 passed: fresh artists, selection and its preview."""

    source: Artist
    survivor: Artist
    selection: dict[str, Any]
    preview: dict[str, Any]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class Runner:
    def __init__(
        self,
        *,
        http: HttpClient,
        settings_source: Callable[[], Mapping[str, str]],
        store: StateStore,
        logger: logging.Logger,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        retry_delay_s: float = 2.0,
        interval_minutes: int = INTERVAL_MINUTES,
        on_report: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._http = http
        self._settings_source = settings_source
        self._state = JsonState(store, logger)
        self._log = logger
        self._clock = clock
        self._now = now
        self._sleep = sleep
        self._retry_delay_s = retry_delay_s
        self._interval = interval_minutes
        self._on_report = on_report

    # --- public -------------------------------------------------------------

    async def lease_held(self) -> bool:
        lease = await self._state.read(LEASE_KEY)
        if not lease or not lease.get("run_id"):
            return False
        expires = parse_iso(lease.get("expires_at"))
        return expires is not None and expires > self._now()

    async def last_report(self) -> dict[str, Any] | None:
        return await self._state.read(REPORT_KEY)

    async def run(self, trigger: str, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or str(uuid.uuid4())
        raw = dict(self._settings_source())
        secrets = secret_values(raw)
        parsed = Settings.parse(raw, interval_minutes=self._interval)
        settings = parsed if isinstance(parsed, Settings) else None
        report = RunReport(
            run_id=run_id,
            trigger=trigger,
            started_at=self._now(),
            version=__version__,
            dry_run=settings.dry_run if settings else True,
            base_url=settings.base_url if settings else None,
        )
        if isinstance(parsed, ConfigError):
            for error in parsed.errors:
                report.add_error("config", error)
            report.finish("config_error", self._now())
            return await self._publish(report, secrets)
        assert settings is not None
        for warning in settings.warnings:
            report.add_warning(warning)
            self._log.warning("mbid-artist-merge config_warning %s", warning)

        if not await self._acquire_lease(run_id, trigger, settings):
            report.finish("lease_held", self._now())
            self._log.info(summary_line(report.data))
            return json.loads(report.to_json(secrets))

        api = DroppedNeedleApi(
            self._http,
            settings.base_url,
            delay_s=settings.request_delay_ms / 1000,
            sleep=self._sleep,
            retry_delay_s=self._retry_delay_s,
            logger=self._log,
        )
        run = _Run(
            settings=settings,
            api=api,
            report=report,
            deadline=self._clock() + settings.time_budget_seconds,
            rules=Rules(
                min_agreeing_samples=settings.min_agreeing_samples,
                exclude_mbids=settings.exclude_mbids,
                exclude_artist_ids=settings.exclude_artist_ids,
            ),
        )
        status = "internal_error"
        try:
            status = await self._execute(run)
        except StopRun as stop:
            status = stop.status
            report.add_error(stop.stage, stop.message, **stop.fields)
        except Unreachable as exc:
            status = "server_unreachable"
            report.add_error("request", str(exc), status=exc.status)
        except Unauthorized as exc:
            status = "auth_lost" if run.authenticated else "auth_failed"
            report.add_error("request", str(exc), status=exc.status)
        except Forbidden as exc:
            status = "not_admin"
            report.add_error("request", str(exc), status=exc.status)
        except ApiError as exc:
            report.add_error("request", str(exc), status=exc.status)
        except asyncio.CancelledError:
            status = "ok"
            run.partial = True
            report.add_error("run", "cancelled (host timeout, plugin disabled or shutdown)")
            raise
        except Exception as exc:  # noqa: BLE001 - a run never raises into the host
            self._log.exception("mbid-artist-merge internal_error run=%s", run_id)
            report.add_error("internal", f"{type(exc).__name__}: {exc}")
        finally:
            data = await self._wrap_up(run, status, secrets)
        return data

    # --- phases -------------------------------------------------------------

    async def _execute(self, run: _Run) -> str:
        await self._authenticate(run)
        progress = await run.api.reconciliation()
        busy = progress.get("state") if isinstance(progress, dict) else None
        if busy in BUSY_STATES:
            raise StopRun(
                "library_busy", "reconciliation", f"artist reconciliation is {busy}; skipped"
            )

        cursor = await self._state.read(CURSOR_KEY)
        after_id = cursor.get("after_id") if cursor else None
        run.cursor = after_id if isinstance(after_id, str) else None
        run.cursor_loaded = True
        cache = await self._state.read(CACHE_KEY)
        artists = cache.get("artists") if cache and cache.get("version") == CACHE_VERSION else None
        run.cache = artists if isinstance(artists, dict) else {}

        try:
            run.inventory = await self._inventory(run)
            totals = run.report.totals
            totals["artists_listed"] = run.inventory.artists_listed
            totals["linked"] = len(run.inventory.linked_by_mbid)
            totals["candidates"] = len(run.inventory.candidates)
            batch = window(run.inventory.candidates, run.cursor, run.settings.max_artists_per_run)
            for candidate in batch:
                self._check_time(run)
                decision = await self._examine(run, candidate)
                run.decisions.append((decision, self._now()))
                run.cursor = candidate.id
                totals["examined"] += 1
        except OutOfTime:
            run.partial = True
        return "ok"

    async def _authenticate(self, run: _Run) -> None:
        settings, api = run.settings, run.api
        if settings.auth_mode == "password":
            try:
                await api.login(settings.username, settings.password)
            except Unauthorized as exc:
                raise StopRun("auth_failed", "auth", LOGIN_HINT, status=exc.status) from None
            except NotFound as exc:
                raise StopRun(
                    "server_unreachable",
                    "auth",
                    "POST /auth/login answered 404: check base_url (include BASE_PATH if set)",
                    status=exc.status,
                ) from None
            run.logged_in = True
        else:
            api.token = settings.api_token
        try:
            me = await api.me()
        except Unauthorized as exc:
            message = TOKEN_HINT if settings.auth_mode == "token" else LOGIN_HINT
            raise StopRun("auth_failed", "auth", message, status=exc.status) from None
        role = me.get("role") if isinstance(me, dict) else None
        if role != "admin":
            raise StopRun("not_admin", "auth", f"account role is {role!r}; an admin is required")
        run.authenticated = True

    async def _inventory(self, run: _Run) -> Inventory:
        artists: list[Artist] = []
        for scope in ("album", "contributors"):
            offset = 0
            while True:
                self._check_time(run)
                page = await run.api.artists_page(scope, offset)
                items = page.get("items") if isinstance(page, dict) else None
                items = items if isinstance(items, list) else []
                for item in items:
                    try:
                        artists.append(Artist.from_api(item))
                    except (KeyError, TypeError, ValueError, AttributeError):
                        artist_id = item.get("id") if isinstance(item, dict) else None
                        run.report.add_error("inventory", "unreadable artist", artist_id=artist_id)
                offset += len(items)
                total = page.get("total") if isinstance(page, dict) else None
                if not items or not isinstance(total, int) or offset >= total:
                    break
        return build_inventory(artists, exclude_artist_ids=run.settings.exclude_artist_ids)

    async def _examine(self, run: _Run, candidate: Artist) -> Decision:
        assert run.inventory is not None
        cached = self._from_cache(run, candidate)
        if cached is not None:
            return cached
        samples = await self._sample(run, candidate)
        decision = decide(
            candidate, samples, linked_by_mbid=run.inventory.linked_by_mbid, rules=run.rules
        )
        if decision.outcome == Outcome.WOULD_MERGE:
            decision = await self._merge(run, decision)
        if decision.outcome in (Outcome.WOULD_MERGE, Outcome.MERGED):
            self._log.info(decision_line(decision))
        elif decision.outcome == Outcome.ERROR:
            self._log.warning(decision_line(decision) + f" detail={decision.detail!r}")
        return decision

    # --- sampling (PLAN §7.4) -----------------------------------------------

    async def _sample(self, run: _Run, artist: Artist) -> list[Sample]:
        api, limit = run.api, run.settings.samples_per_artist
        groups: list[list[TrackRef]] = []
        try:
            albums = await api.artist_albums(artist.id)
        except FATAL:
            raise
        except ApiError as exc:
            self._sample_error(run, "albums", exc, artist_id=artist.id)
            albums = []
        # Round-robin over the first `limit` non-empty albums gives the same picks as
        # over all albums, so stop fetching there.
        for album in albums:
            if sum(1 for g in groups if g) >= limit:
                break
            album_id = album.get("id")
            if not isinstance(album_id, str):
                continue
            try:
                tracks = await api.album_tracks(album_id)
            except FATAL:
                raise
            except ApiError as exc:
                self._sample_error(run, "tracks", exc, album_id=album_id)
                continue
            groups.append(_refs(tracks, album_id, "album_artist"))
        picked = round_robin(groups, limit)

        if len(picked) < limit:
            try:
                appearances = await api.artist_appearances(artist.id)
            except FATAL:
                raise
            except ApiError as exc:
                self._sample_error(run, "appearances", exc, artist_id=artist.id)
                appearances = []
            seen = {ref.track_id for ref in picked}
            groups = []
            for item in appearances:
                album = item.get("album")
                album_id = album.get("id") if isinstance(album, dict) else None
                tracks = item.get("tracks") if isinstance(item.get("tracks"), list) else []
                refs = _refs(
                    tracks, album_id if isinstance(album_id, str) else None, "track_artist"
                )
                groups.append([r for r in refs if r.track_id not in seen])
            picked += round_robin(groups, limit - len(picked))

        samples: list[Sample] = []
        for ref in picked:
            try:
                tag = await api.track_tags(ref.track_id)
            except FATAL:
                raise
            except ApiError as exc:
                self._sample_error(run, "tags", exc, track_id=ref.track_id)
                continue
            if isinstance(tag, dict):
                samples.append(Sample(ref, extract(artist.name, ref.role, tag)))
        return samples

    def _sample_error(self, run: _Run, stage: str, exc: ApiError, **fields: Any) -> None:
        run.report.add_error(stage, exc.message or str(exc), status=exc.status, **fields)
        self._log.warning(
            "mbid-artist-merge sample_error stage=%s status=%s %s",
            stage,
            exc.status,
            " ".join(f"{k}={v}" for k, v in fields.items()),
        )

    # --- merge (PLAN §7.7) --------------------------------------------------

    async def _merge(self, run: _Run, decision: Decision) -> Decision:
        settings = run.settings
        if not settings.dry_run:
            if run.merges_stopped:
                return replace(decision, outcome=Outcome.DEFERRED, detail="merges stopped")
            if run.merges_done >= settings.max_merges_per_run:
                detail = f"max_merges_per_run={settings.max_merges_per_run} reached"
                return replace(decision, outcome=Outcome.DEFERRED, detail=detail)
        checked = await self._preflight(run, decision)
        if isinstance(checked, Decision):
            return checked
        decision = replace(
            decision,
            source=checked.source,
            target=checked.survivor,
            reference_counts=checked.preview.get("reference_counts") or {},
        )
        if settings.dry_run:
            return decision
        return await self._apply(run, decision, checked)

    async def _apply(self, run: _Run, decision: Decision, ready: _Ready) -> Decision:
        """Steps 5-7: merge, verify the response and the survivor's link, record.
        Any verification mismatch stops further merges in this run."""
        source, survivor = ready.source, ready.survivor
        token = ready.preview.get("preview_token")
        if not isinstance(token, str) or not token:
            run.merges_stopped = True
            return replace(decision, outcome=Outcome.ERROR, detail="preview returned no token")
        try:
            result = await run.api.merge(
                ready.selection,
                preview_token=token,
                idempotency_key=idempotency_key(source, survivor),
            )
        except FATAL:
            raise
        except Conflict as exc:
            return replace(
                decision, outcome=Outcome.STALE, detail=f"merge {exc.code}: {exc.message}"
            )
        except ApiError as exc:
            run.merges_stopped = True
            return replace(decision, outcome=Outcome.ERROR, detail=f"merge failed: {exc}")
        run.merges_done += 1

        problems = merge_problems(result, source.id, survivor.id)
        if not problems:
            try:
                after = await run.api.artist(survivor.id)
            except FATAL:
                raise
            except ApiError as exc:
                problems.append(f"survivor re-read failed: {exc}")
            else:
                if not after.is_linked or after.mbid != decision.mbid:
                    problems.append(
                        f"survivor link changed: state={after.artist_identity_state} "
                        f"mbid={after.mbid} expected={decision.mbid}"
                    )
        if problems:
            run.merges_stopped = True
            detail = "verification failed, merges stopped: " + "; ".join(problems)
            detail += f"; response={json.dumps(result, ensure_ascii=False)[:2000]}"
            return replace(decision, outcome=Outcome.ERROR, detail=detail)
        return replace(decision, outcome=Outcome.MERGED)

    async def _preflight(self, run: _Run, decision: Decision) -> Decision | _Ready:
        """Steps 1-4: re-read both artists, build the selection, preview it. Read-only."""
        assert decision.target is not None and decision.mbid is not None
        api, mbid = run.api, decision.mbid
        try:
            source = await api.artist(decision.source.id)
        except Retired as exc:
            detail = f"source now redirects to {exc.survivor_id}"
            return replace(decision, outcome=Outcome.ALREADY_MERGED, detail=detail)
        except NotFound:
            return replace(decision, outcome=Outcome.CHANGED, detail="source no longer exists")
        if not source.is_local_only:
            detail = f"source is now {source.artist_identity_state}"
            return replace(decision, source=source, outcome=Outcome.CHANGED, detail=detail)
        try:
            survivor = await api.artist(decision.target.id)
        except (Retired, NotFound):
            return replace(decision, outcome=Outcome.CHANGED, detail="survivor no longer exists")
        if not survivor.is_linked or survivor.mbid != mbid:
            detail = f"survivor now {survivor.artist_identity_state} mbid={survivor.mbid}"
            return replace(decision, target=survivor, outcome=Outcome.CHANGED, detail=detail)

        selection = {
            "source_artist_ids": [source.id],
            "surviving_artist_id": survivor.id,
            "expected_revisions": {
                source.id: source.row_revision,
                survivor.id: survivor.row_revision,
            },
        }
        fresh = replace(decision, source=source, target=survivor)
        try:
            preview = await api.merge_preview(selection)
        except FATAL:
            raise
        except Conflict as exc:
            return replace(
                fresh, outcome=Outcome.STALE, detail=f"preview {exc.code}: {exc.message}"
            )
        except ApiError as exc:
            run.merges_stopped = True
            return replace(fresh, outcome=Outcome.ERROR, detail=f"preview failed: {exc}")
        if not isinstance(preview, dict):
            run.merges_stopped = True
            return replace(fresh, outcome=Outcome.ERROR, detail="preview returned no object")
        conflicts = preview.get("identity_conflicts")
        if conflicts != []:
            return replace(
                fresh,
                outcome=Outcome.IDENTITY_CONFLICT,
                detail=f"identity_conflicts={conflicts!r}",
                reference_counts=preview.get("reference_counts"),
            )
        return _Ready(source, survivor, selection, preview)

    # --- cache --------------------------------------------------------------

    def _fresh_entry(self, run: _Run, artist: Artist) -> tuple[Outcome, str | None] | None:
        entry = (run.cache or {}).get(artist.id)
        if not isinstance(entry, dict) or entry.get("row_revision") != artist.row_revision:
            return None
        checked = parse_iso(entry.get("checked_at"))
        now = self._now()
        if checked is None or not now - CACHE_TTL <= checked <= now + timedelta(minutes=5):
            return None
        try:
            outcome = Outcome(entry.get("decision"))
        except ValueError:
            return None
        return outcome, normalize_mbid(entry.get("mbid"))

    def _from_cache(self, run: _Run, artist: Artist) -> Decision | None:
        assert run.inventory is not None
        fresh = self._fresh_entry(run, artist)
        if fresh is None:
            return None
        outcome, mbid = fresh
        if outcome in REUSABLE:
            return Decision(outcome, artist, mbid=mbid, cached=True)
        if outcome in UNLINKED and mbid and mbid not in run.inventory.linked_by_mbid:
            # mark_shared() re-derives shared_mbid_without_link for the whole run
            return Decision(Outcome.NO_TARGET, artist, mbid=mbid, cached=True)
        return None

    def _known_peers(self, run: _Run, decided: set[str]) -> dict[str, list[Artist]]:
        if run.inventory is None:
            return {}
        peers: dict[str, list[Artist]] = {}
        for artist in run.inventory.candidates:
            if artist.id in decided:
                continue
            fresh = self._fresh_entry(run, artist)
            if fresh is None:
                continue
            outcome, mbid = fresh
            if outcome in UNLINKED and mbid and mbid not in run.inventory.linked_by_mbid:
                peers.setdefault(mbid, []).append(artist)
        return peers

    def _updated_cache(self, run: _Run, decisions: list[Decision]) -> dict[str, Any]:
        assert run.inventory is not None and run.cache is not None
        artists = {k: v for k, v in run.cache.items() if k in run.inventory.artists}
        stamp = iso(self._now())
        for d in decisions:
            if d.cached:
                continue
            artists[d.source.id] = {
                "row_revision": d.source.row_revision,
                "decision": d.outcome.value,
                "mbid": d.mbid,
                "checked_at": stamp,
            }
        doc = {"version": CACHE_VERSION, "artists": artists}
        while len(dumps(doc).encode("utf-8")) > CACHE_MAX_BYTES and artists:
            oldest = sorted(artists, key=lambda k: str(artists[k].get("checked_at")))
            for key in oldest[: max(1, len(oldest) // 10)]:
                del artists[key]
        return doc

    # --- lease & wrap-up ----------------------------------------------------

    async def _acquire_lease(self, run_id: str, trigger: str, settings: Settings) -> bool:
        if await self.lease_held():
            return False
        started = self._now()
        lease = {
            "run_id": run_id,
            "trigger": trigger,
            "started_at": iso(started),
            "expires_at": iso(
                started + timedelta(seconds=settings.time_budget_seconds + LEASE_GRACE_S)
            ),
        }
        if not await self._state.write(LEASE_KEY, lease):
            return True  # no durable lease available: run anyway
        current = await self._state.read(LEASE_KEY)
        return current is None or current.get("run_id") == run_id

    async def _release_lease(self, run_id: str) -> None:
        current = await self._state.read(LEASE_KEY)
        if current is None or current.get("run_id") in (None, run_id):
            await self._state.write(LEASE_KEY, {})

    def _check_time(self, run: _Run) -> None:
        if self._clock() >= run.deadline:
            raise OutOfTime

    async def _wrap_up(self, run: _Run, status: str, secrets: list[str]) -> dict[str, Any]:
        """Report decisions, save cache + cursor + report, release the lease, log out.
        Each step is best-effort and independent of the others."""
        report = run.report
        try:
            stamped = run.decisions
            decided = {d.source.id for d, _ in stamped}
            marked = mark_shared([d for d, _ in stamped], self._known_peers(run, decided))
            for decision, (_, at) in zip(marked, stamped, strict=True):
                report.add_decision(decision, at)
            if run.inventory is not None and run.cache is not None:
                await self._state.write(CACHE_KEY, self._updated_cache(run, marked))
            if run.cursor_loaded:
                await self._state.write(CURSOR_KEY, {"after_id": run.cursor})
        except Exception:  # noqa: BLE001 - wrap-up is best-effort
            self._log.exception("mbid-artist-merge wrap_up_failed run=%s", report.data["run_id"])
            report.add_error("internal", "saving state failed")
            status = "internal_error"
        report.finish(status, self._now(), partial=run.partial)
        data = await self._publish(report, secrets)
        try:
            await self._release_lease(report.data["run_id"])
        except Exception as exc:  # noqa: BLE001 - an expired lease frees itself
            self._log.warning("mbid-artist-merge lease_release_failed error=%s", exc)
        if run.logged_in:
            try:
                await run.api.logout()
            except Exception as exc:  # noqa: BLE001 - logout is best-effort
                self._log.warning("mbid-artist-merge logout_failed error=%s", exc)
        return data

    async def _publish(self, report: RunReport, secrets: list[str]) -> dict[str, Any]:
        """Log the summary, persist the report, hand it to ``on_report``."""
        text = report.to_json(secrets)
        data = json.loads(text)
        line = summary_line(data)
        if data["status"] == "ok":
            self._log.info(line)
        else:
            self._log.warning(line)
        await self._state.write(REPORT_KEY, text)
        if self._on_report is not None:
            try:
                self._on_report(data)
            except Exception:  # noqa: BLE001 - the report is already saved
                self._log.exception("mbid-artist-merge on_report_failed")
        return data


def idempotency_key(source: Artist, survivor: Artist) -> str:
    """Deterministic, so a retried request is a safe replay of the same merge."""
    return (
        f"mbid-artist-merge:v1:{source.id}:{source.row_revision}"
        f":{survivor.id}:{survivor.row_revision}"
    )


def merge_problems(result: object, source_id: str, survivor_id: str) -> list[str]:
    """What is wrong with a merge response (empty when it is the expected one)."""
    if not isinstance(result, dict):
        return ["response is not an object"]
    problems = []
    if result.get("kind") != "merge_artist":
        problems.append(f"kind={result.get('kind')!r}")
    if result.get("surviving_artist_id") != survivor_id:
        problems.append(f"surviving_artist_id={result.get('surviving_artist_id')!r}")
    retired = result.get("retired_artist_ids")
    if not isinstance(retired, list) or source_id not in retired:
        problems.append(f"retired_artist_ids={retired!r}")
    return problems


def _refs(tracks: list[Any], album_id: str | None, role: str) -> list[TrackRef]:
    refs = []
    for track in tracks:
        track_id = track.get("id") if isinstance(track, dict) else None
        if isinstance(track_id, str) and track_id:
            refs.append(TrackRef(track_id, album_id, role))  # type: ignore[arg-type]
    return refs


def trigger_for(constructed_at: float, now: float, first_tick: bool) -> str:
    """``load`` for the run_on_load tick right after the plugin starts, else ``schedule``."""
    return "load" if first_tick and now - constructed_at < LOAD_TRIGGER_WINDOW_S else "schedule"
