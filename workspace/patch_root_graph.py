"""Patch hermes_cli/web_server.py (venv + source copies) so the dashboard:

1. serves a workspace agent-graph page at "/" (instead of the SPA's
   client-side redirect to /sessions),
2. proxies the workspace orchestrator's /api/graph as /workspace/graph.json
   (same-origin, no CORS),
3. injects a script into the SPA HTML that turns the "Hermes Agent"
   sidebar/header brand into a link back to "/".

Idempotent: skips a file if the patch marker is already present.
"""

import sys

FILES = [
    "/home/sedx3d/Desktop/ai agents workplace/hermes-venv/lib/python3.13/site-packages/hermes_cli/web_server.py",
    "/home/sedx3d/Desktop/ai agents workplace/hermes-agent-main/hermes_cli/web_server.py",
]

MARKER = "_WORKSPACE_GRAPH_HTML"

CONSTANTS = '''
# ── Local workspace patch (essadik agents workspace) ───────────────────────
# Serves the multi-agent workspace graph at "/" (instead of the SPA's
# redirect-to-/sessions) and makes the "Hermes Agent" brand a home link.
# Graph data comes from the workspace orchestrator (default
# http://127.0.0.1:9100, override with HERMES_WORKSPACE_URL) via a
# same-origin proxy so no CORS is involved.

_WORKSPACE_TITLE_LINK_SCRIPT = r"""
<script>
(function () {
  var HOME = (window.__HERMES_BASE_PATH__ || "") + "/";
  var pending = false;
  function wire() {
    pending = false;
    var scopes = document.querySelectorAll("aside, header");
    for (var s = 0; s < scopes.length; s++) {
      var els = scopes[s].querySelectorAll("p, span, div, h1, h2");
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (el.__wsHome || el.childElementCount > 1) continue;
        var t = (el.textContent || "").replace(/\\s+/g, "").toLowerCase();
        if (t === "hermesagent") {
          el.__wsHome = true;
          el.style.cursor = "pointer";
          el.title = "Agent graph (home)";
          el.addEventListener("click", function () { window.location.href = HOME; });
        }
      }
    }
  }
  new MutationObserver(function () {
    if (pending) return;
    pending = true;
    requestAnimationFrame(wire);
  }).observe(document.documentElement, { childList: true, subtree: true });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
</script>
"""

_WORKSPACE_GRAPH_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Agent — Agent Graph</title>
<style>
:root { --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3;
  --muted:#8b949e; --accent:#d4a017; --green:#3fb950; --red:#f85149; --amber:#d29922; }
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif}
header{display:flex;align-items:center;gap:24px;padding:14px 24px;border-bottom:1px solid var(--border);background:var(--panel)}
h1{font-size:16px;letter-spacing:2px;text-transform:uppercase;color:var(--accent)}
nav{margin-left:auto;display:flex;gap:12px}
nav a{color:var(--muted);text-decoration:none;border:1px solid var(--border);padding:6px 14px;border-radius:6px;font-size:13px}
nav a:hover{color:var(--text);border-color:var(--accent)}
main{padding:24px;max-width:1200px;margin:0 auto}
#err{display:none;background:#3d1d1f;border:1px solid var(--red);color:#ffb4af;padding:12px 16px;border-radius:8px;margin-bottom:16px}
#graph-svg{width:100%;min-height:480px;background:var(--panel);border:1px solid var(--border);border-radius:10px}
.meta{color:var(--muted);font-size:12px;margin-top:10px}
</style>
</head>
<body>
<header>
  <h1>Hermes Agent · Agent Graph</h1>
  <nav>
    <a href="/sessions">Dashboard</a>
    <a id="ws-link" href="http://127.0.0.1:9100/" target="_blank" rel="noopener">Workspace console</a>
  </nav>
</header>
<main>
  <div id="err"></div>
  <svg id="graph-svg"></svg>
  <div class="meta">Green: running &amp; healthy · amber: running but failing health checks ·
    red: stopped. Click an agent to open the workspace console. Auto-refreshes every 8s.</div>
</main>
<script>
const WS_URL = "http://127.0.0.1:9100/";
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function render() {
  const err = document.getElementById("err");
  let g;
  try {
    const r = await fetch("/workspace/graph.json", { cache: "no-store" });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || ("HTTP " + r.status));
    g = body;
    err.style.display = "none";
  } catch (e) {
    err.textContent = "Workspace orchestrator unreachable (" + e.message +
      ") — start it with: cd workspace && setsid ../hermes-venv/bin/python server.py";
    err.style.display = "block";
    return;
  }
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
    const cx = i * colW + colW / 2, gy = 70;
    const color = !gw.running ? "var(--red)" : (gw.healthy ? "var(--green)" : "var(--amber)");
    const subs = subsOf(gw.id);
    subs.forEach((s, j) => {
      const sy = 190 + j * 74;
      parts.push(`<line x1="${cx}" y1="${gy + 34}" x2="${cx}" y2="${sy - 22}" stroke="var(--border)" stroke-width="1.5"/>`);
      parts.push(`<g><rect x="${cx - 80}" y="${sy - 22}" width="160" height="44" rx="8" fill="var(--bg)" stroke="var(--border)"/>
        <text x="${cx}" y="${sy + 4}" text-anchor="middle" fill="var(--text)" font-size="12">${esc(s.label)}</text></g>`);
    });
    parts.push(`<g style="cursor:pointer" onclick="window.open(WS_URL)">
      <rect x="${cx - 95}" y="${gy - 34}" width="190" height="68" rx="10" fill="var(--panel)" stroke="${color}" stroke-width="2"/>
      <circle cx="${cx - 75}" cy="${gy - 12}" r="5" fill="${color}"/>
      <text x="${cx - 62}" y="${gy - 7}" fill="var(--text)" font-size="14" font-weight="600">${esc(gw.id)}</text>
      <text x="${cx}" y="${gy + 14}" text-anchor="middle" fill="var(--muted)" font-size="11">:${gw.port} · ${subs.length} subagent${subs.length === 1 ? "" : "s"}</text></g>`);
  });
  svg.innerHTML = parts.join("");
}
render();
setInterval(render, 8000);
</script>
</body>
</html>
"""


'''

ANCHOR_DEF = "def mount_spa(application: FastAPI):"

ANCHOR_HEAD = '        html = html.replace("</head>", f"{bootstrap_script}</head>", 1)'
REPLACE_HEAD = (
    ANCHOR_HEAD
    + "\n"
    + '        html = html.replace("</body>", _WORKSPACE_TITLE_LINK_SCRIPT + "</body>", 1)'
)

ANCHOR_MOUNT = '    application.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")'
ROUTES = '''
    # ── Local workspace patch: agent graph at "/" ──────────────────────
    # Registered BEFORE the "/{full_path:path}" catch-all below, so a full
    # page load of "/" gets this page instead of the SPA (whose router
    # would immediately redirect to /sessions).
    _workspace_url = os.environ.get("HERMES_WORKSPACE_URL", "http://127.0.0.1:9100")

    @application.get("/workspace/graph.json")
    def workspace_graph_json():
        import json as _json
        import urllib.request as _urlreq
        try:
            with _urlreq.urlopen(_workspace_url + "/api/graph", timeout=4) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @application.get("/")
    def workspace_graph_page():
        return HTMLResponse(
            _WORKSPACE_GRAPH_HTML,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )
'''


def patch(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if MARKER in text:
        return "already patched"
    for anchor in (ANCHOR_DEF, ANCHOR_HEAD, ANCHOR_MOUNT):
        if text.count(anchor) != 1:
            return f"ANCHOR NOT UNIQUE ({text.count(anchor)}x): {anchor[:60]!r}"
    text = text.replace(ANCHOR_DEF, CONSTANTS + ANCHOR_DEF, 1)
    text = text.replace(ANCHOR_HEAD, REPLACE_HEAD, 1)
    text = text.replace(ANCHOR_MOUNT, ANCHOR_MOUNT + "\n" + ROUTES, 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return "patched"


for f in FILES:
    print(f, "->", patch(f))

# Sanity: both files must still be valid Python.
import py_compile

for f in FILES:
    try:
        py_compile.compile(f, doraise=True)
        print(f, "-> compiles OK")
    except py_compile.PyCompileError as exc:
        print(f, "-> COMPILE ERROR:", exc)
        sys.exit(1)


# ── Stage 2: gate the graph page to the MAIN dashboard only ────────────────
# Per-agent dashboards (HERMES_HOME = agents/<name>/.hermes) must serve the
# stock SPA at "/" so "Dashboard" from the workspace UI lands on the actual
# dashboard. Autodetects via HERMES_HOME basename; HERMES_ROOT_GRAPH=1/0
# force-overrides.

GATE_MARKER = "_WORKSPACE_IS_HOME"

G_A1 = '_WORKSPACE_TITLE_LINK_SCRIPT = r"""'
G_R1 = '''# The graph landing page belongs to the MAIN dashboard only (its
# HERMES_HOME is .../hermes-home). Per-agent dashboards (HERMES_HOME =
# agents/<name>/.hermes) keep the stock SPA at "/". HERMES_ROOT_GRAPH=1/0
# force-overrides the autodetection.
_WORKSPACE_IS_HOME = (
    os.environ.get("HERMES_ROOT_GRAPH") == "1"
    or (
        os.environ.get("HERMES_ROOT_GRAPH") != "0"
        and Path(os.environ.get("HERMES_HOME", "")).name == "hermes-home"
    )
)

_WORKSPACE_TITLE_LINK_SCRIPT = r"""'''

G_A2 = '        html = html.replace("</body>", _WORKSPACE_TITLE_LINK_SCRIPT + "</body>", 1)'
G_R2 = '''        if _WORKSPACE_IS_HOME:
            html = html.replace("</body>", _WORKSPACE_TITLE_LINK_SCRIPT + "</body>", 1)'''

G_A3 = '''    @application.get("/")
    def workspace_graph_page():
        return HTMLResponse('''
G_R3 = '''    @application.get("/")
    def workspace_graph_page(request: Request):
        if not _WORKSPACE_IS_HOME:
            return _serve_index(_normalise_prefix(request.headers.get("x-forwarded-prefix")))
        return HTMLResponse('''


def gate(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if GATE_MARKER in text:
        return "already gated"
    for anchor in (G_A1, G_A2, G_A3):
        if text.count(anchor) != 1:
            return f"GATE ANCHOR NOT UNIQUE ({text.count(anchor)}x): {anchor[:50]!r}"
    text = text.replace(G_A1, G_R1, 1).replace(G_A2, G_R2, 1).replace(G_A3, G_R3, 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return "gated"


for f in FILES:
    print(f, "->", gate(f))

for f in FILES:
    py_compile.compile(f, doraise=True)
    print(f, "-> compiles OK (gated)")
