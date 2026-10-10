// MBID Artist Merge: settings panel for DroppedNeedle (Settings > Plugins > Plugin panel).
//
// The host runs this file inside a sandboxed iframe: no network, no storage, no
// dialogs. Its only bridge to the plugin is the host's read-only `search.preview`
// RPC, which the host forwards to this plugin's GET ext/search route:
//   query ""          -> panel state (settings summary, running, last report, run code)
//   query "run:<code>" -> start a run, if <code> is the one-time code from the state
(() => {
  "use strict";

  const POLL_MS = 4000;
  const RPC_TIMEOUT_MS = 15000;
  const CONFIRM_MS = 8000;

  const LABELS = {
    merged: "Merged",
    would_merge: "Would merge",
    deferred: "Deferred",
    error: "Error",
    identity_conflict: "Identity conflict",
    stale: "Stale, retry next run",
    changed: "Changed meanwhile",
    already_merged: "Already merged",
    conflict: "Conflicting MBIDs",
    ambiguous: "Ambiguous tags",
    shared_mbid_without_link: "Shared MBID, no link",
    no_target: "No linked artist",
    insufficient_evidence: "Too few tagged files",
    excluded_mbid: "Excluded MBID",
    excluded_target: "Excluded target",
    no_samples: "No readable files",
    self: "Self",
    no_mbid: "No MBID in files",
  };
  const TONE = {
    merged: "good",
    would_merge: "good",
    error: "bad",
    deferred: "warn",
    identity_conflict: "warn",
    stale: "warn",
    changed: "warn",
    conflict: "warn",
    ambiguous: "warn",
  };
  const ORDER = Object.keys(LABELS);
  const STATUS = {
    ok: "OK",
    config_error: "Settings problem",
    auth_failed: "Login failed",
    not_admin: "Account is not an admin",
    auth_lost: "Session lost during the run",
    server_unreachable: "Server unreachable",
    library_busy: "Library busy, skipped",
    lease_held: "Another run was in progress",
    internal_error: "Internal error",
  };

  // --- bridge -----------------------------------------------------------------

  let nextId = 0;
  const pending = new Map();
  window.addEventListener("message", (event) => {
    if (event.source !== parent) return;
    const data = event.data || {};
    const resolve = pending.get(data.id);
    if (!resolve) return;
    pending.delete(data.id);
    resolve(data.error ? { error: data.error } : { result: data.result });
  });

  function ask(query) {
    return new Promise((resolve) => {
      const id = `mam-${++nextId}`;
      pending.set(id, resolve);
      parent.postMessage({ id, method: "search.preview", params: { query } }, "*");
      setTimeout(() => {
        if (pending.delete(id)) resolve({ error: { message: "DroppedNeedle did not answer" } });
      }, RPC_TIMEOUT_MS);
    });
  }

  // --- tiny DOM helper (text only, never HTML) --------------------------------

  function h(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
      if (child === undefined || child === null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function ago(iso) {
    const then = Date.parse(iso || "");
    if (Number.isNaN(then)) return "";
    const s = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }

  const short = (mbid) => (mbid ? `${String(mbid).slice(0, 8)}…` : "");

  // --- state & rendering --------------------------------------------------------

  const ui = {
    state: null, // last panel state from the plugin
    problem: "", // bridge or plugin error text
    note: "", // transient message under the header
    busy: false, // a request to start a run is in flight
    confirming: false, // live mode: waiting for the second click
    waitingFor: null, // run_id we started and are waiting on
  };
  let pollTimer = 0;
  let confirmTimer = 0;

  const root = document.getElementById("panel-root") || document.body;
  root.append(
    h(
      "style",
      null,
      `
      #panel-root{--muted:#5b6470;--line:#e6e8eb}
      .top{position:sticky;top:-12px;margin:-12px -12px 0;padding:12px 12px 8px;background:#fff;border-bottom:1px solid var(--line);z-index:1}
      .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
      .grow{flex:1;min-width:0}
      .badge{font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px;white-space:nowrap}
      .dry{background:#e8efff;color:#1d4ed8}.live{background:#fde8e7;color:#b42318}
      .status{color:var(--muted);font-size:12px}
      .status b{color:#111;font-weight:600}
      button{font:inherit;font-size:12px;font-weight:600;border-radius:6px;border:1px solid transparent;padding:4px 10px;cursor:pointer;background:#111;color:#fff}
      button.danger{background:#b42318}
      button.quiet{background:none;color:var(--muted);border-color:var(--line);font-weight:500}
      button:disabled{opacity:.5;cursor:default}
      button:focus-visible{outline:2px solid #1d4ed8;outline-offset:1px}
      .chips{margin-top:6px;display:flex;gap:4px;flex-wrap:wrap}
      .chip{font-size:11px;padding:0 6px;border-radius:4px;background:#eef1f4;color:#44505c;white-space:nowrap}
      .note{margin-top:6px;font-size:12px}
      .note.bad,.note.warn{padding:4px 8px;border-radius:6px}
      ul{list-style:none;margin:8px 0 0;padding:0}
      li{padding:5px 0;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:baseline}
      li:last-child{border-bottom:0}
      .tag{flex:none;width:118px;font-size:11px;font-weight:600;padding:0 6px;border-radius:4px;background:#eef1f4;color:#44505c;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .who{min-width:0;overflow-wrap:anywhere}
      .sub{display:block;color:var(--muted);font-size:11px}
      .arrow{color:var(--muted);padding:0 4px}
      .empty{color:var(--muted);margin-top:10px}
      .foot{margin-top:8px;color:var(--muted);font-size:11px}
      .good{background:#e7f6ec;color:#0f6b38}.warn{background:#fff4dc;color:#7a4f00}.bad{background:#fde8e7;color:#b42318}
      `
    )
  );
  const view = h("div", { "aria-live": "polite" });
  root.append(view);

  function header() {
    const state = ui.state;
    const report = state && state.report;
    const live = state && state.dry_run === false;
    const running = Boolean(state && state.running) || Boolean(ui.waitingFor);

    let statusLine;
    if (!state) statusLine = [ui.problem ? "Plugin unavailable" : "Loading…"];
    else if (running) statusLine = [h("b", null, "Running…"), " the report updates when it finishes"];
    else if (!report) statusLine = ["No run yet"];
    else {
      const label = STATUS[report.status] || report.status;
      statusLine = [
        "Last run ",
        h("b", null, report.partial && report.status === "ok" ? "OK (partial)" : label),
        ` · ${ago(report.finished_at)}`,
        report.trigger ? ` · ${report.trigger}` : "",
      ];
    }

    const canRun = state && !state.config_error && !running && !ui.busy;
    const button = ui.confirming
      ? h("button", { class: "danger", onclick: start }, "Confirm: merge for real")
      : h(
          "button",
          {
            onclick: start,
            disabled: !canRun,
            title: state && state.config_error ? "Fix the settings first" : null,
          },
          running ? "Running…" : ui.busy ? "Starting…" : "Run now"
        );

    const top = h(
      "div",
      { class: "top" },
      h(
        "div",
        { class: "row" },
        state
          ? h(
              "span",
              { class: `badge ${live ? "live" : "dry"}`, title: live ? "dry_run = false" : "dry_run = true" },
              live ? "Live" : "Dry run"
            )
          : null,
        h("span", { class: "status grow" }, ...statusLine),
        ui.confirming ? h("button", { class: "quiet", onclick: cancelConfirm }, "Cancel") : null,
        state ? button : null,
        !state && ui.problem ? h("button", { class: "quiet", onclick: refresh }, "Retry") : null,
        !running && state ? h("button", { class: "quiet", onclick: refresh, title: "Reload the report" }, "↻") : null
      )
    );

    if (report && report.totals) {
      const chips = ORDER.filter((key) => report.totals[key]).map((key) =>
        h("span", { class: `chip ${TONE[key] || ""}` }, `${report.totals[key]} ${LABELS[key].toLowerCase()}`)
      );
      if (report.totals.examined) chips.push(h("span", { class: "chip" }, `${report.totals.examined} checked`));
      if (chips.length) top.append(h("div", { class: "chips" }, chips));
    }
    if (ui.confirming) {
      top.append(
        h("div", { class: "note bad" }, "Merges cannot be undone. Back up library.db first, then confirm.")
      );
    } else if (state && state.config_error) {
      top.append(h("div", { class: "note bad" }, `Settings problem: ${state.config_error}`));
    } else if (ui.problem) {
      top.append(h("div", { class: "note bad" }, ui.problem));
    } else if (ui.note) {
      top.append(h("div", { class: "note" }, ui.note));
    }
    return top;
  }

  function actionRow(action) {
    const tone = TONE[action.decision] || "";
    const extra = [];
    if (action.mbid) extra.push(`MBID ${short(action.mbid)}`);
    if (action.mbids) extra.push(`MBIDs ${action.mbids.map(short).join(", ")}`);
    if (action.shared_with) extra.push(`also: ${action.shared_with.join(", ")}`);
    if (action.files) extra.push(`${action.files} file${action.files === 1 ? "" : "s"} checked`);
    if (action.detail) extra.push(action.detail);
    return h(
      "li",
      null,
      h("span", { class: `tag ${tone}`, title: action.decision }, LABELS[action.decision] || action.decision),
      h(
        "span",
        { class: "who" },
        action.source || action.source_id,
        action.target ? [h("span", { class: "arrow" }, "→"), action.target] : null,
        extra.length ? h("span", { class: "sub", title: action.mbid || null }, extra.join(" · ")) : null
      )
    );
  }

  function body() {
    const state = ui.state;
    if (!state) return h("div");
    const report = state.report;
    if (!report) {
      return h(
        "div",
        { class: "empty" },
        state.dry_run === false
          ? "No run yet. Run now merges for real (dry_run is off)."
          : "No run yet. Run now to see what it would merge; nothing is changed in dry run."
      );
    }
    const parts = [];
    if (report.actions && report.actions.length) {
      parts.push(h("ul", null, report.actions.map(actionRow)));
      if (report.actions_total > report.actions.length) {
        parts.push(h("div", { class: "foot" }, `…and ${report.actions_total - report.actions.length} more in the full report.`));
      }
    } else {
      parts.push(h("div", { class: "empty" }, "Nothing to act on in the last run."));
    }
    if (report.errors && report.errors.length) {
      parts.push(
        h("div", { class: "foot" }, `${report.errors_total} problem${report.errors_total === 1 ? "" : "s"} during the run:`),
        h(
          "ul",
          null,
          report.errors.map((e) =>
            h("li", null, h("span", { class: "tag bad" }, e.stage || "error"), h("span", { class: "who" }, e.message || ""))
          )
        )
      );
    }
    parts.push(
      h(
        "div",
        { class: "foot" },
        "Full report: /api/v1/plugins/ext/mbid-artist-merge/report",
        state.plugin_version ? ` · v${state.plugin_version}` : ""
      )
    );
    return h("div", null, parts);
  }

  function render() {
    view.replaceChildren(header(), body());
  }

  // --- behaviour ------------------------------------------------------------------

  async function load() {
    const answer = await ask("");
    if (answer.error) {
      ui.problem = `Could not reach the plugin: ${answer.error.message || answer.error.code || "error"}`;
      return null;
    }
    ui.problem = "";
    ui.state = answer.result;
    return answer.result;
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    const running = (ui.state && ui.state.running) || ui.waitingFor;
    if (running) pollTimer = setTimeout(poll, POLL_MS);
  }

  async function poll() {
    const state = await load();
    if (state && ui.waitingFor && !state.running) {
      const report = state.report;
      if (report && report.run_id === ui.waitingFor) {
        ui.note = report.status === "ok" ? "Run finished." : "Run finished with a problem; see below.";
      } else {
        ui.note = "That run ended without a new report (another run was already in progress).";
      }
      ui.waitingFor = null;
    }
    render();
    schedulePoll();
  }

  async function refresh() {
    ui.note = "";
    await load();
    render();
    schedulePoll();
  }

  function cancelConfirm() {
    clearTimeout(confirmTimer);
    ui.confirming = false;
    render();
  }

  async function start() {
    const live = ui.state && ui.state.dry_run === false;
    if (live && !ui.confirming) {
      ui.confirming = true;
      ui.note = "";
      render();
      confirmTimer = setTimeout(cancelConfirm, CONFIRM_MS);
      return;
    }
    clearTimeout(confirmTimer);
    ui.confirming = false;
    ui.busy = true;
    ui.note = "";
    render();
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        const state = await load(); // a fresh one-time code every time
        if (!state) return;
        if (state.running) {
          ui.note = "A run is already in progress.";
          return;
        }
        const answer = await ask(`run:${state.run_code}`);
        if (answer.error) {
          ui.problem = `Could not start the run: ${answer.error.message || "error"}`;
          return;
        }
        const result = answer.result || {};
        if (result.started) {
          ui.waitingFor = result.run_id;
          ui.state = { ...state, running: true };
          ui.note = live ? "Live run started." : "Dry run started.";
          return;
        }
        if (result.reason === "running") {
          ui.note = "A run is already in progress.";
          return;
        }
        // stale_code: the plugin reloaded in between; fetch a new code and retry once
      }
      ui.problem = "Could not start the run; reload the panel and try again.";
    } finally {
      ui.busy = false;
      render();
      schedulePoll();
    }
  }

  render();
  refresh();
})();
