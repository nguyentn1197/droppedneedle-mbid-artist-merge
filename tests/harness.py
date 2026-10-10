"""Wiring for integration tests: fake server + memory state + fake clock -> Runner."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fake_dn import ADMIN_PASSWORD, ADMIN_USER, ORIGIN, FakeDN

from mbid_artist_merge.runner import Runner
from mbid_artist_merge.state import MemoryStateStore

EPOCH = datetime(2026, 10, 9, 5, 0, 0, tzinfo=UTC)
LOGGER = logging.getLogger("plugin.mbid-artist-merge")


@dataclass
class FakeClock:
    t: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def now(self) -> datetime:
        return EPOCH + timedelta(seconds=self.t)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)
        await asyncio.sleep(0)  # yield like a real sleep would


@dataclass
class Harness:
    fake: FakeDN = field(default_factory=FakeDN)
    store: MemoryStateStore = field(default_factory=MemoryStateStore)
    clock: FakeClock = field(default_factory=FakeClock)
    settings: dict[str, str] = field(
        default_factory=lambda: {
            "base_url": ORIGIN,
            "username": ADMIN_USER,
            "password": ADMIN_PASSWORD,
        }
    )
    reports: list[dict[str, Any]] = field(default_factory=list)
    follow_redirects: bool = True

    def __post_init__(self) -> None:
        self.client: httpx.AsyncClient = self.fake.client(follow_redirects=self.follow_redirects)

    def runner(self) -> Runner:
        return Runner(
            http=self.client,
            settings_source=lambda: dict(self.settings),
            store=self.store,
            logger=LOGGER,
            clock=self.clock,
            now=self.clock.now,
            sleep=self.clock.sleep,
            on_report=self.reports.append,
        )

    async def run(self, trigger: str = "manual") -> dict[str, Any]:
        return await self.runner().run(trigger)

    def state(self, key: str) -> Any:
        text = self.store.data.get(key)
        return None if text is None else json.loads(text)


def by_source(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {a["source"]["name"]: a for a in report["actions"]}


def nonzero(report: dict[str, Any]) -> dict[str, int]:
    return {k: v for k, v in report["totals"].items() if v}
