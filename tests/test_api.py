from __future__ import annotations

import httpx
import pytest
from fake_dn import ADMIN_PASSWORD, ADMIN_USER, ORIGIN, FakeDN
from fixture_data import KANADE_ID, OTONOSE_ID, record

from mbid_artist_merge.api import (
    Conflict,
    DroppedNeedleApi,
    NotFound,
    Retired,
    Unauthorized,
    Unreachable,
)


async def no_sleep(seconds: float) -> None:
    return None


async def logged_in(fake: FakeDN, *, follow_redirects: bool = True) -> DroppedNeedleApi:
    api = DroppedNeedleApi(fake.client(follow_redirects=follow_redirects), ORIGIN, sleep=no_sleep)
    await api.login(ADMIN_USER, ADMIN_PASSWORD)
    return api


@pytest.mark.parametrize("follow", [True, False])
async def test_retired_artist_detected_with_or_without_redirect_following(follow: bool) -> None:
    fake = FakeDN()
    fake.retired[OTONOSE_ID] = KANADE_ID
    api = await logged_in(fake, follow_redirects=follow)
    with pytest.raises(Retired) as info:
        await api.artist(OTONOSE_ID)
    assert info.value.survivor_id == KANADE_ID


async def test_artist_detail() -> None:
    api = await logged_in(FakeDN())
    artist = await api.artist(KANADE_ID)
    assert artist.is_linked
    assert artist.row_revision == 1


async def test_error_envelope_is_parsed() -> None:
    api = await logged_in(FakeDN())
    with pytest.raises(NotFound) as info:
        await api.track_tags("00000000-0000-0000-0000-000000000000")
    assert info.value.code == "NOT_FOUND"
    assert "not found" in info.value.message


async def test_stale_revision_is_a_conflict() -> None:
    api = await logged_in(FakeDN())
    with pytest.raises(Conflict) as info:
        await api.merge_preview(record("merge_preview_stale_revision")["request_body"])
    assert info.value.code == "STALE_REVISION"


async def test_calls_without_a_token_fail_fast() -> None:
    fake = FakeDN()
    api = DroppedNeedleApi(fake.client(), ORIGIN, sleep=no_sleep)
    with pytest.raises(Unauthorized):
        await api.me()
    assert fake.calls == []


async def test_logout_clears_the_token_even_on_failure() -> None:
    fake = FakeDN()
    api = await logged_in(fake)
    fake.fail("POST", "/auth/logout", times=2, kind=503)
    with pytest.raises(Unreachable):
        await api.logout()
    assert api.token is None


async def test_ids_are_path_escaped() -> None:
    fake = FakeDN()
    api = await logged_in(fake)
    with pytest.raises(NotFound):
        await api.album_tracks("../../auth/me")
    assert fake.calls[-1].raw_path == "/api/v1/library/albums/..%2F..%2Fauth%2Fme/tracks"


async def test_retry_waits_two_seconds() -> None:
    fake = FakeDN()
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    api = DroppedNeedleApi(fake.client(), ORIGIN, sleep=record)
    fake.fail("POST", "/auth/login", kind="connect")
    await api.login(ADMIN_USER, ADMIN_PASSWORD)
    assert sleeps == [2.0]


async def test_non_json_success_is_an_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy page</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = DroppedNeedleApi(client, ORIGIN, sleep=no_sleep)
    api.token = "t"
    with pytest.raises(Exception) as info:
        await api.me()
    assert getattr(info.value, "code", None) == "BAD_JSON"
