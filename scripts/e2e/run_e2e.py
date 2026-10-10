"""Drive the installed plugin against a real DroppedNeedle and check the outcome (PLAN §11.3).

Usage (after run_server.sh, make_library.py and bootstrap.py, on a fresh data root):
    python scripts/e2e/run_e2e.py

1. enable the plugin with admin credentials (dry_run unset -> true) and wait for
   the run_on_load report: the §7.6 decisions, and no merge;
2. save dry_run=false (a save does not re-run), POST run, wait: exactly one merge
   (Otonose Kanade -> 音乃瀬奏) keeping the MusicBrainz link, nothing else changed,
   the retired id answers 308;
3. POST run again: a no-op.

Exits non-zero on any failure and prints the server log tail.
Environment: DN_ROOT (default .cache/dnroot), DN_PORT (default 18688).
Needs httpx (the server venv has it).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
DN_ROOT = Path(os.environ.get("DN_ROOT", REPO_ROOT / ".cache" / "dnroot")).resolve()
PORT = int(os.environ.get("DN_PORT", "18688"))
SERVER = f"http://127.0.0.1:{PORT}"
BASE = f"{SERVER}/api/v1"
PLUGIN = "mbid-artist-merge"
EXT = f"/plugins/ext/{PLUGIN}"
ADMIN = {"username": "admin", "password": "MbidArtistMerge-e2e-7c41f9"}
SECRET_MASK = "plugin****"
M_KANADE = "11111111-1111-4111-8111-111111111111"
EXPECTED = {  # PLAN §7.6 on the bootstrap library
    "Otonose Kanade": "would_merge",
    "Other Artist": "no_target",
    "Lonely Alias": "no_target",
    "Conflict Name": "conflict",
}
FIELDS = ("id", "name", "musicbrainz_artist_id", "artist_identity_state", "row_revision")


class E2EFailure(Exception):
    pass


def check(condition: object, message: str) -> None:
    if not condition:
        raise E2EFailure(message)


def step(message: str) -> None:
    print(f"e2e: {message}", flush=True)


def artists(client: httpx.Client) -> dict[str, dict[str, Any]]:
    """Both scopes, every page, unioned by id, reduced to the scope-independent fields."""
    found: dict[str, dict[str, Any]] = {}
    for scope in ("album", "contributors"):
        offset = 0
        while True:
            page = client.get(
                "/library/artists", params={"scope": scope, "limit": 100, "offset": offset}
            )
            page.raise_for_status()
            body = page.json()
            for item in body["items"]:
                found.setdefault(item["id"], {k: item[k] for k in FIELDS})
            offset += len(body["items"])
            if not body["items"] or offset >= body["total"]:
                break
    return found


def by_name(found: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {a["name"]: a for a in found.values()}


def ext(client: httpx.Client, method: str, path: str) -> httpx.Response:
    """Call a plugin route, waiting out the per-route rate limit."""
    while True:
        response = client.request(method, f"{EXT}/{path}")
        if response.status_code != 429:
            return response
        time.sleep(int(response.headers.get("retry-after", "5")) + 1)


def wait_report(
    client: httpx.Client, done: Callable[[dict[str, Any]], bool], what: str, timeout: float = 600
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        response = ext(client, "GET", "report")
        if response.status_code == 200:
            last = response.json()
            if isinstance(last, dict) and last.get("finished_at") and done(last):
                return last
        elif response.status_code != 404:
            raise E2EFailure(f"GET report -> {response.status_code} {response.text[:500]}")
        time.sleep(3)  # the report route allows 30 calls a minute
    raise E2EFailure(f"timed out waiting for {what}; last report: {json.dumps(last)[:2000]}")


def start_run(client: httpx.Client) -> str:
    for _ in range(40):
        response = ext(client, "POST", "run")
        check(response.status_code == 200, f"POST run -> {response.status_code} {response.text}")
        body = response.json()
        if body.get("started"):
            return body["run_id"]
        time.sleep(3)  # another run holds the lease
    raise E2EFailure("POST run never started a run")


def save_plugin(client: httpx.Client, settings: dict[str, str]) -> dict[str, Any]:
    response = client.put(f"/plugins/{PLUGIN}", json={"enabled": True, "settings": settings})
    check(response.status_code == 200, f"PUT plugin -> {response.status_code} {response.text}")
    info = response.json()
    check(not info.get("error"), f"plugin failed to load: {info.get('error')}")
    return info


def check_report(report: dict[str, Any], *, dry_run: bool) -> None:
    check(report["status"] == "ok", f"report status {report['status']}: {report['errors']}")
    check(report["dry_run"] is dry_run, f"report dry_run={report['dry_run']}")
    check(report["partial"] is False, "report is partial")
    check(report["totals"]["errors"] == 0, f"report errors: {report['errors']}")
    check(ADMIN["password"] not in json.dumps(report), "report contains the password")


def decisions(report: dict[str, Any]) -> dict[str, str]:
    return {a["source"]["name"]: a["decision"] for a in report["actions"]}


def run(client: httpx.Client) -> None:
    before = artists(client)
    named = by_name(before)
    check(named["音乃瀬奏"]["musicbrainz_artist_id"] == M_KANADE, "音乃瀬奏 is not linked")
    check(named["Otonose Kanade"]["artist_identity_state"] == "local_only", "not split")
    otonose, kanade = named["Otonose Kanade"]["id"], named["音乃瀬奏"]["id"]

    step("enable the plugin (dry-run) and wait for the run_on_load report")
    settings = {"base_url": SERVER, "username": ADMIN["username"], "password": ADMIN["password"]}
    info = save_plugin(client, settings)
    check(
        {"scheduler", "publisher"} <= set(info.get("active_capabilities") or []),
        f"capabilities not active: {info.get('active_capabilities')}",
    )
    dry = wait_report(client, lambda r: r["status"] != "lease_held", "the dry-run report")
    check_report(dry, dry_run=True)
    check(dry["trigger"] == "load", f"first run trigger is {dry['trigger']}")
    check(decisions(dry) == EXPECTED, f"dry-run decisions: {decisions(dry)}")
    totals = dry["totals"]
    check(totals["no_mbid"] == 1 and totals["would_merge"] == 1, f"totals: {totals}")
    check(totals["merged"] == 0, "dry run merged")
    merge = next(a for a in dry["actions"] if a["decision"] == "would_merge")
    check(merge["target"]["id"] == kanade, f"would merge into {merge['target']}")
    check(merge["reference_counts"].get("album_credits"), "no reference counts")
    check(artists(client) == before, "the dry run changed the library")
    step(f"dry run ok: {decisions(dry)}")

    step("save dry_run=false (must not re-run), then POST run")
    save_plugin(client, {**settings, "password": SECRET_MASK, "dry_run": "false"})
    time.sleep(5)
    current = ext(client, "GET", "report").json()
    check(current["run_id"] == dry["run_id"], "saving the settings started a run")
    run_id = start_run(client)
    live = wait_report(client, lambda r: r["run_id"] == run_id, "the live report")
    check_report(live, dry_run=False)
    # the other four are cache hits now: counted in totals, not listed as actions
    totals = live["totals"]
    check(
        (totals["merged"], totals["no_target"], totals["conflict"], totals["no_mbid"])
        == (1, 2, 1, 1),
        f"live totals: {totals}",
    )
    check(decisions(live).get("Otonose Kanade") == "merged", f"live: {decisions(live)}")

    after = artists(client)
    check(otonose not in after, "Otonose Kanade is still listed")
    check(after[kanade] == before[kanade], f"音乃瀬奏 changed: {after[kanade]}")
    others = {k: v for k, v in before.items() if k not in (otonose, kanade)}
    check({k: after.get(k) for k in others} == others, "another artist changed")
    with httpx.Client(base_url=BASE, headers=client.headers, follow_redirects=False) as raw:
        retired = raw.get(f"/library/artists/{otonose}")
    check(retired.status_code == 308, f"retired id answered {retired.status_code}")
    check(retired.headers.get("location", "").endswith(kanade), "308 does not point at 音乃瀬奏")
    step("live run ok: Otonose Kanade merged into 音乃瀬奏, link kept, others unchanged")

    step("POST run again: must be a no-op")
    run_id = start_run(client)
    again = wait_report(client, lambda r: r["run_id"] == run_id, "the second live report")
    check_report(again, dry_run=False)
    check(again["totals"]["merged"] == 0, f"second run merged: {again['totals']}")
    check(again["totals"]["would_merge"] == 0, f"second run: {again['totals']}")
    check(artists(client) == after, "the second run changed the library")
    step("second run ok: nothing to do")


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=30)
    try:
        login = client.post("/auth/login", json=ADMIN)
        login.raise_for_status()
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"
        run(client)
    except (E2EFailure, httpx.HTTPError, KeyError, StopIteration) as exc:
        print(f"e2e: FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        log = DN_ROOT / "server.log"
        if log.is_file():
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
            print("--- server log tail ---", *tail, sep="\n", file=sys.stderr)
        return 1
    finally:
        if "Authorization" in client.headers:
            client.post("/auth/logout")
        client.close()
    print("e2e: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
