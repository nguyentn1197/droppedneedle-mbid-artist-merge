"""Read-only snapshot of everything the plugin reads, for the fake server in tests.

Run right after bootstrap.py (split state, before any merge):
    python scripts/e2e/snapshot_library.py [out-file]
Default out-file: tests/fixtures/api-v2.15.0/library_snapshot.json

The snapshot holds, keyed by the exact request path + query the plugin sends:
  artists?scope=album|contributors pages, every artist detail, /albums,
  /appearances, every album's /tracks and every track's /tags.
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
OUT = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else (REPO_ROOT / "tests" / "fixtures" / "api-v2.15.0" / "library_snapshot.json")
)


def main() -> None:
    client = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
    token = client.post(
        "/auth/login", json={"username": "admin", "password": "MbidArtistMerge-e2e-7c41f9"}
    ).json()["token"]
    client.cookies.clear()
    client.headers["Authorization"] = f"Bearer {token}"
    responses: dict[str, dict] = {}

    def get(path: str, params: dict | None = None) -> object:
        response = client.get(path, params=params)
        key = response.request.url.raw_path.decode().removeprefix("/api/v1")
        responses[key] = {"status": response.status_code, "body": response.json()}
        return response.json()

    get("/auth/me")
    get("/library/artists/reconciliation")
    artist_ids: list[str] = []
    for scope in ("album", "contributors"):
        page = get("/library/artists", {"limit": 100, "offset": 0, "scope": scope})
        artist_ids += [a["id"] for a in page["items"]]
    for artist_id in sorted(set(artist_ids)):
        get(f"/library/artists/{artist_id}")
        albums = get(f"/library/artists/{artist_id}/albums")
        appearances = get(f"/library/artists/{artist_id}/appearances", {"limit": 100, "offset": 0})
        album_ids = [a["id"] for a in albums["items"]] + [
            i["album"]["id"] for i in appearances["items"]
        ]
        for album_id in album_ids:
            tracks = get(f"/library/albums/{album_id}/tracks")
            for track in tracks["items"]:
                get(f"/library/tracks/{track['id']}/tags")
    client.post("/auth/logout")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "server": "DroppedNeedle v2.15.0",
                "state": "split (after bootstrap.py, before any merge)",
                "note": "keys are request paths relative to /api/v1, query string included",
                "responses": responses,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {len(responses)} responses to {OUT}")


if __name__ == "__main__":
    main()
