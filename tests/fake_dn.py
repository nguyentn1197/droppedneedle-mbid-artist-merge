"""Stateful fake DroppedNeedle v2.15.0 behind ``httpx.MockTransport`` (PLAN §11.2).

Seeded from the captured ``library_snapshot.json`` (every GET the plugin makes in
the split state) and the individual fixtures for auth, errors and merge shapes.
Static GETs match on path + sorted query. Artists, auth tokens, merge previews,
merges, idempotency keys and retired-id redirects are simulated. Only the bearer
header authenticates; the login cookie is set but ignored, like a client that
must not rely on it.
"""

from __future__ import annotations

import copy
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from fixture_data import body, snapshot

ORIGIN = "http://dn.test"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "Passw0rd!Passw0rd"
LISTENER_USER = "listener"
LISTENER_PASSWORD = "listener-pass-123"

_ARTIST_KEYS = ("musicbrainz_artist_id", "artist_identity_state", "row_revision", "name")


def norm_key(path_and_query: str) -> str:
    parts = urlsplit(path_and_query)
    query = sorted(parse_qsl(parts.query, keep_blank_values=True))
    return f"{parts.path}?{urlencode(query)}" if query else parts.path


def error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": code, "message": message, "details": None}}
    )


@dataclass
class Call:
    method: str
    path: str  # relative to /api/v1, no query
    query: dict[str, str]
    json: Any
    authorization: str | None
    raw_path: str = ""

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


@dataclass
class Fault:
    method: str
    path: str  # exact relative path, or a prefix when it ends with "*"
    times: int
    kind: str | int  # "connect", "timeout" or an HTTP status
    message: str = "injected"

    def matches(self, call: Call) -> bool:
        if self.method != call.method:
            return False
        if self.path.endswith("*"):
            return call.path.startswith(self.path[:-1])
        return call.path == self.path


@dataclass
class FakeDN:
    origin: str = ORIGIN
    base_path: str = ""
    calls: list[Call] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    hooks: list[tuple[str, str, Callable[[FakeDN, Call], None]]] = field(default_factory=list)
    on_request: Callable[[Call], None] | None = None
    reconciliation_state: str = "succeeded"
    extra_identity_conflicts: list[str] = field(default_factory=list)
    merge_result_patch: dict[str, Any] = field(default_factory=dict)  # corrupt replies
    catalog_revision: int = 9

    def __post_init__(self) -> None:
        snap = snapshot()
        self.static: dict[str, dict[str, Any]] = {
            norm_key(k): copy.deepcopy(v) for k, v in snap.items()
        }
        self.users = {
            ADMIN_USER: {"password": ADMIN_PASSWORD, "role": "admin"},
            LISTENER_USER: {"password": LISTENER_PASSWORD, "role": "user"},
        }
        self.tokens: dict[str, str] = {}
        self.state: dict[str, dict[str, Any]] = {}
        self.scope_items: dict[str, dict[str, dict[str, Any]]] = {"album": {}, "contributors": {}}
        for scope in self.scope_items:
            page = snap[f"/library/artists?limit=100&offset=0&scope={scope}"]["body"]
            for item in page["items"]:
                self.scope_items[scope][item["id"]] = copy.deepcopy(item)
                self.state.setdefault(item["id"], {k: item[k] for k in _ARTIST_KEYS})
        self.details: dict[str, dict[str, Any]] = {}
        for artist_id in self.state:
            entry = snap.get(f"/library/artists/{artist_id}")
            self.details[artist_id] = copy.deepcopy(entry["body"])
        self.retired: dict[str, str] = {}
        self.previews: dict[str, str] = {}
        self.idempotency: dict[str, dict[str, Any]] = {}
        self.merges: list[dict[str, Any]] = []

    # --- test helpers -------------------------------------------------------

    def client(self, *, follow_redirects: bool = True) -> httpx.AsyncClient:
        """A client shaped like the host's shared plugin client."""
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self), follow_redirects=follow_redirects, timeout=30
        )

    def issue_token(self, username: str = ADMIN_USER) -> str:
        token = secrets.token_urlsafe(32)
        self.tokens[token] = username
        return token

    def fail(self, method: str, path: str, *, times: int = 1, kind: str | int = "connect") -> None:
        self.faults.append(Fault(method, path, times, kind))

    def before(self, method: str, path: str, hook: Callable[[FakeDN, Call], None]) -> None:
        self.hooks.append((method, path, hook))

    def set_artist(self, artist_id: str, **fields: Any) -> None:
        self.state[artist_id].update(fields)

    def calls_to(self, method: str, path: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and c.path == path]

    def add_artist(
        self,
        artist_id: str,
        name: str,
        *,
        mbid: str | None = None,
        tracks: int = 0,
        tag_mbid: str | None = None,
    ) -> list[str]:
        """A new album artist with one album of ``tracks`` tagged files. Returns track ids."""
        linked = mbid is not None
        item = copy.deepcopy(next(iter(self.scope_items["album"].values())))
        item.update(
            id=artist_id,
            name=name,
            musicbrainz_artist_id=mbid,
            artist_identity_state="musicbrainz_linked" if linked else "local_only",
            row_revision=1,
        )
        self.scope_items["album"][artist_id] = item
        self.state[artist_id] = {k: item[k] for k in _ARTIST_KEYS}
        self.details[artist_id] = copy.deepcopy(item)
        album_id = f"album-{artist_id}"
        albums = {"items": [{"id": album_id, "title": f"{name} album"}] if tracks else []}
        albums["total"] = len(albums["items"])
        self.static[f"/library/artists/{artist_id}/albums"] = {"status": 200, "body": albums}
        self.static[norm_key(f"/library/artists/{artist_id}/appearances?limit=100&offset=0")] = {
            "status": 200,
            "body": {"items": [], "total": 0, "total_tracks": 0, "offset": 0, "limit": 100},
        }
        track_ids = [f"track-{artist_id}-{n}" for n in range(1, tracks + 1)]
        self.static[f"/library/albums/{album_id}/tracks"] = {
            "status": 200,
            "body": {
                "items": [{"id": t, "album_id": album_id} for t in track_ids],
                "total": tracks,
                "offset": 0,
                "limit": tracks,
            },
        }
        for track_id in track_ids:
            tag = body("track_tags_romanized")
            for pool in ("artists", "album_artists"):
                for credit in tag[pool]:
                    credit.update(name=name, credited_name=name, musicbrainz_artist_id=tag_mbid)
            ids = [tag_mbid] if tag_mbid else []
            tag.update(
                artist=name,
                album_artist=name,
                musicbrainz_artist_id=tag_mbid,
                musicbrainz_album_artist_id=tag_mbid,
                musicbrainz_artist_ids=ids,
                musicbrainz_album_artist_ids=list(ids),
            )
            self.static[f"/library/tracks/{track_id}/tags"] = {"status": 200, "body": tag}
        return track_ids

    # --- transport ----------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        origin = f"{url.scheme}://{url.netloc.decode('ascii')}"
        assert origin == self.origin, f"request outside base_url: {url}"
        prefix = f"{self.base_path}/api/v1"
        assert url.path.startswith(f"{prefix}/"), f"request outside the API: {url}"
        call = Call(
            method=request.method,
            path=url.path[len(prefix) :],
            query=dict(url.params),
            json=json.loads(request.content) if request.content else None,
            authorization=request.headers.get("authorization"),
            raw_path=url.raw_path.decode("ascii"),
        )
        self.calls.append(call)
        if self.on_request is not None:
            self.on_request(call)
        for method, path, hook in list(self.hooks):
            if method == call.method and (path == call.path or path == "*"):
                hook(self, call)
        for fault in self.faults:
            if fault.times > 0 and fault.matches(call):
                fault.times -= 1
                if fault.kind == "connect":
                    raise httpx.ConnectError("connection refused (injected)", request=request)
                if fault.kind == "timeout":
                    raise httpx.ReadTimeout("timed out (injected)", request=request)
                assert isinstance(fault.kind, int)
                return error(fault.kind, "INJECTED", fault.message)
        return self._route(call)

    def _route(self, call: Call) -> httpx.Response:
        if call.key == "POST /auth/login":
            return self._login(call)
        user = self._user(call)
        if user is None:
            return error(401, "UNAUTHORIZED", "Not authenticated")
        if call.key == "POST /auth/logout":
            token = (call.authorization or "").removeprefix("Bearer ")
            self.tokens.pop(token, None)
            return httpx.Response(204)
        if call.key == "GET /auth/me":
            me = body("auth_me")
            me.update(username=user, role=self.users[user]["role"])
            return httpx.Response(200, json=me)
        admin_only = (
            call.path == "/library/artists/reconciliation"
            or call.path.endswith("/tags")
            or call.method == "POST"
        )
        if admin_only and self.users[user]["role"] != "admin":
            return error(403, "FORBIDDEN", "Admin access required")
        if call.key == "GET /library/artists/reconciliation":
            progress = body("reconciliation_progress")
            progress["state"] = self.reconciliation_state
            return httpx.Response(200, json=progress)
        if call.key == "GET /library/artists":
            return self._list(call)
        if call.key == "POST /library/artists/merge-preview":
            return self._preview(call.json)
        if call.key == "POST /library/artists/merge":
            return self._merge(call.json)
        if call.method == "GET" and call.path.startswith("/library/artists/"):
            artist_id, _, rest = call.path.removeprefix("/library/artists/").partition("/")
            if artist_id in self.retired:
                location = f"{self.base_path}/api/v1/library/artists/{self.retired[artist_id]}"
                return httpx.Response(
                    308, headers={"location": location + (f"/{rest}" if rest else "")}
                )
            if not rest:
                return self._detail(artist_id)
        return self._static(call)

    def _user(self, call: Call) -> str | None:
        auth = call.authorization or ""
        if not auth.startswith("Bearer "):
            return None
        return self.tokens.get(auth.removeprefix("Bearer "))

    def _login(self, call: Call) -> httpx.Response:
        creds = call.json or {}
        user = self.users.get(creds.get("username"))
        if user is None or user["password"] != creds.get("password"):
            return error(401, "INTERNAL_ERROR", "Invalid username or password")
        token = self.issue_token(creds["username"])
        reply = body("auth_login")
        reply["token"] = token
        reply["user"].update(username=creds["username"], role=user["role"])
        return httpx.Response(
            200,
            json=reply,
            headers={"set-cookie": f"droppedneedle_session={token}; Path=/; HttpOnly"},
        )

    def _artist_view(self, base: dict[str, Any], artist_id: str) -> dict[str, Any]:
        view = copy.deepcopy(base)
        view.update(self.state[artist_id])
        view["id"] = artist_id
        return view

    def _list(self, call: Call) -> httpx.Response:
        scope = call.query.get("scope", "album")
        scope = scope if scope in self.scope_items else "album"
        limit = max(1, min(100, int(call.query.get("limit", 50))))
        offset = max(0, int(call.query.get("offset", 0)))
        items = [
            self._artist_view(item, artist_id)
            for artist_id, item in self.scope_items[scope].items()
            if artist_id not in self.retired
        ]
        items.sort(key=lambda a: (a["name"].casefold(), a["id"]))
        return httpx.Response(
            200,
            json={
                "items": items[offset : offset + limit],
                "total": len(items),
                "album_artist_total": len(self.scope_items["album"]),
                "contributor_total": len(self.scope_items["contributors"]),
            },
        )

    def _detail(self, artist_id: str) -> httpx.Response:
        if artist_id not in self.state:
            return error(404, "NOT_FOUND", f"Artist {artist_id} not found")
        return httpx.Response(200, json=self._artist_view(self.details[artist_id], artist_id))

    def _static(self, call: Call) -> httpx.Response:
        key = norm_key(f"{call.path}?{urlencode(call.query)}")
        entry = self.static.get(key)
        if entry is None:
            if call.path.endswith("/tags"):
                return httpx.Response(404, json=body("track_tags_not_found"))
            return error(404, "NOT_FOUND", f"{call.path} not found")
        return httpx.Response(entry["status"], json=copy.deepcopy(entry["body"]))

    # --- merges -------------------------------------------------------------

    @staticmethod
    def _selection(data: dict[str, Any]) -> str:
        keys = ("source_artist_ids", "surviving_artist_id", "expected_revisions")
        return json.dumps({k: data.get(k) for k in keys}, sort_keys=True)

    def _check_revisions(self, data: dict[str, Any], message: str) -> httpx.Response | None:
        sources = data.get("source_artist_ids") or []
        if not sources:
            return error(400, "VALIDATION_ERROR", "source_artist_ids must not be empty")
        expected = data.get("expected_revisions") or {}
        for artist_id in [*sources, data.get("surviving_artist_id")]:
            if artist_id in self.retired or artist_id not in self.state:
                return error(409, "STALE_REVISION", message)
            if expected.get(artist_id) != self.state[artist_id]["row_revision"]:
                return error(409, "STALE_REVISION", message)
        return None

    def _preview(self, data: dict[str, Any]) -> httpx.Response:
        problem = self._check_revisions(data, "Artist references changed before preview.")
        if problem is not None:
            return problem
        sources = data["source_artist_ids"]
        named = [*sources, data["surviving_artist_id"]]
        mbids = {self.state[a]["musicbrainz_artist_id"] for a in named} - {None}
        token = f"1791521742.{secrets.token_hex(32)}"
        self.previews[token] = self._selection(data)
        reply = body("merge_preview")
        reply.update(
            preview_token=token,
            target_album_id=data["surviving_artist_id"],
            aliases=list(sources),
            identity_conflicts=(sorted(mbids) if len(mbids) > 1 else [])
            + list(self.extra_identity_conflicts),
        )
        return httpx.Response(200, json=reply)

    def _merge(self, data: dict[str, Any]) -> httpx.Response:
        key = data.get("idempotency_key")
        if key in self.idempotency:
            return httpx.Response(200, json=copy.deepcopy(self.idempotency[key]))
        bound = self.previews.get(data.get("preview_token") or "")
        if bound is None:
            return error(
                409, "STALE_REVISION", "The catalog preview expired. Preview the change again."
            )
        if bound != self._selection(data):
            return error(409, "STALE_REVISION", "The catalog selection changed after preview.")
        problem = self._check_revisions(data, "Artist references changed before merge.")
        if problem is not None:
            return problem
        survivor = data["surviving_artist_id"]
        sources = list(data["source_artist_ids"])
        for source in sources:
            self.retired[source] = survivor
            for items in self.scope_items.values():
                item = items.pop(source, None)
                if item is not None and survivor not in items:
                    items[survivor] = item
        if data.get("provider_choice") != "retain_survivor":
            # the API default ("detach") drops the survivor's MusicBrainz link
            self.state[survivor].update(
                musicbrainz_artist_id=None, artist_identity_state="local_only"
            )
        self.catalog_revision += 1
        result = body("merge_apply")
        result.update(
            surviving_artist_id=survivor,
            retired_artist_ids=sources,
            catalog_revision=self.catalog_revision,
        )
        result.update(self.merge_result_patch)
        self.merges.append(copy.deepcopy(data))
        if key:
            self.idempotency[key] = copy.deepcopy(result)
        return httpx.Response(200, json=result)
