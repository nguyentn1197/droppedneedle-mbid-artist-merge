from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mbid_artist_merge.config import (
    DEFAULT_BASE_URL,
    INTERVAL_MINUTES,
    ConfigError,
    Settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PASSWORD = {"username": "admin", "password": "Passw0rd!Passw0rd"}
GOOD_MBID = "11111111-1111-4111-8111-111111111111"


def parse_ok(**raw: str) -> Settings:
    result = Settings.parse({**PASSWORD, **raw})
    assert isinstance(result, Settings), result
    return result


def parse_err(**raw: str) -> ConfigError:
    result = Settings.parse({**PASSWORD, **raw})
    assert isinstance(result, ConfigError), result
    return result


def test_defaults() -> None:
    s = parse_ok()
    assert s.base_url == DEFAULT_BASE_URL == "http://127.0.0.1:8688"
    assert s.dry_run is True
    assert s.max_merges_per_run == 10
    assert s.max_artists_per_run == 200
    assert s.samples_per_artist == 5
    assert s.min_agreeing_samples == 2
    assert s.request_delay_ms == 100
    assert s.time_budget_seconds == 900
    assert s.exclude_artist_ids == frozenset()
    assert s.exclude_mbids == frozenset()
    assert s.warnings == ()
    assert s.auth_mode == "password"


def test_empty_and_blank_values_mean_default() -> None:
    s = parse_ok(max_merges_per_run="", samples_per_artist="   ", dry_run="", base_url=" ")
    assert s.max_merges_per_run == 10
    assert s.samples_per_artist == 5
    assert s.dry_run is True
    assert s.base_url == DEFAULT_BASE_URL


def test_unknown_keys_are_ignored() -> None:
    assert isinstance(Settings.parse({**PASSWORD, "something_else": "x"}), Settings)


@pytest.mark.parametrize("value", ["false", "FALSE", " False ", "0", "no", "No", "off", "OFF"])
def test_dry_run_off_values(value: str) -> None:
    assert parse_ok(dry_run=value).dry_run is False


@pytest.mark.parametrize(
    "value", ["true", "1", "yes", "on", "", "flase", "nope", "2", "-", "disabled", "f", "n"]
)
def test_dry_run_anything_else_is_true(value: str) -> None:
    assert parse_ok(dry_run=value).dry_run is True


def test_base_url_strips_trailing_slash_and_whitespace() -> None:
    assert parse_ok(base_url=" http://127.0.0.1:8688/music/ ").base_url == (
        "http://127.0.0.1:8688/music"
    )
    assert parse_ok(base_url="https://dn.example").base_url == "https://dn.example"


@pytest.mark.parametrize("value", ["127.0.0.1:8688", "ftp://x", "http://", "https:///path"])
def test_base_url_must_be_http(value: str) -> None:
    err = parse_err(base_url=value)
    assert any("base_url" in e for e in err.errors)


@pytest.mark.parametrize(
    ("key", "low", "high"),
    [
        ("max_merges_per_run", 0, 500),
        ("max_artists_per_run", 1, 10000),
        ("samples_per_artist", 1, 20),
        ("min_agreeing_samples", 1, 20),
        ("request_delay_ms", 0, 5000),
        ("time_budget_seconds", 30, INTERVAL_MINUTES * 60 - 60),
    ],
)
def test_int_bounds(key: str, low: int, high: int) -> None:
    assert getattr(parse_ok(**{key: str(low)}), key) == low
    assert getattr(parse_ok(**{key: str(high)}), key) == high
    for bad in (str(low - 1), str(high + 1)):
        err = parse_err(**{key: bad})
        assert any(key in e for e in err.errors), err


@pytest.mark.parametrize("value", ["abc", "1.5", "1e3", "1_000", "١٠", "ten"])
def test_invalid_numbers_are_errors_not_clamped(value: str) -> None:
    err = parse_err(max_artists_per_run=value)
    assert any("max_artists_per_run" in e for e in err.errors)


def test_all_errors_are_reported_together() -> None:
    err = parse_err(samples_per_artist="0", request_delay_ms="x")
    assert len(err.errors) == 2


def test_max_merges_zero_is_allowed() -> None:
    assert parse_ok(max_merges_per_run="0", dry_run="false").max_merges_per_run == 0


def test_time_budget_upper_bound_follows_interval() -> None:
    s = Settings.parse({**PASSWORD, "time_budget_seconds": "540"}, interval_minutes=10)
    assert isinstance(s, Settings)
    assert isinstance(
        Settings.parse({**PASSWORD, "time_budget_seconds": "541"}, interval_minutes=10),
        ConfigError,
    )


def test_interval_matches_manifest() -> None:
    manifest = tomllib.loads((REPO_ROOT / "plugin.toml").read_text(encoding="utf-8"))
    assert manifest["schedule"]["interval_minutes"] == INTERVAL_MINUTES


def test_token_wins_over_password() -> None:
    s = parse_ok(api_token="  tok  ")
    assert s.auth_mode == "token"
    assert s.api_token == "tok"


def test_token_alone_is_enough() -> None:
    s = Settings.parse({"api_token": "tok"})
    assert isinstance(s, Settings)
    assert s.auth_mode == "token"


@pytest.mark.parametrize(
    "raw",
    [{}, {"username": "admin"}, {"password": "pw"}, {"username": " ", "password": "pw"}],
)
def test_credentials_required(raw: dict[str, str]) -> None:
    err = Settings.parse(raw)
    assert isinstance(err, ConfigError)
    assert any("api_token" in e for e in err.errors)


def test_password_is_not_trimmed_but_username_is() -> None:
    s = Settings.parse({"username": " admin ", "password": " pw "})
    assert isinstance(s, Settings)
    assert s.username == "admin"
    assert s.password == " pw "


def test_secrets_never_in_repr() -> None:
    s = parse_ok(api_token="supersecrettoken")
    assert "supersecrettoken" not in repr(s)
    assert PASSWORD["password"] not in repr(s)


def test_exclude_artist_ids_parsing() -> None:
    s = parse_ok(exclude_artist_ids=" a8a4098f-D8FD-5921-82d5-87baa6aefb57, b ,,c\nd e ")
    assert s.exclude_artist_ids == frozenset(
        {"a8a4098f-d8fd-5921-82d5-87baa6aefb57", "b", "c", "d", "e"}
    )


def test_exclude_mbids_parsing_lowercases_and_warns_on_garbage() -> None:
    s = parse_ok(exclude_mbids=f"{GOOD_MBID.upper()}, not-a-uuid ,")
    assert s.exclude_mbids == frozenset({GOOD_MBID})
    assert len(s.warnings) == 1
    assert "not-a-uuid" in s.warnings[0]
