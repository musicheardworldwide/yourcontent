/* Connect Google panel. Vanilla, strict-CSP safe (no inline code, no innerHTML with data).
   window.MediaLabGoogle = { mount(el), upload(ref, folder) } */
(function () {
  "use strict";

  async function call(method, path, body) {
    const opt = {method, headers: {}, credentials: "same-origin"};
    if (method === "POST") {
      opt.headers["X-Media-Lab"] = "1";
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body === undefined ? {} : body);
    }
    let r;
    try { r = await fetch(path, opt); } catch (e) { throw new Error("Can't reach YourContent. Is it running?"); }
    let j = null;
    try { j = await r.json(); } catch (e) { j = null; }
    if (!r.ok) {
      const err = new Error((j && j.error) || ("Something went wrong (" + r.status + ")."));
      err.status = r.status;
      throw err;
    }
    return j;
  }

  function el(tag, attrs, kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "text") n.textContent = v;
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) n.addEventListener(ev, fn);
      else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of [].concat(kids || [])) if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    return n;
  }

  function link(url, text) {
    return el("a", {href: url, target: "_blank", rel: "noopener noreferrer", class: "gg-link", text});
  }

  function fmtSize(n) {
    if (typeof n !== "number") return "";
    if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
    return Math.max(1, Math.round(n / 1e3)) + " KB";
  }

  const FOLDER_LABELS = {root: "Workspace", inbox: "Inbox", rendered: "Rendered", exports: "Exports", calendar: "Calendar"};

  function mount(root) {
    if (!root) return;
    root.textContent = "";
    root.classList.add("gg");
    root.id = root.id || "google";
    const msg = el("p", {class: "gg-msg", role: "status", "aria-live": "polite"});
    const body = el("div", {class: "gg-body"});
    root.appendChild(el("div", {class: "gg-head"}, [el("h2", {text: "Google"}), el("span", {class: "gg-scope", text: "Drive: only files YourContent creates"})]));
    root.appendChild(body);
    root.appendChild(msg);

    function say(text, kind) {
      msg.textContent = text || "";
      msg.className = "gg-msg" + (kind ? " " + kind : "");
    }

    async function act(btn, fn) {
      if (btn) btn.disabled = true;
      say("Working…");
      try { await fn(); say(""); } catch (e) { say(e.message, "err"); } finally { if (btn) btn.disabled = false; }
    }

    async function refresh() {
      let s;
      try { s = await call("GET", "/api/google/status"); } catch (e) { say(e.message, "err"); return; }
      render(s);
    }

    function render(s) {
      body.textContent = "";
      if (!s.configured) {
        // plain words first; the exact reason (it names a setting) stays one tap away for whoever sets it up
        body.appendChild(el("p", {text: "Google sync isn't switched on for this YourContent yet. Whoever set it " +
          "up can turn it on in about five minutes by following the Google setup guide."}));
        const more = el("details", {class: "gg-more"}, [el("summary", {text: "Technical details"}),
          el("p", {class: "gg-dim", text: s.reason || "Google isn't set up."}),
          el("p", {class: "gg-dim"}, ["Setup guide: ", el("code", {text: s.setup_doc || "docs/GOOGLE.md"})])]);
        body.appendChild(more);
        return;
      }
      if (s.last_error) say(s.last_error, "err");
      if (!s.connected) {
        const b = el("button", {type: "button", text: "Connect Google"});
        b.addEventListener("click", () => act(b, async () => {
          const r = await call("POST", "/api/google/connect");
          window.location.assign(r.url);
        }));
        body.appendChild(el("p", {class: "gg-dim", text: "YourContent asks only for access to the files it creates in your Drive."}));
        body.appendChild(b);
        return;
      }
      body.appendChild(el("p", {class: "gg-who"}, [el("span", {class: "gg-dot"}), "Connected" + (s.email ? " as " + s.email : "")]));
      const ws = s.workspace;
      if (ws && Object.keys(ws.folders || {}).length) {
        const ul = el("ul", {class: "gg-links"});
        for (const k of ["root", "inbox", "rendered", "exports", "calendar"]) {
          const f = ws.folders[k];
          if (f) ul.appendChild(el("li", {}, [link(f.url, (k === "root" && ws.name) ? ws.name : FOLDER_LABELS[k])]));
        }
        if (ws.sheet) ul.appendChild(el("li", {}, [link(ws.sheet.url, "Content Plan (Sheet)")]));
        body.appendChild(ul);
      }
      const row = el("div", {class: "gg-row"});
      if (!ws || !ws.complete) {
        const name = el("input", {type: "text", maxlength: "100", placeholder: "YourContent", "aria-label": "Workspace folder name", value: (ws && ws.name) || ""});
        const b = el("button", {type: "button", text: ws ? "Repair workspace" : "Create workspace"});
        b.addEventListener("click", () => act(b, async () => {
          await call("POST", "/api/google/bootstrap", {name: name.value.trim() || null});
          await refresh();
        }));
        row.appendChild(name);
        row.appendChild(b);
      } else {
        const ib = el("button", {type: "button", class: "ghost", text: "Check Inbox"});
        ib.addEventListener("click", () => act(ib, showInbox));
        const sb = el("button", {type: "button", text: "Sync calendar"});
        sb.addEventListener("click", () => act(sb, async () => {
          const r = await call("POST", "/api/google/sync");
          say("Synced: " + r.from_sheet + " from the Sheet, " + r.to_sheet + " to the Sheet" +
            (r.conflicts ? ", " + r.conflicts + " edited on both sides (YourContent's version kept)" : "") +
            (r.errors.length ? ". Skipped rows: " + r.errors.join("; ") : "."), r.errors.length ? "warn" : "");
        }));
        row.appendChild(sb);
        row.appendChild(ib);
      }
      const d = el("button", {type: "button", class: "ghost danger", text: "Disconnect"});
      d.addEventListener("click", () => act(d, async () => {
        if (!window.confirm("Disconnect Google? YourContent forgets its access; your Drive files stay.")) return;
        await call("POST", "/api/google/disconnect");
        await refresh();
      }));
      row.appendChild(d);
      body.appendChild(row);
      body.appendChild(el("div", {class: "gg-inbox", id: "gg-inbox"}));
    }

    async function showInbox() {
      const box = body.querySelector(".gg-inbox");
      const r = await call("GET", "/api/google/inbox");
      box.textContent = "";
      if (!r.files.length) {
        box.appendChild(el("p", {class: "gg-dim", text: "Nothing in Inbox that YourContent can see. Files you add in Drive yourself stay invisible to it (see the setup guide)."}));
        return;
      }
      const ul = el("ul", {class: "gg-files"});
      for (const f of r.files) {
        const b = el("button", {type: "button", class: "ghost tiny", text: "Import", disabled: !f.importable});
        b.addEventListener("click", () => act(b, async () => {
          await call("POST", "/api/google/inbox/import", {file_id: f.id});
          say("Imported " + f.name + ".", "ok");
          document.dispatchEvent(new CustomEvent("medialab:imported"));
        }));
        ul.appendChild(el("li", {}, [el("span", {class: "gg-fname", text: f.name}), el("span", {class: "gg-dim", text: fmtSize(f.size)}), b]));
      }
      box.appendChild(ul);
    }

    window.addEventListener("hashchange", () => { if (window.location.hash === "#google") refresh(); });
    refresh().then(() => {
      if (window.location.hash === "#google" && root.scrollIntoView) root.scrollIntoView({block: "start"});
    });
  }

  /* For the export / recent views: send one owned file to Exports/ or Rendered/. Resolves to {id, url, ...}. */
  function upload(ref, folder) {
    return call("POST", "/api/google/upload", {ref, folder: folder || "exports"});
  }

  window.MediaLabGoogle = {mount, upload};
})();
