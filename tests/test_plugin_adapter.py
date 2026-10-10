"""plugin.py, loaded exactly the way DroppedNeedle's host does (spec_from_file_location)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fake_dn import ADMIN_PASSWORD, ADMIN_USER, ORIGIN, FakeDN

from mbid_artist_merge.state import LEASE_KEY, REPORT_KEY, MemoryStateStore

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "mbid_artist_merge"
SETTINGS = {
    "base_url": ORIGIN,
    "username": ADMIN_USER,
    "password": ADMIN_PASSWORD,
    "request_delay_ms": "0",
}


class FakeContext:
    def __init__(self, settings: dict[str, str], http: Any = None) -> None:
        self._settings = settings
        self.plugin_name = "mbid-artist-merge"
        self.http = http
        self.logger = logging.getLogger("plugin.mbid-artist-merge")
        self.published: list[tuple[str, object]] = []

    @property
    def settings(self) -> dict[str, str]:
        return dict(self._settings)

    def publish(self, kind: str, payload: object = None, **kwargs: object) -> object:
        self.published.append((kind, payload))
        return None


@pytest.fixture
def isolated_imports() -> Iterator[None]:
    saved_path = list(sys.path)
    saved = {k: v for k, v in sys.modules.items() if k == PACKAGE or k.startswith(f"{PACKAGE}.")}
    try:
        yield
    finally:
        sys.path[:] = saved_path
        for name in [n for n in sys.modules if n == PACKAGE or n.startswith(f"{PACKAGE}.")]:
            del sys.modules[name]
        sys.modules.update(saved)


def load_like_host() -> ModuleType:
    full_name = "droppedneedle_plugin.mbid-artist-merge.plugin"
    spec = importlib.util.spec_from_file_location(full_name, REPO_ROOT / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(full_name, None)
    return module


@pytest.fixture
def plugin_module(isolated_imports: None) -> ModuleType:
    return load_like_host()


@pytest.fixture
def store() -> MemoryStateStore:
    return MemoryStateStore()


@pytest.fixture
def make_plugin(
    plugin_module: ModuleType, store: MemoryStateStore, monkeypatch: pytest.MonkeyPatch
) -> Any:
    monkeypatch.setattr(plugin_module, "HostStateStore", lambda name: store)

    def make(fake: FakeDN | None = None, settings: dict[str, str] | None = None) -> Any:
        fake = fake or FakeDN()
        ctx = FakeContext(dict(SETTINGS if settings is None else settings), fake.client())
        return plugin_module.MbidArtistMerge(ctx)

    return make


# --- loading ----------------------------------------------------------------


@pytest.mark.usefixtures("isolated_imports")
def test_plugin_dir_goes_first_on_sys_path() -> None:
    sys.path.append(str(REPO_ROOT))
    load_like_host()
    assert sys.path[0] == str(REPO_ROOT)
    assert sys.path.count(str(REPO_ROOT)) == 1


@pytest.mark.usefixtures("isolated_imports")
def test_reload_reimports_the_package() -> None:
    first = load_like_host()
    first_pkg = sys.modules[PACKAGE]
    second = load_like_host()
    assert sys.modules[PACKAGE] is not first_pkg
    assert first.MbidArtistMerge is not second.MbidArtistMerge


@pytest.mark.usefixtures("isolated_imports")
def test_entrypoint_class_matches_manifest() -> None:
    manifest = tomllib.loads((REPO_ROOT / "plugin.toml").read_text(encoding="utf-8"))
    module_name, _, class_name = manifest["plugin"]["entrypoint"].partition(":")
    assert module_name == "plugin"
    module = load_like_host()
    instance = getattr(module, class_name)(FakeContext({}))
    assert callable(instance.on_tick)
    assert callable(instance.handle_route)


@pytest.mark.usefixtures("isolated_imports")
def test_version_in_sync_with_manifest() -> None:
    manifest = tomllib.loads((REPO_ROOT / "plugin.toml").read_text(encoding="utf-8"))
    load_like_host()
    assert sys.modules[PACKAGE].__version__ == manifest["plugin"]["version"]


# --- ticks ------------------------------------------------------------------


async def test_first_tick_is_the_load_run(make_plugin: Any, store: MemoryStateStore) -> None:
    plugin = make_plugin()
    await plugin.on_tick()
    report = json.loads(store.data[REPORT_KEY])
    assert report["trigger"] == "load"
    assert report["status"] == "ok"
    assert report["totals"]["would_merge"] == 1


async def test_tick_swallows_exceptions(
    make_plugin: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    plugin = make_plugin()

    async def boom(trigger: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin._runner, "run", boom)
    await plugin.on_tick()  # must not raise
    assert "internal_error" in caplog.text


async def test_tick_with_config_error_reports_it(make_plugin: Any, store: MemoryStateStore) -> None:
    plugin = make_plugin(settings={})
    await plugin.on_tick()
    assert json.loads(store.data[REPORT_KEY])["status"] == "config_error"


# --- routes -----------------------------------------------------------------


async def test_report_404_before_any_run(make_plugin: Any) -> None:
    response = await make_plugin().handle_route("GET", "report", {}, None)
    assert (response.status, response.body) == (404, {"error": "no run yet"})


async def test_report_after_a_run(make_plugin: Any) -> None:
    plugin = make_plugin()
    await plugin.on_tick()
    response = await plugin.handle_route("GET", "report", {}, None)
    assert response.status == 200
    assert response.body["totals"]["would_merge"] == 1
    assert len(json.dumps(response.body).encode()) < 1024 * 1024


async def test_report_survives_a_reload_through_state(make_plugin: Any) -> None:
    await make_plugin().on_tick()
    fresh = make_plugin()  # a reload builds a new instance with no memory
    response = await fresh.handle_route("GET", "report", {}, None)
    assert response.status == 200
    assert response.body["status"] == "ok"


async def test_unknown_route_is_404(make_plugin: Any) -> None:
    plugin = make_plugin()
    for method, path in [("GET", "nope"), ("DELETE", "report"), ("POST", "report")]:
        response = await plugin.handle_route(method, path, {}, None)
        assert response.status == 404


async def test_post_run_starts_a_background_run(make_plugin: Any, store: MemoryStateStore) -> None:
    plugin = make_plugin()
    response = await asyncio.wait_for(plugin.handle_route("POST", "run", {}, None), timeout=5)
    assert response.status == 200
    assert response.body["started"] is True
    run_id = response.body["run_id"]
    assert plugin._task is not None
    await plugin._task
    report = (await plugin.handle_route("GET", "report", {}, None)).body
    assert (report["run_id"], report["trigger"], report["status"]) == (run_id, "manual", "ok")


async def test_post_run_while_running_is_refused(make_plugin: Any) -> None:
    plugin = make_plugin()
    first = await plugin.handle_route("POST", "run", {}, None)
    second = await plugin.handle_route("POST", "run", {}, None)
    assert first.body["started"] is True
    assert second.body == {"started": False, "reason": "running"}
    await plugin._task


async def test_post_run_with_a_held_lease_is_refused(
    make_plugin: Any, store: MemoryStateStore
) -> None:
    store.data[LEASE_KEY] = json.dumps({"run_id": "x", "expires_at": "2999-01-01T00:00:00Z"})
    plugin = make_plugin()
    response = await plugin.handle_route("POST", "run", {}, None)
    assert response.body == {"started": False, "reason": "running"}
    assert plugin._task is None


async def test_manual_run_failure_is_logged_not_raised(
    make_plugin: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    plugin = make_plugin()

    async def boom(trigger: str, run_id: str | None = None) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin._runner, "run", boom)
    await plugin.handle_route("POST", "run", {}, None)
    await plugin._task
    assert "internal_error trigger=manual" in caplog.text
