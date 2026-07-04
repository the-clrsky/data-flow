"use strict";

const $ = (id) => document.getElementById(id);
const api = (url, opts) => fetch(url, opts).then((r) => r.json());

// ---------- tabs ----------
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $(t.dataset.tab).classList.add("active");
    if (t.dataset.tab === "columns") loadMappingSummary();
    if (t.dataset.tab === "migrate") { refreshCompare(); refreshDriftPanel(); refreshCheckpointPanel(); }
    if (t.dataset.tab === "history") loadHistory();
  };
});

function setStatus(el, msg, ok) {
  el.textContent = msg;
  el.className = "status " + (ok === true ? "ok" : ok === false ? "err" : "");
}

// ---------- config ----------
async function loadConfig() {
  const c = await api("/api/config");
  if (c.oracle) {
    o_host.value = c.oracle.host || "";
    o_port.value = c.oracle.port || 1521;
    o_service.value = c.oracle.service || "";
    o_user.value = c.oracle.user || "";
    o_schema.value = c.oracle.schema || "";
  }
  if (c.pg) {
    p_host.value = c.pg.host || "";
    p_port.value = c.pg.port || 5432;
    p_dbname.value = c.pg.dbname || "";
    p_user.value = c.pg.user || "";
    p_target_schema.value = c.pg.target_schema || "public";
  }
  selection = c.selection || [];
}

$("o_save").onclick = async () => {
  const body = {
    host: o_host.value, port: +o_port.value, service: o_service.value,
    user: o_user.value, password: o_password.value, schema: o_schema.value || null,
  };
  const r = await api("/api/config/oracle", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  setStatus($("o_status"), r.ok ? "Saved" : "Error", r.ok);
};

$("p_save").onclick = async () => {
  const body = {
    host: p_host.value, port: +p_port.value, dbname: p_dbname.value,
    user: p_user.value, password: p_password.value, target_schema: p_target_schema.value || "public",
  };
  const r = await api("/api/config/pg", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  setStatus($("p_status"), r.ok ? "Saved" : "Error", r.ok);
};

$("o_test").onclick = async () => {
  setStatus($("o_status"), "Testing…");
  const r = await api("/api/test/oracle", { method: "POST" });
  setStatus($("o_status"), r.ok ? "Connected" : r.error, r.ok);
};
$("p_test").onclick = async () => {
  setStatus($("p_status"), "Testing…");
  const r = await api("/api/test/pg", { method: "POST" });
  setStatus($("p_status"), r.ok ? "Connected" : r.error, r.ok);
};

// ---------- tables ----------
let allTables = [];
let selection = [];
let rowCounts = {};

function renderTables() {
  const f = $("filter").value.toUpperCase();
  const list = $("table_list");
  list.innerHTML = "";
  allTables.filter((t) => t.includes(f)).forEach((t) => {
    const lbl = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = t;
    cb.checked = selection.includes(t);
    cb.onchange = () => {
      if (cb.checked) selection.push(t);
      else selection = selection.filter((x) => x !== t);
    };
    lbl.appendChild(cb);
    const info = document.createElement("span");
    info.className = "table-info";
    const name = document.createElement("span");
    name.className = "table-name";
    name.textContent = t;
    info.appendChild(name);
    const rc = rowCounts[t];
    if (rc !== undefined) {
      const badge = document.createElement("span");
      badge.className = "badge muted";
      badge.textContent = rc === null ? "no stats" : `~${fmt(rc)} rows`;
      info.appendChild(badge);
    }
    lbl.appendChild(info);
    list.appendChild(lbl);
  });
}

$("load_tables").onclick = async () => {
  setStatus($("tables_status"), "Loading…");
  const r = await api("/api/oracle/tables");
  if (r.error) return setStatus($("tables_status"), r.error, false);
  allTables = r.tables;
  rowCounts = {};
  renderTables();
  setStatus($("tables_status"), `${allTables.length} tables`, true);
};
$("filter").oninput = renderTables;
$("select_all").onclick = () => { selection = [...allTables]; renderTables(); };
$("select_none").onclick = () => { selection = []; renderTables(); };
$("show_row_counts").onclick = async () => {
  if (!allTables.length) return setStatus($("tables_status"), "Load tables first", false);
  setStatus($("tables_status"), "Fetching row count estimates…");
  const r = await api("/api/oracle/row-count-estimates");
  if (r.error) return setStatus($("tables_status"), r.error, false);
  rowCounts = r.counts;
  renderTables();
  setStatus($("tables_status"), "Row counts are estimates from table statistics (may be stale)", true);
};
$("save_selection").onclick = async () => {
  const r = await api("/api/selection", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tables: selection }),
  });
  setStatus($("tables_status"), `Saved ${r.count} tables`, true);
};

// ---------- column mapping ----------
let currentColTable = null;
let pgTypeChoices = [];

function typeLabel(c) {
  if (c.dtype === "NUMBER") {
    if (c.prec == null) return "NUMBER";
    return c.scale ? `NUMBER(${c.prec},${c.scale})` : `NUMBER(${c.prec})`;
  }
  return c.dtype;
}

function mappingBadges(row) {
  const badges = [];
  if (row.error) return [`<span class="badge err">${row.error}</span>`];
  if (row.renamed) badges.push(`<span class="badge">${row.renamed} renamed</span>`);
  if (row.skipped) badges.push(`<span class="badge warn">${row.skipped} skipped</span>`);
  if (row.drift) badges.push(`<span class="badge err">mapping changed</span>`);
  if (!row.renamed && !row.skipped) badges.push('<span class="badge muted">default</span>');
  if (row.fast_load) badges.push('<span class="badge">fast load</span>');
  return badges;
}

async function loadMappingSummary() {
  const list = $("mapping_summary");
  list.innerHTML = '<p class="status">Loading…</p>';
  const q = selection.length ? "?tables=" + encodeURIComponent(selection.join(",")) : "";
  const r = await api("/api/mappings" + q);
  if (r.error) {
    list.innerHTML = `<p class="status err">${r.error}</p>`;
    return;
  }
  if (!r.rows.length) {
    list.innerHTML = '<p class="status">No tables selected yet. Pick tables on the Tables tab and save your selection.</p>';
    return;
  }
  list.innerHTML = "";
  r.rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "mapping-item" + (row.table === currentColTable ? " active" : "");
    item.innerHTML =
      `<div class="mapping-item-name">${row.table} &rarr; ${row.pg_table}</div>` +
      `<div class="mapping-item-badges">${mappingBadges(row).join(" ")}</div>`;
    item.onclick = () => loadColumns(row.table);
    list.appendChild(item);
  });
}

function renderColumnGrid(cols) {
  const tb = $("col_grid").querySelector("tbody");
  tb.innerHTML = "";
  cols.forEach((c) => {
    const tr = document.createElement("tr");

    const tdName = document.createElement("td");
    tdName.textContent = c.name;

    const tdType = document.createElement("td");
    tdType.textContent = typeLabel(c);

    const tdTarget = document.createElement("td");
    const targetInput = document.createElement("input");
    targetInput.value = c.target;
    targetInput.dataset.name = c.name;
    targetInput.className = "col-target";
    targetInput.disabled = c.skip;
    tdTarget.appendChild(targetInput);

    const tdPgType = document.createElement("td");
    const typeSelect = document.createElement("select");
    typeSelect.className = "col-pgtype";
    typeSelect.dataset.name = c.name;
    typeSelect.disabled = c.skip;
    const defaultOpt = document.createElement("option");
    defaultOpt.value = "";
    defaultOpt.textContent = `Default (${c.default_pg_type})`;
    typeSelect.appendChild(defaultOpt);
    pgTypeChoices.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = t;
      typeSelect.appendChild(opt);
    });
    typeSelect.value = c.pg_type_override || "";
    tdPgType.appendChild(typeSelect);

    const tdSkip = document.createElement("td");
    const skipCb = document.createElement("input");
    skipCb.type = "checkbox";
    skipCb.checked = c.skip;
    skipCb.className = "col-skip";
    skipCb.dataset.name = c.name;
    skipCb.onchange = () => {
      targetInput.disabled = skipCb.checked;
      typeSelect.disabled = skipCb.checked;
    };
    tdSkip.appendChild(skipCb);

    tr.append(tdName, tdType, tdTarget, tdPgType, tdSkip);
    tb.appendChild(tr);
  });
}

async function loadColumns(table) {
  currentColTable = table;
  $("col_table_title").textContent = table;
  setStatus($("columns_status"), "Loading…");
  const r = await api("/api/table/" + encodeURIComponent(table) + "/columns");
  if (r.error) {
    setStatus($("columns_status"), r.error, false);
    $("col_grid").querySelector("tbody").innerHTML = "";
    $("col_pg_table").value = "";
    $("col_pg_table").disabled = true;
    $("col_fast_load").checked = false;
    $("col_fast_load").disabled = true;
    $("col_save").disabled = true;
    return;
  }
  $("col_pg_table").value = r.pg_table;
  $("col_pg_table").disabled = false;
  $("col_fast_load").checked = !!r.fast_load;
  $("col_fast_load").disabled = false;
  $("col_save").disabled = false;
  pgTypeChoices = r.pg_type_choices || [];
  renderColumnGrid(r.columns);
  setStatus($("columns_status"), "", null);
  loadMappingSummary();
}

$("mapping_refresh").onclick = loadMappingSummary;

$("col_save").onclick = async () => {
  if (!currentColTable) return;
  const rows = [...$("col_grid").querySelectorAll("tbody tr")];
  const columns = rows.map((tr) => ({
    name: tr.querySelector(".col-target").dataset.name,
    target: tr.querySelector(".col-target").value,
    skip: tr.querySelector(".col-skip").checked,
    pg_type: tr.querySelector(".col-pgtype").value || null,
  }));
  const body = {
    pg_table: $("col_pg_table").value || null, columns,
    fast_load: $("col_fast_load").checked,
  };
  await api("/api/table/" + encodeURIComponent(currentColTable) + "/mapping", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  setStatus($("columns_status"), "Saved", true);
  loadMappingSummary();
};

// ---------- migrate / compare / sync ----------
function logLine(s) {
  const log = $("log");
  log.textContent += s + "\n";
  log.scrollTop = log.scrollHeight;
}

$("create_tables").onclick = async () => {
  if (!selection.length) return setStatus($("migrate_status"), "No tables selected", false);
  setStatus($("migrate_status"), "Creating…");
  const r = await api("/api/create-tables", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tables: selection }),
  });
  if (r.error) return setStatus($("migrate_status"), r.error, false);
  r.results.forEach((x) =>
    logLine(`[create] ${x.table} → ${x.pg_table}: ${x.status}${x.error ? " — " + x.error : ""}`)
  );
  const errs = r.results.filter((x) => x.status === "error").length;
  setStatus($("migrate_status"), errs ? `${errs} errors (see log)` : "Tables ready", !errs);
  refreshDriftPanel();
};

// ---------- schema drift / recreate ----------
let driftTables = new Set();

function renderDriftPanel() {
  const panel = $("drift_panel");
  panel.innerHTML = "";
  driftTables.forEach((t) => {
    const div = document.createElement("div");
    div.className = "drift-item";
    const msg = document.createElement("span");
    msg.textContent = `${t}: mapping has changed since it was created. Recreate to apply changes — this drops and reloads all data.`;
    const btn = document.createElement("button");
    btn.className = "secondary";
    btn.textContent = `Recreate ${t}`;
    btn.onclick = () => recreateAndSync(t);
    div.append(msg, btn);
    panel.appendChild(div);
  });
}

async function refreshDriftPanel() {
  if (!selection.length) { driftTables = new Set(); renderDriftPanel(); return; }
  const r = await api("/api/mappings?tables=" + encodeURIComponent(selection.join(",")));
  if (r.error) return;
  driftTables = new Set(r.rows.filter((x) => x.drift).map((x) => x.table));
  renderDriftPanel();
}

async function recreateAndSync(table) {
  logLine(`\n=== recreating ${table}: ${new Date().toLocaleTimeString()} ===`);
  const r = await api("/api/recreate-tables", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tables: [table] }),
  });
  if (r.error) return logLine(`FATAL: ${r.error}`);
  r.results.forEach((x) =>
    logLine(`[recreate] ${x.table} → ${x.pg_table}: ${x.status}${x.error ? " — " + x.error : ""}`)
  );
  driftTables.delete(table);
  renderDriftPanel();
  runSync([table]);
}

function fmt(n) { return n === null || n === undefined ? "—" : n.toLocaleString(); }

let lastCompareRows = [];

function isMismatch(x) {
  return !x.error && x.pg !== null && !x.match;
}

function renderCompareRows(rows) {
  const tb = $("compare_table").querySelector("tbody");
  tb.innerHTML = "";
  rows.forEach((x) => {
    const tr = document.createElement("tr");
    let status = '<span class="pending">not created</span>';
    if (x.error) status = `<span class="mismatch">${x.error}</span>`;
    else if (x.pg !== null) status = x.match
      ? '<span class="match">match</span>'
      : '<span class="mismatch">mismatch</span>';
    tr.innerHTML =
      `<td>${x.table}</td><td>${x.pg_table}</td>` +
      `<td class="num">${fmt(x.oracle)}</td><td class="num">${fmt(x.pg)}</td>` +
      `<td class="num">${fmt(x.diff)}</td><td>${status}</td><td></td>`;
    const btn = document.createElement("button");
    btn.className = "secondary row-sync-btn";
    btn.textContent = "Sync";
    btn.onclick = () => runSync([x.table]);
    tr.lastElementChild.appendChild(btn);
    tb.appendChild(tr);
  });
}

async function refreshCompare() {
  if (!selection.length) return setStatus($("migrate_status"), "No tables selected", false);
  setStatus($("migrate_status"), "Counting…");
  const r = await api("/api/compare?tables=" + encodeURIComponent(selection.join(",")));
  if (r.error) return setStatus($("migrate_status"), r.error, false);
  lastCompareRows = r.rows;
  renderCompareRows(r.rows);
  setStatus($("migrate_status"), "Compared", true);
}

$("compare").onclick = refreshCompare;

function setSyncControlsDisabled(disabled) {
  $("sync_selected").disabled = disabled;
  $("sync_mismatched").disabled = disabled;
  document.querySelectorAll(".row-sync-btn").forEach((b) => { b.disabled = disabled; });
}

function fmtEta(sec) {
  if (sec === null || sec === undefined) return "";
  if (sec < 60) return `${Math.ceil(sec)}s`;
  const m = Math.floor(sec / 60), s = Math.ceil(sec % 60);
  return `${m}m ${s}s`;
}

function runSync(tables, freshTables) {
  setSyncControlsDisabled(true);
  setStatus($("migrate_status"), "Syncing…");
  logLine(`\n=== sync started: ${new Date().toLocaleTimeString()} ===`);
  let url = "/api/sync?tables=" + encodeURIComponent(tables.join(","));
  if (freshTables && freshTables.length) url += "&fresh=" + encodeURIComponent(freshTables.join(","));
  const es = new EventSource(url);
  es.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "retry")
      logLine(`⟳ ${m.label}: ${m.error} — retrying in ${m.delay}s (attempt ${m.attempt}/${m.max_retries})…`);
    else if (m.type === "preflight_error")
      logLine(`✗ pre-flight [${m.check}${m.table ? " " + m.table : ""}]: ${m.error}`);
    else if (m.type === "resuming")
      logLine(`↻ ${m.table}: resuming from row ${fmt(m.offset)}`);
    else if (m.type === "table_start") logLine(`→ ${m.table}: loading…`);
    else if (m.type === "progress") {
      const rate = m.rows_per_sec ? `${fmt(Math.round(m.rows_per_sec))} rows/s` : "";
      const eta = m.eta_seconds != null ? `ETA ${fmtEta(m.eta_seconds)}` : "";
      const skipped = m.rows_skipped ? ` (${fmt(m.rows_skipped)} skipped)` : "";
      setStatus($("migrate_status"), `${m.table}: ${fmt(m.rows)} rows${skipped} — ${rate} ${eta}`.trim());
    }
    else if (m.type === "table_done") {
      const skipped = m.rows_skipped ? `, ${fmt(m.rows_skipped)} skipped (see History for detail)` : "";
      logLine(`✓ ${m.table}: ${fmt(m.rows_loaded)} rows loaded${skipped}  (oracle ${fmt(m.oracle)} / pg ${fmt(m.pg)}) ${m.match ? "MATCH" : "MISMATCH"}`);
      checkpointTables.delete(m.table);
      renderCheckpointPanel();
    }
    else if (m.type === "table_error") {
      logLine(`✗ ${m.table}: ${m.error}`);
      if (m.mapping_drift) { driftTables.add(m.table); renderDriftPanel(); }
    }
    else if (m.type === "constraints_dropped")
      logLine(`⚙ ${m.table}: dropped ${m.ok} constraint(s)/index(es) for fast sync${m.failed ? ` (${m.failed} failed to drop, see below)` : ""}`);
    else if (m.type === "constraints_restored")
      logLine(`⚙ ${m.table}: restored ${m.ok} constraint(s)/index(es)${m.failed ? ` — ${m.failed} FAILED, table may be under-constrained` : ""}`);
    else if (m.type === "constraint_error")
      logLine(`✗ ${m.table} [${m.kind}${m.name ? " " + m.name : ""}]: ${m.error}`);
    else if (m.type === "fatal") { logLine(`FATAL: ${m.error}`); es.close(); setSyncControlsDisabled(false); }
    else if (m.type === "all_done") {
      logLine("=== sync complete ===");
      es.close();
      setSyncControlsDisabled(false);
      setStatus($("migrate_status"), "Sync complete", true);
    }
  };
  es.onerror = () => { es.close(); setSyncControlsDisabled(false); setStatus($("migrate_status"), "Stream closed", false); };
}

$("sync_selected").onclick = () => {
  if (!selection.length) return setStatus($("migrate_status"), "No tables selected", false);
  runSync(selection);
};

$("sync_mismatched").onclick = () => {
  const mismatched = lastCompareRows.filter(isMismatch).map((x) => x.table);
  if (!mismatched.length) return setStatus($("migrate_status"), "No mismatched tables — run Compare first", false);
  runSync(mismatched);
};

// ---------- resume checkpoints ----------
let checkpointTables = new Set();
let checkpointInfo = {};

function renderCheckpointPanel() {
  const panel = $("checkpoint_panel");
  panel.innerHTML = "";
  checkpointTables.forEach((t) => {
    const info = checkpointInfo[t] || {};
    const div = document.createElement("div");
    div.className = "checkpoint-item";
    const msg = document.createElement("span");
    msg.textContent = `${t}: interrupted sync — ${fmt(info.last_offset)} rows loaded so far. Syncing again resumes from there.`;
    const actions = document.createElement("div");
    actions.className = "actions";
    const freshBtn = document.createElement("button");
    freshBtn.className = "secondary";
    freshBtn.textContent = "Start fresh";
    freshBtn.onclick = () => runSync([t], [t]);
    actions.appendChild(freshBtn);
    div.append(msg, actions);
    panel.appendChild(div);
  });
}

async function refreshCheckpointPanel() {
  if (!selection.length) { checkpointTables = new Set(); checkpointInfo = {}; renderCheckpointPanel(); return; }
  const r = await api("/api/checkpoints?tables=" + encodeURIComponent(selection.join(",")));
  if (r.error) return;
  checkpointTables = new Set(r.rows.map((x) => x.table));
  checkpointInfo = Object.fromEntries(r.rows.map((x) => [x.table, x]));
  renderCheckpointPanel();
}

// ---------- dry run ----------
function renderDryRun(rows) {
  const panel = $("dryrun_panel");
  panel.innerHTML = "";
  rows.forEach((r) => {
    const card = document.createElement("div");
    card.className = "dryrun-card";
    if (r.error) {
      card.innerHTML = `<h4>${r.table}</h4><p class="status err">${r.error}</p>`;
      panel.appendChild(card);
      return;
    }
    const meta = document.createElement("div");
    meta.className = "dryrun-meta";
    meta.innerHTML =
      `<span>Oracle rows: ${fmt(r.oracle_rows)}</span>` +
      `<span>PG rows: ${r.exists ? fmt(r.pg_rows) : "table not created yet"}</span>` +
      (r.drift ? `<span class="mismatch">mapping drift — recreate would change DDL</span>` : "") +
      (r.resumable_from != null ? `<span>resumable from row ${fmt(r.resumable_from)}</span>` : "");
    const heading = document.createElement("h4");
    heading.textContent = `${r.table} → ${r.pg_table}`;
    const pre = document.createElement("pre");
    pre.textContent = r.create_ddl;
    card.append(heading, meta, pre);
    if (r.fast_load_drop) {
      const dropNote = document.createElement("div");
      const counts = Object.entries(r.fast_load_drop)
        .filter(([, v]) => v.length)
        .map(([k, v]) => `${v.length} ${k}`)
        .join(", ");
      dropNote.className = "status warn";
      dropNote.textContent = counts
        ? `Fast load would drop & recreate: ${counts}`
        : "Fast load enabled — no constraints/indexes currently found to drop";
      card.appendChild(dropNote);
    }
    panel.appendChild(card);
  });
}

$("dry_run").onclick = async () => {
  if (!selection.length) return setStatus($("migrate_status"), "No tables selected", false);
  setStatus($("migrate_status"), "Computing dry run…");
  const r = await api("/api/dry-run?tables=" + encodeURIComponent(selection.join(",")));
  if (r.error) return setStatus($("migrate_status"), r.error, false);
  renderDryRun(r.rows);
  setStatus($("migrate_status"), "Dry run ready — nothing was executed", true);
};

// ---------- sync history ----------
function fmtDate(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "—"; }
function fmtDuration(sec) {
  if (sec === null || sec === undefined) return "—";
  return sec < 60 ? `${sec.toFixed(1)}s` : fmtEta(sec);
}

async function loadHistory() {
  setStatus($("history_status"), "Loading…");
  const r = await api("/api/sync-runs");
  const tb = $("history_table").querySelector("tbody");
  tb.innerHTML = "";
  (r.runs || []).forEach((run) => {
    const tr = document.createElement("tr");
    const statusClass = run.status === "success" ? "match" : run.status === "failed" ? "mismatch" : "pending";
    tr.innerHTML =
      `<td>${run.table}</td><td>${run.pg_table || "—"}</td>` +
      `<td>${fmtDate(run.started_at)}</td><td class="num">${fmtDuration(run.duration)}</td>` +
      `<td class="num">${fmt(run.rows_loaded)}</td><td class="num">${fmt(run.rows_skipped)}</td>` +
      `<td><span class="${statusClass}">${run.status}</span></td><td></td>`;
    const actions = tr.lastElementChild;
    if (run.rows_skipped) {
      const errBtn = document.createElement("button");
      errBtn.className = "link";
      errBtn.textContent = "View errors";
      errBtn.onclick = () => toggleRunErrors(run.id, tr);
      actions.appendChild(errBtn);
    }
    const dl = document.createElement("a");
    dl.href = `/api/sync-runs/${run.id}/export`;
    dl.textContent = "Download";
    dl.className = "link";
    dl.style.marginLeft = "10px";
    actions.appendChild(dl);
    tb.appendChild(tr);
  });
  setStatus($("history_status"), `${(r.runs || []).length} runs`, true);
}

async function toggleRunErrors(runId, afterRow) {
  const existing = afterRow.nextElementSibling;
  if (existing && existing.classList.contains("errors-row")) {
    existing.remove();
    return;
  }
  const r = await api(`/api/sync-runs/${runId}/errors`);
  const tr = document.createElement("tr");
  tr.className = "errors-row";
  const td = document.createElement("td");
  td.colSpan = 8;
  const detail = document.createElement("div");
  detail.className = "errors-detail";
  detail.innerHTML = (r.errors || [])
    .map((e) => `<div>row ${e.row_ref}: ${e.error}</div>`)
    .join("") || "<div>No error detail found.</div>";
  td.appendChild(detail);
  tr.appendChild(td);
  afterRow.after(tr);
}

$("history_refresh").onclick = loadHistory;

loadConfig();
