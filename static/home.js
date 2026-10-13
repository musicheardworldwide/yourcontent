"use strict";
/* Home, Library and Settings (launch UI, 2026-10-12). Each mounts into its view's .viewbody through studio.js and reads
   the page's own state through window.MediaLabApp (app.js); nothing here keeps a second copy of the files list.
   Home: quick actions, what's next on the calendar, recent files and a setup checklist that doubles as onboarding.
   Library: every recent file with type and licence filters, grid or list. Settings: appearance, access, GPU and
   models, connections, help and licences. */
(function () {
  const A = window.MediaLabApp;
  if (!A) return;
  const {h, fill, api, lsGet, lsSet} = A;
  const svgNS = "http://www.w3.org/2000/svg";
  function icon(name) {
    const s = document.createElementNS(svgNS, "svg");
    s.setAttribute("class", "ic");
    s.setAttribute("aria-hidden", "true");
    const u = document.createElementNS(svgNS, "use");
    u.setAttribute("href", "/static/icons.svg#" + name);
    s.append(u);
    return s;
  }
  const SETUP_VIDEO = "https://yourcontent.online/setup.html";
  const go = id => window.MediaLabStudio && window.MediaLabStudio.show(id);
  const isMedia = it => it.type === "image" || it.type === "video" || it.type === "audio";
  const when = it => {
    const t = it.mtime || it.created || it.time;
    if (!t) return "";
    const d = new Date(typeof t === "number" && t < 1e12 ? t * 1000 : t);
    return isNaN(d) ? "" : d.toLocaleDateString(undefined, {month: "short", day: "numeric"});
  };
  function tile(it, onPick) {
    const src = it.type === "video" ? it.poster : it.type === "image" ? it.url : null;
    const name = A.nameOf(it);
    return h("button", {type: "button", class: "ltile", title: name, on: {click: () => onPick(it)}},
      h("span", {class: "lthumb"}, src ? h("img", {src, alt: "", loading: "lazy"}) : icon(it.type === "audio" ? "music" : "film"),
        it.type === "video" && h("span", {class: "lbadge", text: "Clip"}),
        it.type === "audio" && h("span", {class: "lbadge", text: "Music"}),
        it.commercial_ok === false && h("span", {class: "lnc", title: "Non-commercial: not for business use", text: "NC"})),
      h("span", {class: "lname", text: name}),
      h("span", {class: "lmeta", text: [it.type === "image" ? "Picture" : it.type === "video" ? "Clip" : "Music", when(it)].filter(Boolean).join(" · ")}));
  }
  function empty(text, label, fn) {
    return h("div", {class: "emptybox"}, h("p", {text}), label && h("button", {type: "button", class: "ghost", text: label, on: {click: fn}}));
  }

  /* ------------------------------------------------------------ Home */
  const Home = {
    mount(el) {
      const hour = new Date().getHours();
      const greet = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
      const quick = [
        window.MediaLabPosts && ["layers", "New post", "Pick a style, type your words", () => go("posts")],
        ["image", "Make a picture", "Describe it, generate on your GPU", () => A.make("picture")],
        ["film", "Make a clip", "Animate a picture into video", () => A.make("clip")],
        ["cut", "Make a reel", "Stills, clips and music in one MP4", () => A.make("cut")],
        window.MediaLabCalendar && ["calendar", "Plan the week", "Ideas, drafts and ready posts", () => go("calendar")],
      ].filter(Boolean);
      const next = h("div", {class: "hlist"});
      const recent = h("div", {class: "lgrid small"});
      const check = h("ol", {class: "checklist"});
      const prog = h("span", {class: "cprog"});
      fill(el,
        h("header", {class: "homehead"}, h("div", null, h("p", {class: "eyebrow", text: greet}),
          h("h2", {class: "hometitle", text: "What are we making today?"})),
          window.MediaLabPosts && h("button", {type: "button", on: {click: () => go("posts")}}, icon("plus"), "New post")),
        h("div", {class: "quick"}, ...quick.map(([ic, t, sub, fn]) =>
          h("button", {type: "button", class: "qcard", on: {click: fn}}, h("span", {class: "qic"}, icon(ic)),
            h("b", {text: t}), h("span", {text: sub})))),
        h("div", {class: "homegrid"},
          h("section", {class: "hcard"}, h("div", {class: "hcardhead"}, h("h3", {text: "Recent"}),
            h("button", {type: "button", class: "link", text: "Open library", on: {click: () => go("library")}})), recent),
          h("div", {class: "hcol"},
            h("section", {class: "hcard"}, h("div", {class: "hcardhead"}, h("h3", {text: "Get set up"}), prog), check),
            window.MediaLabCalendar && h("section", {class: "hcard"}, h("div", {class: "hcardhead"}, h("h3", {text: "Up next"}),
              h("button", {type: "button", class: "link", text: "Calendar", on: {click: () => go("calendar")}})), next))));

      const drawRecent = () => {
        const list = A.recent().filter(it => it.type === "image" || it.type === "video").slice(0, 8);
        fill(recent, ...(list.length ? list.map(it => tile(it, A.open))
          : [empty("Nothing made yet. Your pictures and clips land here.", "Make a picture", () => A.make("picture"))]));
      };
      const steps = {gpu: null, kit: null, file: null, google: null, agent: null};
      const drawCheck = () => {
        const hh = A.health();
        steps.gpu = !!(hh && hh.comfy && hh.comfy.ok);
        steps.file = A.recent().length > 0;
        const items = [
          ["gpu", "Connect your GPU", "Start ComfyUI so you can make pictures, clips and music.", "Check", () => { A.recheck(); go("settings"); }],
          window.MediaLabPosts && ["kit", "Make a brand kit", "Your colors, fonts, logo and handle, used by every style.", "Open Posts", () => go("posts")],
          ["file", "Make your first picture", "Describe it and press Generate.", "Create", () => A.make("picture")],
          window.MediaLabGoogle && ["google", "Connect Google Drive", "Files and your content plan, kept in sync.", "Connect", () => go("connect")],
          window.MediaLabAgents && ["agent", "Connect your AI agent", "Let it make and schedule. It never posts.", "Connect", () => go("connect")],
        ].filter(Boolean);
        const done = items.filter(([k]) => steps[k]).length;
        prog.textContent = done + " of " + items.length;
        fill(check, ...items.map(([k, t, sub, lbl, fn]) => h("li", {class: steps[k] ? "done" : null},
          h("span", {class: "cdot"}, steps[k] ? icon("check") : null),
          h("div", null, h("b", {text: t}), h("span", {text: sub})),
          !steps[k] && h("button", {type: "button", class: "ghost tiny", text: lbl, on: {click: fn}}))));
      };
      const loadExtra = async () => {
        const jobs = [];
        if (window.MediaLabPosts) jobs.push(api("GET", "/api/brandkits").then(r => { steps.kit = !!(r && r.kits && r.kits.length); }).catch(() => {}));
        if (window.MediaLabGoogle) jobs.push(api("GET", "/api/google/status").then(r => { steps.google = !!(r && r.connected); }).catch(() => {}));
        if (window.MediaLabAgents) jobs.push(api("GET", "/api/agents").then(r => { steps.agent = !!(r && r.agents && r.agents.length); }).catch(() => {}));
        if (window.MediaLabCalendar) {
          const from = new Date().toISOString().slice(0, 10);
          jobs.push(api("GET", "/api/calendar?from=" + from).then(r => {
            const list = ((r && r.entries) || []).filter(e => e.status !== "posted" && e.status !== "skipped").slice(0, 5);
            fill(next, ...(list.length ? list.map(e => h("button", {type: "button", class: "hrow", on: {click: () => go("calendar")}},
              h("span", {class: "stdot st-" + e.status}), h("span", {class: "hrowt", text: e.title || "Untitled"}),
              h("span", {class: "hrowm", text: String(e.when || "").slice(0, 10) + " · " + e.status})))
              : [empty("Nothing planned yet.", "Plan a post", () => go("calendar"))]));
          }).catch(() => fill(next, empty("The calendar isn't available.", null))));
        }
        await Promise.all(jobs);
        drawCheck();
      };
      drawRecent(); drawCheck(); loadExtra();
      window.addEventListener("medialab:recent", () => { drawRecent(); drawCheck(); });
      window.addEventListener("medialab:health", drawCheck);
      window.addEventListener("medialab:view", e => { if (e.detail === "home") loadExtra(); });
    },
  };

  /* ------------------------------------------------------------ Library */
  const Library = {
    mount(el) {
      let type = "all", lic = "all", q = "", mode = lsGet("ml-libmode") === "list" ? "list" : "grid";
      const grid = h("div", {class: "lgrid"});
      const count = h("span", {class: "hint"});
      const chipRow = (label, opts, get, set) => h("div", {class: "chips", role: "group", "aria-label": label},
        ...opts.map(([v, t]) => h("button", {type: "button", class: "chip", "aria-pressed": String(get() === v), text: t,
          on: {click: e => { set(v); for (const b of e.target.parentNode.children) b.setAttribute("aria-pressed", String(b === e.target)); draw(); }}})));
      const search = h("input", {type: "search", placeholder: "Search by name", "aria-label": "Search the library",
        on: {input: e => { q = e.target.value.trim().toLowerCase(); draw(); }}});
      const modeBtn = (m, ic, t) => h("button", {type: "button", class: "ghost iconbtn", "aria-pressed": String(mode === m), "aria-label": t, title: t,
        on: {click: e => { mode = m; lsSet("ml-libmode", m); for (const b of e.currentTarget.parentNode.children) b.setAttribute("aria-pressed", String(b === e.currentTarget)); draw(); }}}, icon(ic));
      fill(el,
        h("div", {class: "libbar"}, search,
          chipRow("Type", [["all", "All"], ["image", "Pictures"], ["video", "Clips"], ["audio", "Music"]], () => type, v => { type = v; }),
          chipRow("License", [["all", "Any license"], ["ok", "Business OK"], ["nc", "Non-commercial"]], () => lic, v => { lic = v; }),
          h("span", {class: "sp"}), count,
          h("div", {class: "seg"}, modeBtn("grid", "grid", "Grid"), modeBtn("list", "list", "List"))),
        grid);
      function draw() {
        const list = A.recent().filter(isMedia)
          .filter(it => type === "all" || it.type === type)
          .filter(it => lic === "all" || (lic === "nc" ? it.commercial_ok === false : it.commercial_ok !== false))
          .filter(it => !q || A.nameOf(it).toLowerCase().includes(q));
        grid.className = "lgrid" + (mode === "list" ? " aslist" : "");
        count.textContent = list.length + (list.length === 1 ? " file" : " files");
        fill(grid, ...(list.length ? list.map(it => tile(it, A.open))
          : [A.recent().length ? empty("No files match these filters.", null)
            : empty("Your library is empty. Everything you make or upload shows up here.", "Make a picture", () => A.make("picture"))]));
      }
      draw();
      window.addEventListener("medialab:recent", draw);
      window.addEventListener("medialab:view", e => { if (e.detail === "library") A.loadRecent(); });
    },
  };

  /* ------------------------------------------------------------ Settings */
  const Settings = {
    mount(el) {
      const cfg = A.config() || {};
      const gpu = h("div", {class: "setval"});
      const row = (title, sub, ...ctrl) => h("div", {class: "setrow"}, h("div", null, h("b", {text: title}), sub && h("span", {text: sub})),
        h("div", {class: "setctl"}, ...ctrl));
      const card = (title, ...rows) => h("section", {class: "setcard"}, h("h3", {text: title}), ...rows);
      const themeBtn = h("button", {type: "button", class: "ghost", "aria-label": "Change theme"}, icon("system"), h("span", {class: "lbltext", text: "System"}));
      const drawGpu = () => {
        const hh = A.health(), ok = !!(hh && hh.comfy && hh.comfy.ok);
        fill(gpu, h("span", {class: "pill " + (ok ? "ok" : hh ? "bad" : ""), text: ok ? "Ready" : hh ? "Offline" : "Checking…"}),
          ok && hh.comfy.version ? h("span", {class: "hint", text: "ComfyUI " + hh.comfy.version}) : null);
      };
      fill(el, h("div", {class: "setgrid"},
        card("Appearance",
          row("Theme", "Follow your system, or pick light or dark.", themeBtn)),
        card("GPU and models",
          row("ComfyUI", "Makes pictures, clips and music on your graphics card.", gpu,
            h("button", {type: "button", class: "ghost", text: "Check again", on: {click: () => A.recheck()}})),
          row("What each feature needs", "Missing models are listed with the command that fetches them.",
            h("button", {type: "button", class: "ghost", text: "Show status", on: {click: () => A.about()}}))),
        card("Connections",
          row("Google Drive and your AI agent", "Sync files and your content plan, or let an agent make and schedule.",
            h("button", {type: "button", class: "ghost", on: {click: () => go("connect")}}, icon("link"), "Open Connect"))),
        card("Access",
          cfg.auth ? row("Signed in", "This YourContent asks for its access key.",
            h("button", {type: "button", class: "ghost", on: {click: async () => {
              try { await api("POST", "/api/logout"); } catch (e) { /* signed out either way */ }
              location.assign("/login");
            }}}, icon("logout"), "Sign out"))
            : row("No access key set", "Anyone who can reach this address can use it. Set MEDIA_LAB_TOKEN to require a key.")),
        card("Help",
          row("Welcome", "The first-run steps, again.", h("button", {type: "button", class: "ghost", text: "Open", on: {click: () => window.MediaLabWelcome && window.MediaLabWelcome.open()}})),
          row("Setup video", "Three minutes from install to your first post, with captions. Opens yourcontent.online.",
            h("button", {type: "button", class: "ghost", text: "Open", on: {click: () => window.open(SETUP_VIDEO, "_blank", "noopener")}})),
          window.MediaLabTour && row("Tour", "A short walk through every view.", h("button", {type: "button", class: "ghost", text: "Start", on: {click: () => window.MediaLabTour.start()}})),
          row("About and licenses", (cfg.app || "") + " " + (cfg.version || ""), h("button", {type: "button", class: "ghost", text: "Open", on: {click: () => A.about()}})))));
      // the theme button joins theme.js's set: same cycle, same label
      const real = document.getElementById("themebtn");
      themeBtn.addEventListener("click", () => { if (real) real.click(); paintTheme(); });
      function paintTheme() {
        const v = window.MediaLabTheme ? window.MediaLabTheme.get() : "system";
        themeBtn.querySelector(".lbltext").textContent = {system: "System", light: "Light", dark: "Dark"}[v];
        themeBtn.querySelector("use").setAttribute("href", "/static/icons.svg#" + {system: "system", light: "sun", dark: "moon"}[v]);
      }
      paintTheme(); drawGpu();
      window.addEventListener("medialab:health", drawGpu);
    },
  };

  window.MediaLabHome = Home;
  window.MediaLabLibrary = Library;
  window.MediaLabSettings = Settings;
})();
