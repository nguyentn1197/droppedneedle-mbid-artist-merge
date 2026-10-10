"""DroppedNeedle v2 adapter for the mbid-artist-merge plugin.

The host executes this file as ``droppedneedle_plugin.mbid-artist-merge.plugin``
via ``spec_from_file_location``, so sibling modules are not importable by
default. Put this folder first on ``sys.path`` and drop any previously imported
copy of the package, so a plugin reload (any plugin save) picks up new code.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
_PACKAGE = "mbid_artist_merge"


def _prepare_imports() -> None:
    sys.path[:] = [_PLUGIN_DIR, *(p for p in sys.path if p != _PLUGIN_DIR)]
    for name in [n for n in sys.modules if n == _PACKAGE or n.startswith(f"{_PACKAGE}.")]:
        del sys.modules[name]


_prepare_imports()

from mbid_artist_merge.report import notice_for
from mbid_artist_merge.runner import Runner, trigger_for
from mbid_artist_merge.state import HostStateStore


class _RouteResponse:
    """Stand-in for the host's ``PluginRouteResponse``; the host only reads
    ``.status`` and ``.body``."""

    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.body = body


def _response(status: int, body: object) -> object:
    try:
        from infrastructure.plugins.protocols import PluginRouteResponse
    except ImportError:
        return _RouteResponse(status, body)
    return PluginRouteResponse(status=status, body=body)


class MbidArtistMerge:
    def __init__(self, context: Any) -> None:
        self._ctx = context
        self._log = context.logger
        self._created = time.monotonic()
        self._ticks = 0
        self._last_report: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None  # keeps the manual run alive
        self._runner = Runner(
            http=context.http,
            settings_source=lambda: context.settings,
            store=HostStateStore(context.plugin_name),
            logger=context.logger,
            on_report=self._remember,
        )

    def _remember(self, report: dict[str, Any]) -> None:
        self._last_report = report

    async def on_tick(self) -> None:
        self._ticks += 1
        trigger = trigger_for(self._created, time.monotonic(), self._ticks == 1)
        try:
            await self._notify(await self._runner.run(trigger))
        except Exception:  # noqa: BLE001 - never raise into the host
            self._log.exception("mbid-artist-merge internal_error trigger=%s", trigger)

    async def _notify(self, report: dict[str, Any]) -> None:
        """Tell subscriber plugins about merges (best-effort; the UI shows nothing)."""
        notice = notice_for(report)
        if notice is None:
            return
        try:
            result = self._ctx.publish("plugin_notice", notice)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - a notice is never worth failing a run
            self._log.warning("mbid-artist-merge notice_failed error=%s", exc)
            return
        if getattr(result, "ok", True) is False:
            self._log.warning(
                "mbid-artist-merge notice_rejected status=%s error=%s",
                getattr(result, "status", None),
                getattr(result, "error", None),
            )

    async def handle_route(self, method: str, subpath: str, query: dict, body: object) -> object:
        verb = (method or "").upper()
        try:
            if verb == "GET" and subpath == "report":
                return await self._report()
            if verb == "POST" and subpath == "run":
                return await self._start_run()
        except Exception:  # noqa: BLE001 - answer with an error body, never a traceback
            self._log.exception("mbid-artist-merge route_failed method=%s path=%s", verb, subpath)
            return _response(500, {"error": "internal error, see the server log"})
        return _response(404, {"error": "not found"})

    async def _start_run(self) -> object:
        """Start a manual run in the background; answer well inside the 5 s budget."""
        if (self._task is not None and not self._task.done()) or await self._runner.lease_held():
            return _response(200, {"started": False, "reason": "running"})
        run_id = str(uuid.uuid4())
        self._task = asyncio.create_task(self._manual_run(run_id))
        return _response(200, {"started": True, "run_id": run_id})

    async def _manual_run(self, run_id: str) -> None:
        try:
            await self._notify(await self._runner.run("manual", run_id=run_id))
        except Exception:  # noqa: BLE001 - nobody awaits this task
            self._log.exception("mbid-artist-merge internal_error trigger=manual run=%s", run_id)

    async def _report(self) -> object:
        report = self._last_report or await self._runner.last_report()
        if not report:
            return _response(404, {"error": "no run yet"})
        return _response(200, report)
