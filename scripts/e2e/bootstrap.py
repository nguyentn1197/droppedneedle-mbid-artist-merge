"""Bring a fresh test server to the "split artist" state the plugin must fix.

Usage (after scripts/e2e/run_server.sh and make_library.py):
    python scripts/e2e/bootstrap.py

Steps, all through the public API except step 5:
 1. create the first admin (admin / Passw0rd!Passw0rd) if setup is still required
 2. replace the default /music library root with $DN_ROOT/music, policy
    "local_metadata" (scan only: no MusicBrainz lookups, no file moves)
 3. run the policy_reconcile scan the settings change asks for
 4. wait until the six test album artists are listed
 5. TEST-ONLY: link the "音乃瀬奏" local artist to the fake MBID straight in
    SQLite and bump the catalog revision. A real server gets this link from
    MusicBrainz identification, which needs network access the test cannot rely
    on. The plugin itself never touches SQLite.

Environment: DN_ROOT (default .cache/dnroot), DN_PORT (default 18688).
Needs httpx (the server venv has it).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
DN_ROOT = Path(os.environ.get("DN_ROOT", REPO_ROOT / ".cache" / "dnroot")).resolve()
PORT = int(os.environ.get("DN_PORT", "18688"))
BASE = f"http://127.0.0.1:{PORT}/api/v1"
ADMIN = {"username": "admin", "password": "Passw0rd!Passw0rd"}
M_KANADE = "11111111-1111-4111-8111-111111111111"
EXPECTED_ARTISTS = {
    "音乃瀬奏",
    "Otonose Kanade",
    "Other Artist",
    "Some Band",
    "Conflict Name",
    "Lonely Alias",
}


def fail(message: str) -> None:
    print(f"bootstrap: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    music = DN_ROOT / "music"
    if not any(music.rglob("*.flac")):
        fail(f"no FLAC files under {music}; run make_library.py first")
    client = httpx.Client(base_url=BASE, timeout=30)

    if client.get("/auth/setup/status").json().get("required"):
        client.post("/auth/setup", json={"display_name": "Admin", **ADMIN}).raise_for_status()
    login = client.post("/auth/login", json=ADMIN)
    login.raise_for_status()
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"

    current = client.get("/settings/library").json()
    roots = current["library_roots"]
    if not any(root["path"] == str(music) for root in roots):
        settings = {
            "library_roots": [
                {
                    "id": str(uuid.uuid4()),
                    "path": str(music),
                    "label": "testlib",
                    "policy": "local_metadata",
                    "rules": [],
                }
            ],
            "staging_path": current.get("staging_path", ""),
            "naming_template": current.get("naming_template", ""),
            "acoustid_api_key": current.get("acoustid_api_key", ""),
            "enabled": True,
        }
        saved = client.put(
            "/settings/library",
            json={"settings": settings, "expected_policy_revision": current["policy_revision"]},
        )
        if saved.status_code != 200:
            fail(f"saving library roots failed: {saved.status_code} {saved.text}")
        revision = saved.json()["policy_revision"]
        scan = client.post(
            "/library/scan-runs",
            json={"kind": "policy_reconcile", "expected_policy_revision": revision},
        )
        if scan.status_code != 202:
            fail(f"scan request failed: {scan.status_code} {scan.text}")

    deadline = time.time() + 120
    names: set[str] = set()
    while time.time() < deadline:
        page = client.get("/library/artists", params={"limit": 100, "scope": "album"}).json()
        names = {artist["name"] for artist in page["items"]}
        if EXPECTED_ARTISTS <= names:
            break
        time.sleep(2)
    else:
        fail(f"scan did not produce the expected artists; saw {sorted(names)}")

    db = sqlite3.connect(DN_ROOT / "cache" / "library.db", timeout=30)
    with db:
        row = db.execute(
            "SELECT id FROM local_artists WHERE display_name = ? AND retired_into_artist_id IS NULL",
            ("音乃瀬奏",),
        ).fetchone()
        if row is None:
            fail("local artist 音乃瀬奏 not found in the database")
        db.execute(
            "INSERT OR IGNORE INTO local_artist_external_identities "
            "(local_artist_id, provider, provider_artist_id, decision_source, selected_at) "
            "VALUES (?, 'musicbrainz', ?, 'manual', ?)",
            (row[0], M_KANADE, time.time()),
        )
        db.execute("UPDATE library_catalog_revision SET value = value + 1 WHERE singleton = 1")
    db.close()

    artists = client.get("/library/artists", params={"limit": 100, "scope": "album"}).json()[
        "items"
    ]
    summary = {a["name"]: a["artist_identity_state"] for a in artists}
    client.post("/auth/logout")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if (
        summary.get("音乃瀬奏") != "musicbrainz_linked"
        or summary.get("Otonose Kanade") != "local_only"
    ):
        fail("bootstrap did not reach the split state")
    print("bootstrap: split state ready")


if __name__ == "__main__":
    main()
