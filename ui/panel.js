// MBID Artist Merge: settings panel for DroppedNeedle (Settings > Plugins > Plugin panel).
//
// The host runs this file inside a sandboxed iframe of fixed height (~192 px): no
// network, no storage, no dialogs, and no way to resize the frame. Its only bridge to
// the plugin is the host's read-only `search.preview` RPC, which the host forwards to
// this plugin's GET ext/search route:
//   query ""           -> panel state (settings summary, running, last report, run code)
//   query "run:<code>" -> start a run, if <code> is the one-time code from the state
//
// Layout for the small frame: a one-line bar (mode, last run, Run now) over two
// independently scrolling columns: decision filters on the left, one line per artist
// on the right. Narrow frames stack the filters as a horizontal strip.
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
  const DETAILS = {
    more_ids_than_credits: "more MBIDs than credited names",
    same_name_different_ids: "same name credited with different MBIDs",
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
  const PROBLEMS = "__problems";

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
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  // --- state --------------------------------------------------------------------

  const ui = {
    state: null, // last panel state from the plugin
    problem: "", // bridge or plugin error text
    note: "", // short message after the status
    busy: false, // a request to start a run is in flight
    confirming: false, // live mode: waiting for the second click
    waitingFor: null, // run_id we started and are waiting on
    filter: "all", // "all", a decision, or PROBLEMS
  };
  let pollTimer = 0;
  let confirmTimer = 0;

  const root = document.getElementById("panel-root") || document.body;
  root.append(
    h(
      "style",
      null,
      `
      html,body{height:100%}
      body{padding:0!important;overflow:hidden}
      #panel-root{height:100%;--muted:#5b6470;--line:#e6e8eb;--soft:#eef1f4}
      .app{display:flex;flex-direction:column;height:100%}
      .bar{flex:none;display:flex;align-items:center;gap:8px;padding:6px 10px;border-bottom:1px solid var(--line)}
      .status{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted);font-size:12px}
      .status b{color:#111;font-weight:600}
      .banner{flex:none;padding:3px 10px;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .badge{flex:none;font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px;white-space:nowrap}
      .dry{background:#e8efff;color:#1d4ed8}.live{background:#fde8e7;color:#b42318}
      button{font:inherit;font-size:12px;font-weight:600;border-radius:6px;border:1px solid transparent;padding:3px 10px;cursor:pointer;background:#111;color:#fff;white-space:nowrap}
      button.danger{background:#b42318}
      button.quiet{background:none;color:var(--muted);border-color:var(--line);font-weight:500;padding:3px 8px}
      button:disabled{opacity:.5;cursor:default}
      button:focus-visible{outline:2px solid #1d4ed8;outline-offset:1px}
      .main{flex:1;min-height:0;display:flex}
      .side{flex:none;width:200px;overflow-y:auto;border-right:1px solid var(--line);padding:4px 0}
      .list{flex:1;min-width:0;overflow-y:auto}
      .filter{display:flex;align-items:center;gap:6px;width:100%;padding:2px 10px;border:0;border-radius:0;background:none;color:#111;font-weight:500;text-align:left}
      .filter:hover{background:#f6f7f9}
      .filter[aria-pressed="true"]{background:var(--soft);font-weight:600}
      .filter .lbl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
      .n{flex:none;font-size:11px;font-weight:600;min-width:22px;text-align:center;padding:0 5px;border-radius:999px;background:var(--soft);color:#44505c}
      .side .meta{padding:4px 10px 0;color:var(--muted);font-size:11px}
      .row{display:flex;align-items:baseline;gap:8px;padding:3px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
      .tag{flex:none;width:112px;font-size:11px;font-weight:600;padding:0 6px;border-radius:4px;background:var(--soft);color:#44505c;overflow:hidden;text-overflow:ellipsis}
      .who{flex:0 1 auto;min-width:0;max-width:60%;overflow:hidden;text-overflow:ellipsis}
      .arrow{color:var(--muted);padding:0 4px}
      .sub{flex:1 1 0;min-width:0;overflow:hidden;text-overflow:ellipsis;color:var(--muted);font-size:11px}
      .msg{padding:8px 10px;color:var(--muted)}
      .good{background:#e7f6ec;color:#0f6b38}.warn{background:#fff4dc;color:#7a4f00}.bad{background:#fde8e7;color:#b42318}
      @media (max-width:560px){
        .main{flex-direction:column}
        .side{width:auto;display:flex;gap:2px;overflow-x:auto;overflow-y:hidden;border-right:0;border-bottom:1px solid var(--line);padding:3px 6px}
        .filter{width:auto;flex:none;padding:1px 6px;border-radius:4px}
        .side .meta{display:none}
        .tag{width:auto;max-width:96px}
      }
      `
    )
  );
  const view = h("div", { class: "app", "aria-live": "polite" });
  root.append(view);

  // --- rendering --------------------------------------------------------------------

  function bar() {
    const state = ui.state;
    const report = state && state.report;
    const live = state && state.dry_run === false;
    const running = Boolean(state && state.running) || Boolean(ui.waitingFor);

    let status;
    if (!state) status = [ui.problem ? "Plugin unavailable" : "Loading…"];
    else if (ui.confirming) status = [h("b", null, "Merges cannot be undone."), " Back up library.db, then confirm."];
    else if (running) status = [h("b", null, "Running…"), " the report updates when it finishes"];
    else if (!report) status = ["No run yet"];
    else {
      const label = STATUS[report.status] || report.status;
      status = [
        "Last run ",
        h("b", null, report.partial && report.status === "ok" ? "OK (partial)" : label),
        ` · ${ago(report.finished_at)}`,
        report.trigger ? ` · ${report.trigger}` : "",
      ];
    }
    if (ui.note && !ui.confirming) status.push(` · ${ui.note}`);

    const canRun = state && !state.config_error && !running && !ui.busy;
    return h(
      "div",
      { class: "bar" },
      state
        ? h(
            "span",
            { class: `badge ${live ? "live" : "dry"}`, title: live ? "dry_run = false" : "dry_run = true" },
            live ? "Live" : "Dry run"
          )
        : null,
      h("span", { class: "status", title: status.map((s) => (s instanceof Node ? s.textContent : s)).join("") }, status),
      ui.confirming ? h("button", { class: "quiet", onclick: cancelConfirm }, "Cancel") : null,
      ui.confirming
        ? h("button", { class: "danger", onclick: start }, "Confirm: merge for real")
        : state
          ? h(
              "button",
              { onclick: start, disabled: !canRun, title: state.config_error ? "Fix the settings first" : null },
              running ? "Running…" : ui.busy ? "Starting…" : "Run now"
            )
          : null,
      state && !running ? h("button", { class: "quiet", onclick: refresh, title: "Reload the report" }, "↻") : null,
      !state && ui.problem ? h("button", { class: "quiet", onclick: refresh }, "Retry") : null
    );
  }

  function banner() {
    const state = ui.state;
    const text = state && state.config_error ? `Settings problem: ${state.config_error}` : ui.problem;
    return text ? h("div", { class: "banner bad", title: text }, text) : null;
  }

  function listedCounts(report) {
    const counts = {};
    for (const a of report.actions || []) counts[a.decision] = (counts[a.decision] || 0) + 1;
    return counts;
  }

  function filters(report) {
    const totals = report.totals || {};
    const listed = listedCounts(report);
    const items = [["all", "All listed", (report.actions || []).length, ""]];
    for (const key of ORDER) if (totals[key]) items.push([key, LABELS[key], totals[key], TONE[key] || ""]);
    if (report.errors_total) items.push([PROBLEMS, "Problems", report.errors_total, "bad"]);
    const buttons = items.map(([key, label, n, tone]) =>
      h(
        "button",
        {
          class: "filter",
          "aria-pressed": ui.filter === key ? "true" : "false",
          title: key in listed || key === "all" || key === PROBLEMS ? null : "Counted only, not listed",
          onclick: () => {
            ui.filter = key;
            render(true);
          },
        },
        h("span", { class: "lbl" }, label),
        h("span", { class: `n ${tone}` }, n)
      )
    );
    const meta = [];
    if (totals.examined) meta.push(`${totals.examined} checked`);
    if (ui.state && ui.state.plugin_version) meta.push(`v${ui.state.plugin_version}`);
    return h("div", { class: "side" }, buttons, meta.length ? h("div", { class: "meta" }, meta.join(" · ")) : null);
  }

  function actionRow(action, showTag) {
    const extra = [];
    if (action.mbid) extra.push(`MBID ${short(action.mbid)}`);
    if (action.mbids) extra.push(`MBIDs ${action.mbids.map(short).join(", ")}`);
    if (action.shared_with) extra.push(`also: ${action.shared_with.join(", ")}`);
    if (action.files) extra.push(plural(action.files, "file") + " checked");
    if (action.detail) extra.push(DETAILS[action.detail] || action.detail);
    const name = action.target ? `${action.source} → ${action.target}` : action.source || action.source_id;
    const full = [LABELS[action.decision] || action.decision, name, action.mbid, ...(action.mbids || []), ...extra.slice(1)]
      .filter(Boolean)
      .join("\n");
    return h(
      "div",
      { class: "row", title: full },
      showTag ? h("span", { class: `tag ${TONE[action.decision] || ""}` }, LABELS[action.decision] || action.decision) : null,
      h(
        "span",
        { class: "who" },
        action.source || action.source_id,
        action.target ? [h("span", { class: "arrow" }, "→"), action.target] : null
      ),
      extra.length ? h("span", { class: "sub" }, extra.join(" · ")) : null
    );
  }

  function list(report) {
    const rows = [];
    const totals = report.totals || {};
    if (ui.filter === PROBLEMS) {
      for (const e of report.errors || []) {
        const text = [e.message, e.status ? `HTTP ${e.status}` : ""].filter(Boolean).join(" · ");
        rows.push(h("div", { class: "row", title: text }, h("span", { class: "tag bad" }, e.stage || "error"), h("span", { class: "sub" }, text)));
      }
      if (report.errors_total > (report.errors || []).length) {
        rows.push(h("div", { class: "msg" }, `…and ${report.errors_total - report.errors.length} more in the full report.`));
      }
      return h("div", { class: "list" }, rows);
    }
    const all = ui.filter === "all";
    const actions = (report.actions || []).filter((a) => all || a.decision === ui.filter);
    rows.push(...actions.map((a) => actionRow(a, all)));
    if (!all) {
      const hidden = (totals[ui.filter] || 0) - actions.length;
      if (hidden > 0) {
        const why =
          ui.filter === "no_mbid"
            ? "Artists whose files carry no MBID are only counted."
            : `${hidden} more unchanged since an earlier run (reused from the cache), so only counted.`;
        rows.push(h("div", { class: "msg" }, actions.length ? why : `${why} Nothing new to list.`));
      }
    } else if (!actions.length) {
      rows.push(h("div", { class: "msg" }, "Nothing to act on in the last run."));
    }
    if (all && report.actions_total > (report.actions || []).length) {
      rows.push(h("div", { class: "msg" }, `…and ${report.actions_total - report.actions.length} more in the full report.`));
    }
    return h("div", { class: "list" }, rows);
  }

  function main() {
    const state = ui.state;
    if (!state) return null;
    const report = state.report;
    if (!report) {
      return h(
        "div",
        { class: "msg" },
        state.dry_run === false
          ? "No run yet. Run now merges for real (dry_run is off)."
          : "No run yet. Run now to see what it would merge; nothing is changed in dry run."
      );
    }
    if (ui.filter !== "all" && ui.filter !== PROBLEMS && !(report.totals || {})[ui.filter]) ui.filter = "all";
    if (ui.filter === PROBLEMS && !report.errors_total) ui.filter = "all";
    return h("div", { class: "main" }, filters(report), list(report));
  }

  function render(resetList) {
    const old = view.querySelector(".list");
    const side = view.querySelector(".side");
    const listTop = old && !resetList ? old.scrollTop : 0;
    const sideTop = side ? side.scrollTop : 0;
    const sideLeft = side ? side.scrollLeft : 0;
    view.replaceChildren(...[bar(), banner(), main()].filter(Boolean));
    const fresh = view.querySelector(".list");
    if (fresh) fresh.scrollTop = listTop;
    const freshSide = view.querySelector(".side");
    if (freshSide) {
      freshSide.scrollTop = sideTop;
      freshSide.scrollLeft = sideLeft;
    }
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
        ui.note = report.status === "ok" ? "Run finished." : "Run finished with a problem.";
      } else {
        ui.note = "That run ended without a new report (another run was in progress).";
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
