"""DroppedNeedle v2 adapter for the mbid-artist-merge plugin.

The host executes this file as ``droppedneedle_plugin.mbid-artist-merge.plugin``
via ``spec_from_file_location``, so sibling modules are not importable by
default. Put this folder first on ``sys.path`` and drop any previously imported
copy of the package, so a plugin reload (any plugin save) picks up new code.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
_PACKAGE = "mbid_artist_merge"


def _prepare_imports() -> None:
    sys.path[:] = [_PLUGIN_DIR, *(p for p in sys.path if p != _PLUGIN_DIR)]
    for name in [n for n in sys.modules if n == _PACKAGE or n.startswith(f"{_PACKAGE}.")]:
        del sys.modules[name]


_prepare_imports()

from mbid_artist_merge import __version__
from mbid_artist_merge.config import ConfigError, Settings


class MbidArtistMerge:
    def __init__(self, context: object) -> None:
        self._ctx = context
        self._log = context.logger

    async def on_tick(self) -> None:
        try:
            settings = Settings.parse(self._ctx.settings)
            if isinstance(settings, ConfigError):
                self._log.warning("mbid-artist-merge config_error %s", settings.message)
                return
            self._log.info(
                "mbid-artist-merge version=%s dry_run=%s (runner not wired yet)",
                __version__,
                str(settings.dry_run).lower(),
            )
        except Exception:  # noqa: BLE001 - never raise into the host
            self._log.exception("mbid-artist-merge internal_error")
