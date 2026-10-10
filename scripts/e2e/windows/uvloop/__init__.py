"""Windows stand-in for uvloop (e2e test server only): DroppedNeedle's launcher
passes ``--loop uvloop`` to uvicorn, and uvloop has no Windows build."""

from __future__ import annotations

import asyncio

new_event_loop = asyncio.new_event_loop
EventLoopPolicy = asyncio.DefaultEventLoopPolicy


def install() -> None:
    pass
