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
    lbl.appendChild(document.createTextNode(t));
    list.appendChild(lbl);
  });
}

$("load_tables").onclick = async () => {
  setStatus($("tables_status"), "Loading…");
  const r = await api("/api/oracle/tables");
  if (r.error) return setStatus($("tables_status"), r.error, false);
  allTables = r.tables;
  renderTables();
  setStatus($("tables_status"), `${allTables.length} tables`, true);
};
$("filter").oninput = renderTables;
$("select_all").onclick = () => { selection = [...allTables]; renderTables(); };
$("select_none").onclick = () => { selection = []; renderTables(); };
$("save_selection").onclick = async () => {
  const r = await api("/api/selection", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tables: selection }),
  });
  setStatus($("tables_status"), `Saved ${r.count} tables`, true);
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
};

function fmt(n) { return n === null || n === undefined ? "—" : n.toLocaleString(); }

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
      `<td class="num">${fmt(x.diff)}</td><td>${status}</td>`;
    tb.appendChild(tr);
  });
}

$("compare").onclick = async () => {
  if (!selection.length) return setStatus($("migrate_status"), "No tables selected", false);
  setStatus($("migrate_status"), "Counting…");
  const r = await api("/api/compare?tables=" + encodeURIComponent(selection.join(",")));
  if (r.error) return setStatus($("migrate_status"), r.error, false);
  renderCompareRows(r.rows);
  setStatus($("migrate_status"), "Compared", true);
};

$("sync_selected").onclick = () => {
  if (!selection.length) return setStatus($("migrate_status"), "No tables selected", false);
  $("sync_selected").disabled = true;
  setStatus($("migrate_status"), "Syncing…");
  logLine(`\n=== sync started: ${new Date().toLocaleTimeString()} ===`);
  const es = new EventSource("/api/sync?tables=" + encodeURIComponent(selection.join(",")));
  es.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "table_start") logLine(`→ ${m.table}: loading…`);
    else if (m.type === "progress") setStatus($("migrate_status"), `${m.table}: ${fmt(m.rows)} rows`);
    else if (m.type === "table_done")
      logLine(`✓ ${m.table}: ${fmt(m.rows)} rows  (oracle ${fmt(m.oracle)} / pg ${fmt(m.pg)}) ${m.match ? "MATCH" : "MISMATCH"}`);
    else if (m.type === "table_error") logLine(`✗ ${m.table}: ${m.error}`);
    else if (m.type === "fatal") { logLine(`FATAL: ${m.error}`); es.close(); $("sync_selected").disabled = false; }
    else if (m.type === "all_done") {
      logLine("=== sync complete ===");
      es.close();
      $("sync_selected").disabled = false;
      setStatus($("migrate_status"), "Sync complete", true);
    }
  };
  es.onerror = () => { es.close(); $("sync_selected").disabled = false; setStatus($("migrate_status"), "Stream closed", false); };
};

loadConfig();
