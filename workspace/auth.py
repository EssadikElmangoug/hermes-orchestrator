"""Universal workspace authentication.

One login covers the workspace UI AND every agent dashboard subdomain: the
session cookie is issued for the parent domain (``.orchestrator.example.com``)
so ``<agent>.orchestrator.example.com`` pages are authenticated by the same
cookie — no per-agent logins, no web-server auth config.

Modes
- Password configured (WORKSPACE_PASSWORD or WORKSPACE_PASSWORD_FILE):
  every request must carry a valid session cookie, or Basic/Bearer
  credentials (for curl/scripts). /login serves the sign-in page.
- No password configured (typical local machine, bound to 127.0.0.1):
  the gate is inert.

Sessions are stateless signed tokens: ``<expiry>.<hmac>`` using a secret
persisted in workspace/auth_secret (auto-generated).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import secrets
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

_SESSION_TTL = 30 * 24 * 3600            # 30 days
_COOKIE = "hermes_workspace_session"
_EXEMPT_PATHS = {"/api/tls-check", "/favicon.ico"}


def _secret(workspace_dir: Path) -> bytes:
    path = workspace_dir / "auth_secret"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip().encode()


def _sign(secret: bytes, exp: int) -> str:
    sig = hmac.new(secret, str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _verify(secret: bytes, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(
        sig, hmac.new(secret, exp_s.encode(), hashlib.sha256).hexdigest())


class AuthGate:
    """Outermost ASGI middleware — runs before the dashboard proxy so agent
    subdomains are protected by the same session."""

    def __init__(self, app, password: str, domain: str, workspace_dir: Path):
        self.app = app
        self.password = password or ""
        self.domain = (domain or "").strip().lower()
        self.secret = _secret(workspace_dir) if self.password else b""

    # ── request credentials ─────────────────────────────────────────────

    def _authed(self, scope) -> bool:
        headers = dict(scope.get("headers") or [])
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get(b"cookie", b"").decode("latin-1"))
        except Exception:
            pass
        morsel = cookie.get(_COOKIE)
        if morsel and _verify(self.secret, morsel.value):
            return True
        # Script-friendly fallbacks: Bearer <password> or Basic *:<password>
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if auth.startswith("Bearer "):
            return hmac.compare_digest(auth[7:], self.password)
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                return hmac.compare_digest(
                    decoded.split(":", 1)[1], self.password)
            except Exception:
                return False
        return False

    # ── ASGI entry ──────────────────────────────────────────────────────

    async def __call__(self, scope, receive, send):
        if not self.password or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "/")
        if path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        if path == "/login" and scope["type"] == "http":
            await self._login(scope, receive, send)
            return
        if path == "/logout" and scope["type"] == "http":
            await self._logout(send)
            return
        if self._authed(scope):
            await self.app(scope, receive, send)
            return
        await self._deny(scope, receive, send)

    # ── responses ───────────────────────────────────────────────────────

    def _cookie_header(self, value: str, max_age: int) -> bytes:
        parts = [f"{_COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Lax",
                 f"Max-Age={max_age}"]
        if self.domain:
            parts += [f"Domain=.{self.domain}", "Secure"]
        return "; ".join(parts).encode()

    async def _deny(self, scope, receive, send):
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 4401})
            return
        headers = dict(scope.get("headers") or [])
        accept = headers.get(b"accept", b"").decode("latin-1")
        if "text/html" in accept and scope["method"] in ("GET", "HEAD"):
            host = headers.get(b"host", b"").decode("latin-1")
            proto = "https" if self.domain else "http"
            here = f"{proto}://{host}{scope['path']}"
            login_host = self.domain or host
            location = f"{proto}://{login_host}/login?next={quote(here)}"
            await _respond(send, 302, b"", [(b"location", location.encode())])
        else:
            await _respond(send, 401,
                           b'{"detail":"authentication required"}',
                           [(b"content-type", b"application/json")])

    async def _login(self, scope, receive, send):
        qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        next_url = self._safe_next(qs.get("next", ["/"])[0])
        if scope["method"] == "GET":
            await _respond(send, 200, _login_page(next_url).encode(),
                           [(b"content-type", b"text/html; charset=utf-8")])
            return
        if scope["method"] != "POST":
            await _respond(send, 405, b"method not allowed", [])
            return
        body = b""
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                return
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        form = parse_qs(body.decode("utf-8", "replace"))
        supplied = form.get("password", [""])[0]
        next_url = self._safe_next(form.get("next", [next_url])[0])
        if hmac.compare_digest(supplied, self.password):
            token = _sign(self.secret, int(time.time()) + _SESSION_TTL)
            await _respond(send, 302, b"", [
                (b"location", next_url.encode()),
                (b"set-cookie", self._cookie_header(token, _SESSION_TTL)),
            ])
        else:
            time.sleep(1.5)            # slow brute force
            await _respond(send, 200,
                           _login_page(next_url, error=True).encode(),
                           [(b"content-type", b"text/html; charset=utf-8")])

    async def _logout(self, send):
        await _respond(send, 302, b"", [
            (b"location", b"/login"),
            (b"set-cookie", self._cookie_header("gone", 0)),
        ])

    def _safe_next(self, candidate: str) -> str:
        """Only same-site targets: relative paths or our (sub)domains."""
        if candidate.startswith("/") and not candidate.startswith("//"):
            return candidate
        try:
            parsed = urlparse(candidate)
        except ValueError:
            return "/"
        host = (parsed.hostname or "").lower()
        if self.domain and parsed.scheme == "https" and (
                host == self.domain or host.endswith("." + self.domain)):
            return candidate
        return "/"


async def _respond(send, status: int, body: bytes, headers):
    headers = list(headers) + [(b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status,
                "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _login_page(next_url: str, error: bool = False) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Orchestrator — Sign in</title><style>
body {{ background:#0d1117; color:#e6edf3; font:14px/1.5 system-ui,sans-serif;
  display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0 }}
.box {{ background:#161b22; border:1px solid #30363d; border-radius:10px;
  padding:32px; width:min(360px,90vw) }}
h1 {{ font-size:16px; letter-spacing:2px; text-transform:uppercase; color:#d4a017;
  margin:0 0 6px }}
p {{ color:#8b949e; font-size:12px; margin:0 0 18px }}
input {{ width:100%; box-sizing:border-box; background:#0d1117; border:1px solid #30363d;
  color:#e6edf3; padding:10px 12px; border-radius:6px; font-size:14px }}
input:focus {{ outline:none; border-color:#d4a017 }}
button {{ width:100%; margin-top:12px; background:#0d1117; color:#d4a017;
  border:1px solid #d4a017; padding:10px; border-radius:6px; font-size:14px; cursor:pointer }}
.err {{ color:#f85149; font-size:12px; margin-top:10px }}
</style></head><body><form class="box" method="post" action="/login">
<h1>☤ Hermes Orchestrator</h1>
<p>One sign-in covers the workspace and every agent dashboard.</p>
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
<input type="hidden" name="next" value="{html.escape(next_url, quote=True)}">
<button type="submit">Sign in</button>
{'<div class="err">Wrong password — try again.</div>' if error else ''}
</form></body></html>"""
