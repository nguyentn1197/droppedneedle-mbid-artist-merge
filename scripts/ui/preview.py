"""Preview ui/panel.js the way DroppedNeedle's settings page shows it, without a server.

Usage:
    uv run python scripts/ui/preview.py [--screenshot] [--browser PATH]

Writes .cache/panel-preview/<scenario>.html (open them in any browser) and, with
--screenshot, <scenario>.png rendered by a headless Edge/Chrome.

Each page copies the host's PluginPanel.svelte (v2.15.0): the panel runs in an
``<iframe sandbox="allow-scripts">`` whose srcdoc carries the same CSP and body
style, ~192 px tall. A fake bridge in the page answers the panel's
``search.preview`` RPC from ``sample_state.json`` (a real state captured from
v2.15.0 by run_e2e.py with E2E_PANEL_STATE set). Scenarios that "click" append a
harness-only script after the panel code; the real host never does that.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT = REPO_ROOT / ".cache" / "panel-preview"
SAMPLE = json.loads((Path(__file__).parent / "sample_state.json").read_text(encoding="utf-8"))
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google-chrome",
    "chromium",
    "microsoft-edge",
]

# Copied from frontend/src/lib/components/settings/PluginPanel.svelte (v2.15.0).
HOST_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; connect-src 'none'; frame-src 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'none';"
)
HOST_STYLE = (
    "body{font:13px/1.5 system-ui,sans-serif;margin:0;padding:12px;color:#111;background:#fff}"
    "#panel-root{min-height:40px}"
)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>panel preview: __NAME__</title>
<style>
  body{margin:0;padding:16px;background:#f4f5f7;font:13px system-ui,sans-serif}
  .card{border:1px solid #dde1e6;border-radius:12px;background:#fff;padding:12px}
  .label{margin:0 0 8px;font-size:12px;font-weight:600;color:#6b7280}
  iframe{display:block;width:100%;height:192px;border:1px solid #e5e7eb;border-radius:8px;background:#fff}
  .hint{margin-top:4px;font-size:11px;color:#9ca3af}
</style></head>
<body>
<div class="card" style="max-width:__PANEL_WIDTH__">
  <p class="label">MBID Artist Merge panel <span style="font-weight:400">(preview: __NAME__)</span></p>
  <div id="slot"></div>
  <p class="hint">Sandboxed: scripts only, no network.</p>
</div>
<script>
const PANEL_JS = __PANEL_JS__;
const SCENARIO = __SCENARIO__;
const HOST_CSP = __HOST_CSP__;
const HOST_STYLE = __HOST_STYLE__;
// Same order as the host's Svelte component: the iframe is created with its
// sandbox + srcdoc and inserted, then an effect (a microtask later) adds the
// `csp` attribute. Chromium enforces that attribute only on later navigations.
const frame = document.createElement("iframe");
frame.title = "MBID Artist Merge panel";
frame.setAttribute("sandbox", "allow-scripts");

function escapeForInlineScript(js) { return js.replace(/<[/]script/gi, "<\\\\/script"); }
const harness = SCENARIO.clicks
  ? `<` + `script>${escapeForInlineScript(
      `(() => { let n = ${SCENARIO.clicks}; const click = () => {` +
      `const sel = ${JSON.stringify(SCENARIO.clickSelector || "")};` +
      `const b = sel ? document.querySelector(sel) : [...document.querySelectorAll("button")].find(x => !x.classList.contains("quiet") && !x.disabled);` +
      `if (b) { b.click(); n--; } if (n > 0) setTimeout(click, 700); }; setTimeout(click, 900); })();`
    )}<` + `/script>`
  : "";
frame.srcdoc =
  `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${HOST_CSP}">` +
  `<style>${HOST_STYLE}</style></head><body><div id="panel-root"></div><` +
  `script>${escapeForInlineScript(PANEL_JS)}<` + `/script>${harness}</body></html>`;
document.getElementById("slot").append(frame);
queueMicrotask(() => frame.setAttribute("csp", "default-src 'none'; connect-src 'none'; form-action 'none'"));

// --- fake host bridge: search.preview -> the plugin's GET ext/search ---------
let state = structuredClone(SCENARIO.state);
const minutesAgo = (m) => new Date(Date.now() - m * 60000).toISOString().replace(/\\.\\d+Z$/, "Z");
if (state.report) state.report.finished_at = minutesAgo(SCENARIO.finishedMinutesAgo ?? 5);
let polls = 0;
let runId = null;

function answer(query) {
  if (SCENARIO.bridgeError) return { error: { code: "search_unavailable", message: "Request failed (502)" } };
  if (query.startsWith("run:")) {
    if (query.slice(4) !== state.run_code) return { result: { started: false, reason: "stale_code" } };
    runId = "preview-run";
    state = { ...state, running: true, run_code: "used" };
    return { result: { started: true, run_id: runId } };
  }
  if (state.running && runId) {
    polls++;
    if (SCENARIO.finishAfterPolls && polls > SCENARIO.finishAfterPolls) {
      const report = { ...(state.report || {}), run_id: runId, trigger: "manual", finished_at: minutesAgo(0) };
      if (SCENARIO.finishedReport) Object.assign(report, SCENARIO.finishedReport);
      state = { ...state, running: false, run_code: "fresh", report };
    }
  }
  return { result: state };
}

window.addEventListener("message", (event) => {
  if (event.origin !== "null" || event.source !== frame.contentWindow) return;
  const { id, method, params } = event.data || {};
  const reply = method === "search.preview"
    ? answer((params && params.query) || "")
    : { error: { code: "unknown_method", message: `Unknown method ${method}` } };
  setTimeout(() => event.source.postMessage({ id, ...reply }, "*"), 60);
});
</script>
</body></html>
"""


def big_report() -> dict[str, Any]:
    """A larger report: 39 would merge, 31 ambiguous, 87 no linked artist (20 listed)…"""
    state = copy.deepcopy(SAMPLE)
    names = ["Otonose Kanade", "Hoshimachi Suisei", "Minato Aqua", "Usada Pekora", "KAF"]

    def mbid(n: int) -> str:
        return f"{n:08x}-1111-4111-8111-111111111111"

    actions: list[dict[str, Any]] = []
    for n in range(39):
        actions.append(
            {
                "decision": "would_merge",
                "source": f"{names[n % 5]} {n}" if n >= 5 else names[n],
                "target": "音乃瀬奏" if n == 0 else f"アーティスト {n}",
                "mbid": mbid(n),
                "files": 5,
            }
        )
    for n in range(31):
        actions.append(
            {
                "decision": "ambiguous",
                "source": "KAF & ORESAMA" if n == 0 else f"Masayuki Suzuki feat.Airi Suzuki {n}",
                "detail": "more_ids_than_credits" if n % 3 else "same_name_different_ids",
                "files": 1,
            }
        )
    actions += [
        {
            "decision": "shared_mbid_without_link",
            "source": f"Alias {n}",
            "mbid": mbid(900),
            "shared_with": [f"Alias {1 - n}"],
            "files": 2,
        }
        for n in range(2)
    ]
    actions += [
        {"decision": "no_target", "source": f"Indie Artist {n}", "mbid": mbid(500 + n), "files": 3}
        for n in range(20)
    ]
    actions.append(
        {
            "decision": "insufficient_evidence",
            "source": "Half Tagged",
            "mbid": mbid(7),
            "detail": "1 of 5 sampled files carry the mbid, need 2",
            "files": 5,
        }
    )
    actions.append(
        {
            "decision": "excluded_mbid",
            "source": "Various Artists Clone",
            "mbid": mbid(8),
            "files": 2,
        }
    )
    state["report"].update(
        trigger="manual",
        actions=actions,
        actions_total=len(actions),
        totals={
            "artists_listed": 420,
            "linked": 160,
            "candidates": 260,
            "examined": 200,
            "skipped_cached": 67,
            "would_merge": 39,
            "ambiguous": 31,
            "shared_mbid_without_link": 2,
            "no_target": 87,
            "insufficient_evidence": 1,
            "excluded_mbid": 1,
            "no_mbid": 39,
        },
    )
    return state


def scenarios() -> dict[str, dict[str, Any]]:
    dry = copy.deepcopy(SAMPLE)
    live = {**copy.deepcopy(SAMPLE), "dry_run": False}
    merged = copy.deepcopy(live)
    merged["report"]["dry_run"] = False
    merged["report"]["totals"] = {**merged["report"]["totals"], "would_merge": 0, "merged": 1}
    merged["report"]["totals"].pop("would_merge")
    merged["report"]["actions"][0]["decision"] = "merged"
    problems = copy.deepcopy(SAMPLE)
    problems["report"].update(
        status="server_unreachable",
        partial=True,
        actions=problems["report"]["actions"][:1],
        actions_total=1,
        errors=[
            {"stage": "tags", "status": 400, "message": "Unreadable audio file"},
            {"stage": "request", "status": None, "message": "GET /library/artists -> ConnectError"},
        ],
        errors_total=2,
    )
    return {
        "dry": {"state": dry},
        "dry-narrow": {"state": dry, "panelWidth": 360},
        "big": {"state": big_report()},
        "big-narrow": {"state": big_report(), "panelWidth": 420},
        "big-filter-no-target": {
            "state": big_report(),
            "clicks": 1,
            "clickSelector": ".filter:nth-of-type(5)",
        },
        "big-filter-no-mbid": {
            "state": big_report(),
            "clicks": 1,
            "clickSelector": ".filter:nth-of-type(8)",
        },
        "empty": {"state": {**dry, "report": None}},
        "running": {"state": dry, "clicks": 1},
        "finished": {"state": dry, "clicks": 1, "finishAfterPolls": 1, "finishedMinutesAgo": 0},
        "live": {"state": merged},
        "live-confirm": {"state": live, "clicks": 1},
        "live-finished": {
            "state": live,
            "clicks": 2,
            "finishAfterPolls": 1,
            "finishedReport": {
                "status": "ok",
                **{k: merged["report"][k] for k in ("totals", "actions")},
            },
        },
        "problems": {"state": problems},
        "config-error": {
            "state": {
                **dry,
                "config_error": "samples_per_artist must be a whole number (got 'lots')",
            }
        },
        "bridge-error": {"state": dry, "bridgeError": True},
    }


def page(name: str, scenario: dict[str, Any], panel_js: str) -> str:
    def js(value: object) -> str:
        return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")

    return (
        PAGE.replace("__NAME__", name)
        .replace(
            "__PANEL_WIDTH__", f"{scenario['panelWidth']}px" if "panelWidth" in scenario else "none"
        )
        .replace("__PANEL_JS__", js(panel_js))
        .replace("__SCENARIO__", js(scenario))
        .replace("__HOST_CSP__", js(HOST_CSP))
        .replace("__HOST_STYLE__", js(HOST_STYLE))
    )


def find_browser(explicit: str | None) -> str:
    for candidate in [explicit] if explicit else BROWSERS:
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return candidate
    sys.exit("no Edge/Chrome found; pass --browser PATH")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--screenshot", action="store_true")
    parser.add_argument("--browser")
    args = parser.parse_args()
    panel_js = (REPO_ROOT / "ui" / "panel.js").read_text(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    browser = find_browser(args.browser) if args.screenshot else ""
    for name, scenario in scenarios().items():
        html = OUT / f"{name}.html"
        html.write_text(page(name, scenario, panel_js), encoding="utf-8")
        if not args.screenshot:
            print(html)
            continue
        png = OUT / f"{name}.png"
        budget = 12000 if scenario.get("finishAfterPolls") else 3000
        subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                # the sandboxed iframe is cross-origin; keep it in-process so the
                # virtual-time budget also drives its timers
                "--disable-site-isolation-trials",
                "--disable-features=site-per-process,IsolateOrigins",
                "--window-size=820,270",
                f"--virtual-time-budget={budget}",
                f"--screenshot={png}",
                html.as_uri(),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        print(png)


if __name__ == "__main__":
    main()
