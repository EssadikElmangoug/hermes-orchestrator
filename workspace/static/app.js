/* Hermes Orchestrator UI — no build step, plain JS. */
"use strict";

const $main = document.getElementById("main");
const VIEWS = ["agents", "workflows", "graph", "shared", "clis", "incidents"];
let view = "agents";
let agents = [];
let openAgent = null;      // name of agent whose detail modal is open
let openTab = "overview";
let refreshTimer = null;

/* URL routing: /<view> for every tab, /workflows/<id> for an open editor.
   The server returns index.html for these paths so refreshes stick. */
function routeFromLocation() {
  const parts = location.pathname.split("/").filter(Boolean);
  view = VIEWS.includes(parts[0]) ? parts[0] : "agents";
  window.wfRouteId = view === "workflows" ? (parts[1] || null) : undefined;
  document.querySelectorAll("nav button").forEach(b =>
    b.classList.toggle("active", b.dataset.view === view));
}

function navigate(path) {
  if (location.pathname !== path) history.pushState({}, "", path);
  routeFromLocation();
  render();
}

window.addEventListener("popstate", () => { routeFromLocation(); render(); });

document.querySelectorAll("nav button").forEach(btn => {
  btn.onclick = () => navigate(`/${btn.dataset.view}`);
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
        ${a.adopted ? '<span class="badge installed">installed</span>'
          : (a.external ? '<span class="badge">systemd</span>' : "")}
      </h3>
      <div class="meta">${esc(a.description) || "&nbsp;"}</div>
      <div>
        ${a.api_port ? `<span class="badge">api :${a.api_port}</span>` : ""}
        ${a.unit ? `<span class="badge">${esc(a.unit)}</span>` : ""}
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
      <p style="margin-top:8px">${a.api_port ? `<span class="badge">api port</span> ${a.api_port}` : ""}
         <span class="badge">dashboard port</span> ${a.dash_port}
         ${a.unit ? `<span class="badge">systemd</span> <code style="font-size:12px">${esc(a.unit)} (${esc(a.scope || "user")})</code>` : ""}</p>
      ${a.adopted ? `<p style="margin-top:8px" class="meta">Adopted pre-installed agent — the
         workspace never modifies its files, config or memory. Its API keys, provider
         logins, skills and tools were copied into the shared layer for new agents.</p>` : ""}
      <p style="margin-top:8px"><span class="badge">status</span>
         ${a.running ? (a.healthy ? "running & healthy" : "running (api not ready)") : "stopped"}</p>`;
  } else if (openTab === "subagents") {
    body.innerHTML = `
      ${a.read_only
        ? `<div class="meta" style="margin-bottom:12px">Adopted installed agent — profiles are
             shown read-only. Create or delete them with the <code>hermes profile</code> CLI.</div>`
        : `<div class="formrow">
             <input id="sub-name" placeholder="subagent-name" style="width:170px">
             <input id="sub-desc" placeholder="Role / description" style="flex:1">
             <button class="act primary" id="btn-sub">+ Create subagent</button>
           </div>`}
      <div id="sub-list"><div class="empty">Loading…</div></div>`;
    if (!a.read_only) body.querySelector("#btn-sub").onclick = async () => {
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
            ${a.read_only ? "" : `<button class="act danger" onclick="removeSub('${a.name}','${s.name}')">Delete</button>`}
          </div>`).join("")
        : `<div class="empty">${a.read_only ? "No profiles in this install." : "No subagents yet — create the first one above."}</div>`;
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

/* ─── Graph view — draggable canvas ───────────────────────────────────── */

const GW_W = 196, GW_H = 74, SUB_W = 158, SUB_H = 46;
let gpos = {};                 // node id -> {x, y} (world coords, persisted)
let gview = null;              // {x, y, k} pan/zoom (persisted)
let graphTimer = null;

function loadGraphState() {
  try { gpos = JSON.parse(localStorage.getItem("graph.pos") || "{}"); } catch { gpos = {}; }
  try { gview = JSON.parse(localStorage.getItem("graph.view")) || null; } catch { gview = null; }
}
const saveGraphState = () => {
  localStorage.setItem("graph.pos", JSON.stringify(gpos));
  localStorage.setItem("graph.view", JSON.stringify(gview));
};

function defaultLayout(g) {
  const parentOf = id => (g.edges.find(e => e.to === id && e.kind === "profile") || {}).from;
  const gateways = g.nodes.filter(n => n.type === "gateway");
  const hubs = gateways.filter(gw => gw.adopted && !parentOf(gw.id));
  const orbits = gateways.filter(gw => gw.adopted && parentOf(gw.id));
  const solo = gateways.filter(gw => !gw.adopted);
  const pos = {};
  hubs.forEach((h, i) => { pos[h.id] = { x: i * 720, y: 0 }; });
  orbits.forEach((o, i) => {
    const c = pos[parentOf(o.id)] || { x: 0, y: 0 };
    const angle = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(orbits.length, 3);
    pos[o.id] = { x: c.x + Math.cos(angle) * 330, y: c.y + Math.sin(angle) * 230 };
  });
  solo.forEach((s, i) => { pos[s.id] = { x: 620, y: -260 + i * 130 }; });
  // subagents hang under their gateway
  g.nodes.filter(n => n.type === "subagent").forEach(n => {
    const e = g.edges.find(e2 => e2.to === n.id && e2.kind === "subagent");
    const p = (e && pos[e.from]) || { x: 0, y: 0 };
    const siblings = g.edges.filter(e2 => e2.kind === "subagent" && e2.from === (e || {}).from);
    const idx = siblings.findIndex(e2 => e2.to === n.id);
    pos[n.id] = { x: p.x + (idx - (siblings.length - 1) / 2) * (SUB_W + 18), y: p.y + 130 + (idx % 2) * 8 };
  });
  return pos;
}

function edgePath(a, b, aH, bH) {
  const y1 = a.y + aH / 2, y2 = b.y - bH / 2;
  const bend = Math.max(36, (y2 - y1) / 2);
  return `M ${a.x} ${y1} C ${a.x} ${y1 + bend}, ${b.x} ${y2 - bend}, ${b.x} ${y2}`;
}

async function renderGraph() {
  loadGraphState();
  $main.innerHTML = `
    <div id="graph-wrap">
      <div id="graph-toolbar">
        <button class="act" id="g-fit">⤢ Fit</button>
        <button class="act" id="g-zoom-in" title="Zoom in">＋</button>
        <button class="act" id="g-zoom-out" title="Zoom out">−</button>
        <button class="act" id="g-reset">↺ Reset layout</button>
        <span class="hint">drag nodes · drag canvas to pan · scroll or pinch to zoom · click for details</span>
      </div>
      <svg id="graph-svg">
        <defs>
          <radialGradient id="g-bgglow" cx="50%" cy="35%" r="75%">
            <stop offset="0%" stop-color="#141c28"/><stop offset="100%" stop-color="#0b0f16"/>
          </radialGradient>
          <linearGradient id="g-card" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#1c2430"/><stop offset="100%" stop-color="#12171f"/>
          </linearGradient>
          <linearGradient id="g-card-sub" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#161c26"/><stop offset="100%" stop-color="#0f141b"/>
          </linearGradient>
          <filter id="g-shadow" x="-40%" y="-40%" width="180%" height="180%">
            <feDropShadow dx="0" dy="5" stdDeviation="9" flood-color="#000" flood-opacity="0.55"/>
          </filter>
        </defs>
        <rect id="g-bg" x="-100000" y="-100000" width="200000" height="200000" fill="url(#g-bgglow)"/>
        <g id="g-world"><g id="g-edges"></g><g id="g-nodes"></g></g>
      </svg>
    </div>`;

  const svg = document.getElementById("graph-svg");
  const world = document.getElementById("g-world");
  let data = await api("/api/graph");
  const defaults = defaultLayout(data);
  data.nodes.forEach(n => { if (!gpos[n.id]) gpos[n.id] = { ...defaults[n.id] }; });

  // Some renderers skip repainting SVG mutated via innerHTML/setAttribute;
  // appending a throwaway node reliably invalidates the layer.
  const kick = () => {
    const k = document.createElementNS("http://www.w3.org/2000/svg", "g");
    svg.appendChild(k);
    requestAnimationFrame(() => k.remove());
  };
  const applyView = () => {
    world.setAttribute("transform",
      `translate(${gview.x},${gview.y}) scale(${gview.k})`);
    kick();
  };
  const fit = () => {
    const xs = data.nodes.map(n => gpos[n.id].x), ys = data.nodes.map(n => gpos[n.id].y);
    const pad = 150;
    const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
    const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
    const r = svg.getBoundingClientRect();
    const k = Math.min(r.width / (maxX - minX), r.height / (maxY - minY), 1.4);
    gview = { k, x: r.width / 2 - k * (minX + maxX) / 2, y: r.height / 2 - k * (minY + maxY) / 2 };
    applyView(); saveGraphState();
  };
  if (!gview) fit(); else applyView();

  const parentOf = id => (data.edges.find(e => e.to === id && e.kind === "profile") || {}).from;

  function draw() {
    const byId = Object.fromEntries(data.nodes.map(n => [n.id, n]));
    document.getElementById("g-edges").innerHTML = data.edges.map(e => {
      const a = gpos[e.from], b = gpos[e.to];
      if (!a || !b || !byId[e.to]) return "";
      const sub = e.kind === "subagent";
      return `<path d="${edgePath(a, b, GW_H, sub ? SUB_H : GW_H)}" fill="none"
        stroke="${sub ? "#2c3542" : "#3d4d63"}" stroke-width="${sub ? 1.6 : 2}"
        ${sub ? "" : 'stroke-dasharray="7 6"'} opacity="0.9"/>`;
    }).join("");
    document.getElementById("g-nodes").innerHTML = data.nodes.map(n => {
      const p = gpos[n.id];
      if (n.type === "subagent") {
        return `<g class="gnode" data-id="${esc(n.id)}" transform="translate(${p.x},${p.y})">
          <rect x="${-SUB_W / 2}" y="${-SUB_H / 2}" width="${SUB_W}" height="${SUB_H}" rx="10"
            fill="url(#g-card-sub)" stroke="#2a3340" filter="url(#g-shadow)"/>
          <text y="-2" text-anchor="middle" fill="#c6ceda" font-size="12" font-weight="600">${esc(n.label)}</text>
          <text y="14" text-anchor="middle" fill="#67707d" font-size="9" letter-spacing="1">SUBAGENT</text>
        </g>`;
      }
      const color = !n.running ? "var(--red)" : (n.healthy ? "var(--green)" : "var(--amber)");
      const tag = n.adopted ? (parentOf(n.id) ? "INSTALLED · PROFILE" : "INSTALLED · MAIN")
                            : (n.id === "fixer" ? "WORKSPACE · FIXER" : "WORKSPACE");
      const subCount = data.edges.filter(e => e.from === n.id && e.kind === "subagent").length;
      return `<g class="gnode" data-id="${esc(n.id)}" transform="translate(${p.x},${p.y})">
        <rect x="${-GW_W / 2}" y="${-GW_H / 2}" width="${GW_W}" height="${GW_H}" rx="13"
          fill="url(#g-card)" stroke="${n.adopted ? "#43506a" : "#3b4455"}" stroke-width="1.2" filter="url(#g-shadow)"/>
        <rect x="${-GW_W / 2}" y="${-GW_H / 2}" width="${GW_W}" height="3" rx="1.5" fill="${color}" opacity="0.85"/>
        <circle cx="${-GW_W / 2 + 18}" cy="-8" r="5" fill="${color}" class="${n.running && n.healthy ? "pulse" : ""}"/>
        <text x="${-GW_W / 2 + 32}" y="-3" fill="#e6edf3" font-size="14" font-weight="700">${esc(n.id)}</text>
        <text x="${-GW_W / 2 + 18}" y="17" fill="#8b949e" font-size="9" letter-spacing="1.2">${tag}</text>
        <text x="${GW_W / 2 - 16}" y="17" text-anchor="end" fill="#67707d" font-size="9">${subCount ? subCount + " sub" : ""}${n.port ? (subCount ? " · " : "") + ":" + n.port : ""}</text>
      </g>`;
    }).join("");
    kick();
  }
  draw();

  /* interactions */
  let drag = null;   // {id?, startX, startY, origin, moved}
  let pinch = null;  // {d0, k0, world0} two-finger zoom state
  const pointers = new Map();   // active pointerId -> {x, y}
  const toWorld = (cx, cy) => {
    const r = svg.getBoundingClientRect();
    return { x: (cx - r.left - gview.x) / gview.k, y: (cy - r.top - gview.y) / gview.k };
  };
  const zoomAt = (mx, my, factor) => {
    const k = Math.min(2.5, Math.max(0.25, gview.k * factor));
    gview.x = mx - (mx - gview.x) * (k / gview.k);
    gview.y = my - (my - gview.y) * (k / gview.k);
    gview.k = k;
    applyView(); saveGraphState();
  };
  svg.addEventListener("pointerdown", e => {
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    try { svg.setPointerCapture(e.pointerId); } catch {}
    if (pointers.size === 2) {
      // second finger down: switch from drag/pan to pinch-zoom
      const [p1, p2] = [...pointers.values()];
      const r = svg.getBoundingClientRect();
      const mid = { x: (p1.x + p2.x) / 2 - r.left, y: (p1.y + p2.y) / 2 - r.top };
      pinch = {
        d0: Math.hypot(p1.x - p2.x, p1.y - p2.y) || 1,
        k0: gview.k,
        world0: { x: (mid.x - gview.x) / gview.k, y: (mid.y - gview.y) / gview.k },
      };
      drag = null;
      return;
    }
    const nodeEl = e.target.closest(".gnode");
    drag = nodeEl
      ? { id: nodeEl.dataset.id, start: toWorld(e.clientX, e.clientY),
          origin: { ...gpos[nodeEl.dataset.id] }, moved: false }
      : { pan: true, startX: e.clientX - gview.x, startY: e.clientY - gview.y, moved: false };
  });
  svg.addEventListener("pointermove", e => {
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pointers.size >= 2) {
      const [p1, p2] = [...pointers.values()];
      const r = svg.getBoundingClientRect();
      const mid = { x: (p1.x + p2.x) / 2 - r.left, y: (p1.y + p2.y) / 2 - r.top };
      const d = Math.hypot(p1.x - p2.x, p1.y - p2.y) || 1;
      gview.k = Math.min(2.5, Math.max(0.25, pinch.k0 * d / pinch.d0));
      gview.x = mid.x - pinch.world0.x * gview.k;
      gview.y = mid.y - pinch.world0.y * gview.k;
      applyView();
      return;
    }
    if (!drag) return;
    drag.moved = true;
    if (drag.pan) {
      gview.x = e.clientX - drag.startX; gview.y = e.clientY - drag.startY; applyView();
    } else {
      const w = toWorld(e.clientX, e.clientY);
      gpos[drag.id] = { x: drag.origin.x + w.x - drag.start.x, y: drag.origin.y + w.y - drag.start.y };
      draw();
    }
  });
  const endPointer = e => {
    pointers.delete(e.pointerId);
    if (pinch) {
      if (pointers.size < 2) { pinch = null; saveGraphState(); }
      return;
    }
    if (drag && !drag.moved && drag.id && e.type === "pointerup") {
      const n = data.nodes.find(x => x.id === drag.id);
      if (n) openDetail(n.type === "subagent" ? drag.id.split("/")[0] : drag.id,
                        n.type === "subagent" ? "subagents" : "overview");
    }
    if (drag && drag.moved) saveGraphState();
    drag = null;
  };
  svg.addEventListener("pointerup", endPointer);
  svg.addEventListener("pointercancel", endPointer);
  svg.addEventListener("wheel", e => {
    e.preventDefault();
    const r = svg.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.12 : 1 / 1.12);
  }, { passive: false });

  const zoomCenter = factor => {
    const r = svg.getBoundingClientRect();
    zoomAt(r.width / 2, r.height / 2, factor);
  };
  document.getElementById("g-zoom-in").onclick = () => zoomCenter(1.3);
  document.getElementById("g-zoom-out").onclick = () => zoomCenter(1 / 1.3);
  document.getElementById("g-fit").onclick = fit;
  document.getElementById("g-reset").onclick = () => {
    gpos = {}; gview = null; localStorage.removeItem("graph.pos");
    localStorage.removeItem("graph.view"); renderGraph();
  };

  /* live status refresh without touching layout */
  clearInterval(graphTimer);
  graphTimer = setInterval(async () => {
    if (view !== "graph") { clearInterval(graphTimer); return; }
    try {
      const fresh = await api("/api/graph");
      const defaults2 = defaultLayout(fresh);
      fresh.nodes.forEach(n => { if (!gpos[n.id]) gpos[n.id] = { ...defaults2[n.id] }; });
      data = fresh;
      if (!drag && !pinch) draw();
    } catch {}
  }, 8000);
}

/* ─── Shared resources view ───────────────────────────────────────────── */

async function renderShared() {
  const [s, clis] = await Promise.all([api("/api/shared"), api("/api/clis")]);
  const chip = list => list.length
    ? list.map(x => `<span class="badge">${esc(x)}</span>`).join(" ")
    : '<span class="empty" style="padding:0">none yet</span>';
  const last = s.last_sync || {};
  $main.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <h3>How sharing works</h3>
      <div class="meta">Configure LLMs, API keys, tools, and MCP servers in <b>any</b> agent's
      dashboard — the workspace merges the change and pushes it to every workspace-created agent
      (last writer wins). OAuth logins, memory, skills, CLI tools, and webhook routes are
      physically shared — whether created manually or by an agent. When shared skills change,
      running agents are reloaded automatically so they actually see the new skills.
      Channels (Telegram, Discord…) always stay per-agent. Adopted pre-installed agents are
      <b>read-only sources</b>: everything they create (skills, tools, webhooks…) flows into the
      shared layer continuously, but the workspace never writes back into an existing install.</div>
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
      <div class="card"><h3>Shared plugins</h3>
        <div style="margin-top:10px">${chip(s.plugins || [])}</div></div>
      <div class="card"><h3>Shared CLI tools</h3>
        <div style="margin-top:10px">${chip(clis.map(t => t.name))}</div>
        <div class="row"><button class="act" onclick="document.querySelector('nav button[data-view=clis]').click()">Open CLI Tools tab →</button></div></div>
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

/* ─── CLI tools view ──────────────────────────────────────────────────── */

async function renderClis() {
  const tools = await api("/api/clis");
  $main.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <h3>Shared CLI tools</h3>
      <div class="meta">Works exactly like shared skills: executables live in the shared
      <code>bin</code> (on every agent's PATH) and each tool has a markdown manifest in the
      shared <code>clis</code> folder. Any agent can register a tool by writing those two
      files (the shared <b>cli-tools</b> skill teaches them how) — it then appears here and
      is usable by you and every other agent immediately.</div>
      <div class="formrow" style="margin:14px 0 0">
        <input id="cli-name" placeholder="tool-name" style="width:160px">
        <input id="cli-desc" placeholder="What does this tool do?" style="flex:1;min-width:200px">
        <button class="act primary" id="btn-cli">+ Register CLI tool</button>
      </div>
      <textarea id="cli-cmds" placeholder="Example commands (one per line)" rows="3"
        style="width:100%;margin-top:8px;box-sizing:border-box"></textarea>
    </div>
    <div class="grid">${tools.length ? tools.map(t => `
      <div class="card">
        <h3>${esc(t.name)}
          ${t.documented ? "" : '<span class="badge">undocumented</span>'}</h3>
        <div class="meta">${esc(t.description)}</div>
        ${t.commands ? `<pre class="log" style="max-height:140px">${esc(t.commands)}</pre>` : ""}
        ${t.documented ? `<div class="row">
          <button class="act danger" onclick="removeCli('${esc(t.name)}')">Remove manifest</button>
        </div>` : ""}
      </div>`).join("")
      : '<div class="empty">No CLI tools registered yet.</div>'}</div>`;
  document.getElementById("btn-cli").onclick = async () => {
    const name = document.getElementById("cli-name").value.trim();
    const description = document.getElementById("cli-desc").value.trim();
    const commands = document.getElementById("cli-cmds").value.trim();
    if (!name || !description) return toast("Name and description are required", true);
    try {
      await api("/api/clis", { method: "POST", body: JSON.stringify({ name, description, commands }) });
      toast(`CLI tool ${name} registered`);
      renderClis();
    } catch (e) { toast(e.message, true); }
  };
}

window.removeCli = async (name) => {
  if (!confirm(`Remove the manifest for '${name}'? (binaries in shared bin are kept)`)) return;
  try {
    await api(`/api/clis/${name}`, { method: "DELETE" });
    toast("Manifest removed");
    renderClis();
  } catch (e) { toast(e.message, true); }
};

/* ─── Incidents view ──────────────────────────────────────────────────── */

async function renderIncidents() {
  const inc = await api("/api/incidents");
  $main.innerHTML = inc.length ? `
    <div class="table-wrap">
    <table class="incidents">
      <thead><tr><th>#</th><th>Time</th><th>Agent</th><th>Type</th><th>Detail</th><th>Auto action</th><th>Fixer</th></tr></thead>
      <tbody>${inc.map(i => `
        <tr>
          <td data-label="#">${i.id}</td>
          <td data-label="Time" style="white-space:nowrap">${new Date(i.ts * 1000).toLocaleTimeString()}</td>
          <td data-label="Agent"><b>${esc(i.agent)}</b></td>
          <td data-label="Type" class="kind-${esc(i.kind)}">${esc(i.kind)}</td>
          <td data-label="Detail" class="detail">${esc(i.detail.slice(0, 400))}</td>
          <td data-label="Action">${esc(i.action)}</td>
          <td data-label="Fixer" class="detail">${esc(i.fixer)}</td>
        </tr>`).join("")}
      </tbody>
    </table></div>`
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
  clearInterval(graphTimer);
  if (typeof wfTeardown === "function") wfTeardown();
  if (view === "agents") { renderAgents(); refresh(); refreshTimer = setInterval(refresh, 5000); }
  else if (view === "workflows") { renderWorkflows(); }
  else if (view === "graph") { renderGraph(); }
  else if (view === "shared") { renderShared(); }
  else if (view === "clis") { renderClis(); }
  else if (view === "incidents") { renderIncidents(); refreshTimer = setInterval(renderIncidents, 6000); }
}

routeFromLocation();
refresh().then(render);
