/* AI Agents Workspace UI — no build step, plain JS. */
"use strict";

const $main = document.getElementById("main");
let view = "agents";
let agents = [];
let openAgent = null;      // name of agent whose detail modal is open
let openTab = "overview";
let refreshTimer = null;

document.querySelectorAll("nav button").forEach(btn => {
  btn.onclick = () => {
    view = btn.dataset.view;
    document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b === btn));
    render();
  };
});

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast";
  el.style.borderColor = isErr ? "var(--red)" : "var(--accent)";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function dotClass(a) {
  if (!a.running) return "off";
  return a.healthy ? "on" : "warn";
}

/* ─── Agents view ─────────────────────────────────────────────────────── */

function renderAgents() {
  $main.innerHTML = `
    <div class="formrow">
      <input id="new-name" placeholder="agent-name" maxlength="32" style="width:180px">
      <input id="new-desc" placeholder="What is this agent for?" style="flex:1;min-width:220px">
      <button class="act primary" id="btn-create">+ Create agent (gateway)</button>
    </div>
    <div class="grid" id="agent-grid"></div>`;
  document.getElementById("btn-create").onclick = createAgent;
  drawAgentCards();
}

function drawAgentCards() {
  const grid = document.getElementById("agent-grid");
  if (!grid) return;
  grid.innerHTML = agents.map(a => `
    <div class="card">
      <h3><span class="dot ${dotClass(a)}"></span>${esc(a.name)}
        ${a.name === "fixer" ? '<span class="badge fixer">FIXER</span>' : ""}
        ${a.external ? '<span class="badge">systemd</span>' : ""}
      </h3>
      <div class="meta">${esc(a.description) || "&nbsp;"}</div>
      <div>
        <span class="badge">api :${a.api_port}</span>
        <span class="badge">${a.subagents.length} subagent${a.subagents.length === 1 ? "" : "s"}</span>
        <span class="badge">${a.running ? (a.healthy ? "healthy" : "starting…") : "stopped"}</span>
      </div>
      <div class="row">
        ${a.running
          ? `<button class="act" onclick="doAction('${a.name}','stop')">Stop</button>
             <button class="act" onclick="doAction('${a.name}','restart')">Restart</button>`
          : `<button class="act primary" onclick="doAction('${a.name}','start')">Start</button>`}
        <button class="act" onclick="openDetail('${a.name}','subagents')">Subagents</button>
        <button class="act" onclick="openDetail('${a.name}','logs')">Logs</button>
        ${a.running ? `<button class="act" onclick="openDash('${a.name}')">Dashboard ↗</button>` : ""}
        ${!a.external ? `<button class="act danger" onclick="removeAgent('${a.name}')">Delete</button>` : ""}
      </div>
    </div>`).join("");
}

async function createAgent() {
  const name = document.getElementById("new-name").value.trim();
  const description = document.getElementById("new-desc").value.trim();
  if (!name) return toast("Give the agent a name", true);
  try {
    toast(`Creating ${name}… (first start can take ~30s)`);
    await api("/api/agents", { method: "POST", body: JSON.stringify({ name, description }) });
    toast(`Agent ${name} created and starting`);
    refresh();
  } catch (e) { toast(e.message, true); }
}

window.doAction = async (name, action) => {
  try {
    await api(`/api/agents/${name}/${action}`, { method: "POST" });
    toast(`${name}: ${action} ok`);
    refresh();
  } catch (e) { toast(e.message, true); }
};

window.removeAgent = async (name) => {
  if (!confirm(`Delete agent '${name}' and its entire home folder?`)) return;
  try {
    await api(`/api/agents/${name}`, { method: "DELETE" });
    toast(`${name} deleted`);
    refresh();
  } catch (e) { toast(e.message, true); }
};

window.openDash = async (name) => {
  try {
    toast("Starting dashboard…");
    const { url } = await api(`/api/agents/${name}/dashboard`, { method: "POST" });
    setTimeout(() => window.open(url, "_blank"), 2500);
  } catch (e) { toast(e.message, true); }
};

/* ─── Agent detail modal (subagents tab, logs) ────────────────────────── */

window.openDetail = (name, tab) => { openAgent = name; openTab = tab || "overview"; renderModal(); };

async function renderModal() {
  document.querySelector(".modal-bg")?.remove();
  if (!openAgent) return;
  const a = agents.find(x => x.name === openAgent);
  if (!a) return;

  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.onclick = e => { if (e.target === bg) { openAgent = null; bg.remove(); } };
  bg.innerHTML = `
    <div class="modal">
      <h2><span class="dot ${dotClass(a)}" style="display:inline-block"></span> ${esc(a.name)}</h2>
      <div class="meta">${esc(a.description)}</div>
      <div class="tabs">
        ${["overview", "subagents", "logs"].map(t =>
          `<button data-tab="${t}" class="${openTab === t ? "active" : ""}">${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}
      </div>
      <div id="modal-body"></div>
    </div>`;
  document.body.appendChild(bg);
  bg.querySelectorAll(".tabs button").forEach(b => b.onclick = () => { openTab = b.dataset.tab; renderModal(); });

  const body = bg.querySelector("#modal-body");
  if (openTab === "overview") {
    body.innerHTML = `
      <p><span class="badge">home</span> <code style="font-size:12px">${esc(a.home)}</code></p>
      <p style="margin-top:8px"><span class="badge">api port</span> ${a.api_port}
         <span class="badge">dashboard port</span> ${a.dash_port}</p>
      <p style="margin-top:8px"><span class="badge">status</span>
         ${a.running ? (a.healthy ? "running & healthy" : "running (api not ready)") : "stopped"}</p>`;
  } else if (openTab === "subagents") {
    body.innerHTML = `
      <div class="formrow">
        <input id="sub-name" placeholder="subagent-name" style="width:170px">
        <input id="sub-desc" placeholder="Role / description" style="flex:1">
        <button class="act primary" id="btn-sub">+ Create subagent</button>
      </div>
      <div id="sub-list"><div class="empty">Loading…</div></div>`;
    body.querySelector("#btn-sub").onclick = async () => {
      const name = body.querySelector("#sub-name").value.trim();
      const description = body.querySelector("#sub-desc").value.trim();
      if (!name) return toast("Name the subagent", true);
      try {
        toast(`Creating subagent ${name}…`);
        await api(`/api/agents/${a.name}/subagents`,
          { method: "POST", body: JSON.stringify({ name, description }) });
        toast("Subagent created");
        refresh(); renderModal();
      } catch (e) { toast(e.message, true); }
    };
    try {
      const subs = await api(`/api/agents/${a.name}/subagents`);
      body.querySelector("#sub-list").innerHTML = subs.length
        ? subs.map(s => `
          <div class="sub-item">
            <div><b>${esc(s.name)}</b>
              <div class="meta" style="margin:0">${esc(s.description)}</div></div>
            <button class="act danger" onclick="removeSub('${a.name}','${s.name}')">Delete</button>
          </div>`).join("")
        : `<div class="empty">No subagents yet — create the first one above.</div>`;
    } catch (e) {
      body.querySelector("#sub-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  } else if (openTab === "logs") {
    body.innerHTML = `<pre class="log" id="log-view">Loading…</pre>
      <div class="row"><button class="act" id="btn-log-refresh">Refresh</button></div>`;
    const load = async () => {
      try {
        const text = await api(`/api/agents/${a.name}/logs?lines=200`);
        const el = body.querySelector("#log-view");
        el.textContent = text || "(log is empty)";
        el.scrollTop = el.scrollHeight;
      } catch (e) { toast(e.message, true); }
    };
    body.querySelector("#btn-log-refresh").onclick = load;
    load();
  }
}

window.removeSub = async (agent, sub) => {
  if (!confirm(`Delete subagent '${sub}' of '${agent}'?`)) return;
  try {
    await api(`/api/agents/${agent}/subagents/${sub}`, { method: "DELETE" });
    toast("Subagent deleted");
    refresh(); renderModal();
  } catch (e) { toast(e.message, true); }
};

/* ─── Graph view ──────────────────────────────────────────────────────── */

async function renderGraph() {
  $main.innerHTML = `<svg id="graph-svg"></svg>`;
  const g = await api("/api/graph");
  const svg = document.getElementById("graph-svg");
  const W = svg.clientWidth || 1100;
  const gateways = g.nodes.filter(n => n.type === "gateway");
  const subsOf = id => g.edges.filter(e => e.from === id).map(e => g.nodes.find(n => n.id === e.to));

  const colW = Math.max(200, Math.min(300, W / Math.max(gateways.length, 1)));
  const H = Math.max(480, 180 + Math.max(...gateways.map(gw => subsOf(gw.id).length), 0) * 74);
  svg.setAttribute("viewBox", `0 0 ${Math.max(W, gateways.length * colW)} ${H}`);
  svg.style.minHeight = H + "px";

  let parts = [];
  gateways.forEach((gw, i) => {
    const cx = i * colW + colW / 2;
    const gy = 70;
    const color = !gw.running ? "var(--red)" : (gw.healthy ? "var(--green)" : "var(--amber)");
    const subs = subsOf(gw.id);
    subs.forEach((s, j) => {
      const sy = 190 + j * 74;
      parts.push(`<line x1="${cx}" y1="${gy + 34}" x2="${cx}" y2="${sy - 22}"
        stroke="var(--border)" stroke-width="1.5"/>`);
      parts.push(`<g style="cursor:pointer" onclick="openDetail('${gw.id}','subagents')">
        <rect x="${cx - 80}" y="${sy - 22}" width="160" height="44" rx="8"
          fill="var(--bg)" stroke="var(--border)"/>
        <text x="${cx}" y="${sy + 4}" text-anchor="middle" fill="var(--text)" font-size="12">${esc(s.label)}</text>
      </g>`);
    });
    parts.push(`<g style="cursor:pointer" onclick="openDetail('${gw.id}','overview')">
      <rect x="${cx - 95}" y="${gy - 34}" width="190" height="68" rx="10"
        fill="var(--panel)" stroke="${color}" stroke-width="2"/>
      <circle cx="${cx - 75}" cy="${gy - 12}" r="5" fill="${color}"/>
      <text x="${cx - 62}" y="${gy - 7}" fill="var(--text)" font-size="14" font-weight="600">${esc(gw.id)}</text>
      <text x="${cx}" y="${gy + 14}" text-anchor="middle" fill="var(--muted)" font-size="11">
        :${gw.port} · ${subs.length} subagent${subs.length === 1 ? "" : "s"}</text>
    </g>`);
  });
  svg.innerHTML = parts.join("");
}

/* ─── Shared resources view ───────────────────────────────────────────── */

async function renderShared() {
  const s = await api("/api/shared");
  const chip = list => list.length
    ? list.map(x => `<span class="badge">${esc(x)}</span>`).join(" ")
    : '<span class="empty" style="padding:0">none yet</span>';
  const last = s.last_sync || {};
  $main.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <h3>How sharing works</h3>
      <div class="meta">Configure LLMs, API keys, tools, and MCP servers in <b>any</b> agent's
      dashboard — the workspace merges the change and pushes it to every other agent
      (last writer wins). OAuth logins, memory, and skills are physically shared.
      Channels (Telegram, Discord…) always stay per-agent.</div>
      <div class="row"><button class="act primary" id="btn-sync">Sync now</button></div>
    </div>
    <div class="grid">
      <div class="card"><h3>Model</h3><div class="meta" style="margin-top:10px">
        ${esc(s.model.provider || "—")} / ${esc(s.model.default || "—")}</div></div>
      <div class="card"><h3>Provider logins (auth.json)</h3>
        <div style="margin-top:10px">${chip(s.providers)}</div></div>
      <div class="card"><h3>Shared API keys (.env)</h3>
        <div style="margin-top:10px">${chip(s.env_keys)}</div></div>
      <div class="card"><h3>MCP servers</h3>
        <div style="margin-top:10px">${chip(s.mcp_servers)}</div></div>
      <div class="card"><h3>Shared skills</h3>
        <div style="margin-top:10px">${chip(s.skills)}</div></div>
      <div class="card"><h3>Last sync</h3><div class="meta" style="margin-top:10px">
        ${last.synced_at ? new Date(last.synced_at * 1000).toLocaleString() : "—"}<br>
        edited by: ${esc((last.editors || []).join(", ") || "—")}<br>
        pushed to: ${esc((last.changed || []).join(", ") || "—")}</div></div>
    </div>`;
  document.getElementById("btn-sync").onclick = async () => {
    try {
      const r = await api("/api/sync", { method: "POST" });
      toast(`Synced — updated: ${r.changed.join(", ") || "nothing to change"}`);
      renderShared();
    } catch (e) { toast(e.message, true); }
  };
}

/* ─── Incidents view ──────────────────────────────────────────────────── */

async function renderIncidents() {
  const inc = await api("/api/incidents");
  $main.innerHTML = inc.length ? `
    <table>
      <tr><th>#</th><th>Time</th><th>Agent</th><th>Type</th><th>Detail</th><th>Auto action</th><th>Fixer</th></tr>
      ${inc.map(i => `
        <tr>
          <td>${i.id}</td>
          <td style="white-space:nowrap">${new Date(i.ts * 1000).toLocaleTimeString()}</td>
          <td><b>${esc(i.agent)}</b></td>
          <td class="kind-${esc(i.kind)}">${esc(i.kind)}</td>
          <td class="detail">${esc(i.detail.slice(0, 400))}</td>
          <td>${esc(i.action)}</td>
          <td class="detail">${esc(i.fixer)}</td>
        </tr>`).join("")}
    </table>`
    : `<div class="empty">No incidents — everything has been running clean.</div>`;
}

/* ─── main loop ───────────────────────────────────────────────────────── */

async function refresh() {
  try {
    agents = await api("/api/agents");
    if (view === "agents") drawAgentCards();
  } catch (e) { /* server restarting */ }
}

function render() {
  clearInterval(refreshTimer);
  if (view === "agents") { renderAgents(); refresh(); refreshTimer = setInterval(refresh, 5000); }
  else if (view === "graph") { renderGraph(); refreshTimer = setInterval(renderGraph, 8000); }
  else if (view === "shared") { renderShared(); }
  else if (view === "incidents") { renderIncidents(); refreshTimer = setInterval(renderIncidents, 6000); }
}

refresh().then(render);
