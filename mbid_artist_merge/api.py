"""Typed async calls to DroppedNeedle's public API (docs/API-CONTRACT.md).

The HTTP client is injected (the host's ``context.http``). Only ``get``/``post``
with ``params``/``json``/``headers`` and ``status_code``/``headers``/``json()`` are
used, which keeps this portable to v3's ``ctx.http``. Every authenticated call
sends the bearer header; the session cookie login leaves on the shared client is
never relied on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from .models import Artist

API_PREFIX = "/api/v1"
PAGE_LIMIT = 100
RETRY_DELAY_S = 2.0
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any: ...


class HttpClient(Protocol):
    def get(self, url: str, **kwargs: Any) -> Awaitable[HttpResponse]: ...
    def post(self, url: str, **kwargs: Any) -> Awaitable[HttpResponse]: ...


class ApiError(Exception):
    def __init__(
        self,
        method: str,
        path: str,
        status: int | None,
        code: str | None = None,
        message: str = "",
    ) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.code = code
        self.message = message
        parts = [f"{method} {path}", f"status={status}"]
        if code:
            parts.append(f"code={code}")
        if message:
            parts.append(message)
        super().__init__(" ".join(parts))


class BadRequest(ApiError):
    """400."""


class Unauthorized(ApiError):
    """401: no, invalid, expired or revoked token (or a rejected login)."""


class Forbidden(ApiError):
    """403: the account is not an admin."""


class NotFound(ApiError):
    """404."""


class Conflict(ApiError):
    """409: ``STALE_REVISION`` (revisions moved, preview token invalid) or ``CONFLICT``."""


class Unreachable(ApiError):
    """Transport error or 5xx, still failing after one retry."""


class Retired(ApiError):
    """The requested artist id was merged away; it now redirects to ``survivor_id``."""

    def __init__(self, method: str, path: str, survivor_id: str | None) -> None:
        super().__init__(method, path, 308, "RETIRED", f"redirects to {survivor_id}")
        self.survivor_id = survivor_id


_ERRORS: dict[int, type[ApiError]] = {
    400: BadRequest,
    401: Unauthorized,
    403: Forbidden,
    404: NotFound,
    409: Conflict,
}


class DroppedNeedleApi:
    def __init__(
        self,
        http: HttpClient,
        base_url: str,
        *,
        delay_s: float = 0.0,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        retry_delay_s: float = RETRY_DELAY_S,
        logger: logging.Logger | None = None,
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._delay_s = delay_s
        self._sleep = sleep
        self._retry_delay_s = retry_delay_s
        self._log = logger or logging.getLogger(__name__)
        self._calls = 0
        self.token: str | None = None

    # --- auth ---------------------------------------------------------------

    async def login(self, username: str, password: str) -> None:
        body = await self._json(
            "POST", "/auth/login", json={"username": username, "password": password}, auth=False
        )
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise ApiError("POST", "/auth/login", 200, "NO_TOKEN", "login returned no token")
        self.token = token

    async def logout(self) -> None:
        try:
            await self._call("POST", "/auth/logout")
        finally:
            self.token = None

    async def me(self) -> dict[str, Any]:
        return await self._json("GET", "/auth/me")

    # --- catalog ------------------------------------------------------------

    async def reconciliation(self) -> dict[str, Any]:
        return await self._json("GET", "/library/artists/reconciliation")

    async def artists_page(self, scope: str, offset: int) -> dict[str, Any]:
        params = {"limit": PAGE_LIMIT, "offset": offset, "scope": scope}
        return await self._json("GET", "/library/artists", params=params)

    async def artist(self, artist_id: str) -> Artist:
        """Fresh artist detail. Raises :class:`Retired` when the id was merged away,
        whether or not the client followed the 308."""
        path = f"/library/artists/{_seg(artist_id)}"
        response = await self._call("GET", path, allow_redirect=True)
        if response.status_code in _REDIRECTS:
            location = response.headers.get("location") or response.headers.get("Location") or ""
            survivor = urlsplit(location).path.rstrip("/").rsplit("/", 1)[-1] or None
            raise Retired("GET", path, survivor)
        artist = Artist.from_api(_body(response, "GET", path))
        if artist.id != artist_id:
            raise Retired("GET", path, artist.id)
        return artist

    async def artist_albums(self, artist_id: str) -> list[dict[str, Any]]:
        body = await self._json("GET", f"/library/artists/{_seg(artist_id)}/albums")
        return _items(body)

    async def artist_appearances(self, artist_id: str) -> list[dict[str, Any]]:
        path = f"/library/artists/{_seg(artist_id)}/appearances"
        body = await self._json("GET", path, params={"limit": PAGE_LIMIT, "offset": 0})
        return _items(body)

    async def album_tracks(self, album_id: str) -> list[dict[str, Any]]:
        return _items(await self._json("GET", f"/library/albums/{_seg(album_id)}/tracks"))

    async def track_tags(self, track_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/library/tracks/{_seg(track_id)}/tags")

    async def merge_preview(self, selection: Mapping[str, Any]) -> dict[str, Any]:
        return await self._json("POST", "/library/artists/merge-preview", json=dict(selection))

    async def merge(
        self, selection: Mapping[str, Any], *, preview_token: str, idempotency_key: str
    ) -> dict[str, Any]:
        """IRREVERSIBLE. Same selection values as the preview. ``provider_choice`` is
        fixed here and nowhere else: the API default (``detach``) deletes the
        survivor's MusicBrainz link."""
        request = {
            **selection,
            "preview_token": preview_token,
            "idempotency_key": idempotency_key,
            "provider_choice": "retain_survivor",
        }
        return await self._json("POST", "/library/artists/merge", json=request)

    # --- transport ----------------------------------------------------------

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._call(method, path, **kwargs)
        return _body(response, method, path)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        auth: bool = True,
        allow_redirect: bool = False,
    ) -> HttpResponse:
        response = await self._send(method, path, params=params, json=json, auth=auth)
        status = response.status_code
        if 200 <= status < 300 or (allow_redirect and status in _REDIRECTS):
            return response
        code, message = _error_of(response)
        raise _ERRORS.get(status, ApiError)(method, path, status, code, message)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        json: Any,
        auth: bool,
    ) -> HttpResponse:
        url = f"{self._base}{API_PREFIX}{path}"
        headers = {"Accept": "application/json"}
        if auth:
            if not self.token:
                raise Unauthorized(method, path, None, "NO_TOKEN", "not logged in")
            headers["Authorization"] = f"Bearer {self.token}"
        failure: Unreachable | None = None
        for attempt in (1, 2):
            if attempt == 2:
                self._log.warning(
                    "mbid-artist-merge retry method=%s path=%s reason=%s", method, path, failure
                )
                await self._sleep(self._retry_delay_s)
            await self._pace()
            try:
                if method == "GET":
                    response = await self._http.get(url, params=params, headers=headers)
                else:
                    response = await self._http.post(url, json=json, headers=headers)
            except Exception as exc:  # noqa: BLE001 - the injected client's transport errors
                failure = Unreachable(method, path, None, type(exc).__name__, str(exc))
                continue
            if response.status_code < 500:
                return response
            failure = Unreachable(method, path, response.status_code, *_error_of(response))
        assert failure is not None
        raise failure

    async def _pace(self) -> None:
        if self._calls and self._delay_s > 0:
            await self._sleep(self._delay_s)
        self._calls += 1


def _seg(value: str) -> str:
    return quote(value, safe="")


def _body(response: HttpResponse, method: str, path: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ApiError(method, path, response.status_code, "BAD_JSON", str(exc)) from exc


def _items(body: Any) -> list[dict[str, Any]]:
    items = body.get("items") if isinstance(body, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _error_of(response: HttpResponse) -> tuple[str | None, str]:
    """``(code, message)`` from DroppedNeedle's error envelope, if there is one."""
    try:
        error = response.json().get("error")
    except Exception:  # noqa: BLE001 - empty or non-JSON error bodies are normal
        return None, ""
    if not isinstance(error, dict):
        return None, ""
    code = error.get("code")
    message = error.get("message")
    return (code if isinstance(code, str) else None, message if isinstance(message, str) else "")
