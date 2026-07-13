"""Host-based dashboard proxy.

When the workspace runs behind a domain (WORKSPACE_DOMAIN, e.g.
``orchestrator.kundlas.com``), each agent's native Hermes dashboard is served
at ``https://<agent>.<domain>/`` — the workspace reverse-proxies HTTP and
WebSocket traffic to the agent's local dashboard port. The reverse proxy in
front (Caddy) needs exactly one static site pointing every host at the
workspace; agents created later work with no extra configuration.

Without a domain the middleware is inert and dashboards are reached directly
on 127.0.0.1:<port> as before (local-machine mode).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx
import websockets
import yaml

import orchestrator as orc

_DASH_START_TIMEOUT = 25          # seconds to wait for a dashboard to come up

# Hop-by-hop / proxy-layer headers never forwarded to the dashboard. The
# workspace's own Basic Auth (terminated at Caddy) is stripped too — the
# loopback dashboard neither needs nor understands it. Origin/Referer are
# rewritten to the loopback origin: the dashboard's DNS-rebinding guard
# rejects any Origin that doesn't match its bound host.
_STRIP_REQ = {b"host", b"connection", b"keep-alive", b"te", b"trailer",
              b"transfer-encoding", b"upgrade", b"proxy-authorization",
              b"proxy-authenticate", b"authorization", b"origin", b"referer",
              # plain responses from the dashboard let us inject the
              # files-tab removal snippet; compression happens at the edge
              b"accept-encoding"}

# The dashboard's file manager exposes the agent's filesystem over the web —
# removed entirely when dashboards are served through the proxy: its API is
# blocked server-side and the tab is hidden client-side. All client-side
# enhancements (files-tab removal, CLI Tools panel, provider-health banner,
# the model picker's "All models" tab) live in static/dash_inject.js, served
# same-origin at /workspace-api/inject.js; the HTML injection is just a
# version-stamped script tag so browsers cache the file across page loads.
_FILES_API_PREFIX = "/api/files"
_CLIS_API_PATH = "/workspace-api/clis"
_INJECT_JS_PATH = "/workspace-api/inject.js"
_HEALTH_API_PATH = "/workspace-api/provider-health"
_INJECT_FILE = orc.WORKSPACE / "static" / "dash_inject.js"


def _inject_tag() -> bytes:
    try:
        version = int(_INJECT_FILE.stat().st_mtime)
    except OSError:
        return b""
    return f'<script src="{_INJECT_JS_PATH}?v={version}"></script>'.encode()


def _provider_health(name: str) -> Dict[str, Any]:
    """Configured model/provider plus that provider's login state, so the
    dashboard can explain why chats silently fall back to another model."""
    try:
        agent = orc.load_registry()["agents"][name]
        home = Path(agent["home"])
        cfg = yaml.safe_load((home / "config.yaml").read_text()) or {}
    except Exception:
        return {}
    model_cfg = cfg.get("model") or {}
    provider = model_cfg.get("provider")
    out: Dict[str, Any] = {
        "configured_provider": provider,
        "configured_model": model_cfg.get("default") or model_cfg.get("name"),
        "auth_error": None,
        "fallback": None,
    }
    fallbacks = cfg.get("fallback_providers") or []
    if fallbacks and isinstance(fallbacks[0], dict):
        out["fallback"] = {"provider": fallbacks[0].get("provider"),
                           "model": fallbacks[0].get("model")}
    try:
        auth = json.loads((home / "auth.json").read_text())
        err = ((auth.get("providers") or {}).get(provider) or {}).get("last_auth_error")
        # Only a relogin_required error is a reliable "this provider is down"
        # signal — transient errors may linger in auth.json after recovery.
        if err and err.get("relogin_required"):
            out["auth_error"] = {"code": err.get("code"),
                                 "message": err.get("message"),
                                 "relogin_required": True}
    except Exception:
        pass
    return out


# ── /api/model/options cache ─────────────────────────────────────────────
#
# Building the model list is expensive on a cold dashboard (serial live
# /models calls per provider plus pricing/capability lookups), and this
# workspace restarts dashboards often. The proxy — which is long-lived —
# caches the JSON per agent with stale-while-revalidate semantics and
# persists it to disk, so the picker opens instantly even right after a
# dashboard restart. A model switch invalidates and re-warms the cache;
# the picker's explicit "Refresh Models" click always goes upstream.
_OPTIONS_PATH = "/api/model/options"
_OPTIONS_TTL = 900                 # max age served from cache
_OPTIONS_SWR_AGE = 45              # background-refresh beyond this age
_OPTIONS_CACHE_DIR = orc.WORKSPACE / "dash_cache"
_options_mem: Dict[str, Dict[str, Any]] = {}
_options_refreshing: set = set()
_agent_tokens: Dict[str, str] = {}   # last seen dashboard session token


def _options_key(name: str, qs: str) -> str:
    params = sorted(p for p in qs.split("&") if p and not p.startswith("refresh="))
    return name + ("|" + "&".join(params) if params else "")


def _options_file(key: str) -> Path:
    return _OPTIONS_CACHE_DIR / (re.sub(r"[^A-Za-z0-9_-]", "_", key) + ".json")


def _options_get(key: str) -> Optional[Dict[str, Any]]:
    ent = _options_mem.get(key)
    if ent is None:
        try:
            ent = json.loads(_options_file(key).read_text())
            _options_mem[key] = ent
        except Exception:
            return None
    return ent


def _options_put(key: str, body: str) -> None:
    ent = {"ts": time.time(), "body": body}
    _options_mem[key] = ent
    try:
        _OPTIONS_CACHE_DIR.mkdir(exist_ok=True)
        _options_file(key).write_text(json.dumps(ent))
    except OSError:
        pass


def _options_drop(key: str) -> None:
    _options_mem.pop(key, None)
    try:
        _options_file(key).unlink()
    except OSError:
        pass
_STRIP_RESP = {b"connection", b"keep-alive", b"te", b"trailer",
               b"transfer-encoding", b"upgrade"}


def agent_for_host(host: str, domain: str) -> Optional[str]:
    """Registry agent name for ``<agent>.<domain>`` hosts, else None."""
    if not domain:
        return None
    host = host.split(":")[0].strip().lower()
    if not host.endswith("." + domain):
        return None
    sub = host[: -len(domain) - 1]
    if not orc.NAME_RE.match(sub):
        return None
    return sub if sub in orc.load_registry()["agents"] else None


class DashboardProxy:
    """ASGI middleware: routes subdomain hosts to agent dashboards."""

    def __init__(self, app: Any, domain: str):
        self.app = app
        self.domain = (domain or "").strip().lower()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=None, write=60, pool=None),
            follow_redirects=False,
        )

    async def __call__(self, scope, receive, send):
        if self.domain and scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            host = headers.get(b"host", b"").decode("latin-1")
            if host.split(":")[0].strip().lower() == f"dash.{self.domain}":
                await self._dash_host(scope, receive, send, host)
                return
            name = agent_for_host(host, self.domain)
            if name:
                await self._serve_agent(scope, receive, send, host, name)
                return
        await self.app(scope, receive, send)

    async def _serve_agent(self, scope, receive, send, host: str, name: str):
        port, err = await asyncio.get_event_loop().run_in_executor(
            None, _ensure_dashboard, name)
        if port is None:
            await self._reject(scope, receive, send, name, err)
            return
        # Pre-build the model-options cache so the first picker open is
        # instant even on a freshly started dashboard.
        if _options_get(_options_key(name, "")) is None:
            asyncio.ensure_future(self._warm_options(name, port))
        if scope["type"] == "http":
            await self._proxy_http(scope, receive, send, host, port, name)
        else:
            await self._proxy_ws(scope, receive, send, port)

    # ── single dashboard host (dash.<domain>) ───────────────────────────
    #
    # Per-agent subdomains need a freshly issued certificate per agent —
    # in practice that produced browser CT errors at click time. Instead
    # ONE fixed hostname serves whichever agent's dashboard was selected:
    # /a/<name> pins the selection in a cookie and redirects to /, where
    # the dashboard SPA runs at a genuine root origin (absolute asset
    # URLs, client-side routing and WebSockets all behave natively).
    # Only two certificates ever exist: the workspace and dash hosts.

    async def _dash_host(self, scope, receive, send, host: str):
        path = scope.get("path", "/")
        if path == "/a" or path.startswith("/a/"):
            parts = [p for p in path.split("/") if p]
            name = parts[1] if len(parts) > 1 else ""
            if scope["type"] != "http":
                await send({"type": "websocket.close", "code": 1008})
                return
            if not (orc.NAME_RE.match(name)
                    and name in orc.load_registry()["agents"]):
                await _send_html(send, 404, f"No such agent: {name or '?'}")
                return
            port, err = await asyncio.get_event_loop().run_in_executor(
                None, _ensure_dashboard, name)
            if port is None:
                await self._reject(scope, receive, send, name, err)
                return
            rest = "/" + "/".join(parts[2:]) if len(parts) > 2 else "/"
            cookie = (f"dash_agent={name}; Path=/; HttpOnly; "
                      f"SameSite=Lax; Secure")
            await send({"type": "http.response.start", "status": 302,
                        "headers": [(b"location", rest.encode()),
                                    (b"set-cookie", cookie.encode())]})
            await send({"type": "http.response.body", "body": b""})
            return
        name = self._pinned_agent(scope)
        if name:
            await self._serve_agent(scope, receive, send, host, name)
            return
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        await _send_html(
            send, 404,
            "No dashboard selected — open one from the "
            f"<a href='https://{self.domain}/' style='color:#58a6ff'>workspace</a>.")

    def _pinned_agent(self, scope) -> Optional[str]:
        headers = dict(scope.get("headers") or [])
        for chunk in headers.get(b"cookie", b"").decode("latin-1").split(";"):
            key, _, value = chunk.strip().partition("=")
            if key == "dash_agent" and orc.NAME_RE.match(value) \
                    and value in orc.load_registry()["agents"]:
                return value
        return None

    # ── HTTP ────────────────────────────────────────────────────────────

    async def _proxy_http(self, scope, receive, send, host: str, port: int,
                          name: str):
        path = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
        if path == _CLIS_API_PATH:
            # Same-origin CLI registry for the injected dashboard panel.
            body = json.dumps(orc.list_clis()).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        if path == _INJECT_JS_PATH:
            try:
                body = _INJECT_FILE.read_bytes()
            except OSError:
                await _send_html(send, 404, "not found")
                return
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type",
                                     b"application/javascript; charset=utf-8"),
                                    (b"cache-control", b"public, max-age=86400"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        if path == _HEALTH_API_PATH:
            body = json.dumps(await asyncio.get_event_loop().run_in_executor(
                None, _provider_health, name)).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        if path.startswith(_FILES_API_PREFIX):
            await _send_html(send, 403,
                             "The Files section is disabled on this workspace.")
            return
        if path == "/files" or path.startswith("/files/"):
            await send({"type": "http.response.start", "status": 302,
                        "headers": [(b"location", b"/")]})
            await send({"type": "http.response.body", "body": b""})
            return
        qs = scope.get("query_string", b"").decode("latin-1")
        url = f"http://127.0.0.1:{port}{path}" + (f"?{qs}" if qs else "")
        headers = [(k, v) for k, v in scope["headers"]
                   if k.lower() not in _STRIP_REQ]
        had_origin = any(k.lower() == b"origin" for k, _ in scope["headers"])
        headers += [(b"host", f"127.0.0.1:{port}".encode()),
                    (b"x-forwarded-host", host.encode()),
                    (b"x-forwarded-proto", b"https")]
        if had_origin:
            headers.append((b"origin", f"http://127.0.0.1:{port}".encode()))

        token = next((v.decode("latin-1") for k, v in scope["headers"]
                      if k.lower() == b"x-hermes-session-token"), "")
        if token:
            _agent_tokens[name] = token

        if path == _OPTIONS_PATH and scope["method"] == "GET":
            key = _options_key(name, qs)
            ent = None if "refresh=" in qs else _options_get(key)
            now = time.time()
            if ent and now - ent["ts"] < _OPTIONS_TTL:
                if now - ent["ts"] > _OPTIONS_SWR_AGE:
                    asyncio.ensure_future(
                        self._refresh_options(key, port, url, token))
                body = ent["body"].encode()
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            try:
                resp = await self._client.get(url, headers=headers, timeout=300)
            except httpx.HTTPError:
                await _send_html(send, 502, "dashboard unreachable")
                return
            if resp.status_code == 200:
                _options_put(key, resp.text)
            body = resp.content
            await send({"type": "http.response.start",
                        "status": resp.status_code,
                        "headers": [(b"content-type",
                                     resp.headers.get("content-type",
                                                      "application/json").encode()),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return

        async def body():
            while True:
                msg = await receive()
                if msg["type"] == "http.request":
                    if msg.get("body"):
                        yield msg["body"]
                    if not msg.get("more_body"):
                        return
                else:                          # http.disconnect
                    return

        content = body() if scope["method"] not in ("GET", "HEAD") else None
        req = self._client.build_request(
            scope["method"], url, headers=headers, content=content)
        try:
            resp = await self._client.send(req, stream=True)
        except httpx.HTTPError:
            await _send_html(send, 502, "dashboard unreachable")
            return
        if path == "/api/model/set" and resp.status_code == 200:
            # Model switched — the cached options' "current" markers are
            # stale. Drop and re-warm so the next picker open is both
            # correct and instant.
            key = _options_key(name, "")
            _options_drop(key)
            asyncio.ensure_future(self._refresh_options(
                key, port, f"http://127.0.0.1:{port}{_OPTIONS_PATH}",
                token or _agent_tokens.get(name, "")))
        is_html = resp.headers.get("content-type", "").startswith("text/html")
        out_headers = []
        for k, v in resp.headers.raw:
            if k.lower() in _STRIP_RESP:
                continue
            if is_html and k.lower() == b"content-length":
                continue                       # recomputed after injection
            # Redirects the dashboard issues against its loopback address
            # must come back on the public origin.
            if k.lower() == b"location":
                v = v.replace(f"http://127.0.0.1:{port}".encode(),
                              f"https://{host}".encode())
            out_headers.append((k, v))
        if is_html:
            try:
                body = await resp.aread()
            finally:
                await resp.aclose()
            lower = body.lower()
            idx = lower.rfind(b"</body>")
            tag = _inject_tag()
            body = (body[:idx] + tag + body[idx:]
                    if idx != -1 else body + tag)
            out_headers.append((b"content-length", str(len(body)).encode()))
            await send({"type": "http.response.start",
                        "status": resp.status_code, "headers": out_headers})
            await send({"type": "http.response.body", "body": body})
            return
        await send({"type": "http.response.start",
                    "status": resp.status_code, "headers": out_headers})
        try:
            async for chunk in resp.aiter_raw():
                await send({"type": "http.response.body",
                            "body": chunk, "more_body": True})
        finally:
            await resp.aclose()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    # ── model-options cache upkeep ──────────────────────────────────────

    async def _refresh_options(self, key: str, port: int, url: str,
                               token: str) -> None:
        if key in _options_refreshing:
            return
        _options_refreshing.add(key)
        try:
            headers = {"host": f"127.0.0.1:{port}"}
            if token:
                headers["X-Hermes-Session-Token"] = token
            resp = await self._client.get(url, headers=headers, timeout=300)
            if resp.status_code == 200:
                _options_put(key, resp.text)
        except Exception:
            pass
        finally:
            _options_refreshing.discard(key)

    async def _warm_options(self, name: str, port: int) -> None:
        """Fetch the dashboard's session token from its HTML, then build the
        options cache — so the very first picker open needs no slow call."""
        token = _agent_tokens.get(name, "")
        if not token:
            try:
                resp = await self._client.get(
                    f"http://127.0.0.1:{port}/",
                    headers={"host": f"127.0.0.1:{port}"}, timeout=30)
                m = re.search(r'__HERMES_SESSION_TOKEN__="([^"]+)"', resp.text)
                if m:
                    token = m.group(1)
                    _agent_tokens[name] = token
            except Exception:
                return
        await self._refresh_options(
            _options_key(name, ""), port,
            f"http://127.0.0.1:{port}{_OPTIONS_PATH}", token)

    # ── WebSocket ───────────────────────────────────────────────────────

    async def _proxy_ws(self, scope, receive, send, port: int):
        msg = await receive()                  # websocket.connect
        if msg["type"] != "websocket.connect":
            return
        path = scope["path"]
        qs = scope.get("query_string", b"").decode("latin-1")
        uri = f"ws://127.0.0.1:{port}{path}" + (f"?{qs}" if qs else "")
        fwd: Dict[str, str] = {"Origin": f"http://127.0.0.1:{port}"}
        subprotocols = None
        for k, v in scope["headers"]:
            lk = k.lower()
            if lk == b"cookie":
                fwd["Cookie"] = v.decode("latin-1")
            elif lk == b"sec-websocket-protocol":
                subprotocols = [p.strip() for p in v.decode().split(",")]
        try:
            upstream = await websockets.connect(
                uri, additional_headers=fwd, subprotocols=subprotocols,
                max_size=None, open_timeout=15)
        except Exception:
            await send({"type": "websocket.close", "code": 1011})
            return
        await send({"type": "websocket.accept",
                    "subprotocol": upstream.subprotocol})

        async def client_to_upstream():
            while True:
                m = await receive()
                if m["type"] == "websocket.receive":
                    data = m.get("text")
                    await upstream.send(data if data is not None else m.get("bytes", b""))
                else:                          # websocket.disconnect
                    return

        async def upstream_to_client():
            async for data in upstream:
                if isinstance(data, str):
                    await send({"type": "websocket.send", "text": data})
                else:
                    await send({"type": "websocket.send", "bytes": data})

        try:
            done, pending = await asyncio.wait(
                [asyncio.ensure_future(client_to_upstream()),
                 asyncio.ensure_future(upstream_to_client())],
                return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            await upstream.close()
            try:
                await send({"type": "websocket.close"})
            except Exception:
                pass                           # client already gone

    async def _reject(self, scope, receive, send, name: str, err: str):
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1013})
            return
        await _send_html(
            send, 503,
            f"Dashboard for <b>{name}</b> is not available: {err or 'not running'}."
            f"<br>Start the agent from the workspace, then reload this page.")


def _ensure_dashboard(name: str) -> Tuple[Optional[int], str]:
    """Dashboard port for the agent, starting the dashboard if needed."""
    agent = orc.load_registry()["agents"].get(name)
    if not agent:
        return None, "no such agent"
    port = agent["dash_port"]
    if orc._port_open(port):
        return port, ""
    try:
        orc.start_dashboard(name)
    except (ValueError, KeyError) as exc:
        return None, str(exc)
    deadline = time.time() + _DASH_START_TIMEOUT
    while time.time() < deadline:
        if orc._port_open(port):
            return port, ""
        time.sleep(0.5)
    return None, "dashboard did not come up in time"


async def _send_html(send, status: int, message: str):
    body = (f"<html><body style='font-family:system-ui;background:#0d1117;"
            f"color:#e6edf3;padding:40px'>{message}</body></html>").encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/html; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
