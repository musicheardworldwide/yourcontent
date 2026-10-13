"use strict";
/* Connect your agent (studio merge). Vanilla JS, strict CSP: no inline handlers or styles.
   Makes an agent token on the server and shows the setup prompt to paste into the agent. The token is in the
   prompt and is shown once; the server keeps only its hash. */
(function () {
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat()) if (c != null && c !== false) el.append(c.nodeType ? c : String(c));
    return el;
  }
  async function api(method, path, body) {
    const opt = {method, headers: {}};
    if (method === "POST") {
      opt.headers["X-Media-Lab"] = "1";
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body || {});
    }
    const r = await fetch(path, opt);
    let j = null;
    try { j = await r.json(); } catch (e) { j = null; }
    if (!r.ok) throw new Error((j && j.error) || ("Something went wrong (" + r.status + ")."));
    return j;
  }
  const when = t => t ? new Date(t * 1000).toLocaleString() : "never";

  function mount(root) {
    const status = h("p", {class: "status", role: "status"});
    const name = h("input", {id: "agname", maxlength: "60", placeholder: "e.g. My assistant", autocomplete: "off"});
    const preset = h("select", {id: "agpreset"});
    const out = h("div", {class: "agout", hidden: true});
    const list = h("ul", {class: "aglist"});
    const go = h("button", {type: "button", text: "Make setup prompt"});

    async function refresh() {
      try {
        const j = await api("GET", "/api/agents");
        if (!preset.options.length) {
          for (const [k, label] of Object.entries(j.presets)) preset.append(h("option", {value: k, text: label}));
        }
        list.replaceChildren(...j.agents.filter(a => !a.revoked).map(a => h("li", {},
          h("span", {class: "agname", text: a.name}),
          h("span", {class: "hint", text: " · " + a.preset + " · last used " + when(a.last_used)}),
          h("button", {class: "ghost tiny danger", type: "button", text: "Revoke",
            "aria-label": "Revoke " + a.name, on: {click: () => revoke(a)}}))));
        if (!list.children.length) list.append(h("li", {class: "hint", text: "No agents yet."}));
      } catch (e) { status.textContent = e.message; status.classList.add("err"); }
    }
    async function revoke(a) {
      if (!confirm("Revoke " + a.name + "? It stops working at once.")) return;
      try { await api("POST", "/api/agents/" + encodeURIComponent(a.id) + "/revoke"); await refresh(); }
      catch (e) { status.textContent = e.message; }
    }
    go.addEventListener("click", async () => {
      go.disabled = true; status.textContent = "Making…"; status.classList.remove("err");
      try {
        const j = await api("POST", "/api/agents", {name: name.value, preset: preset.value});
        const ta = h("textarea", {class: "agprompt", readonly: true, rows: "14", "aria-label": "Setup prompt"});
        ta.value = j.prompt;
        const copy = h("button", {type: "button", text: "Copy prompt"});
        copy.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(ta.value); copy.textContent = "Copied"; }
          catch (e) { ta.select(); copy.textContent = "Press Ctrl+C"; }
        });
        out.replaceChildren(
          h("p", {class: "hint", text: j.token_shown_once
            ? "The prompt holds this agent's token. It is shown only now: copy it before you close this."
            : "This YourContent listens only on this computer, so the agent needs no token."}),
          ta, copy);
        out.hidden = false;
        status.textContent = "Paste the prompt into " + (preset.selectedOptions[0] || {}).textContent + ".";
        await refresh();
      } catch (e) { status.textContent = e.message; status.classList.add("err"); }
      go.disabled = false;
    });

    // what an agent can delete depends on this install: only an access token keeps agents (and everyone else) out
    const safety = h("p", {class: "hint", text: "What else an agent can do depends on how this YourContent is set " +
      "up; see docs/AGENTS.md."});
    api("GET", "/api/config").then(c => {
      const tok = typeof c.token_required === "boolean" ? c.token_required : typeof c.auth === "boolean" ? c.auth : null;
      if (tok === true) safety.textContent = "This YourContent has an access token: an agent's own token cannot " +
        "delete files.";
      else if (tok === false) safety.textContent = "This YourContent has no access token, so anything that can " +
        "reach it, an agent included, can also delete files. Set a token (docs/AGENTS.md) if other devices can reach it.";
    }).catch(() => {});
    root.replaceChildren(
      h("p", {text: "Let an AI agent use YourContent for you: it can make pictures, clips and music, style posts, " +
        "cut reels and plan the calendar. It never posts anywhere."}), safety,
      h("label", {for: "agname", text: "Agent name"}), name,
      h("label", {for: "agpreset", text: "Which agent"}), preset,
      h("div", {class: "agrow"}, go), status, out,
      h("h3", {text: "Connected agents"}), list);
    refresh();
  }
  window.MediaLabAgents = {mount};
})();
