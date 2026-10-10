"""Load plugin.py exactly the way DroppedNeedle's host does (spec_from_file_location)."""

from __future__ import annotations

import importlib.util
import logging
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "mbid_artist_merge"


class FakeContext:
    def __init__(self, settings: dict[str, str]) -> None:
        self._settings = settings
        self.plugin_name = "mbid-artist-merge"
        self.http = None
        self.logger = logging.getLogger("plugin.mbid-artist-merge")

    @property
    def settings(self) -> dict[str, str]:
        return dict(self._settings)


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


@pytest.mark.usefixtures("isolated_imports")
def test_version_in_sync_with_manifest() -> None:
    manifest = tomllib.loads((REPO_ROOT / "plugin.toml").read_text(encoding="utf-8"))
    load_like_host()
    assert sys.modules[PACKAGE].__version__ == manifest["plugin"]["version"]
