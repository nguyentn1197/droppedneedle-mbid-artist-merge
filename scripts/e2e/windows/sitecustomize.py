"""Windows shim for the throwaway e2e DroppedNeedle server (never part of the plugin).

DroppedNeedle targets Linux. ``run_server.sh`` copies this file into the server
venv's site-packages on Windows (Git Bash) so v2.15.0 starts there:

- ``os.O_DIRECTORY`` / ``os.O_NOFOLLOW`` do not exist on Windows. The server only
  uses them for best-effort directory fsyncs and file moves this test never does.
- ``ctypes.CDLL(None)`` ("the C library" on POSIX) is mapped to msvcrt; the
  Linux-only ``renameat2`` it is used for is never called by the test.
"""

from __future__ import annotations

import ctypes
import os
import sys

if sys.platform == "win32":
    for _name in ("O_DIRECTORY", "O_NOFOLLOW"):
        if not hasattr(os, _name):
            setattr(os, _name, 0)

    _CDLL = ctypes.CDLL

    class _CDLLShim(_CDLL):
        def __init__(self, name: str | None, *args: object, **kwargs: object) -> None:
            super().__init__("msvcrt" if name is None else name, *args, **kwargs)

    ctypes.CDLL = _CDLLShim
