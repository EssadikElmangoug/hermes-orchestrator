/* Workspace enhancements injected into every proxied Hermes dashboard.
 * Served by workspace/proxy.py at /workspace-api/inject.js — the proxy
 * injects a <script src> tag into the dashboard HTML. Everything here is
 * defensive: each feature is wrapped so a dashboard-markup change degrades
 * that feature to a no-op instead of breaking the page.
 *
 * Features:
 *  1. Files tab removal (the Files API is blocked server-side by the proxy).
 *  2. Shared CLI Tools panel (reads the workspace CLI registry).
 *  3. Provider-health banner: warns when the configured model's provider has
 *     a broken login, which makes chats silently fall back to another model
 *     (the "badge says gpt-5.5, chat says deepseek" confusion).
 *  4. "All models" view in the model-picker dialog: a flat, searchable list
 *     of every model across all linked (authenticated) providers, shown as
 *     the default first tab; the native per-provider view stays one click
 *     away.
 */
(function () {
  "use strict";

  var TOKEN = window.__HERMES_SESSION_TOKEN__ || "";

  function apiHeaders(extra) {
    var h = extra || {};
    if (TOKEN) h["X-Hermes-Session-Token"] = TOKEN;
    return h;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function toast(msg, ms) {
    try {
      var t = document.createElement("div");
      t.style.cssText =
        "position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:2147483200;" +
        "background:#10151d;color:#e6edf3;border:1px solid #d4a017;border-radius:10px;" +
        "padding:11px 18px;font:13px/1.5 system-ui,sans-serif;max-width:min(560px,90vw);" +
        "box-shadow:0 6px 24px rgba(0,0,0,.55)";
      t.textContent = msg;
      document.body.appendChild(t);
      setTimeout(function () { t.remove(); }, ms || 7000);
    } catch (e) { /* cosmetic only */ }
  }

  /* ── 1. Files tab removal ─────────────────────────────────────────── */
  var hideFiles = function () {
    if (location.pathname === "/files" || location.pathname.indexOf("/files/") === 0) {
      location.replace("/");
      return;
    }
    var els = document.querySelectorAll("a,button,[role=tab]");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var txt = (el.textContent || "").trim().toLowerCase();
      var href = (el.getAttribute && el.getAttribute("href")) || "";
      if (txt === "files" || /\/files\/?$/.test(href)) {
        (el.closest("li") || el).style.display = "none";
      }
    }
  };

  /* ── 2. Shared CLI Tools panel ────────────────────────────────────── */
  var openClis = function () {
    var bg = document.createElement("div");
    bg.id = "ws-cli-overlay";
    bg.style.cssText =
      "position:fixed;inset:0;z-index:2147483000;background:rgba(2,6,14,.9);overflow:auto;" +
      "font:14px/1.5 system-ui,sans-serif;color:#e6edf3;padding:5vh 16px;box-sizing:border-box";
    bg.innerHTML =
      '<div style="max-width:820px;margin:0 auto;background:#10151d;border:1px solid #2a3038;border-radius:14px;padding:clamp(14px,4vw,26px)">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">' +
      '<div style="font-size:16px;letter-spacing:2px;text-transform:uppercase;color:#d4a017">&#9880; Shared CLI Tools</div>' +
      '<button id="ws-cli-close" style="background:none;border:1px solid #2a3038;color:#8b949e;border-radius:8px;padding:6px 14px;cursor:pointer">Close</button></div>' +
      '<div style="color:#8b949e;font-size:12px;margin-bottom:18px">Available to every agent in this workspace &mdash; executables are on the PATH, manifests live in the shared clis folder.</div>' +
      '<div id="ws-cli-list">Loading&hellip;</div></div>';
    document.body.appendChild(bg);
    bg.addEventListener("click", function (e) { if (e.target === bg) bg.remove(); });
    document.getElementById("ws-cli-close").onclick = function () { bg.remove(); };
    fetch("/workspace-api/clis").then(function (r) { return r.json(); }).then(function (tools) {
      document.getElementById("ws-cli-list").innerHTML = tools.length
        ? tools.map(function (t) {
            return '<div style="border:1px solid #2a3038;border-radius:10px;padding:14px 16px;margin-bottom:12px;background:#0b0f15">' +
              '<div style="font-weight:600;margin-bottom:4px">' + esc(t.name) +
              (t.documented ? "" : ' <span style="font-size:11px;color:#8b949e">(undocumented)</span>') + "</div>" +
              '<div style="color:#8b949e;font-size:12px">' + esc(t.description) + "</div>" +
              (t.commands
                ? '<pre style="background:#02040a;border:1px solid #2a3038;border-radius:8px;padding:10px;font:12px/1.5 ui-monospace,monospace;overflow:auto;margin:10px 0 0;color:#c9d1d9">' + esc(t.commands) + "</pre>"
                : "") +
              "</div>";
          }).join("")
        : '<div style="color:#8b949e">No CLI tools registered yet.</div>';
    }).catch(function () {
      document.getElementById("ws-cli-list").textContent = "Could not load the CLI registry.";
    });
  };

  var addCliBtn = function () {
    if (document.getElementById("ws-cli-btn")) return;
    var b = document.createElement("button");
    b.id = "ws-cli-btn";
    b.textContent = "⌘ CLI Tools";
    b.style.cssText =
      "position:fixed;right:18px;bottom:18px;z-index:2147482000;background:#10151d;color:#d4a017;" +
      "border:1px solid #d4a017;border-radius:999px;padding:9px 16px;font:600 12px system-ui,sans-serif;" +
      "cursor:pointer;box-shadow:0 4px 18px rgba(0,0,0,.5)";
    b.onclick = openClis;
    document.body.appendChild(b);
  };

  /* ── 3. Provider-health banner ────────────────────────────────────── */
  function checkProviderHealth() {
    fetch("/workspace-api/provider-health").then(function (r) { return r.json(); }).then(function (h) {
      if (!h || !h.auth_error || !h.configured_provider) return;
      if (document.getElementById("ws-provider-banner")) return;
      var fb = h.fallback && h.fallback.model
        ? " Chats are silently falling back to <b>" + esc(h.fallback.model) + "</b> (" + esc(h.fallback.provider) + ") — that is why the model badge and the chat can disagree."
        : "";
      var d = document.createElement("div");
      d.id = "ws-provider-banner";
      d.style.cssText =
        "position:fixed;top:0;left:50%;transform:translateX(-50%);z-index:2147483100;" +
        "background:#2b1d05;color:#ffd27a;border:1px solid #d4a017;border-top:none;" +
        "border-radius:0 0 12px 12px;padding:10px 42px 10px 16px;font:12px/1.6 system-ui,sans-serif;" +
        "max-width:min(720px,94vw);box-shadow:0 6px 24px rgba(0,0,0,.5)";
      d.innerHTML =
        "&#9888;&#65039; The configured provider <b>" + esc(h.configured_provider) +
        "</b> (model <b>" + esc(h.configured_model || "?") + "</b>) has a broken login" +
        (h.auth_error.relogin_required ? " and needs re-authentication" : "") + "." + fb +
        (h.auth_error.message ? '<div style="color:#b78a2e;margin-top:4px">' + esc(h.auth_error.message) + "</div>" : "") +
        '<button id="ws-provider-banner-x" style="position:absolute;right:10px;top:8px;background:none;border:none;color:#ffd27a;font-size:15px;cursor:pointer">&times;</button>';
      document.body.appendChild(d);
      document.getElementById("ws-provider-banner-x").onclick = function () { d.remove(); };
    }).catch(function () { /* endpoint optional */ });
  }

  /* ── 4. "All models" first tab in the model-picker dialog ─────────── */
  var ROW_STYLE =
    "display:flex;align-items:center;gap:10px;padding:7px 14px;cursor:pointer;" +
    "font:12px/1.5 ui-monospace,monospace;border-left:2px solid transparent";

  function buildRows(payload) {
    var rows = [];
    (payload.providers || []).forEach(function (p) {
      if (!p.authenticated || !p.models || !p.models.length) return;
      p.models.forEach(function (m) {
        rows.push({
          provider: p.slug,
          providerName: p.name || p.slug,
          model: m,
          current: p.slug === payload.provider && m === payload.model,
        });
      });
    });
    rows.sort(function (a, b) { return (b.current ? 1 : 0) - (a.current ? 1 : 0); });
    return rows;
  }

  function setModel(provider, model, confirmed, done) {
    fetch("/api/model/set", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        scope: "main",
        provider: provider,
        model: model,
        confirm_expensive_model: !!confirmed,
      }),
    }).then(function (r) { return r.json(); }).then(function (r) {
      if (r && r.confirm_required) {
        if (window.confirm(r.confirm_message || "This model has unusually high known pricing. Switch anyway?")) {
          setModel(provider, model, true, done);
        } else {
          done(false);
        }
        return;
      }
      if (r && r.ok === false) {
        toast("Switch failed: " + (r.error || r.detail || "unknown error"));
        done(false);
        return;
      }
      toast("Model set to " + model + " (" + provider + "). New chats use it — type /new in the chat, or reload the page, to apply it here.", 9000);
      done(true);
    }).catch(function (e) {
      toast("Switch failed: " + e);
      done(false);
    });
  }

  function enhancePicker(dialog) {
    var card = dialog.querySelector(":scope > div");
    if (!card || card.dataset.wsAllModels) return;
    card.dataset.wsAllModels = "1";

    var header = card.querySelector(":scope > header");
    var footer = card.querySelector(":scope > footer");
    if (!header || !footer) return;
    var nativeSearch = header.nextElementSibling;
    var nativeGrid = nativeSearch && nativeSearch.nextElementSibling;
    if (!nativeGrid || nativeGrid === footer) return;

    /* tab bar */
    var tabs = document.createElement("div");
    tabs.style.cssText = "display:flex;gap:6px;padding:8px 20px 0";
    var TAB_BASE =
      "background:none;border:1px solid transparent;border-bottom:none;cursor:pointer;" +
      "padding:5px 14px;font:600 11px system-ui,sans-serif;letter-spacing:1px;" +
      "text-transform:uppercase;border-radius:8px 8px 0 0";
    var tabAll = document.createElement("button");
    tabAll.textContent = "★ All models";
    var tabProv = document.createElement("button");
    tabProv.textContent = "By provider";
    tabs.appendChild(tabAll);
    tabs.appendChild(tabProv);
    header.insertAdjacentElement("afterend", tabs);

    /* flat panel */
    var panel = document.createElement("div");
    panel.style.cssText = "flex:1 1 auto;min-height:0;display:flex;flex-direction:column";
    var searchWrap = document.createElement("div");
    searchWrap.style.cssText = "padding:10px 20px 8px";
    var search = document.createElement("input");
    search.placeholder = "Search all linked models…";
    search.style.cssText =
      "width:100%;box-sizing:border-box;background:rgba(127,127,127,.08);color:inherit;" +
      "border:1px solid rgba(127,127,127,.35);border-radius:8px;padding:7px 12px;" +
      "font:13px/1.4 system-ui,sans-serif;outline:none";
    searchWrap.appendChild(search);
    var list = document.createElement("div");
    list.style.cssText = "flex:1 1 auto;min-height:0;overflow-y:auto;padding-bottom:6px";
    panel.appendChild(searchWrap);
    panel.appendChild(list);
    footer.insertAdjacentElement("beforebegin", panel);

    var rows = null;
    var applying = false;

    function renderList() {
      if (!rows) { list.innerHTML = '<div style="padding:16px 20px;font:12px system-ui;opacity:.6">loading…</div>'; return; }
      var terms = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      var shown = rows.filter(function (r) {
        var hay = (r.model + " " + r.provider + " " + r.providerName).toLowerCase();
        return terms.every(function (t) { return hay.indexOf(t) !== -1; });
      });
      if (!shown.length) {
        list.innerHTML = '<div style="padding:16px 20px;font:12px system-ui;opacity:.6">' +
          (rows.length ? "no matches" : "no models on any linked provider") + "</div>";
        return;
      }
      list.innerHTML = "";
      shown.forEach(function (r) {
        var el = document.createElement("div");
        el.style.cssText = ROW_STYLE + (r.current ? ";border-left-color:#d4a017" : "");
        el.innerHTML =
          '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(r.model) + "</span>" +
          '<span style="opacity:.55;font-size:11px">' + esc(r.providerName) + "</span>" +
          (r.current ? '<span style="color:#d4a017;font-size:10px;letter-spacing:1px;text-transform:uppercase">current</span>' : "");
        el.onmouseenter = function () { el.style.background = "rgba(212,160,23,.10)"; };
        el.onmouseleave = function () { el.style.background = ""; };
        el.onclick = function () {
          if (applying) return;
          applying = true;
          el.style.opacity = "0.5";
          setModel(r.provider, r.model, false, function (ok) {
            applying = false;
            el.style.opacity = "";
            if (ok) {
              var closeBtn = card.querySelector('button[aria-label="Close"]');
              if (closeBtn) closeBtn.click();
            }
          });
        };
        list.appendChild(el);
      });
    }

    search.addEventListener("input", renderList);

    function activate(all) {
      panel.style.display = all ? "flex" : "none";
      nativeSearch.style.display = all ? "none" : "";
      nativeGrid.style.display = all ? "none" : "";
      tabAll.style.cssText = TAB_BASE + (all
        ? ";color:#d4a017;border-color:rgba(212,160,23,.6);background:rgba(212,160,23,.08)"
        : ";opacity:.6");
      tabProv.style.cssText = TAB_BASE + (all
        ? ";opacity:.6"
        : ";color:#d4a017;border-color:rgba(212,160,23,.6);background:rgba(212,160,23,.08)");
      if (all) search.focus();
    }
    tabAll.onclick = function () { activate(true); };
    tabProv.onclick = function () { activate(false); };

    /* All-models is the default first tab */
    activate(true);
    renderList();

    fetch("/api/model/options", { headers: apiHeaders() })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (payload) {
        rows = buildRows(payload);
        renderList();
      })
      .catch(function () {
        /* data unavailable — fall back to the native picker */
        activate(false);
        tabs.style.display = "none";
      });
  }

  function scanForPicker() {
    var dialog = document.querySelector('div[role="dialog"][aria-labelledby="model-picker-title"]');
    if (dialog) {
      try { enhancePicker(dialog); } catch (e) { /* leave native UI intact */ }
    }
  }

  /* ── wiring ───────────────────────────────────────────────────────── */
  var onMutate = function () {
    try { hideFiles(); } catch (e) {}
    try { addCliBtn(); } catch (e) {}
    try { scanForPicker(); } catch (e) {}
  };
  new MutationObserver(onMutate).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", function () {
    onMutate();
    checkProviderHealth();
  });
  onMutate();
  if (document.readyState !== "loading") checkProviderHealth();
})();
