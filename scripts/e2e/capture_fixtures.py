"""Re-capture the real API payloads in tests/fixtures/api-<version>/.

Run against a THROWAWAY server right after bootstrap.py: it performs a real,
irreversible artist merge (Otonose Kanade -> 音乃瀬奏) to record the merge and
post-merge responses, and creates a non-admin user to record 403s.

Usage:
    python scripts/e2e/capture_fixtures.py [out-dir]
Environment: DN_PORT (default 18688).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("DN_PORT", "18688"))
BASE = f"http://127.0.0.1:{PORT}/api/v1"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "tests" / "fixtures" / "api-captured"
ADMIN = {"username": "admin", "password": "MbidArtistMerge-e2e-7c41f9"}
LISTENER = {"username": "listener", "password": "MbidArtistMerge-e2e-listener-2b8d"}
REDACT = "<redacted-session-token>"


def save(name: str, response: httpx.Response, note: str = "") -> object:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict) and "token" in body:
        body = {**body, "token": REDACT}
    record: dict[str, object] = {
        "request": f"{response.request.method} {response.request.url.raw_path.decode()}",
        "status": response.status_code,
        "note": note,
        "body": body,
    }
    if response.status_code in (301, 302, 307, 308):
        record["location"] = response.headers.get("location")
    if response.request.content:
        try:
            record["request_body"] = json.loads(response.request.content)
        except ValueError:
            pass
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(f"{name}: {response.status_code}")
    return body


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=30) as anonymous:
        save(
            "unauthenticated",
            anonymous.get("/library/artists"),
            "no bearer token and no session cookie",
        )

    client = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
    login = client.post("/auth/login", json=ADMIN)
    save("auth_login", login, "token redacted; the real value is an opaque ~44-char string")
    save("auth_login_bad_password", client.post("/auth/login", json={**ADMIN, "password": "wrong"}))
    client.cookies.clear()  # login also sets a session cookie; use the header only
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    save("auth_me", client.get("/auth/me"))

    save("reconciliation_progress", client.get("/library/artists/reconciliation"))
    album_scope = save(
        "artists_scope_album",
        client.get(
            "/library/artists",
            params={"limit": 100, "offset": 0, "scope": "album", "sort_by": "name"},
        ),
    )
    save(
        "artists_scope_contributors",
        client.get(
            "/library/artists",
            params={"limit": 100, "offset": 0, "scope": "contributors", "sort_by": "name"},
        ),
    )
    save(
        "artists_scope_all_rejected",
        client.get("/library/artists", params={"limit": 100, "scope": "all"}),
        "the route normalizes unknown scopes (including 'all') to 'album'",
    )

    by_name = {a["name"]: a for a in album_scope["items"]}
    otonose, kanade, conflict = (
        by_name["Otonose Kanade"],
        by_name["音乃瀬奏"],
        by_name["Conflict Name"],
    )

    save("artist_detail_local_only", client.get(f"/library/artists/{otonose['id']}"))
    save("artist_detail_linked", client.get(f"/library/artists/{kanade['id']}"))
    albums = save("artist_albums", client.get(f"/library/artists/{otonose['id']}/albums"))
    appearances = save(
        "artist_appearances",
        client.get(
            f"/library/artists/{otonose['id']}/appearances", params={"limit": 100, "offset": 0}
        ),
    )
    tracks = save("album_tracks", client.get(f"/library/albums/{albums['items'][0]['id']}/tracks"))
    save("track_tags_romanized", client.get(f"/library/tracks/{tracks['items'][0]['id']}/tags"))
    duet = appearances["items"][0]["tracks"][0]
    save(
        "track_tags_collab_contributor",
        client.get(f"/library/tracks/{duet['id']}/tags"),
        "Otonose Kanade is credit position 1; MBIDs are positional",
    )
    conflict_albums = client.get(f"/library/artists/{conflict['id']}/albums").json()
    conflict_tracks = client.get(
        f"/library/albums/{conflict_albums['items'][0]['id']}/tracks"
    ).json()
    for index, track in enumerate(conflict_tracks["items"]):
        save(f"track_tags_conflict_{index + 1}", client.get(f"/library/tracks/{track['id']}/tags"))
    save(
        "track_tags_not_found",
        client.get("/library/tracks/00000000-0000-0000-0000-000000000000/tags"),
    )

    # Non-admin 403s.
    created = client.post(
        "/auth/admin/users", json={"display_name": "Listener", "role": "user", **LISTENER}
    )
    if created.status_code not in (201, 409, 400):
        created.raise_for_status()
    with httpx.Client(base_url=BASE, timeout=30) as listener:
        token = listener.post("/auth/login", json=LISTENER).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        save(
            "forbidden_non_admin_tags",
            listener.get(f"/library/tracks/{tracks['items'][0]['id']}/tags", headers=headers),
            "non-admin user",
        )
        save(
            "forbidden_non_admin_merge_preview",
            listener.post(
                "/library/artists/merge-preview",
                headers=headers,
                json={
                    "source_artist_ids": ["a"],
                    "surviving_artist_id": "b",
                    "expected_revisions": {"a": 1, "b": 1},
                },
            ),
            "non-admin user",
        )
        listener.post("/auth/logout", headers=headers)

    selection = {
        "source_artist_ids": [otonose["id"]],
        "surviving_artist_id": kanade["id"],
        "expected_revisions": {
            otonose["id"]: otonose["row_revision"],
            kanade["id"]: kanade["row_revision"],
        },
    }
    stale = {
        **selection,
        "expected_revisions": {otonose["id"]: 999, kanade["id"]: kanade["row_revision"]},
    }
    save("merge_preview_stale_revision", client.post("/library/artists/merge-preview", json=stale))
    preview = save("merge_preview", client.post("/library/artists/merge-preview", json=selection))
    save(
        "merge_apply_bad_token",
        client.post(
            "/library/artists/merge",
            json={
                **selection,
                "preview_token": "123.deadbeef",
                "idempotency_key": "capture-bad-token",
                "provider_choice": "retain_survivor",
            },
        ),
    )
    apply_body = {
        **selection,
        "preview_token": preview["preview_token"],
        "idempotency_key": "mbid-artist-merge:capture:1",
        "provider_choice": "retain_survivor",
    }
    save("merge_apply", client.post("/library/artists/merge", json=apply_body))
    save(
        "merge_apply_replay_same_key",
        client.post("/library/artists/merge", json=apply_body),
        "same idempotency_key replays the stored result",
    )
    save(
        "artist_detail_retired_redirect",
        client.get(f"/library/artists/{otonose['id']}"),
        "retired id answers 308 to the survivor",
    )
    save("artist_detail_survivor_after_merge", client.get(f"/library/artists/{kanade['id']}"))
    save(
        "artists_scope_album_after_merge",
        client.get("/library/artists", params={"limit": 100, "scope": "album"}),
    )
    save(
        "artists_scope_contributors_after_merge",
        client.get("/library/artists", params={"limit": 100, "scope": "contributors"}),
    )
    save("auth_logout", client.post("/auth/logout"))
    save("after_logout", client.get("/auth/me"), "token is revoked by logout")


if __name__ == "__main__":
    main()
