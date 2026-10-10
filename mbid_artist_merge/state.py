"""Plugin state: a tiny key -> text store (PLAN §8).

v2 keeps it in files inside the plugin folder through the host helper, so a
reinstall wipes it. Everything here is treated as a cache: reads that fail mean
"nothing stored", writes that fail log a warning and never fail the run.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,63}$")

LEASE_KEY = "state/lease"
CURSOR_KEY = "state/cursor"
CACHE_KEY = "state/cache"
REPORT_KEY = "state/last-report"


def check_key(key: str) -> str:
    if not KEY_RE.match(key) or key.endswith("/") or "//" in key:
        raise ValueError(f"invalid state key {key!r}")
    return key


class StateStore(Protocol):
    async def read(self, key: str) -> str | None: ...
    async def write(self, key: str, value: str) -> None: ...


class HostStateStore:
    """DroppedNeedle v2: ``get_plugin_host().read/write_plugin_state_file``."""

    def __init__(self, plugin_name: str) -> None:
        self._plugin_name = plugin_name

    async def read(self, key: str) -> str | None:
        from core.dependencies import get_plugin_host
        from infrastructure.plugins.host import PluginStateFileError

        try:
            data = await get_plugin_host().read_plugin_state_file(self._plugin_name, check_key(key))
        except PluginStateFileError:
            return None
        return data.decode("utf-8")

    async def write(self, key: str, value: str) -> None:
        from core.dependencies import get_plugin_host

        await get_plugin_host().write_plugin_state_file(
            self._plugin_name, check_key(key), value.encode("utf-8")
        )


class MemoryStateStore:
    """In-memory store for tests; validates keys like the host does."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.fail_reads = False
        self.fail_writes = False

    async def read(self, key: str) -> str | None:
        check_key(key)
        if self.fail_reads:
            raise OSError("state read failed (test)")
        return self.data.get(key)

    async def write(self, key: str, value: str) -> None:
        check_key(key)
        if self.fail_writes:
            raise OSError("state write failed (test)")
        self.data[key] = value


class JsonState:
    """Best-effort JSON objects on top of a :class:`StateStore`."""

    def __init__(self, store: StateStore, logger: logging.Logger) -> None:
        self._store = store
        self._log = logger

    async def read(self, key: str) -> dict[str, Any] | None:
        try:
            text = await self._store.read(key)
        except Exception as exc:  # noqa: BLE001 - state is a cache; a failed read means empty
            self._log.warning("mbid-artist-merge state_read_failed key=%s error=%s", key, exc)
            return None
        if not text:
            return None
        try:
            value = json.loads(text)
        except ValueError:
            self._log.warning("mbid-artist-merge state_corrupt key=%s", key)
            return None
        return value if isinstance(value, dict) else None

    async def write(self, key: str, value: dict[str, Any] | str) -> bool:
        text = value if isinstance(value, str) else dumps(value)
        try:
            await self._store.write(key, text)
        except Exception as exc:  # noqa: BLE001 - writes are best-effort by design
            self._log.warning("mbid-artist-merge state_write_failed key=%s error=%s", key, exc)
            return False
        return True


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
