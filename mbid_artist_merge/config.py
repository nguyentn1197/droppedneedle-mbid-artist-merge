"""Plugin settings: parse the host's ``dict[str, str]`` into typed values (PLAN §6).

The manifest cannot carry defaults, so they live here. An out-of-range or
unparseable number is an error, never clamped, so the owner notices.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from .constants import MBID_RE

INTERVAL_MINUTES = 360  # keep in sync with plugin.toml [schedule] interval_minutes
DEFAULT_BASE_URL = "http://127.0.0.1:8688"

_FALSE_WORDS = frozenset({"false", "0", "no", "off"})
_INT_RE = re.compile(r"-?[0-9]+")
_LIST_SPLIT_RE = re.compile(r"[,\s]+")


@dataclass(frozen=True)
class ConfigError:
    errors: tuple[str, ...]

    @property
    def message(self) -> str:
        return "; ".join(self.errors)


@dataclass(frozen=True)
class Settings:
    base_url: str = DEFAULT_BASE_URL
    username: str = ""
    password: str = field(default="", repr=False)
    api_token: str = field(default="", repr=False)
    dry_run: bool = True
    max_merges_per_run: int = 10
    max_artists_per_run: int = 200
    samples_per_artist: int = 5
    min_agreeing_samples: int = 2
    request_delay_ms: int = 100
    time_budget_seconds: int = 900
    exclude_artist_ids: frozenset[str] = frozenset()
    exclude_mbids: frozenset[str] = frozenset()
    warnings: tuple[str, ...] = ()

    @property
    def auth_mode(self) -> Literal["token", "password"]:
        return "token" if self.api_token else "password"

    @classmethod
    def parse(
        cls, raw: Mapping[str, str], *, interval_minutes: int = INTERVAL_MINUTES
    ) -> Settings | ConfigError:
        errors: list[str] = []
        warnings: list[str] = []

        def text(key: str) -> str:
            value = raw.get(key)
            return value.strip() if isinstance(value, str) else ""

        base_url = text("base_url") or DEFAULT_BASE_URL
        parts = urlsplit(base_url)
        if "@" in parts.netloc:
            # httpx would send Basic auth from it instead of the bearer token
            errors.append("base_url must not contain user:password@")
        elif parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            errors.append(f"base_url must start with http:// or https:// (got {base_url!r})")
        base_url = base_url.rstrip("/")

        api_token = text("api_token")
        username = text("username")
        raw_password = raw.get("password")
        # Not trimmed: a password may legitimately start or end with a space.
        password = raw_password if isinstance(raw_password, str) else ""
        if not api_token and not (username and password.strip()):
            errors.append("set api_token, or both username and password")

        ints: dict[str, int] = {}
        bounds = {
            "max_merges_per_run": (10, 0, 500),
            "max_artists_per_run": (200, 1, 10000),
            "samples_per_artist": (5, 1, 20),
            "min_agreeing_samples": (2, 1, 20),
            "request_delay_ms": (100, 0, 5000),
            "time_budget_seconds": (900, 30, interval_minutes * 60 - 60),
        }
        for key, (default, low, high) in bounds.items():
            value = text(key)
            if not value:
                ints[key] = default
            elif not _INT_RE.fullmatch(value):
                errors.append(f"{key} must be a whole number (got {value!r})")
            elif not low <= int(value) <= high:
                errors.append(f"{key} must be between {low} and {high} (got {value})")
            else:
                ints[key] = int(value)

        exclude_mbids: set[str] = set()
        for entry in _split_list(text("exclude_mbids")):
            if MBID_RE.match(entry):
                exclude_mbids.add(entry)
            else:
                warnings.append(f"exclude_mbids: ignoring {entry!r}, not an MBID")

        if errors:
            return ConfigError(tuple(errors))
        return cls(
            base_url=base_url,
            username=username,
            password=password,
            api_token=api_token,
            dry_run=text("dry_run").lower() not in _FALSE_WORDS,
            exclude_artist_ids=frozenset(_split_list(text("exclude_artist_ids"))),
            exclude_mbids=frozenset(exclude_mbids),
            warnings=tuple(warnings),
            **ints,
        )


def _split_list(value: str) -> list[str]:
    return [item.lower() for item in _LIST_SPLIT_RE.split(value) if item]
