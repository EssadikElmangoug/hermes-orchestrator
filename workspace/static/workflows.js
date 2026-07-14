/* Hermes Orchestrator — Workflows canvas.
 * Visual DAG editor over the fleet's resources: agent steps joined by flow
 * edges, capabilities (skills/CLIs/MCPs/env/plugins) attached to steps, plus
 * triggers, approval gates and channel/webhook outputs. The same document is
 * edited by the workflow-builder agent through the API, so the canvas polls
 * for remote changes and live-renders them ("watch it build").
 * Shares globals (api, esc, toast, $main, view) with app.js — no build step.
 */
"use strict";

/* ─── node type registry ──────────────────────────────────────────────── */

const WF_TYPES = {
  "trigger.manual":  { cat: "trigger", ico: "▶", name: "Manual trigger", sub: "start from the UI" },
  "trigger.cron":    { cat: "trigger", ico: "⏱", name: "Schedule",       sub: "cron timer" },
  "trigger.webhook": { cat: "trigger", ico: "⚡", name: "Webhook",        sub: "external HTTP call" },
  "step.agent":      { cat: "agent",   ico: "◈", name: "Agent step",     sub: "runs a task" },
  "gate.approval":   { cat: "logic",   ico: "✋", name: "Approval",       sub: "waits for a human" },
  "out.channel":     { cat: "output",  ico: "✉", name: "Channel",        sub: "deliver to a chat" },
  "out.webhook":     { cat: "output",  ico: "⇄", name: "Webhook out",    sub: "POST the result" },
  "cap.skill":       { cat: "cap",     ico: "✦", name: "Skill" },
  "cap.cli":         { cat: "cap",     ico: "⌘", name: "CLI tool" },
  "cap.mcp":         { cat: "cap",     ico: "⚙", name: "MCP server" },
  "cap.env":         { cat: "cap",     ico: "$", name: "Env key" },
  "cap.plugin":      { cat: "cap",     ico: "⬡", name: "Plugin" },
};
const WF_COLOR = { trigger: "#58a6ff", agent: "#d4a017", logic: "#bc8cff",
                   output: "#3fb950", cap: "#39c5cf" };
const NW = 212, NH = 72, CW = 166, CH = 44;      // node / capability size

const wfIsCap = t => t.startsWith("cap.");
const wfIsTrigger = t => t.startsWith("trigger.");
const wfIsSink = t => t.startsWith("out.");
const wfColor = t => WF_COLOR[(WF_TYPES[t] || {}).cat] || "#8b949e";

/* ─── state ───────────────────────────────────────────────────────────── */

let wfCur = null;            // open workflow doc (else the list is shown)
let wfRes = null;            // palette resources
let wfDirty = false, wfSaving = false, wfSaveTimer = null;
let wfSel = null;            // {node: id} | {nodes: [ids]} | {edge: index}
let wfRun = null;            // run being viewed (live or historical)
let wfRunTimer = null, wfPollTimer = null;
let wfChatOpen = false, wfChatBusy = false;
let wfPanel = null;          // "config" | "runs" | null
let wfCv = null;             // canvas state {view:{x,y,k}, drag, link, pointers, pinch}

function wfTeardown() {
  clearInterval(wfRunTimer); clearInterval(wfPollTimer);
  clearTimeout(wfSaveTimer);
  if (wfDirty) wfSave();                       // flush pending edits
  document.getElementById("main").classList.remove("wf-full");
}

/* ─── labels ──────────────────────────────────────────────────────────── */

function wfLabel(n) {
  const c = n.config || {};
  if (c.title) return c.title;
  if (n.type === "step.agent") return c.agent || "agent step";
  if (n.type === "out.channel") return c.channel || "channel";
  if (n.type === "trigger.cron") return c.schedule || "schedule";
  if (wfIsCap(n.type)) return c.name || WF_TYPES[n.type].name;
  return WF_TYPES[n.type].name;
}
function wfSubLabel(n) {
  const c = n.config || {};
  if (n.type === "step.agent") {
    const base = c.title ? (c.agent || "agent") : "agent step";
    return c.model ? `${base} · ${c.model.split("/").pop()}` : base;
  }
  if (n.type === "out.channel") return `via ${c.agent || "?"}`;
  if (wfIsCap(n.type)) return WF_TYPES[n.type].name;
  return WF_TYPES[n.type].sub || "";
}
const wfTrim = (s, m) => { s = String(s ?? ""); return s.length > m ? s.slice(0, m - 1) + "…" : s; };

/* ─── list view ───────────────────────────────────────────────────────── */

async function renderWorkflows() {
  // the router (app.js) sets wfRouteId: a workflow id to open, or null for
  // the list; undefined means no route opinion (e.g. programmatic rerender)
  if (window.wfRouteId !== undefined) {
    const rid = window.wfRouteId;
    window.wfRouteId = undefined;
    if (rid) return wfOpen(rid);
    wfCur = null;
  } else if (wfCur) return wfOpen(wfCur.id);
  document.getElementById("main").classList.remove("wf-full");
  let list = [];
  try { list = await api("/api/workflows"); } catch (e) { toast(e.message, true); }
  if (view !== "workflows") return;
  $main.innerHTML = `
    <div class="wf-hero">
      <div>
        <h2>Workflows</h2>
        <div class="sub">Chain agents, skills and channels into automations —
          drag them on the canvas, or let the AI builder do it from chat.</div>
      </div>
      <button class="wf-primary" id="wf-new">+ New workflow</button>
    </div>
    <div class="grid" id="wf-list">
      ${list.map(w => `
        <div class="wf-card" data-id="${esc(w.id)}">
          <h3>${esc(w.name)}</h3>
          <div class="meta">${esc(w.description) || "&nbsp;"}</div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px">
            <span class="badge">${w.nodes} node${w.nodes === 1 ? "" : "s"}</span>
            ${w.last_run ? `<span class="wf-status ${esc(w.last_run.status)}">${esc(w.last_run.status)}</span>` : '<span class="wf-status">never ran</span>'}
          </div>
          <div class="row">
            <button class="act primary" data-open>Open</button>
            <button class="act danger" data-del>Delete</button>
          </div>
        </div>`).join("")
      || `<div class="empty" style="grid-column:1/-1;padding:60px 0">
            No workflows yet.<br><br>
            <button class="wf-primary" id="wf-first">✨ Create your first workflow</button>
          </div>`}
    </div>`;
  const create = async () => {
    const name = prompt("Name the workflow:", "my-automation");
    if (name === null) return;
    try {
      const doc = await api("/api/workflows", { method: "POST", body: JSON.stringify({ name }) });
      wfOpen(doc.id);
    } catch (e) { toast(e.message, true); }
  };
  document.getElementById("wf-new").onclick = create;
  document.getElementById("wf-first")?.addEventListener("click", create);
  document.querySelectorAll(".wf-card").forEach(card => {
    const id = card.dataset.id;
    card.onclick = () => wfOpen(id);
    card.querySelector("[data-open]").onclick = e => { e.stopPropagation(); wfOpen(id); };
    card.querySelector("[data-del]").onclick = async e => {
      e.stopPropagation();
      if (!confirm("Delete this workflow and its run history?")) return;
      try { await api(`/api/workflows/${id}`, { method: "DELETE" }); renderWorkflows(); }
      catch (err) { toast(err.message, true); }
    };
  });
}

/* ─── editor ──────────────────────────────────────────────────────────── */

async function wfOpen(id) {
  try {
    const [doc, res] = await Promise.all([
      api(`/api/workflows/${id}`), wfRes ? Promise.resolve(wfRes) : api("/api/workflows/resources"),
    ]);
    wfCur = doc; wfRes = res;
  } catch (e) { wfCur = null; toast(e.message, true); return renderWorkflows(); }
  wfSel = null; wfRun = null; wfPanel = null; wfChatOpen = false;
  wfDirty = false; wfSaving = false;
  if (location.pathname !== `/workflows/${wfCur.id}`)
    history.pushState({}, "", `/workflows/${wfCur.id}`);
  wfRenderEditor();
}

function wfClose() {
  wfTeardown();
  wfCur = null;
  if (location.pathname !== "/workflows") history.pushState({}, "", "/workflows");
  renderWorkflows();
}

function wfRenderEditor() {
  document.getElementById("main").classList.add("wf-full");
  const hdr = document.querySelector("header");
  if (hdr) document.documentElement.style.setProperty("--wf-hdr", `${hdr.offsetHeight}px`);
  $main.innerHTML = `
    <div id="wf-editor">
      <div id="wf-topbar">
        <button class="act" id="wf-back">←</button>
        <input class="wf-name" id="wf-name" value="${esc(wfCur.name)}" maxlength="80">
        <span class="wf-savestate" id="wf-savestate">saved</span>
        <span id="wf-runchip"></span>
        <span style="flex:1"></span>
        <button class="act" id="wf-runs-btn">Runs</button>
        <button class="act" id="wf-chat-btn" style="border-color:var(--wf-logic);color:var(--wf-logic)">✨ AI Build</button>
        <button class="wf-primary" id="wf-run-btn">▶ Run</button>
      </div>
      <div id="wf-body">
        <div id="wf-palette"></div>
        <div id="wf-canvas-wrap">
          <svg id="wf-svg">
            <defs>
              <radialGradient id="wf-bgglow" cx="50%" cy="30%" r="80%">
                <stop offset="0%" stop-color="#131b27"/><stop offset="100%" stop-color="#0a0e15"/>
              </radialGradient>
              <pattern id="wf-dots" width="26" height="26" patternUnits="userSpaceOnUse">
                <circle cx="1.2" cy="1.2" r="1.2" fill="#2a3342"/>
              </pattern>
              <linearGradient id="wf-card" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="#202939"/><stop offset="100%" stop-color="#131a24"/>
              </linearGradient>
              <linearGradient id="wf-card-cap" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="#14212a"/><stop offset="100%" stop-color="#0e161d"/>
              </linearGradient>
              <filter id="wf-shadow" x="-40%" y="-40%" width="180%" height="180%">
                <feDropShadow dx="0" dy="6" stdDeviation="10" flood-color="#000" flood-opacity="0.55"/>
              </filter>
            </defs>
            <rect x="-100000" y="-100000" width="200000" height="200000" fill="url(#wf-bgglow)"/>
            <g id="wf-world">
              <rect x="-100000" y="-100000" width="200000" height="200000" fill="url(#wf-dots)" opacity=".55"/>
              <g id="wf-edges"></g><g id="wf-nodes"></g><g id="wf-templink"></g>
            </g>
          </svg>
          <div id="wf-tools">
            <button class="act" id="wf-fit">⤢ Fit</button>
            <button class="act" id="wf-zin">＋</button>
            <button class="act" id="wf-zout">−</button>
            <button class="act" id="wf-marquee" title="Box select — or hold Shift and drag">⬚</button>
            <button class="act" id="wf-clearrun" style="display:none">Hide run</button>
          </div>
          <div id="wf-hint">drag from a port to connect · click a node to configure · shift+drag to box-select · scroll or pinch to zoom</div>
          <button class="wf-fab" id="wf-fab">＋</button>
        </div>
      </div>
    </div>`;
  document.getElementById("wf-back").onclick = wfClose;
  document.getElementById("wf-name").onchange = e => {
    wfCur.name = e.target.value.trim() || "Untitled workflow"; wfMutate(false);
  };
  document.getElementById("wf-runs-btn").onclick = () => wfTogglePanel("runs");
  document.getElementById("wf-chat-btn").onclick = wfToggleChat;
  document.getElementById("wf-run-btn").onclick = wfRunDialog;
  document.getElementById("wf-fab").onclick = () => wfTogglePanel("palette");
  wfBuildPalette(document.getElementById("wf-palette"));
  wfSetupCanvas();
  wfDraw();
  wfUpdateRunChip();
  // watch for remote edits (workflow-builder / other sessions)
  clearInterval(wfPollTimer);
  wfPollTimer = setInterval(wfPollRemote, 3000);
}

/* ─── palette ─────────────────────────────────────────────────────────── */

function wfPaletteGroups() {
  const T = (type, preset, name, sub, extra) =>
    ({ type, preset: preset || {}, name, sub, ...(extra || {}) });
  const agents = (wfRes.agents || []);
  const usable = agents.filter(a => a.api);
  const groups = [
    { key: "triggers", sec: "Triggers", color: WF_COLOR.trigger, items: [
      T("trigger.manual", null, "Manual", "run from the UI"),
      T("trigger.cron", { schedule: "0 9 * * *" }, "Schedule", "cron timer"),
      T("trigger.webhook", null, "Webhook", "external HTTP call"),
    ]},
    { key: "agents", sec: "Agent steps", color: WF_COLOR.agent,
      note: usable.length === agents.length ? ""
        : "Greyed-out agents have no API server, so workflows can't send them "
          + "tasks. Enable it with API_SERVER_ENABLED=1 (+ PORT/KEY) in that "
          + "agent's .env and restart it.",
      items: [
        ...usable.map(a => T("step.agent", { agent: a.name }, a.name,
          a.description || "agent step", { status: a.running })),
        ...agents.filter(a => !a.api).map(a => T("step.agent", { agent: a.name },
          a.name, "no API server — not callable", { disabled: true })),
      ]},
    { key: "logic", sec: "Logic", color: WF_COLOR.logic, items: [
      T("gate.approval", null, "Approval gate", "pause for a human"),
    ]},
    { key: "outputs", sec: "Outputs", color: WF_COLOR.output,
      note: (wfRes.channels || []).length ? ""
        : "Link Telegram, WhatsApp… in an agent to deliver results",
      items: [
        ...(wfRes.channels || []).map(c => T("out.channel",
          { agent: c.agent, channel: c.channel }, c.channel, `via ${c.agent}`)),
        T("out.webhook", null, "Webhook out", "POST the result"),
      ]},
  ];
  const caps = [["skills", "Skills", "cap.skill", wfRes.skills],
                ["clis", "CLI tools", "cap.cli", wfRes.clis],
                ["mcp", "MCP servers", "cap.mcp", wfRes.mcp_servers],
                ["env", "Env keys", "cap.env", wfRes.env_keys],
                ["plugins", "Plugins", "cap.plugin", wfRes.plugins]];
  for (const [key, sec, type, names] of caps) {
    if (!(names || []).length) continue;
    groups.push({ key, sec, color: WF_COLOR.cap,
      items: names.map(n => T(type, { name: n }, n, WF_TYPES[type].name)) });
  }
  return groups;
}

// core sections start open; the long capability lists start collapsed
const WF_PAL_DEFAULT_OPEN = { triggers: 1, agents: 1, logic: 1, outputs: 1 };

function wfBuildPalette(host) {
  // the search input is created once and never re-rendered — re-rendering it
  // per keystroke reset the caret to position 0 (text came out reversed)
  host.innerHTML = `
    <div class="wf-pal-search"><span class="mag">⌕</span>
      <input id="wf-pal-q" placeholder="Search agents, skills, tools…"></div>
    <div class="wf-pal-scroll"></div>`;
  const q = host.querySelector("#wf-pal-q");
  const scroll = host.querySelector(".wf-pal-scroll");
  let open;
  try { open = JSON.parse(localStorage.getItem("wf.pal.open")) || { ...WF_PAL_DEFAULT_OPEN }; }
  catch { open = { ...WF_PAL_DEFAULT_OPEN }; }

  const itemHtml = it => {
    const t = WF_TYPES[it.type], c = wfColor(it.type);
    const st = it.status === undefined ? ""
      : `<span class="st ${it.status ? "on" : "off"}" title="${it.status ? "running" : "stopped"}"></span>`;
    return `<div class="wf-pal-item${it.disabled ? " dis" : ""}" style="--c:${c}"
              ${it.disabled ? 'title="This agent has no API server, so workflow steps cannot call it"' : ""}
              data-type="${esc(it.type)}" data-preset='${esc(JSON.stringify(it.preset))}'>
      <span class="ico">${t.ico}</span>
      <div><b>${esc(wfTrim(it.name, 24))}${st}</b>
        <div class="sub">${esc(wfTrim(it.sub || "", 34))}</div></div>
    </div>`;
  };

  const paint = () => {
    const f = q.value.trim().toLowerCase();
    scroll.innerHTML = wfPaletteGroups().map(g => {
      const items = g.items.filter(it =>
        !f || `${it.name} ${it.sub || ""} ${it.type}`.toLowerCase().includes(f));
      if (f && !items.length) return "";
      const isOpen = f ? true : !!open[g.key];      // searching expands all
      return `<div class="wf-pal-group ${isOpen ? "open" : ""}">
        <button class="wf-pal-head" data-g="${esc(g.key)}">
          <span class="dot" style="--gc:${g.color}"></span>
          <span class="t">${esc(g.sec)}</span>
          <span class="wf-pal-count">${items.length}</span>
          <span class="chev">▸</span>
        </button>
        ${isOpen ? `<div class="wf-pal-items">
          ${items.map(itemHtml).join("") || ""}
          ${g.note && !f ? `<div class="wf-pal-note">${esc(g.note)}</div>` : ""}
        </div>` : ""}
      </div>`;
    }).join("") ||
      `<div class="wf-pal-note" style="padding:14px 8px">Nothing matches “${esc(q.value.trim())}”.</div>`;
    scroll.querySelectorAll(".wf-pal-head").forEach(h => h.onclick = () => {
      open[h.dataset.g] = open[h.dataset.g] ? 0 : 1;
      localStorage.setItem("wf.pal.open", JSON.stringify(open));
      paint();
    });
    scroll.querySelectorAll(".wf-pal-item:not(.dis)").forEach(el => wfPaletteDrag(el, host));
  };
  q.oninput = paint;
  paint();
}

function wfPaletteDrag(el, host) {
  el.addEventListener("pointerdown", e => {
    if (e.pointerType !== "mouse") return;       // touch adds by tap
    e.preventDefault();
    const ghost = el.cloneNode(true);
    ghost.classList.add("wf-ghost");
    ghost.style.width = `${el.offsetWidth}px`;
    let moved = false;
    const move = ev => {
      moved = true;
      if (!ghost.parentNode) document.body.appendChild(ghost);
      ghost.style.left = `${ev.clientX}px`; ghost.style.top = `${ev.clientY}px`;
    };
    const up = ev => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      ghost.remove();
      const svg = document.getElementById("wf-svg");
      const r = svg?.getBoundingClientRect();
      if (moved && r && ev.clientX >= r.left && ev.clientX <= r.right &&
          ev.clientY >= r.top && ev.clientY <= r.bottom) {
        wfAddNode(el.dataset.type, JSON.parse(el.dataset.preset),
                  wfToWorld(ev.clientX, ev.clientY));
      } else if (!moved) wfAddNode(el.dataset.type, JSON.parse(el.dataset.preset));
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
  });
  // touch / pen: simple tap-to-add
  el.addEventListener("pointerup", e => {
    if (e.pointerType === "mouse") return;
    wfAddNode(el.dataset.type, JSON.parse(el.dataset.preset));
    if (wfPanel === "palette") wfTogglePanel(null);
  });
}

function wfAddNode(type, preset, at) {
  if (!at) {
    const svg = document.getElementById("wf-svg");
    const r = svg.getBoundingClientRect();
    at = wfToWorld(r.left + r.width / 2, r.top + r.height / 2);
    at.x += (Math.random() - 0.5) * 60; at.y += (Math.random() - 0.5) * 60;
  }
  const id = "n" + Date.now().toString(36) + Math.floor(Math.random() * 46656).toString(36);
  const cfg = { ...preset };
  wfCur.nodes.push({ id, type, x: Math.round(at.x), y: Math.round(at.y), config: cfg });
  wfSel = { node: id };
  wfMutate();
  wfOpenDrawer();
}

/* ─── canvas ──────────────────────────────────────────────────────────── */

function wfViewKey() { return `wf.view.${wfCur.id}`; }

function wfToWorld(cx, cy) {
  const svg = document.getElementById("wf-svg");
  const r = svg.getBoundingClientRect();
  const v = wfCv.view;
  return { x: (cx - r.left - v.x) / v.k, y: (cy - r.top - v.y) / v.k };
}

function wfSetupCanvas() {
  const svg = document.getElementById("wf-svg");
  const world = document.getElementById("wf-world");
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(wfViewKey())); } catch {}
  wfCv = { view: saved || { x: 80, y: 120, k: 1 }, drag: null, link: null,
           pointers: new Map(), pinch: null };
  const kick = () => {           // force repaint (same workaround as the graph view)
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    svg.appendChild(g); requestAnimationFrame(() => g.remove());
  };
  const apply = () => {
    world.setAttribute("transform",
      `translate(${wfCv.view.x},${wfCv.view.y}) scale(${wfCv.view.k})`);
    kick();
    localStorage.setItem(wfViewKey(), JSON.stringify(wfCv.view));
  };
  wfCv.apply = apply;
  const fit = () => {
    const ns = wfCur.nodes;
    if (!ns.length) { wfCv.view = { x: 100, y: 140, k: 1 }; return apply(); }
    const xs = ns.map(n => n.x), ys = ns.map(n => n.y), pad = 160;
    const r = svg.getBoundingClientRect();
    const k = Math.min(r.width / (Math.max(...xs) - Math.min(...xs) + 2 * pad),
                       r.height / (Math.max(...ys) - Math.min(...ys) + 2 * pad), 1.25);
    wfCv.view = { k,
      x: r.width / 2 - k * (Math.min(...xs) + Math.max(...xs)) / 2,
      y: r.height / 2 - k * (Math.min(...ys) + Math.max(...ys)) / 2 };
    apply();
  };
  if (!saved) requestAnimationFrame(fit); else apply();

  const zoomAt = (mx, my, f) => {
    const v = wfCv.view;
    const k = Math.min(2.2, Math.max(0.3, v.k * f));
    v.x = mx - (mx - v.x) * (k / v.k); v.y = my - (my - v.y) * (k / v.k); v.k = k;
    apply();
  };
  svg.addEventListener("wheel", e => {
    e.preventDefault();
    const r = svg.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.12 : 1 / 1.12);
  }, { passive: false });
  document.getElementById("wf-fit").onclick = fit;
  document.getElementById("wf-zin").onclick = () => {
    const r = svg.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 1.3); };
  document.getElementById("wf-zout").onclick = () => {
    const r = svg.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 1 / 1.3); };
  document.getElementById("wf-clearrun").onclick = () => {
    wfRun = null; clearInterval(wfRunTimer);
    document.getElementById("wf-clearrun").style.display = "none";
    wfBanner(); wfDraw(); wfUpdateRunChip();
  };
  document.getElementById("wf-marquee").onclick = () => {
    wfCv.marqueeMode = !wfCv.marqueeMode;
    document.getElementById("wf-marquee").classList.toggle("on", wfCv.marqueeMode);
    svg.style.cursor = wfCv.marqueeMode ? "crosshair" : "";
  };

  svg.addEventListener("pointerdown", e => {
    wfCv.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    try { svg.setPointerCapture(e.pointerId); } catch {}
    if (wfCv.pointers.size === 2) {              // pinch zoom
      const [p1, p2] = [...wfCv.pointers.values()];
      const r = svg.getBoundingClientRect();
      const mid = { x: (p1.x + p2.x) / 2 - r.left, y: (p1.y + p2.y) / 2 - r.top };
      wfCv.pinch = { d0: Math.hypot(p1.x - p2.x, p1.y - p2.y) || 1, k0: wfCv.view.k,
        w0: { x: (mid.x - wfCv.view.x) / wfCv.view.k, y: (mid.y - wfCv.view.y) / wfCv.view.k } };
      wfCv.drag = wfCv.link = null;
      return;
    }
    const port = e.target.closest(".wf-port");
    const nodeEl = e.target.closest(".wf-node");
    const edgeEl = e.target.closest(".wf-edge");
    const xBtn = e.target.closest(".wf-edge-x");
    if (xBtn) { wfDeleteEdge(+xBtn.dataset.idx); return; }
    if (port) {
      wfCv.link = { from: port.dataset.node, dir: port.dataset.dir,
                    x: e.clientX, y: e.clientY };
    } else if (nodeEl) {
      if (e.shiftKey) { wfToggleSel(nodeEl.dataset.id); return; }
      const selIds = wfSelIds();
      if (selIds.length > 1 && selIds.includes(nodeEl.dataset.id)) {
        // drag the whole selection as a group
        wfCv.drag = { group: selIds, start: wfToWorld(e.clientX, e.clientY),
          origins: Object.fromEntries(selIds.map(id => {
            const n = wfCur.nodes.find(x => x.id === id);
            return [id, { x: n.x, y: n.y }];
          })), moved: false };
      } else {
        const n = wfCur.nodes.find(x => x.id === nodeEl.dataset.id);
        wfCv.drag = { id: n.id, start: wfToWorld(e.clientX, e.clientY),
                      origin: { x: n.x, y: n.y }, moved: false };
      }
    } else if (edgeEl) {
      wfSel = { edge: +edgeEl.dataset.idx }; wfCloseDrawer(); wfSelBar(); wfDraw();
    } else if (e.shiftKey || wfCv.marqueeMode) {
      wfCv.marquee = { a: wfToWorld(e.clientX, e.clientY), b: null };
    } else {
      wfCv.drag = { pan: true, sx: e.clientX - wfCv.view.x, sy: e.clientY - wfCv.view.y, moved: false };
      if (wfSel) wfSetSel([]);
    }
  });
  svg.addEventListener("pointermove", e => {
    if (wfCv.pointers.has(e.pointerId))
      wfCv.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (wfCv.pinch && wfCv.pointers.size >= 2) {
      const [p1, p2] = [...wfCv.pointers.values()];
      const r = svg.getBoundingClientRect();
      const mid = { x: (p1.x + p2.x) / 2 - r.left, y: (p1.y + p2.y) / 2 - r.top };
      const d = Math.hypot(p1.x - p2.x, p1.y - p2.y) || 1;
      const v = wfCv.view;
      v.k = Math.min(2.2, Math.max(0.3, wfCv.pinch.k0 * d / wfCv.pinch.d0));
      v.x = mid.x - wfCv.pinch.w0.x * v.k; v.y = mid.y - wfCv.pinch.w0.y * v.k;
      apply();
      return;
    }
    if (wfCv.link) { wfCv.link.x = e.clientX; wfCv.link.y = e.clientY; wfDrawTempLink(); return; }
    if (wfCv.marquee) {
      wfCv.marquee.b = wfToWorld(e.clientX, e.clientY);
      wfDrawMarquee();
      return;
    }
    if (!wfCv.drag) return;
    wfCv.drag.moved = true;
    if (wfCv.drag.pan) {
      wfCv.view.x = e.clientX - wfCv.drag.sx; wfCv.view.y = e.clientY - wfCv.drag.sy; apply();
    } else if (wfCv.drag.group) {
      const w = wfToWorld(e.clientX, e.clientY);
      const dx = w.x - wfCv.drag.start.x, dy = w.y - wfCv.drag.start.y;
      for (const id of wfCv.drag.group) {
        const n = wfCur.nodes.find(x => x.id === id);
        const o = wfCv.drag.origins[id];
        if (n && o) { n.x = Math.round(o.x + dx); n.y = Math.round(o.y + dy); }
      }
      wfDraw();
    } else {
      const w = wfToWorld(e.clientX, e.clientY);
      const n = wfCur.nodes.find(x => x.id === wfCv.drag.id);
      n.x = Math.round(wfCv.drag.origin.x + w.x - wfCv.drag.start.x);
      n.y = Math.round(wfCv.drag.origin.y + w.y - wfCv.drag.start.y);
      wfDraw();
    }
  });
  const up = e => {
    wfCv.pointers.delete(e.pointerId);
    if (wfCv.pinch) { if (wfCv.pointers.size < 2) wfCv.pinch = null; return; }
    if (wfCv.marquee) {
      const m = wfCv.marquee;
      wfCv.marquee = null;
      document.getElementById("wf-templink").innerHTML = "";
      if (m.b && e.type === "pointerup") {
        const x1 = Math.min(m.a.x, m.b.x), x2 = Math.max(m.a.x, m.b.x);
        const y1 = Math.min(m.a.y, m.b.y), y2 = Math.max(m.a.y, m.b.y);
        wfSetSel(wfCur.nodes
          .filter(n => n.x >= x1 && n.x <= x2 && n.y >= y1 && n.y <= y2)
          .map(n => n.id));
      }
      return;
    }
    if (wfCv.link) {
      document.getElementById("wf-templink").innerHTML = "";
      if (e.type === "pointerup") {
        const el = document.elementFromPoint(e.clientX, e.clientY);
        const target = el && el.closest && el.closest(".wf-node");
        if (target && target.dataset.id !== wfCv.link.from)
          wfConnect(wfCv.link, target.dataset.id);
      }
      wfCv.link = null;
      return;
    }
    if (wfCv.drag && !wfCv.drag.moved && wfCv.drag.id && e.type === "pointerup") {
      wfSel = { node: wfCv.drag.id };
      wfSelBar(); wfDraw(); wfOpenDrawer();
    } else if (wfCv.drag && wfCv.drag.moved && (wfCv.drag.id || wfCv.drag.group)) {
      wfMutate(false);
    }
    wfCv.drag = null;
  };
  svg.addEventListener("pointerup", up);
  svg.addEventListener("pointercancel", up);

  document.onkeydown = e => {
    if (view !== "workflows" || !wfCur) return;
    if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "")) return;
    if (e.key === "Escape") {
      if (wfCv.marqueeMode) document.getElementById("wf-marquee")?.click();
      if (wfSel) wfSetSel([]);
      return;
    }
    if (!wfSel) return;
    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      if (wfSel.nodes) wfDeleteNodes(wfSel.nodes);
      else if (wfSel.node) wfDeleteNode(wfSel.node);
      else if (wfSel.edge != null) wfDeleteEdge(wfSel.edge);
    }
  };
}

/* ─── multi-selection ─────────────────────────────────────────────────── */

function wfSelIds() {
  return wfSel ? (wfSel.nodes || (wfSel.node ? [wfSel.node] : [])) : [];
}

function wfSetSel(ids) {
  wfSel = !ids.length ? null : ids.length === 1 ? { node: ids[0] } : { nodes: ids };
  if (!wfSel || wfSel.nodes) wfCloseDrawer();
  wfDraw(); wfSelBar();
}

function wfToggleSel(id) {
  const ids = new Set(wfSelIds());
  ids.has(id) ? ids.delete(id) : ids.add(id);
  wfSetSel([...ids]);
}

function wfSelBar() {
  document.getElementById("wf-selbar")?.remove();
  const ids = wfSel && wfSel.nodes ? wfSel.nodes : [];
  if (ids.length < 2) return;
  const bar = document.createElement("div");
  bar.id = "wf-selbar";
  bar.innerHTML = `<b>${ids.length}</b>&nbsp;nodes selected
    <button class="act danger" id="wf-selbar-del">Delete</button>
    <button class="act" id="wf-selbar-x">Clear</button>`;
  document.getElementById("wf-canvas-wrap")?.appendChild(bar);
  bar.querySelector("#wf-selbar-del").onclick = () => wfDeleteNodes(ids);
  bar.querySelector("#wf-selbar-x").onclick = () => wfSetSel([]);
}

function wfDeleteNodes(ids) {
  const gone = new Set(ids);
  wfCur.nodes = wfCur.nodes.filter(n => !gone.has(n.id));
  wfCur.edges = wfCur.edges.filter(e => !gone.has(e.from) && !gone.has(e.to));
  wfSel = null; wfCloseDrawer(); wfSelBar(); wfMutate();
}

function wfDrawMarquee() {
  const g = document.getElementById("wf-templink");
  const m = wfCv.marquee;
  if (!g || !m || !m.b) return;
  const x = Math.min(m.a.x, m.b.x), y = Math.min(m.a.y, m.b.y);
  const w = Math.abs(m.a.x - m.b.x), h = Math.abs(m.a.y - m.b.y);
  g.innerHTML = `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="6"
    fill="rgba(233,189,69,.07)" stroke="#e9bd45" stroke-width="1.4"
    stroke-dasharray="6 5"/>`;
  wfCv.apply();
}

/* connection rules */
function wfConnect(link, targetId) {
  const a = wfCur.nodes.find(n => n.id === link.from);
  const b = wfCur.nodes.find(n => n.id === targetId);
  if (!a || !b) return;
  let from = a, to = b, kind = "flow";
  if (link.dir === "in") [from, to] = [b, a];          // dragged backwards
  if (wfIsCap(a.type) || wfIsCap(b.type)) {
    kind = "cap";
    const cap = wfIsCap(a.type) ? a : b, step = cap === a ? b : a;
    if (wfIsCap(a.type) && wfIsCap(b.type)) return toast("Attach capabilities to an agent step", true);
    if (step.type !== "step.agent") return toast("Capabilities attach to agent steps", true);
    from = cap; to = step;
  } else {
    if (wfIsSink(from.type)) return toast("Outputs are end nodes", true);
    if (wfIsTrigger(to.type)) return toast("Triggers cannot receive input", true);
    // cheap client-side cycle check (the server re-validates)
    const adj = {};
    for (const e of wfCur.edges.filter(x => (x.kind || "flow") === "flow"))
      (adj[e.from] = adj[e.from] || []).push(e.to);
    const stack = [to.id], seen = new Set();
    while (stack.length) {
      const cur = stack.pop();
      if (cur === from.id) return toast("That would create a loop", true);
      if (!seen.has(cur)) { seen.add(cur); stack.push(...(adj[cur] || [])); }
    }
  }
  if (wfCur.edges.some(e => e.from === from.id && e.to === to.id)) return;
  wfCur.edges.push({ from: from.id, to: to.id, kind });
  wfMutate();
}

function wfDeleteNode(id) {
  wfCur.nodes = wfCur.nodes.filter(n => n.id !== id);
  wfCur.edges = wfCur.edges.filter(e => e.from !== id && e.to !== id);
  wfSel = null; wfCloseDrawer(); wfMutate();
}
function wfDeleteEdge(idx) {
  wfCur.edges.splice(idx, 1);
  wfSel = null; wfMutate();
}

/* ─── drawing ─────────────────────────────────────────────────────────── */

function wfPortPos(n, dir) {
  if (wfIsCap(n.type)) return { x: n.x, y: n.y - CH / 2 };
  if (dir === "in") return { x: n.x - NW / 2, y: n.y };
  return { x: n.x + NW / 2, y: n.y };
}

function wfEdgePath(e, byId) {
  const a = byId[e.from], b = byId[e.to];
  if (!a || !b) return "";
  if (e.kind === "cap") {
    const p1 = { x: a.x, y: a.y - CH / 2 }, p2 = { x: b.x, y: b.y + NH / 2 };
    return `M ${p1.x} ${p1.y} C ${p1.x} ${p1.y - 40}, ${p2.x} ${p2.y + 40}, ${p2.x} ${p2.y}`;
  }
  const p1 = wfPortPos(a, "out"), p2 = wfPortPos(b, "in");
  const h = Math.max(60, Math.abs(p2.x - p1.x) / 2);
  return `M ${p1.x} ${p1.y} C ${p1.x + h} ${p1.y}, ${p2.x - h} ${p2.y}, ${p2.x} ${p2.y}`;
}

function wfNodeStatus(id) {
  return wfRun && wfRun.nodes && wfRun.nodes[id] ? wfRun.nodes[id].status : null;
}
const WF_ST_COLOR = { running: "#d4a017", done: "#3fb950", failed: "#f85149",
                      waiting: "#d29922", pending: "#4b5666",
                      rejected: "#8b949e", cancelled: "#8b949e" };
const WF_ST_GLYPH = { running: "●", done: "✓", failed: "✕", waiting: "⏸", pending: "○" };

function wfDraw() {
  const byId = Object.fromEntries(wfCur.nodes.map(n => [n.id, n]));
  const edgesG = document.getElementById("wf-edges");
  const nodesG = document.getElementById("wf-nodes");
  if (!edgesG || !nodesG) return;
  edgesG.innerHTML = wfCur.edges.map((e, i) => {
    const cap = e.kind === "cap";
    const selected = wfSel && wfSel.edge === i;
    const tgtRunning = wfNodeStatus(e.to) === "running";
    const stroke = selected ? "#e9bd45" : cap ? "rgba(57,197,207,.65)"
      : tgtRunning ? "#d4a017" : "#41506a";
    const mid = (() => {          // midpoint for the delete button
      const a = byId[e.from], b = byId[e.to];
      return a && b ? { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - (cap ? 10 : 0) } : null;
    })();
    return `
      <g class="wf-edge" data-idx="${i}">
        <path d="${wfEdgePath(e, byId)}" fill="none" stroke="transparent" stroke-width="16"/>
        <path d="${wfEdgePath(e, byId)}" fill="none" stroke="${stroke}"
          stroke-width="${selected ? 2.6 : 2}" ${cap ? 'stroke-dasharray="4 5"' : ""}
          class="${tgtRunning ? "wf-edge-live" : ""}" opacity="${cap ? .9 : .95}"/>
        ${selected && mid ? `
          <g class="wf-edge-x" data-idx="${i}" style="cursor:pointer">
            <circle cx="${mid.x}" cy="${mid.y}" r="11" fill="#1c2430" stroke="#f85149"/>
            <text x="${mid.x}" y="${mid.y + 4}" text-anchor="middle" fill="#f85149" font-size="12">✕</text>
          </g>` : ""}
      </g>`;
  }).join("");

  const selIds = new Set(wfSelIds());
  nodesG.innerHTML = wfCur.nodes.map(n => {
    const c = wfColor(n.type), t = WF_TYPES[n.type];
    const sel = selIds.has(n.id);
    const st = wfNodeStatus(n.id);
    const stC = st ? WF_ST_COLOR[st] : null;
    if (wfIsCap(n.type)) {
      return `<g class="wf-node ${st === "running" ? "wf-running" : ""}" data-id="${esc(n.id)}"
                 transform="translate(${n.x},${n.y})">
        <rect x="${-CW / 2}" y="${-CH / 2}" width="${CW}" height="${CH}" rx="21"
          fill="url(#wf-card-cap)" stroke="${sel ? "#e9bd45" : "rgba(57,197,207,.5)"}"
          stroke-width="${sel ? 2 : 1.2}" filter="url(#wf-shadow)"/>
        <text x="${-CW / 2 + 16}" y="5" fill="${c}" font-size="12">${t.ico}</text>
        <text x="${-CW / 2 + 36}" y="-1" fill="#cfd8e3" font-size="11.5" font-weight="600">${esc(wfTrim(wfLabel(n), 17))}</text>
        <text x="${-CW / 2 + 36}" y="13" fill="#67707d" font-size="8.5" letter-spacing="1">${esc(wfSubLabel(n).toUpperCase())}</text>
        <circle class="wf-port" data-node="${esc(n.id)}" data-dir="cap"
          cx="0" cy="${-CH / 2}" r="6.5" fill="#0d1117" stroke="${c}" stroke-width="1.6"/>
      </g>`;
    }
    const ports = [];
    if (!wfIsTrigger(n.type)) ports.push(`<circle class="wf-port" data-node="${esc(n.id)}"
      data-dir="in" cx="${-NW / 2}" cy="0" r="7" fill="#0d1117" stroke="${c}" stroke-width="1.8"/>`);
    if (!wfIsSink(n.type)) ports.push(`<circle class="wf-port" data-node="${esc(n.id)}"
      data-dir="out" cx="${NW / 2}" cy="0" r="7" fill="#0d1117" stroke="${c}" stroke-width="1.8"/>`);
    return `<g class="wf-node ${st === "running" ? "wf-running" : ""}" data-id="${esc(n.id)}"
               transform="translate(${n.x},${n.y})">
      <rect x="${-NW / 2}" y="${-NH / 2}" width="${NW}" height="${NH}" rx="14"
        fill="url(#wf-card)" stroke="${sel ? "#e9bd45" : (stC || `color-mix(in srgb, ${c} 42%, #30363d)`)}"
        stroke-width="${sel || (st && st !== "pending") ? 2 : 1.2}" filter="url(#wf-shadow)"/>
      <rect x="${-NW / 2}" y="${-NH / 2}" width="${NW}" height="3.5" rx="1.75" fill="${c}" opacity=".9"/>
      <rect x="${-NW / 2 + 12}" y="-15" width="30" height="30" rx="9"
        fill="color-mix(in srgb, ${c} 13%, transparent)" stroke="color-mix(in srgb, ${c} 40%, transparent)"/>
      <text x="${-NW / 2 + 27}" y="6" text-anchor="middle" fill="${c}" font-size="14">${t.ico}</text>
      <text x="${-NW / 2 + 52}" y="0" fill="#e9eef5" font-size="13.5" font-weight="650">${esc(wfTrim(wfLabel(n), 18))}</text>
      <text x="${-NW / 2 + 52}" y="17" fill="#7d8794" font-size="9" letter-spacing="1.4">${esc(wfSubLabel(n).toUpperCase().slice(0, 26))}</text>
      ${st ? `<g><circle cx="${NW / 2 - 16}" cy="${-NH / 2 + 16}" r="9" fill="#0d1117"
          stroke="${stC}" stroke-width="1.6"/>
        <text x="${NW / 2 - 16}" y="${-NH / 2 + 20}" text-anchor="middle"
          fill="${stC}" font-size="10">${WF_ST_GLYPH[st] || "○"}</text></g>` : ""}
      ${ports.join("")}
    </g>`;
  }).join("");
  wfCv && wfCv.apply && wfCv.apply();
}

function wfDrawTempLink() {
  const g = document.getElementById("wf-templink");
  const link = wfCv.link;
  if (!g || !link) return;
  const n = wfCur.nodes.find(x => x.id === link.from);
  const p1 = wfPortPos(n, link.dir === "in" ? "in" : "out");
  const p2 = wfToWorld(link.x, link.y);
  g.innerHTML = `<path d="M ${p1.x} ${p1.y} L ${p2.x} ${p2.y}" fill="none"
    stroke="#e9bd45" stroke-width="2" stroke-dasharray="6 5" opacity=".85"/>
    <circle cx="${p2.x}" cy="${p2.y}" r="4" fill="#e9bd45"/>`;
  wfCv.apply();
}

/* ─── persistence ─────────────────────────────────────────────────────── */

function wfMutate(redraw = true) {
  wfDirty = true;
  wfSetSaveState("editing…");
  clearTimeout(wfSaveTimer);
  wfSaveTimer = setTimeout(wfSave, 800);
  if (redraw) wfDraw();
}

async function wfSave() {
  if (!wfCur || wfSaving) return;
  wfSaving = true; wfDirty = false;
  wfSetSaveState("saving…");
  try {
    const doc = await api(`/api/workflows/${wfCur.id}`, {
      method: "PUT",
      body: JSON.stringify({ name: wfCur.name, description: wfCur.description || "",
                             nodes: wfCur.nodes, edges: wfCur.edges }),
    });
    wfCur.updated_at = doc.updated_at;
    wfCur.edges = doc.edges;                     // server normalizes cap edges
    wfSetSaveState(wfDirty ? "editing…" : "saved");
  } catch (e) {
    wfDirty = true;
    wfSetSaveState("save failed");
    toast(`Save failed: ${e.message}`, true);
  }
  wfSaving = false;
  if (wfDirty) { clearTimeout(wfSaveTimer); wfSaveTimer = setTimeout(wfSave, 800); }
}

function wfSetSaveState(s) {
  const el = document.getElementById("wf-savestate");
  if (el) { el.textContent = s; el.style.color = s === "save failed" ? "var(--red)" : ""; }
}

async function wfPollRemote() {
  if (!wfCur || view !== "workflows" || wfDirty || wfSaving) return;
  if (wfCv && (wfCv.drag || wfCv.link || wfCv.pinch || wfCv.marquee)) return;
  try {
    const doc = await api(`/api/workflows/${wfCur.id}`);
    if (doc.updated_at > (wfCur.updated_at || 0) + 0.001) {
      const name = document.getElementById("wf-name");
      wfCur = doc;
      if (name && document.activeElement !== name) name.value = doc.name;
      if (wfSel && wfSel.node && !doc.nodes.some(n => n.id === wfSel.node)) {
        wfSel = null; wfCloseDrawer();
      }
      wfDraw();
      if (wfSel && wfSel.node && wfPanel === "config") wfOpenDrawer();
    }
  } catch {}
}

/* ─── node config drawer ──────────────────────────────────────────────── */

function wfCloseDrawer() { if (wfPanel === "config") wfTogglePanel(null); }

function wfPanelHost() {
  let el = document.getElementById("wf-side");
  if (!el) {
    el = document.createElement("div");
    el.id = "wf-side"; el.className = "wf-panel";
    document.getElementById("wf-body")?.appendChild(el);
  }
  return el;
}

function wfTogglePanel(which) {
  if (which === wfPanel) which = null;
  wfPanel = which;
  document.getElementById("wf-side")?.remove();
  if (!which) return;
  if (which === "config") return wfOpenDrawer(true);
  if (which === "runs") return wfRunsPanel();
  if (which === "palette") {
    const host = wfPanelHost();
    host.innerHTML = "";
    const inner = document.createElement("div");
    host.appendChild(inner);
    wfBuildPalette(inner);
    inner.insertAdjacentHTML("afterbegin",
      `<h3>Add a node <button class="x" onclick="wfTogglePanel(null)">✕</button></h3>`);
  }
}

function wfField(label, inner) {
  return `<label>${label}</label>${inner}`;
}

function wfOpenDrawer(force) {
  if (!wfSel || !wfSel.node) return;
  const n = wfCur.nodes.find(x => x.id === wfSel.node);
  if (!n) return;
  wfPanel = "config";
  const host = wfPanelHost();
  const t = WF_TYPES[n.type], c = wfColor(n.type), cfg = n.config || (n.config = {});
  const nr = wfRun && wfRun.nodes ? wfRun.nodes[n.id] : null;
  let fields = wfField("Title (optional)",
    `<input data-k="title" value="${esc(cfg.title || "")}" placeholder="${esc(wfLabel(n))}">`);

  if (n.type === "step.agent") {
    const agents = (wfRes.agents || []).filter(a => a.api);
    const cur = agents.find(a => a.name === cfg.agent);
    const models = wfRes.models || [];
    const provs = [...new Set(models.map(m => m.provider))];
    fields += wfField("Agent", `<select data-k="agent">
        ${agents.map(a => `<option ${a.name === cfg.agent ? "selected" : ""}>${esc(a.name)}</option>`).join("")}
      </select>`) +
      wfField("Model for this step", `<select data-k="model">
        <option value="">Agent default${cur && cur.model ? ` (${esc(cur.model)})` : ""}</option>
        ${provs.map(p => {
          const ms = models.filter(m => m.provider === p);
          const ready = ms[0].ready;
          return `<optgroup label="${esc(p)}${ready ? "" : " — no shared credentials"}">
            ${ms.map(m => `<option value="${esc(m.id)}" ${m.id === cfg.model ? "selected" : ""}>${esc(m.model)}</option>`).join("")}
          </optgroup>`;
        }).join("")}
      </select>`) +
      wfField("Instruction", `<textarea data-k="instruction" rows="6"
        placeholder="What should this step do? Upstream outputs are attached automatically.">${esc(cfg.instruction || "")}</textarea>`) +
      wfField("Output", `<select data-k="output">
        <option value="text" ${cfg.output !== "json" ? "selected" : ""}>Free text</option>
        <option value="json" ${cfg.output === "json" ? "selected" : ""}>Strict JSON</option></select>`) +
      (cfg.output === "json" ? wfField("JSON fields (comma-separated)",
        `<input data-k="json_fields" value="${esc(cfg.json_fields || "")}" placeholder="title, script, sources">`) : "") +
      `<div class="meta" style="margin-top:12px">Attach skills, CLI tools, MCP servers,
        env keys or plugins by linking capability nodes to this step.</div>`;
  } else if (n.type === "trigger.cron") {
    fields += wfField("Cron schedule (min hour dom mon dow)",
      `<input data-k="schedule" value="${esc(cfg.schedule || "")}" placeholder="0 9 * * *">`) +
      `<div class="meta" style="margin-top:8px">e.g. <code>0 9 * * *</code> daily 09:00 ·
        <code>*/30 * * * *</code> every 30 min · <code>0 18 * * 5</code> Fridays 18:00</div>`;
  } else if (n.type === "trigger.webhook") {
    const url = `${location.origin}/api/hooks/${wfCur.id}/${wfCur.hook_secret || ""}`;
    fields += wfField("Trigger URL (POST — body becomes the workflow input)",
      `<pre style="max-height:none">${esc(url)}</pre>
       <button class="act" id="wf-copyhook" style="margin-top:6px">Copy URL</button>`);
  } else if (n.type === "trigger.manual") {
    fields += `<div class="meta" style="margin-top:10px">Runs when you press ▶ Run.
      You can pass optional input text at run time.</div>`;
  } else if (n.type === "gate.approval") {
    fields += `<div class="meta" style="margin-top:10px">The run pauses here. Approve or
      reject from the banner on the canvas (or the run view). The gate passes its
      input through unchanged.</div>`;
  } else if (n.type === "out.channel") {
    const chans = wfRes.channels || [];
    fields += wfField("Deliver via", `<select data-k="_chan">
        ${chans.map(cc => {
          const v = `${cc.agent}|${cc.channel}`;
          const sel = cc.agent === cfg.agent && cc.channel === cfg.channel;
          return `<option value="${esc(v)}" ${sel ? "selected" : ""}>${esc(cc.channel)} — via ${esc(cc.agent)}</option>`;
        }).join("") || "<option value=''>no channels linked</option>"}
      </select>`) +
      wfField("Target (chat id / phone / handle — empty = default chat)",
        `<input data-k="target" value="${esc(cfg.target || "")}" placeholder="default chat">`);
  } else if (n.type === "out.webhook") {
    fields += wfField("POST URL", `<input data-k="url" value="${esc(cfg.url || "")}"
      placeholder="https://example.com/hook">`);
  } else if (wfIsCap(n.type)) {
    const pool = { "cap.skill": wfRes.skills, "cap.cli": wfRes.clis, "cap.mcp": wfRes.mcp_servers,
                   "cap.env": wfRes.env_keys, "cap.plugin": wfRes.plugins }[n.type] || [];
    fields += wfField(t.name, `<select data-k="name">
        ${pool.map(p => `<option ${p === cfg.name ? "selected" : ""}>${esc(p)}</option>`).join("")}
      </select>`) +
      `<div class="meta" style="margin-top:10px">Link this to an agent step — the step is
        told to use it.</div>`;
  }

  host.innerHTML = `
    <h3><span style="color:${c}">${t.ico}</span> ${esc(t.name)}
      <button class="x" id="wf-drawer-x">✕</button></h3>
    <div class="meta" style="margin:0">${esc(wfLabel(n))}</div>
    ${fields}
    ${nr && (nr.output || nr.error) ? `
      <label>Run ${nr.error ? "error" : "output"}
        <span class="wf-out-pill" style="margin-left:6px">${esc(nr.status)}</span>
        ${nr.model ? `<span class="wf-out-pill" style="margin-left:4px">${esc(nr.model)}</span>` : ""}</label>
      <pre>${esc(nr.error || nr.output)}</pre>` : ""}
    <label>&nbsp;</label>
    <button class="act danger" id="wf-del-node" style="width:100%">Delete node</button>`;
  host.querySelector("#wf-drawer-x").onclick = () => { wfSel = null; wfTogglePanel(null); wfDraw(); };
  host.querySelector("#wf-del-node").onclick = () => wfDeleteNode(n.id);
  host.querySelector("#wf-copyhook")?.addEventListener("click", () => {
    navigator.clipboard?.writeText(`${location.origin}/api/hooks/${wfCur.id}/${wfCur.hook_secret}`);
    toast("Webhook URL copied");
  });
  host.querySelectorAll("[data-k]").forEach(el => {
    el.onchange = () => {
      if (el.dataset.k === "_chan") {
        const [agent, channel] = el.value.split("|");
        cfg.agent = agent; cfg.channel = channel;
      } else cfg[el.dataset.k] = el.value;
      if (el.dataset.k === "output") wfOpenDrawer(true);   // reveal json fields
      wfMutate();
    };
  });
}

/* ─── running ─────────────────────────────────────────────────────────── */

function wfRunDialog() {
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.innerHTML = `<div class="modal" style="width:min(480px,92vw)">
    <h2>▶ Run “${esc(wfCur.name)}”</h2>
    <div class="meta">Optional input for the trigger — it becomes the first step's input.</div>
    <textarea id="wf-run-input" rows="3" style="width:100%;box-sizing:border-box;margin-top:10px"
      placeholder="(no input)"></textarea>
    <div class="row" style="margin-top:14px">
      <button class="wf-primary" id="wf-run-go">Run now</button>
      <button class="act" id="wf-run-cancel">Cancel</button>
    </div></div>`;
  document.body.appendChild(bg);
  bg.onclick = e => { if (e.target === bg) bg.remove(); };
  bg.querySelector("#wf-run-cancel").onclick = () => bg.remove();
  bg.querySelector("#wf-run-go").onclick = async () => {
    const input = bg.querySelector("#wf-run-input").value;
    bg.remove();
    if (wfDirty) await wfSave();
    try {
      wfRun = await api(`/api/workflows/${wfCur.id}/run`,
        { method: "POST", body: JSON.stringify({ input }) });
      toast("Run started");
      wfWatchRun();
    } catch (e) { toast(e.message, true); }
  };
}

function wfWatchRun() {
  document.getElementById("wf-clearrun").style.display = "";
  wfDraw(); wfUpdateRunChip(); wfBanner();
  clearInterval(wfRunTimer);
  wfRunTimer = setInterval(async () => {
    if (!wfRun || view !== "workflows") return clearInterval(wfRunTimer);
    try {
      const fresh = await api(`/api/workflows/${wfCur.id}/runs/${wfRun.id}`);
      const done = !["running", "waiting"].includes(fresh.status);
      wfRun = fresh;
      wfDraw(); wfUpdateRunChip(); wfBanner();
      if (wfSel && wfSel.node && wfPanel === "config") wfOpenDrawer(true);
      if (done) {
        clearInterval(wfRunTimer);
        toast(`Run ${fresh.status}${fresh.error ? `: ${fresh.error}` : ""}`,
              fresh.status !== "success");
      }
    } catch {}
  }, 1500);
}

function wfUpdateRunChip() {
  const el = document.getElementById("wf-runchip");
  if (!el) return;
  el.innerHTML = wfRun
    ? `<span class="wf-status ${esc(wfRun.status)}">${esc(wfRun.status)}</span>` : "";
}

function wfBanner() {
  document.getElementById("wf-banner")?.remove();
  if (!wfRun || wfRun.status !== "waiting") return;
  const gate = Object.entries(wfRun.nodes || {}).find(([, v]) => v.status === "waiting");
  if (!gate) return;
  const node = wfCur.nodes.find(n => n.id === gate[0]);
  const div = document.createElement("div");
  div.id = "wf-banner";
  div.innerHTML = `⏸ Waiting for approval at <b>${esc(node ? wfLabel(node) : gate[0])}</b>
    <button class="act" id="wf-appr-view">View output</button>
    <button class="act" style="border-color:var(--green);color:var(--green)" id="wf-appr-ok">Approve</button>
    <button class="act danger" id="wf-appr-no">Reject</button>`;
  document.getElementById("wf-canvas-wrap")?.appendChild(div);
  div.querySelector("#wf-appr-view").onclick = () => { wfSel = { node: gate[0] }; wfDraw(); wfOpenDrawer(true); };
  const decide = approve => async () => {
    try {
      await api(`/api/workflows/${wfCur.id}/runs/${wfRun.id}/approve`,
        { method: "POST", body: JSON.stringify({ node_id: gate[0], approve }) });
      toast(approve ? "Approved — resuming" : "Rejected");
    } catch (e) { toast(e.message, true); }
  };
  div.querySelector("#wf-appr-ok").onclick = decide(true);
  div.querySelector("#wf-appr-no").onclick = decide(false);
}

async function wfRunsPanel() {
  const host = wfPanelHost();
  host.innerHTML = `<h3>Run history <button class="x" onclick="wfTogglePanel(null)">✕</button></h3>
    <div id="wf-runs-list" class="empty">Loading…</div>`;
  let runs = [];
  try { runs = await api(`/api/workflows/${wfCur.id}/runs`); } catch (e) { toast(e.message, true); }
  const list = host.querySelector("#wf-runs-list");
  if (!list) return;
  list.className = "";
  list.innerHTML = runs.length ? runs.map(r => `
    <div class="wf-run-item" data-id="${esc(r.id)}">
      <span class="wf-status ${esc(r.status)}">${esc(r.status)}</span>
      <span>${esc(r.trigger)}</span>
      <span class="t">${new Date(r.started * 1000).toLocaleString()}</span>
    </div>`).join("")
    : `<div class="empty">No runs yet — press ▶ Run.</div>`;
  list.querySelectorAll(".wf-run-item").forEach(el => {
    el.onclick = async () => {
      try {
        wfRun = await api(`/api/workflows/${wfCur.id}/runs/${el.dataset.id}`);
        wfTogglePanel(null);
        if (["running", "waiting"].includes(wfRun.status)) wfWatchRun();
        else {
          document.getElementById("wf-clearrun").style.display = "";
          wfDraw(); wfUpdateRunChip(); wfBanner();
        }
      } catch (e) { toast(e.message, true); }
    };
  });
}

/* ─── AI builder chat ─────────────────────────────────────────────────── */

function wfChatKey() { return `wf.chat.${wfCur.id}`; }
function wfChatLoad() {
  try { return JSON.parse(localStorage.getItem(wfChatKey())) || []; } catch { return []; }
}
function wfChatSave(msgs) {
  localStorage.setItem(wfChatKey(), JSON.stringify(msgs.slice(-60)));
}

function wfToggleChat() {
  wfChatOpen = !wfChatOpen;
  document.getElementById("wf-chat")?.remove();
  if (!wfChatOpen) return;
  document.getElementById("wf-side")?.remove(); wfPanel = null;
  const el = document.createElement("div");
  el.id = "wf-chat"; el.className = "wf-panel";
  el.innerHTML = `
    <h3><span style="color:var(--wf-logic)">✨</span> AI Builder
      <span class="wf-out-pill">workflow-builder</span>
      <button class="x" id="wf-chat-x">✕</button></h3>
    <div id="wf-chat-log"></div>
    <div id="wf-chat-row">
      <textarea id="wf-chat-in" rows="2" placeholder="Describe the automation… e.g. “every morning research AI news and send a summary to my telegram”"></textarea>
      <button class="wf-primary" id="wf-chat-send">Send</button>
    </div>`;
  document.getElementById("wf-body").appendChild(el);
  el.querySelector("#wf-chat-x").onclick = wfToggleChat;
  const log = el.querySelector("#wf-chat-log");
  const msgs = wfChatLoad();
  const paint = () => {
    log.innerHTML = msgs.map(m =>
      `<div class="wf-msg ${m.role === "user" ? "user" : "bot"}">${esc(m.text)}</div>`).join("")
      + (wfChatBusy ? `<div class="wf-msg bot thinking">building</div>` : "")
      || `<div class="meta" style="padding:8px">Tell me what to automate — I'll build it
          on the canvas while you watch. I can use every agent, skill, CLI tool and
          channel in this workspace.</div>`;
    log.scrollTop = log.scrollHeight;
  };
  paint();
  const input = el.querySelector("#wf-chat-in");
  const send = async () => {
    const text = input.value.trim();
    if (!text || wfChatBusy) return;
    input.value = "";
    msgs.push({ role: "user", text }); wfChatSave(msgs);
    wfChatBusy = true; paint();
    if (wfDirty) await wfSave();
    try {
      const r = await api("/api/workflows/chat", {
        method: "POST",
        body: JSON.stringify({ workflow_id: wfCur.id, message: text }),
      });
      msgs.push({ role: "bot", text: r.reply });
      if (r.workflow && r.workflow.updated_at > (wfCur.updated_at || 0)) {
        wfCur = r.workflow; wfDraw();
        const name = document.getElementById("wf-name");
        if (name) name.value = wfCur.name;
      }
    } catch (e) {
      msgs.push({ role: "bot", text: `⚠ ${e.message}` });
    }
    wfChatBusy = false;
    wfChatSave(msgs); paint();
  };
  el.querySelector("#wf-chat-send").onclick = send;
  input.onkeydown = e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  };
  input.focus();
}
