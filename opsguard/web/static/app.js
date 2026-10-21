"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  info: null, token: null, me: null, policy: null, overview: null, overviewKey: "",
  approvals: [], audit: [], lastAuditId: 0, busy: false,
  page: "overview", openHosts: new Set(), serverFilter: "all", approvalFilter: "pending",
  spotlight: null, tour: -1, tourDone: false,
};

const PAGES = {
  overview: "Overview", servers: "Servers", cluster: "Cluster", approvals: "Approvals",
  audit: "Audit log", access: "Access", integrations: "Integrations",
};
// status -> [css class, icon, word]
const VERDICT = {
  ok: ["ok", "✓", "Allowed"],
  pending_approval: ["held", "⏸", "Held"],
  denied: ["blocked", "✕", "Blocked"],
  rate_limited: ["blocked", "✕", "Too fast"],
  error: ["error", "!", "Failed"],
};
const AUDIT = {
  executed: ["ok", "✓", ""],
  denied: ["blocked", "✕", ""],
  approval_requested: ["held", "⏸", ""],
  approval_granted: ["ok", "✓", "Approved: "],
  approval_rejected: ["blocked", "✕", "Rejected: "],
  rate_limited: ["blocked", "✕", "Too fast: "],
  error: ["error", "!", ""],
};
const VERBS = {
  list_hosts: "List servers", host_overview: "Check", list_services: "List services", service_logs: "Logs",
  restart_service: "Restart", cleanup_logs: "Clean up logs", reboot_host: "Reboot",
  list_containers: "List containers", container_logs: "Logs", restart_container: "Restart", remove_container: "Remove",
  cluster_status: "Check cluster", list_jobs: "List jobs", job_status: "Check job", cancel_job: "Cancel job",
  drain_node: "Drain", resume_node: "Resume", approval_status: "Check approval",
};
const ACCESS_ICON = { allow: "✓", approval: "⏸", deny: "✕" };
const CONNECTOR_INFO = {
  linux: ["🐧", "Linux hosts", "Health, services, logs, cleanup, reboot"],
  docker: ["🐳", "Docker", "Containers, logs, restart, remove"],
  slurm: ["🖥", "Slurm", "Nodes, jobs, drain, cancel"],
};

// ---------- helpers ----------

function storageGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode etc. */ }
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, { ...options, headers: { ...headers, ...(options.headers || {}) } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
  return body;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");
const time = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const CHEV = '<svg class="chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';

// Minimal markdown for the assistant's replies: code blocks, inline code, bold, bullet lists.
function renderMarkdown(text) {
  const blocks = esc(text).split(/```(?:\w+)?\n?([\s\S]*?)```/g);
  return blocks.map((chunk, i) => {
    if (i % 2 === 1) return `<pre>${chunk.replace(/\n$/, "")}</pre>`;
    return chunk.split(/\n{2,}/).filter((p) => p.trim()).map((para) => {
      const inline = (s) => s
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|\W)_([^_]+)_(?=\W|$)/g, "$1<em>$2</em>");
      const lines = para.trim().split("\n");
      const head = lines.filter((l) => !/^\s*- /.test(l));
      const items = lines.filter((l) => /^\s*- /.test(l));
      let html = head.length ? `<p>${head.map(inline).join("<br>")}</p>` : "";
      if (items.length) html += `<ul>${items.map((l) => `<li>${inline(l.replace(/^\s*- /, ""))}</li>`).join("")}</ul>`;
      return html;
    }).join("");
  }).join("");
}

// "restart_container {host: web-01, container: nginx}" -> "Restart nginx" + "web-01"
function phrase(tool, args) {
  const a = args || {};
  const thing = a.container || a.service || a.job_id || a.node || "";
  return { what: `${VERBS[tool] || tool}${thing ? ` ${thing}` : ""}`, where: a.host || "" };
}

function phraseHtml(tool, args) {
  const p = phrase(tool, args);
  return `${esc(p.what)}${p.where ? ` <span class="on">${esc(p.where)}</span>` : ""}`;
}

// The guard's reasons are precise but long; show the gist.
function shortReason(status, message) {
  const m = message || "";
  if (/protected target/.test(m)) return "Protected server: needs an admin";
  if (status === "pending_approval") return "Risky: needs an admin";
  if (/belongs to/.test(m)) return "Not your job";
  const role = m.match(/role '(\w+)' has 'deny'/);
  if (role) return `${cap(role[1])}s can't do this`;
  if (/override/.test(m)) return "Switched off by an admin";
  if (status === "rate_limited") return "Too many requests, wait a minute";
  return m.replace(/^Not allowed: /, "").split(/[.;]/)[0];
}

function riskOf(tool) {
  const t = state.me && state.me.tools.find((x) => x.name === tool);
  return t ? t.risk : "read";
}

function roleOf(name) {
  const u = state.info && state.info.demo_users.find((x) => x.name === name);
  return u ? u.role : (state.me && state.me.name === name ? state.me.role : "");
}

function avatar(name, cls = "avatar") {
  return `<span class="${cls} av-${esc(roleOf(name))}" title="${esc(name)}">${esc(name.slice(0, 1).toUpperCase())}</span>`;
}

function pct(p) {
  if (p == null) return '<span class="muted">-</span>';
  const cls = p >= 90 ? "bad" : p >= 75 ? "warn" : "";
  return `<span class="mini-bar"><span class="bar"><span class="fill ${cls}" style="width:${Math.min(100, p)}%"></span></span><span class="pct">${Math.round(p)}%</span></span>`;
}

const isWide = () => window.matchMedia("(min-width: 1201px)").matches;

// ---------- action buttons ----------

function actButton(label, tool, args, access) {
  const key = [tool, ...Object.values(args)].join(":");
  const gate = access && access !== "allow" ? `<span class="gate ${access}">${ACCESS_ICON[access]}</span>` : "";
  const tip = access === "deny" ? "You can't do this. Click to see it blocked."
    : access === "approval" ? "Needs an admin's approval" : "";
  const b = el(`<button type="button" class="act risk-${riskOf(tool)} ${access === "deny" ? "denied" : ""}"
    data-key="${esc(key)}" title="${esc(tip)}">${esc(label)}${gate}</button>`);
  b.onclick = (e) => { e.stopPropagation(); runTool(tool, args); };
  return b;
}

function aiButton(label, question, key) {
  const b = el(`<button type="button" class="act ai" data-key="${esc(key)}">${esc(label)}</button>`);
  b.onclick = (e) => { e.stopPropagation(); askAI(question); };
  return b;
}

// ---------- routing ----------

function route() {
  const page = (location.hash.replace(/^#\//, "") || "overview").split("?")[0];
  state.page = PAGES[page] ? page : "overview";
  $("page-title").textContent = PAGES[state.page];
  document.title = `${PAGES[state.page]} · OpsGuard`;
  for (const a of document.querySelectorAll(".nav a")) a.classList.toggle("active", a.dataset.page === state.page);
  for (const p of document.querySelectorAll(".page")) p.hidden = p.id !== `page-${state.page}`;
  renderPage();
}

function go(page) {
  if (location.hash !== `#/${page}`) location.hash = `#/${page}`;
  else route();
}

function renderPage() {
  renderApprovalBanner();
  const p = state.page;
  if (p === "overview") renderOverviewPage();
  else if (p === "servers") renderServers();
  else if (p === "cluster") renderCluster();
  else if (p === "approvals") renderApprovals();
  else if (p === "audit") renderAudit();
  else if (p === "access") renderAccess();
  else if (p === "integrations") renderIntegrations();
  applySpotlight();
}

// ---------- overview ----------

function hostHealth(h) {
  const broken = (h.containers || []).some((c) => ["exited", "dead", "restarting"].includes(c.state))
    || (h.services || []).some((s) => ["failed", "exited", "stopped"].includes(s.state));
  if (broken) return ["bad", "Down"];
  if (h.disks.some((d) => d.used_percent >= 90)) return ["warn", "Disk full"];
  return ["ok", "Healthy"];
}

function renderOverviewPage() {
  const o = state.overview;
  if (!o) return;
  const hosts = o.hosts;
  const problems = hosts.filter((h) => hostHealth(h)[0] !== "ok").length;
  const waiting = state.approvals.filter((r) => r.status === "pending").length;
  const tiles = [
    ["servers", "Servers", hosts.length, ""],
    ["servers", "With problems", problems, problems ? "bad" : "ok"],
    ["approvals", "Waiting approval", waiting, waiting ? "warn" : ""],
  ];
  if (o.slurm) {
    tiles.push(["cluster", "Jobs running", o.slurm.jobs.filter((j) => j.state === "RUNNING").length, ""]);
  }
  $("stats").innerHTML = tiles.map(([page, label, value, cls]) =>
    `<a class="stat" href="#/${page}"><div class="label">${label}</div><div class="value ${cls}">${value}</div></a>`).join("");

  const ul = $("incidents");
  ul.innerHTML = "";
  if (!o.incidents.length) {
    ul.appendChild(el('<li class="none"><span class="title">✓ All good</span></li>'));
    return;
  }
  const hostNames = new Set(hosts.map((h) => h.host));
  const seen = new Set();
  for (const inc of o.incidents) {
    const li = el(`<li class="${esc(inc.severity)}"><span class="title">${esc(inc.title)}</span></li>`);
    if (hostNames.has(inc.target) && !seen.has(inc.target)) {
      seen.add(inc.target);
      li.appendChild(aiButton("Ask AI why", `Why is the website down on ${inc.target}?`, `ai-incident:${inc.target}`));
    }
    ul.appendChild(li);
  }
}

// ---------- servers ----------

function unitRow(name, st, buttons) {
  const bad = ["exited", "dead", "failed", "restarting", "stopped"].includes(st);
  const row = el(`<div class="unit">
    <span class="dot ${bad ? "bad" : st === "running" || st === "active" ? "ok" : "warn"}" title="${esc(st)}"></span>
    <span class="grow"><span class="uname">${esc(name)}</span>${bad ? `<span class="ustate bad">${esc(st)}</span>` : ""}</span>
    <span class="btns"></span></div>`);
  const box = row.querySelector(".btns");
  for (const b of buttons) box.appendChild(b);
  return row;
}

function hostDetail(h) {
  const a = h.access;
  const wrap = el(`<div class="detail-grid">
    <div><h4>Containers</h4><div class="units" data-k="c"></div></div>
    <div><h4>Services</h4><div class="units" data-k="s"></div></div>
    <div><h4>Actions</h4><div class="host-actions"></div></div>
  </div>`);
  const cBox = wrap.querySelector('[data-k="c"]');
  for (const c of h.containers || []) {
    cBox.appendChild(unitRow(c.name, c.state, [
      actButton("Logs", "container_logs", { host: h.host, container: c.name }, a.container_logs),
      actButton("Restart", "restart_container", { host: h.host, container: c.name }, a.restart_container),
      actButton("Remove", "remove_container", { host: h.host, container: c.name }, a.remove_container),
    ]));
  }
  if (!cBox.children.length) cBox.appendChild(el('<span class="muted">None</span>'));
  const sBox = wrap.querySelector('[data-k="s"]');
  for (const s of (h.services || []).slice(0, 50)) {
    sBox.appendChild(unitRow(s.name, s.state, [
      actButton("Logs", "service_logs", { host: h.host, service: s.name }, a.service_logs),
      actButton("Restart", "restart_service", { host: h.host, service: s.name }, a.restart_service),
    ]));
  }
  if (!sBox.children.length) sBox.appendChild(el('<span class="muted">None</span>'));
  const actions = wrap.querySelector(".host-actions");
  actions.appendChild(aiButton("Ask AI", `What's wrong on ${h.host}?`, `ai:${h.host}`));
  for (const d of h.log_dirs || []) {
    const size = d.size_gb != null && d.size_gb >= 1 ? ` ${d.size_gb} GB` : "";
    actions.appendChild(actButton(`Clean logs${size}`, "cleanup_logs", { host: h.host, path: d.path }, a.cleanup_logs));
  }
  actions.appendChild(actButton("Reboot", "reboot_host", { host: h.host }, a.reboot_host));
  return wrap;
}

function renderServers() {
  const o = state.overview;
  if (!o) return;
  const q = $("server-search").value.trim().toLowerCase();
  const rows = o.hosts.filter((h) => {
    const [health] = hostHealth(h);
    if (state.serverFilter === "bad" && health === "ok") return false;
    if (state.serverFilter === "ok" && health !== "ok") return false;
    return !q || h.host.toLowerCase().includes(q) || (h.os || "").toLowerCase().includes(q);
  });
  const tbody = $("servers");
  tbody.innerHTML = "";
  for (const h of rows) {
    const [health, label] = hostHealth(h);
    const fullest = h.disks.reduce((m, d) => (!m || d.used_percent > m.used_percent ? d : m), null);
    const open = state.openHosts.has(h.host);
    const tr = el(`<tr class="row-link ${open ? "open" : ""}" data-host="${esc(h.host)}">
      <td><span class="name-cell"><span class="dot ${health}"></span>${esc(h.host)}
        ${h.protected ? '<span class="tag-protected" title="Any change here needs approval">🔒</span>' : ""}</span></td>
      <td><span class="pill ${health}">${label}</span></td>
      <td class="hide-sm">${pct(h.cpu_percent)}</td>
      <td class="hide-sm">${pct(h.memory && h.memory.used_percent)}</td>
      <td title="${esc(fullest ? fullest.mount : "")}">${pct(fullest && fullest.used_percent)}</td>
      <td class="chev-cell">${CHEV}</td></tr>`);
    tr.onclick = () => {
      if (state.openHosts.has(h.host)) state.openHosts.delete(h.host); else state.openHosts.add(h.host);
      renderServers();
    };
    tbody.appendChild(tr);
    if (open) {
      const detail = el('<tr class="detail"><td colspan="6"></td></tr>');
      detail.firstElementChild.appendChild(hostDetail(h));
      tbody.appendChild(detail);
    }
  }
  if (!rows.length) tbody.appendChild(el('<tr class="empty-row"><td colspan="6">No servers match</td></tr>'));
}

// ---------- cluster ----------

function renderCluster() {
  const s = state.overview && state.overview.slurm;
  if (!s) return;
  const running = s.jobs.filter((j) => j.state === "RUNNING").length;
  const pending = s.jobs.filter((j) => j.state === "PENDING").length;
  $("slurm-summary").textContent = `${s.nodes.length} nodes · ${running} running · ${pending} waiting`;

  const nodes = $("nodes");
  nodes.innerHTML = "";
  for (const n of s.nodes) {
    const st = String(n.state).toLowerCase();
    const drained = st.startsWith("drain") || st.startsWith("down");
    const card = el(`<div class="node ${drained ? "drained" : ""}" title="${esc(n.reason || st)}">
      <div class="nname"><span class="dot ${drained ? "warn" : "ok"}"></span>${esc(n.node)}</div>
      <div class="nmeta">${esc(drained ? (n.reason || st).split(" - ")[0] : st)}</div>
      <div class="btns"></div></div>`);
    card.querySelector(".btns").appendChild(drained
      ? actButton("Resume", "resume_node", { node: n.node }, n.access.resume_node)
      : actButton("Drain", "drain_node", { node: n.node, reason: "maintenance" }, n.access.drain_node));
    nodes.appendChild(card);
  }

  const q = $("job-search").value.trim().toLowerCase();
  const tbody = $("jobs");
  tbody.innerHTML = "";
  const me = state.me;
  for (const j of s.jobs) {
    if (q && !`${j.job_id} ${j.name} ${j.user} ${j.state}`.toLowerCase().includes(q)) continue;
    const row = el(`<tr>
      <td><span class="mono">${esc(j.job_id)}</span> <span class="jname">${esc(j.name)}</span></td>
      <td>${esc(j.user)}</td>
      <td><span class="pill ${esc(j.state)}">${esc(j.state.toLowerCase())}</span></td>
      <td><span class="btns"></span></td></tr>`);
    const btns = row.querySelector(".btns");
    btns.appendChild(actButton("Why?", "job_status", { job_id: j.job_id }, j.access.job_status));
    if (["PENDING", "RUNNING"].includes(j.state)) {
      // Ownership is checked by the Slurm connector, not the role policy, so
      // mark someone else's job here as a hint; the server still decides.
      const notMine = me && me.role !== "admin" && j.user !== me.slurm_user;
      btns.appendChild(actButton("Cancel", "cancel_job", { job_id: j.job_id }, notMine && j.access.cancel_job === "allow" ? "deny" : j.access.cancel_job));
    }
    tbody.appendChild(row);
  }
  if (!tbody.children.length) tbody.appendChild(el('<tr class="empty-row"><td colspan="4">No jobs match</td></tr>'));
}

async function refreshOverview() {
  if (!state.token) return;
  try {
    const o = await api("/api/overview");
    const key = JSON.stringify(o);
    // Only re-render on change, so buttons don't flicker under the cursor.
    if (key === state.overviewKey) return;
    state.overview = o;
    state.overviewKey = key;
    $("nav-cluster").hidden = !o.slurm;
    if (["overview", "servers", "cluster"].includes(state.page)) renderPage();
  } catch { /* transient; next poll retries */ }
}

// ---------- approvals ----------

async function refreshApprovals() {
  if (!state.me) return;
  let rows;
  try { rows = await api("/api/approvals"); } catch { return; }
  const changed = JSON.stringify(rows) !== JSON.stringify(state.approvals);
  state.approvals = rows;
  const waiting = rows.filter((r) => r.status === "pending").length;
  $("nav-approvals").hidden = !waiting;
  $("nav-approvals").textContent = waiting;
  // Update held decision cards once someone decides.
  for (const line of document.querySelectorAll("[data-approval]")) {
    const r = rows.find((x) => x.id === Number(line.dataset.approval));
    if (r && r.status !== "pending") {
      line.innerHTML = approvalOutcome(r);
      line.removeAttribute("data-approval");
      const op = line.closest(".op");
      const [cls, label] = r.status === "rejected" ? ["blocked", "✕ Rejected"]
        : r.status === "expired" ? ["error", "Expired"] : r.status === "failed" ? ["error", "! Failed"] : ["ok", "✓ Approved"];
      if (op) {
        op.className = `op s-${cls}`;
        const v = op.querySelector(".verdict");
        if (v) { v.className = `verdict ${cls}`; v.textContent = label; }
      }
    }
  }
  if (changed) {
    renderApprovalBanner();
    if (state.page === "approvals" || state.page === "overview") renderPage();
  }
}

function approvalOutcome(r) {
  const res = r.result || {};
  const summary = (res.result && res.result.summary) || "";
  if (r.status === "rejected") return `✕ Rejected by ${esc(r.decided_by)}`;
  if (r.status === "expired") return "Expired (demo was reset)";
  if (r.status === "failed") return `! Approved by ${esc(r.decided_by)}, but failed`;
  return `✓ Approved by ${esc(r.decided_by)}${summary ? ` · ${esc(cap(summary))}` : ""}`;
}

function decisionButtons(r) {
  const box = el('<span class="btns"></span>');
  const me = state.me;
  if (r.status !== "pending") return box;
  if (me.can_approve && r.requester !== me.name) {
    for (const [decision, label] of [["approve", "Approve"], ["reject", "Reject"]]) {
      const b = el(`<button class="${decision === "reject" ? "ghost" : ""} small" data-key="${decision}:${r.id}">${label}</button>`);
      b.onclick = () => decide(r.id, decision);
      box.appendChild(b);
    }
  } else if (state.info.demo) {
    const approver = state.info.demo_users.find((u) => u.role === "admin" && u.name !== r.requester && u.workspace === state.me.workspace);
    if (approver) {
      const b = el(`<button class="ghost small">Approve as ${esc(cap(approver.name))}</button>`);
      b.onclick = () => switchUser(approver.name);
      box.appendChild(b);
    }
  } else {
    box.appendChild(el('<span class="muted">Needs an admin</span>'));
  }
  return box;
}

function renderApprovalBanner() {
  const box = $("approval-banner");
  const pending = state.approvals.filter((r) => r.status === "pending");
  box.hidden = !pending.length || state.page === "approvals";
  box.innerHTML = "";
  for (const r of pending.slice(0, 2)) {
    const item = el(`<div class="item">
      <div class="req">⏸ ${avatar(r.requester, "mini-av")} <strong>${esc(r.requester)}</strong> wants to
        <span class="what">${phraseHtml(r.tool, r.args)}</span></div></div>`);
    item.appendChild(decisionButtons(r));
    box.appendChild(item);
  }
  if (pending.length > 2) {
    box.appendChild(el(`<a class="muted" href="#/approvals">+${pending.length - 2} more waiting →</a>`));
  }
}

function renderApprovals() {
  const rows = state.approvals.filter((r) => state.approvalFilter === "all" || r.status === "pending");
  const tbody = $("approvals");
  tbody.innerHTML = "";
  for (const r of rows) {
    const status = r.status === "executed" || r.status === "approved" ? "ok" : r.status;
    const tr = el(`<tr>
      <td class="muted">${r.id}</td>
      <td>${phraseHtml(r.tool, r.args)}</td>
      <td>${avatar(r.requester, "mini-av")} ${esc(r.requester)}</td>
      <td><span class="pill ${esc(status)}">${esc(r.status)}</span>${r.decided_by ? ` <span class="muted">${esc(r.decided_by)}</span>` : ""}</td>
      <td></td></tr>`);
    tr.lastElementChild.appendChild(decisionButtons(r));
    tbody.appendChild(tr);
  }
  if (!rows.length) tbody.appendChild(el('<tr class="empty-row"><td colspan="5">Nothing waiting ✓</td></tr>'));
}

async function decide(id, decision) {
  try {
    await api(`/api/approvals/${id}/${decision}`, { method: "POST" });
  } catch (err) {
    alert(err.message);
  }
  await refreshAll();
}

// ---------- live decisions ----------

function badge(status) {
  const [cls, icon, word] = VERDICT[status] || ["error", "!", status];
  return `<span class="verdict ${cls}">${icon} ${word}</span>`;
}

function resultHtml(result) {
  if (!result || typeof result !== "object") return "";
  const parts = [];
  const text = result.explanation || result.summary;
  if (text) parts.push(`<div class="op-note">${esc(cap(text))}</div>`);
  if (Array.isArray(result.lines)) parts.push(`<pre>${esc(result.lines.join("\n"))}</pre>`);
  return parts.join("");
}

function stepNote(step) {
  if (step.status === "ok") {
    return step.redactions ? `<div class="op-note shield">🛡 ${step.redactions} secrets hidden</div>` : "";
  }
  const attr = step.status === "pending_approval" ? ` data-approval="${step.approval_id}"` : "";
  return `<div class="op-note"${attr}>${esc(shortReason(step.status, step.message))}</div>`;
}

function setPanel(open) {
  $("activity-panel").hidden = !open;
  document.body.classList.toggle("panel-open", open);
  $("activity-toggle").classList.toggle("on", open);
  if (open) $("activity-dot").hidden = true;
}

function opShell(cls) {
  const empty = $("activity").querySelector(".empty-activity");
  if (empty) empty.remove();
  const op = el(`<article class="op ${cls} fresh"></article>`);
  $("activity").prepend(op);
  while ($("activity").children.length > 30) $("activity").lastChild.remove();
  setPanel(true);
  $("activity").scrollTop = 0;
  return op;
}

function opHead(main) {
  return `<div class="op-row">${main}<span class="op-meta">${avatar(state.me.name, "mini-av")}${time(new Date())}</span></div>`;
}

function showEmptyActivity() {
  $("activity").innerHTML = `<div class="empty-activity">Click any action or ask the AI.
    <div class="flow">${badge("ok")}${badge("pending_approval")}${badge("denied")}</div></div>`;
}

async function runTool(tool, args) {
  const op = opShell("");
  op.innerHTML = opHead(`<span class="verdict wait">…</span><span class="op-what">${phraseHtml(tool, args)}</span>`);
  try {
    const step = await api(`/api/tools/${tool}`, { method: "POST", body: JSON.stringify({ args }) });
    op.className = `op s-${(VERDICT[step.status] || ["error"])[0]}`;
    op.innerHTML = opHead(`${badge(step.status)}<span class="op-what">${phraseHtml(tool, args)}</span>`)
      + stepNote(step) + (step.status === "ok" ? resultHtml(step.result) : "");
    await refreshAll();
    return step;
  } catch (err) {
    op.className = "op s-error";
    op.innerHTML = opHead(`${badge("error")}<span class="op-what">${esc(err.message)}</span>`);
    return null;
  }
}

function stepChip(s) {
  const [cls, icon] = VERDICT[s.status] || ["error", "!"];
  const p = phrase(s.tool, s.args);
  return `<span class="chip ${cls}" title="${esc(shortReason(s.status, s.message))}">${icon} ${esc(p.what)}${p.where ? ` · ${esc(p.where)}` : ""}</span>`;
}

async function askAI(text) {
  if (state.busy || !text.trim()) return null;
  state.busy = true;
  $("ask-send").disabled = true;
  const op = opShell("ai");
  const head = opHead(`<span class="verdict ai">✦ AI</span><span class="op-what">${esc(text)}</span>`);
  op.innerHTML = `${head}<div class="op-note thinking">Thinking…</div>`;
  try {
    const res = await api("/api/chat", { method: "POST", body: JSON.stringify({ message: text }) });
    const chips = res.steps.length ? `<div class="chips">${res.steps.map(stepChip).join("")}</div>` : "";
    op.innerHTML = `${head}${chips}<div class="reply">${renderMarkdown(res.reply)}</div>`;
    return res;
  } catch (err) {
    op.innerHTML = `${head}<div class="op-note">${esc(err.message)}</div>`;
    return null;
  } finally {
    state.busy = false;
    $("ask-send").disabled = false;
    refreshAll();
  }
}

// ---------- audit log ----------

async function refreshAudit() {
  if (!state.token) return;
  try {
    const entries = await api(`/api/audit?after=${state.lastAuditId}&limit=200`);
    if (!entries.length) return;
    const first = state.lastAuditId === 0;
    for (const e of entries) {
      e.fresh = !first;
      state.audit.unshift(e);
      state.lastAuditId = Math.max(state.lastAuditId, e.id);
    }
    state.audit.length = Math.min(state.audit.length, 300);
    if (state.page === "audit") renderAudit();
  } catch { /* transient */ }
}

function renderAudit() {
  const tbody = $("chain");
  tbody.innerHTML = "";
  for (const e of state.audit.slice(0, 200)) {
    const [cls, icon, prefix] = AUDIT[e.event] || ["error", "!", ""];
    tbody.appendChild(el(`<tr class="${e.fresh ? "fresh" : ""}"
        title="${esc(`${e.event}${e.detail ? ` · ${e.detail}` : ""}\nhash ${e.hash}\nprev ${e.prev_hash}`)}">
      <td><span class="ic ${cls}">${icon}</span></td>
      <td>${avatar(e.actor, "mini-av")} ${esc(e.actor)}</td>
      <td>${esc(prefix)}${phraseHtml(e.tool || "", e.args)}</td>
      <td class="hide-sm muted">${esc(e.source)}</td>
      <td class="hide-sm hash">${esc((e.hash || "").slice(0, 8))}</td>
      <td class="muted">${esc(time(new Date(e.ts)))}</td></tr>`));
    e.fresh = false;
  }
  if (!state.audit.length) tbody.appendChild(el('<tr class="empty-row"><td colspan="6">No entries yet</td></tr>'));
}

async function verifyAudit() {
  const out = $("verify-result");
  try {
    const r = await api("/api/audit/verify");
    out.className = r.ok ? "good" : "bad";
    out.textContent = r.ok ? `✓ Untouched (${r.checked} entries)` : `✕ Edited at entry #${r.broken_at}`;
  } catch (err) {
    out.className = "bad";
    out.textContent = err.message;
  }
}

// ---------- access & integrations ----------

function renderAccess() {
  const p = state.policy;
  if (!p) return;
  const roles = Object.keys(p.roles);
  const head = `<thead><tr><th>Action</th>${roles.map((r) => `<th class="${r === state.me.role ? "me" : ""}">${esc(cap(r))}</th>`).join("")}</tr></thead>`;
  const body = p.tools.map((t) => `<tr title="${esc(t.description)}">
    <td><span class="act risk-${t.risk} static">${esc(t.name.replace(/_/g, " "))}</span></td>
    ${roles.map((r) => {
      const rule = p.tool_overrides[t.name] || p.roles[r][t.risk];
      return `<td class="a-${rule} ${r === state.me.role ? "me" : ""}">${ACCESS_ICON[rule]}</td>`;
    }).join("")}</tr>`).join("");
  $("matrix").innerHTML = head + `<tbody>${body}</tbody>`;
  const prot = p.protected_targets.length ? `🔒 Protected: ${p.protected_targets.join(", ")} (changes always need approval). ` : "";
  $("access-note").textContent = `${prot}Approvers: ${p.approvers.join(", ")}. Nobody approves their own request.`;
}

function renderIntegrations() {
  const p = state.policy;
  if (!p) return;
  $("connectors").innerHTML = Object.entries(p.connectors).map(([name, mode]) => {
    const [icon, title, desc] = CONNECTOR_INFO[name] || ["🔌", name, ""];
    return `<div class="card conn"><h3>${icon} ${esc(title)}</h3>
      <span><span class="pill ${mode === "off" ? "" : "ok"}">${esc(mode === "demo" ? "simulated" : mode)}</span></span>
      <span class="muted">${esc(desc)}</span></div>`;
  }).join("");
  const config = {
    mcpServers: { opsguard: { command: "opsguard", args: ["mcp"], env: { OPSGUARD_TOKEN: state.token || "your-token" } } },
  };
  $("mcp-config").textContent = JSON.stringify(config, null, 2);
}

// ---------- people ----------

function renderUserButton() {
  const me = state.me;
  $("user-avatar").innerHTML = avatar(me.name);
  $("user-name").textContent = cap(me.name);
  $("user-role").textContent = me.role;
}

function renderUserList() {
  const users = state.info.demo_users;
  const q = $("user-search").value.trim().toLowerCase();
  $("user-search").hidden = users.length <= 6;
  $("user-list").innerHTML = "";
  let group = null;
  for (const u of [...users].sort((a, b) => a.workspace.localeCompare(b.workspace))) {
    if (q && !`${u.name} ${u.role} ${u.workspace}`.toLowerCase().includes(q)) continue;
    if (u.workspace !== group) {
      group = u.workspace;
      $("user-list").appendChild(el(`<div class="menu-group">🏢 ${esc(cap(group))}</div>`));
    }
    const b = el(`<button class="menu-item ${state.me && u.name === state.me.name ? "active" : ""}" role="menuitem">
      ${avatar(u.name)}<span>${esc(cap(u.name))}</span><span class="role">${esc(u.role)}</span></button>`);
    b.onclick = () => { closeMenu(); switchUser(u.name); };
    $("user-list").appendChild(b);
  }
}

function openMenu() {
  $("user-menu").hidden = false;
  $("user-btn").setAttribute("aria-expanded", "true");
  if (state.info.demo) renderUserList();
}
function closeMenu() {
  $("user-menu").hidden = true;
  $("user-btn").setAttribute("aria-expanded", "false");
}

async function switchUser(name) {
  const user = state.info.demo_users.find((u) => u.name === name);
  if (!user) return;
  state.token = user.token;
  storageSet("opsguard.user", name);
  await loadMe();
}

async function loadMe() {
  const before = state.me && state.me.workspace;
  state.me = await api("/api/me");
  if (before && before !== state.me.workspace) {
    // A different tenant: drop everything cached from the old one.
    state.audit = [];
    state.lastAuditId = 0;
    state.approvals = [];
    state.openHosts.clear();
    showEmptyActivity();
    await refreshAudit();
  }
  $("workspace").textContent = `🏢 ${cap(state.me.workspace)}`;
  state.policy = await api("/api/policy");
  state.overviewKey = "";
  renderUserButton();
  await Promise.all([refreshOverview(), refreshApprovals()]);
  renderPage();
}

async function refreshAll() {
  await Promise.all([refreshOverview(), refreshApprovals(), refreshAudit()]);
}

async function resetDemo() {
  await api("/api/demo/reset", { method: "POST" });
  await api("/api/chat/reset", { method: "POST" });
  showEmptyActivity();
  state.overviewKey = "";
  state.openHosts.clear();
  await refreshAll();
}

// ---------- guided tour ----------

function pendingApproval(tool) {
  return state.approvals.find((r) => r.status === "pending" && r.tool === tool);
}

function openHost(host) {
  state.openHosts.add(host);
  go("servers");
}

const TOUR = [
  {
    title: "The website is down",
    body: "You'll be Bob (operator), Carol (admin) and Alice (viewer). Let's fix it.",
    cta: "Start",
    run: async () => { await resetDemo(); await switchUser("bob"); go("overview"); },
  },
  {
    title: "Bob asks the AI",
    body: "Every check the AI makes goes through OpsGuard first.",
    cta: "Ask AI",
    page: () => go("overview"),
    spot: "ai-incident:web-01",
    run: async () => { await switchUser("bob"); await askAI("Why is the website down on web-01?"); },
  },
  {
    title: "Restart doesn't stick",
    body: "Allowed, but nginx crashes again. The disk is still full.",
    cta: "Restart nginx",
    page: () => openHost("web-01"),
    spot: "restart_container:web-01:nginx",
    run: async () => { await switchUser("bob"); await runTool("restart_container", { host: "web-01", container: "nginx" }); },
  },
  {
    title: "Deleting is risky",
    body: "Cleaning logs can't be undone, so OpsGuard holds it for an admin.",
    cta: "Clean logs",
    page: () => openHost("web-01"),
    spot: "cleanup_logs:web-01:/var/log/app",
    run: async () => { await switchUser("bob"); await runTool("cleanup_logs", { host: "web-01", path: "/var/log/app" }); },
  },
  {
    title: "Carol approves",
    body: "A second person says yes. Bob can't approve his own request.",
    cta: "Approve as Carol",
    page: () => go("approvals"),
    run: async () => {
      await switchUser("carol");
      const r = pendingApproval("cleanup_logs");
      if (r) await decide(r.id, "approve");
    },
  },
  {
    title: "Back online",
    body: "Space is free, so the restart works.",
    cta: "Restart nginx",
    page: () => openHost("web-01"),
    spot: "restart_container:web-01:nginx",
    run: async () => { await switchUser("bob"); await runTool("restart_container", { host: "web-01", container: "nginx" }); },
  },
  {
    title: "Viewers only look",
    body: "Alice can see everything but change nothing.",
    cta: "Restart as Alice",
    page: () => openHost("api-01"),
    spot: "restart_container:api-01:api",
    run: async () => { await switchUser("alice"); await runTool("restart_container", { host: "api-01", container: "api" }); },
  },
  {
    title: "Secrets stay hidden",
    body: "These logs hold API keys and a card number. Nobody sees them, not even the AI.",
    cta: "Read the logs",
    page: () => openHost("api-01"),
    spot: "container_logs:api-01:api",
    run: async () => { await switchUser("alice"); await runTool("container_logs", { host: "api-01", container: "api" }); },
  },
  {
    title: "Production is protected",
    body: "Any change on db-01 needs an admin.",
    cta: "Restart postgresql",
    page: () => openHost("db-01"),
    spot: "restart_service:db-01:postgresql",
    run: async () => { await switchUser("bob"); await runTool("restart_service", { host: "db-01", service: "postgresql" }); },
  },
  {
    title: "Nothing can be erased",
    body: "Every action is logged. Editing the log is detected.",
    cta: "Check the log",
    page: () => go("audit"),
    run: async () => { await verifyAudit(); },
  },
  {
    title: "That's OpsGuard",
    body: "Works here, or inside Claude Desktop, Claude Code and Cursor. Now try anything.",
    cta: "Done",
    page: () => go("integrations"),
    run: async () => {},
  },
];

function applySpotlight() {
  for (const b of document.querySelectorAll(".spotlight")) b.classList.remove("spotlight");
  if (!state.spotlight) return;
  const target = document.querySelector(`[data-key="${CSS.escape(state.spotlight)}"]`);
  if (target) target.classList.add("spotlight");
}

function showTourStep(i) {
  state.tour = i;
  state.tourDone = false;
  const step = TOUR[i];
  if (step.page) step.page();
  $("tour").hidden = false;
  $("tour-count").textContent = i === 0 || i === TOUR.length - 1 ? "" : `${i} / ${TOUR.length - 2}`;
  $("tour-title").textContent = step.title;
  $("tour-body").textContent = step.body;
  $("tour-do").textContent = step.cta;
  $("tour-do").disabled = false;
  $("tour-back").hidden = i === 0;
  state.spotlight = step.spot || null;
  applySpotlight();
}

function endTour() {
  $("tour").hidden = true;
  state.tour = -1;
  state.spotlight = null;
  applySpotlight();
}

async function tourPrimary() {
  const i = state.tour;
  if (state.tourDone) {
    showTourStep(i + 1);
    return;
  }
  $("tour-do").disabled = true;
  try {
    await TOUR[i].run();
  } finally {
    $("tour-do").disabled = false;
  }
  if (i === TOUR.length - 1) { endTour(); return; }
  if (i === 0) { showTourStep(1); return; }
  state.tourDone = true;
  state.spotlight = null;
  applySpotlight();
  $("tour-do").textContent = "Next →";
}

// ---------- start ----------

function wireUi() {
  window.addEventListener("hashchange", route);
  $("ask").onsubmit = (e) => {
    e.preventDefault();
    const text = $("ask-input").value;
    $("ask-input").value = "";
    askAI(text);
  };
  $("activity-toggle").onclick = () => setPanel($("activity-panel").hidden);
  $("activity-close").onclick = () => setPanel(false);
  $("user-btn").onclick = (e) => { e.stopPropagation(); $("user-menu").hidden ? openMenu() : closeMenu(); };
  $("user-menu").onclick = (e) => e.stopPropagation();
  document.addEventListener("click", closeMenu);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
  $("user-search").oninput = renderUserList;
  $("server-search").oninput = renderServers;
  $("job-search").oninput = renderCluster;
  for (const [segId, key, render] of [["server-filter", "serverFilter", renderServers], ["approval-filter", "approvalFilter", renderApprovals]]) {
    for (const b of $(segId).querySelectorAll("button")) {
      b.onclick = () => {
        state[key] = b.dataset.f;
        for (const x of $(segId).querySelectorAll("button")) x.classList.toggle("active", x === b);
        render();
      };
    }
  }
  $("verify").onclick = verifyAudit;
  $("copy-mcp").onclick = async () => {
    try { await navigator.clipboard.writeText($("mcp-config").textContent); $("copy-mcp").textContent = "Copied ✓"; }
    catch { $("copy-mcp").textContent = "Select and copy"; }
  };
  $("tour-do").onclick = tourPrimary;
  $("tour-back").onclick = () => showTourStep(Math.max(0, state.tour - 1));
  $("tour-close").onclick = endTour;
  $("start-tour").onclick = () => showTourStep(0);
}

async function init() {
  wireUi();
  state.info = await api("/api/info");
  const info = state.info;
  $("mode-note").textContent = `${info.assistant === "claude" ? "AI: Claude" : "AI: scripted demo"} · v${info.version}`;
  showEmptyActivity();
  setPanel(isWide());

  if (info.demo) {
    $("reset-demo").hidden = false;
    $("reset-demo").onclick = resetDemo;
    $("start-tour").hidden = false;
    const saved = storageGet("opsguard.user");
    await switchUser(info.demo_users.some((u) => u.name === saved) ? saved : "bob");
  } else {
    $("menu-head").textContent = "Sign in with your token";
    $("token-box").hidden = false;
    const input = $("token-input");
    input.value = storageGet("opsguard.token") || "";
    const login = async () => {
      state.token = input.value.trim();
      try { await loadMe(); storageSet("opsguard.token", state.token); closeMenu(); }
      catch (err) { alert(`Sign in failed: ${err.message}`); }
    };
    input.onchange = login;
    if (input.value) await login(); else openMenu();
  }

  route();
  refreshAudit();
  if (info.demo && new URLSearchParams(location.search).get("tour") === "1") {
    history.replaceState(null, "", "/app#/overview");
    showTourStep(0);
  }
  setInterval(refreshOverview, 3000);
  setInterval(refreshApprovals, 3000);
  setInterval(refreshAudit, 3000);
}

init().catch((err) => {
  $("activity").textContent = `Could not start: ${err.message}`;
});
