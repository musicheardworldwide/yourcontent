"use strict";
/* Content calendar (studio merge): a planner in the Buffer / Later mould. A drafts rail of unscheduled posts
   (grouped Idea, Draft, Ready) beside a month or week grid; drag a draft onto a day to schedule it, or use its
   "Schedule" button (keyboard and touch). Status filter chips; every post shows its status as a dot and a word.
   Changing a status can carry a note, and each post keeps a history (who, when, note) shown in its editor.
   Vanilla JS, no build step, strict CSP (no inline handlers or styles: every look is a class in calendar.css).
   Talks to /api/calendar*, and reads /api/projects and /api/recent for the link pickers. Every POST carries
   X-Media-Lab: 1, as in app.js.

   window.MediaLabCalendar.mount(el) renders into el and returns {reload, add}; add({asset, title, caption}) opens
   a new draft with that file attached. A button with
   data-calendar-toggle="<host id>" opens and closes that host (mounting it on first open), so the page needs no
   change to app.js. Keyboard: Tab reaches every day and entry; arrow keys move between days; Alt + arrow keys move
   the focused entry a day (left/right) or a week (up/down); Enter opens it. View and filter are remembered. */
(function () {
  const STATUSES = ["idea", "draft", "ready", "posted", "skipped"];
  const RAIL = ["idea", "draft", "ready"];          // the rail's groups; posted/skipped show only when present
  const SHORT = {idea: "Idea", draft: "Draft", ready: "Ready", posted: "Posted", skipped: "Skip"};
  const CHANNELS = ["instagram_post", "instagram_reel", "instagram_story", "tiktok", "youtube_short", "x_post",
    "blog", "newsletter"];
  const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
    "November", "December"];
  const DRAG = "application/x-media-lab-calendar";
  const IMAGE = /\.(png|jpe?g|webp)$/i;
  const narrow = () => !!(window.matchMedia && window.matchMedia("(max-width: 799px)").matches);
  const coarse = () => !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
  const MAXCAP = 2200, MAXNOTE = 500;
  let seq = 0;

  /* ---------------------------------------------------------------- small helpers (same shape as app.js) */

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null) continue;
      if (v === false) { if (k in el) el[k] = false; continue; }
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
      else if (k === "data") Object.assign(el.dataset, v);
      else if (typeof v !== "string" && k in el) el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat()) if (c != null && c !== false) el.append(c.nodeType ? c : String(c));
    return el;
  }
  /* an icon from the sprite (icons.svg), always placed beside a text label or inside a labelled button */
  function icon(name, extra) {
    const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg"), use = document.createElementNS(NS, "use");
    svg.setAttribute("class", "ic" + (extra ? " " + extra : "")); svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", "/static/icons.svg#" + name);
    svg.append(use);
    return svg;
  }
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } }
  const label = s => s ? s[0].toUpperCase() + s.slice(1) : "";
  const dot = () => h("span", {class: "cal-dot", "aria-hidden": "true"});

  async function api(method, path, body) {
    const opt = {method, headers: {}};
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
      const msg = r.status === 401 ? "Sign in first." : ((j && j.error) || ("Something went wrong (" + r.status + ")."));
      const err = new Error(msg);
      err.status = r.status; err.body = j;
      throw err;
    }
    return j;
  }

  /* ---------------------------------------------------------------- dates: local days as YYYY-MM-DD */

  const pad = n => String(n).padStart(2, "0");
  const ymd = d => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  function parseDay(s) { const p = s.split("-").map(Number); return new Date(p[0], p[1] - 1, p[2]); }
  function addDays(d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); }
  function mondayOf(d) { return addDays(d, -((d.getDay() + 6) % 7)); }
  function longDay(d) { return WEEKDAYS[(d.getDay() + 6) % 7] + " " + d.getDate() + " " + MONTHS[d.getMonth()]; }
  /* "2026-10-12T09:30:15-07:00" -> {day: "2026-10-12", time: "09:30", tail: ":15-07:00"}; a date has time "" */
  function splitWhen(w) {
    const day = String(w || "").slice(0, 10);
    const m = /^[T ](\d\d:\d\d)(:\d\d)?(Z|[+-]\d\d:?\d\d)?$/.exec(String(w || "").slice(10));
    return m ? {day, time: m[1], tail: (m[2] || "") + (m[3] || "")} : {day, time: "", tail: ""};
  }
  function joinWhen(day, time, tail) { return time ? day + "T" + time + (tail || "") : day; }
  function stamp(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return String(iso || "");
    return d.getDate() + " " + MONTHS[d.getMonth()].slice(0, 3) + " " + d.getFullYear() + ", " +
      pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  /* ---------------------------------------------------------------- one mounted calendar */

  function create(host) {
    const n = ++seq, id = s => "cal" + n + "-" + s;
    const savedView = lsGet("ml-cal-view"), savedFilter = lsGet("ml-cal-filter");
    const C = {
      view: ["month", "week"].includes(savedView) ? savedView : (narrow() ? "week" : "month"),
      filter: STATUSES.includes(savedFilter) ? savedFilter : "all",
      cursor: new Date(), focus: ymd(new Date()), all: new Map(), loading: 0, editing: null, projects: [], recent: [],
      asset: null, opener: null, dragId: null, railOpen: false, scheduling: null,
    };
    const shown = e => C.filter === "all" || e.status === C.filter;

    /* toolbar */
    const prev = h("button", {class: "ghost cal-nav", type: "button", text: "‹"});
    const next = h("button", {class: "ghost cal-nav", type: "button", text: "›"});
    const today = h("button", {class: "ghost", type: "button", text: "Today"});
    const title = h("h2", {class: "cal-title", id: id("title"), "aria-live": "polite"});
    const vMonth = h("button", {class: "cal-seg", type: "button", text: "Month", data: {view: "month"}});
    const vWeek = h("button", {class: "cal-seg", type: "button", text: "Week", data: {view: "week"}});
    const railCount = h("span", {class: "cal-count"});
    const railBtn = h("button", {class: "ghost cal-railbtn", type: "button", "aria-controls": id("rail"),
      "aria-expanded": "false"}, icon("layers"), "Drafts", railCount);
    const ics = h("a", {class: "btn ghost", href: "/api/calendar.ics", download: "yourcontent-calendar.ics",
      title: "Download the calendar (.ics) for a calendar app"}, icon("download"), h("span", {class: "cal-lbl", text: "Download .ics"}));
    const sub = h("button", {class: "ghost", type: "button", text: "Subscribe",
      title: "Copy a private link that Google, Apple or Outlook Calendar can subscribe to"});
    const add = h("button", {class: "cal-add", type: "button"}, icon("plus"), "New post");
    const status = h("div", {class: "status", role: "status"});

    /* status filter chips */
    const chips = ["all"].concat(STATUSES).map(s => h("button", {class: "chip cal-fchip" + (s === "all" ? "" : " st-" + s),
      type: "button", data: {filter: s}}, s === "all" ? null : dot(), s === "all" ? "All" : label(s)));
    const filters = h("div", {class: "cal-filters", role: "group", "aria-label": "Show posts by status"}, chips);

    /* the rail (a side column on wide screens, a sheet on phones) */
    const railList = h("div", {class: "cal-railbody"});
    const railClose = h("button", {class: "ghost cal-railx", type: "button", text: "Close"});
    const railNew = h("button", {class: "ghost tiny cal-railnew", type: "button"}, icon("plus"), "New idea");
    const rail = h("aside", {class: "cal-rail", id: id("rail"), "aria-labelledby": id("railtitle")},
      h("div", {class: "cal-railhead"}, h("h3", {id: id("railtitle"), text: "Unscheduled"}), h("span", {class: "sp"}),
        railNew, railClose),
      h("p", {class: "hint cal-railhint", text: coarse() ? "Use Schedule to give a draft a day."
        : "Drag a draft onto a day, or use Schedule. Drop a post here to unschedule it."}),
      railList);
    const scrim = h("div", {class: "cal-scrim", hidden: true});

    const heads = h("div", {class: "cal-heads", "aria-hidden": "true"},
      WEEKDAYS.map(d => h("span", {text: d.slice(0, 3)})));
    const days = h("ol", {class: "cal-days", "aria-labelledby": id("title")});
    const empty = h("div", {class: "cal-empty", hidden: true});
    const hint = h("p", {class: "hint cal-hint", text: coarse()
      ? "Tap a day to add a post. Tap a post to edit or move it."
      : "Click a day to add a post. Drag a post to another day, or focus it and press Alt + arrow keys. Arrow keys " +
        "move between days."});
    const main = h("div", {class: "cal-main"}, empty, h("div", {class: "cal-grid"}, heads, days), h("div", {class: "cal-foot"}, hint));
    const root = h("div", {class: "cal"},
      h("div", {class: "cal-bar"}, railBtn, h("div", {class: "cal-navs"}, prev, today, next), title, h("span", {class: "sp"}),
        h("div", {class: "cal-views", role: "group", "aria-label": "View"}, vMonth, vWeek), ics, sub, add),
      filters, status, h("div", {class: "cal-body"}, rail, main), scrim);
    host.replaceChildren(root);

    sub.addEventListener("click", async () => {
      try {
        const j = await api("GET", "/api/calendar/feed");
        try { await navigator.clipboard.writeText(j.url); say("Subscribe link copied. Paste it into your " +
          "calendar app's “add by URL”. Anyone with the link can read the plan."); }
        catch (e) { say("Subscribe link (anyone with it can read the plan): " + j.url); }
      } catch (e) { say(e.message, "err"); }
    });

    function say(text, kind) {
      status.className = "status" + (kind ? " " + kind : "");
      status.setAttribute("role", kind === "err" ? "alert" : "status");
      status.textContent = text || "";
    }

    /* the visible range */
    function range() {
      if (C.view === "week") { const a = mondayOf(C.cursor); return [a, addDays(a, 6)]; }
      const first = new Date(C.cursor.getFullYear(), C.cursor.getMonth(), 1);
      const last = new Date(C.cursor.getFullYear(), C.cursor.getMonth() + 1, 0);
      return [mondayOf(first), addDays(mondayOf(last), 6)];
    }

    async function load() {
      const [a, b] = range();
      const tok = ++C.loading;
      try {
        const [j, u] = await Promise.all([api("GET", "/api/calendar?from=" + ymd(a) + "&to=" + ymd(b)),
          api("GET", "/api/calendar?unscheduled=1")]);
        if (tok !== C.loading) return;
        C.all = new Map();
        for (const e of (j.entries || []).concat(u.entries || [])) C.all.set(e.id, e);
        render();
        if (status.classList.contains("err")) say("");
      } catch (e) { if (tok === C.loading) say(e.message, "err"); }
    }

    const put = e => { C.all.set(e.id, e); };
    const drop = eid => { C.all.delete(eid); };
    const byTime = (x, y) => (x.when + x.id < y.when + y.id ? -1 : 1);
    const scheduled = () => [...C.all.values()].filter(e => e.when).sort(byTime);
    const unscheduled = () => [...C.all.values()].filter(e => !e.when);

    /* ------------------------------------------------ grid */

    function chip(e) {
      const w = splitWhen(e.when);
      const lab = [e.title, e.channel, w.time || "all day", label(e.status)].filter(Boolean).join(", ");
      return h("li", {class: "cal-item"},
        h("button", {class: "cal-entry st-" + e.status, type: "button", draggable: "true", data: {id: e.id, fk: "e" + e.id},
          title: lab, "aria-label": lab + ". Open to edit"},
          e.asset && IMAGE.test(e.asset.name) ? h("img", {class: "cal-pic", alt: "", loading: "lazy", draggable: "false",
            src: "/m/" + encodeURIComponent(e.asset.root) + "/" + encodeURIComponent(e.asset.name)}) : null,
          dot(),
          w.time ? h("span", {class: "cal-time", text: w.time}) : null,
          e.channel ? h("span", {class: "cal-chan", text: e.channel}) : null,
          h("span", {class: "cal-ttl", text: e.title}),
          h("span", {class: "cal-stw", text: label(e.status)}),
          h("span", {class: "cal-sta", text: SHORT[e.status] || label(e.status)})));
    }

    function renderGrid() {
      const [a, b] = range();
      const now = ymd(new Date()), month = C.cursor.getMonth();
      title.textContent = C.view === "month" ? MONTHS[month] + " " + C.cursor.getFullYear()
        : a.getDate() + " " + MONTHS[a.getMonth()].slice(0, 3) + " – " + b.getDate() + " " +
          MONTHS[b.getMonth()].slice(0, 3) + " " + b.getFullYear();
      const unit = C.view === "month" ? "month" : "week";
      prev.setAttribute("aria-label", "Previous " + unit);
      next.setAttribute("aria-label", "Next " + unit);
      vMonth.setAttribute("aria-pressed", String(C.view === "month"));
      vWeek.setAttribute("aria-pressed", String(C.view === "week"));
      root.classList.toggle("cal-week", C.view === "week");
      root.classList.toggle("cal-month", C.view === "month");
      const byDay = new Map();
      let inView = 0, hiddenByFilter = 0;
      const lo = ymd(C.view === "month" ? new Date(C.cursor.getFullYear(), month, 1) : a);
      const hi = ymd(C.view === "month" ? new Date(C.cursor.getFullYear(), month + 1, 0) : b);
      for (const e of scheduled()) {
        const d = splitWhen(e.when).day;
        if (d >= lo && d <= hi) { if (shown(e)) inView++; else hiddenByFilter++; }
        if (!shown(e)) continue;
        if (!byDay.has(d)) byDay.set(d, []);
        byDay.get(d).push(e);
      }
      const had = document.activeElement && root.contains(document.activeElement) ? document.activeElement.dataset.fk : null;
      const cells = [];
      for (let d = a; d <= b; d = addDays(d, 1)) {
        const key = ymd(d), list = byDay.get(key) || [];
        const cls = "cal-day" + (C.view === "month" && d.getMonth() !== month ? " cal-out" : "") +
          (key === now ? " cal-today" : "");
        const count = list.length ? ", " + list.length + (list.length === 1 ? " post" : " posts") : "";
        cells.push(h("li", {class: cls, data: {day: key}, "aria-current": key === now ? "date" : null},
          h("button", {class: "cal-daybtn", type: "button", data: {day: key, fk: "d" + key},
            "aria-label": longDay(d) + count + ". Add a post", tabindex: key === C.focus ? "0" : "-1"},
            h("span", {class: "cal-wd", text: WEEKDAYS[(d.getDay() + 6) % 7].slice(0, 3) + " "}),
            String(d.getDate())),
          list.length ? h("ul", {class: "cal-list"}, list.map(chip)) : null));
      }
      days.replaceChildren(...cells);
      if (!days.querySelector('.cal-daybtn[tabindex="0"]')) {
        const first = days.querySelector(".cal-daybtn");
        if (first) first.tabIndex = 0;
      }
      // empty state: one line, one action
      if (!inView) {
        const span = C.view === "month" ? "this month" : "this week";
        if (hiddenByFilter) {
          empty.replaceChildren(h("p", {text: "No " + label(C.filter) + " posts " + span + "."}),
            h("button", {class: "ghost", type: "button", text: "Show all", on: {click: () => setFilter("all")}}));
        } else {
          empty.replaceChildren(h("p", {text: "Nothing planned " + span + "."}),
            h("button", {class: "ghost", type: "button", on: {click: ev => openEditor(null, C.focus, ev.currentTarget)}},
              icon("plus"), "Plan a post"));
        }
      }
      empty.hidden = !!inView;
      if (had) {
        const back = [...root.querySelectorAll("[data-fk]")].find(x => x.dataset.fk === had);
        if (back) back.focus({preventScroll: true});
      }
    }

    /* ------------------------------------------------ rail */

    function card(e) {
      const lab = [e.title, e.channel, label(e.status)].filter(Boolean).join(", ");
      return h("li", {class: "cal-card st-" + e.status, draggable: "true", data: {id: e.id}},
        h("button", {class: "cal-cardmain", type: "button", data: {id: e.id, fk: "r" + e.id, act: "open"},
          "aria-label": lab + ". Open to edit"},
          h("span", {class: "cal-cardst"}, dot(), label(e.status)),
          h("span", {class: "cal-ttl", text: e.title}),
          e.channel ? h("span", {class: "cal-chan", text: e.channel}) : null),
        h("button", {class: "ghost cal-sched", type: "button", data: {id: e.id, fk: "s" + e.id, act: "schedule"},
          "aria-label": "Schedule “" + e.title + "”"}, icon("calendar"), "Schedule"));
    }

    function renderRail() {
      const list = unscheduled().sort((x, y) => (String(x.created) + x.id < String(y.created) + y.id ? -1 : 1));
      railCount.textContent = String(list.length);
      railBtn.setAttribute("aria-label", "Drafts, " + list.length + " unscheduled");
      const groups = (C.filter === "all" ? STATUSES : [C.filter])
        .filter(s => RAIL.includes(s) || list.some(e => e.status === s));
      const kids = [];
      let any = 0;
      for (const s of groups) {
        const items = list.filter(e => e.status === s);
        if (!items.length) continue;
        any += items.length;
        kids.push(h("section", {class: "cal-group st-" + s, "aria-label": label(s)},
          h("h4", null, dot(), label(s), h("span", {class: "cal-gcount", text: String(items.length)})),
          h("ul", {class: "cal-cards"}, items.map(card))));
      }
      if (!any) {
        kids.push(C.filter !== "all" && list.length
          ? h("div", {class: "cal-railempty"}, h("p", {text: "No unscheduled " + label(C.filter) + " posts."}),
            h("button", {class: "ghost", type: "button", text: "Show all", on: {click: () => setFilter("all")}}))
          : h("div", {class: "cal-railempty"}, h("p", {text: "Nothing waiting for a date."}),
            h("button", {class: "ghost", type: "button", on: {click: ev => newIdea(ev.currentTarget)}}, icon("plus"), "New idea")));
      }
      const had = document.activeElement && rail.contains(document.activeElement) ? document.activeElement.dataset.fk : null;
      railList.replaceChildren(...kids);
      if (had) {
        const back = [...rail.querySelectorAll("[data-fk]")].find(x => x.dataset.fk === had);
        if (back) back.focus({preventScroll: true});
      }
    }

    function render() {
      for (const b of chips) b.setAttribute("aria-pressed", String(b.dataset.filter === C.filter));
      renderGrid();
      renderRail();
    }

    function setFilter(s) { C.filter = s; lsSet("ml-cal-filter", s); render(); }
    for (const b of chips) b.addEventListener("click", () => setFilter(b.dataset.filter));

    function showRail(open) {
      C.railOpen = open;
      root.classList.toggle("cal-railopen", open);
      railBtn.setAttribute("aria-expanded", String(open));
      scrim.hidden = !open;
      if (open) {
        const t = rail.querySelector(".cal-cardmain, .cal-railempty button") || railClose;
        t.focus({preventScroll: true});
      } else if (narrow()) railBtn.focus({preventScroll: true});
    }
    railBtn.addEventListener("click", () => showRail(!C.railOpen));
    railClose.addEventListener("click", () => showRail(false));
    scrim.addEventListener("click", () => showRail(false));
    rail.addEventListener("keydown", e => { if (e.key === "Escape" && C.railOpen) { e.preventDefault(); showRail(false); } });
    railNew.addEventListener("click", () => newIdea(railNew));
    function newIdea(opener) { openEditor(null, null, opener); f.unsched.checked = true; syncUnsched(); }

    rail.addEventListener("click", e => {
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const x = C.all.get(b.dataset.id);
      if (!x) return;
      if (b.dataset.act === "open") openEditor(x, null, b);
      else openSchedule(x, b);
    });

    /* ------------------------------------------------ navigation */

    function go(delta) {
      if (C.view === "month") C.cursor = new Date(C.cursor.getFullYear(), C.cursor.getMonth() + delta, 1);
      else C.cursor = addDays(C.cursor, 7 * delta);
      load();
    }
    function setView(v) { C.view = v; lsSet("ml-cal-view", v); C.cursor = parseDay(C.focus); load(); }
    function focusDay(key) {
      const [a, b] = range();
      C.focus = key;
      const d = parseDay(key);
      const inside = d >= a && d <= b && (C.view === "week" || d.getMonth() === C.cursor.getMonth());
      const finish = () => {
        for (const x of days.querySelectorAll(".cal-daybtn")) x.tabIndex = x.dataset.day === key ? 0 : -1;
        const btn = days.querySelector('.cal-daybtn[data-day="' + key + '"]');
        if (btn) btn.focus();
      };
      if (inside) { finish(); return; }
      C.cursor = d;
      load().then(finish);
    }

    prev.addEventListener("click", () => go(-1));
    next.addEventListener("click", () => go(1));
    today.addEventListener("click", () => { C.cursor = new Date(); C.focus = ymd(C.cursor); load(); });
    vMonth.addEventListener("click", () => setView("month"));
    vWeek.addEventListener("click", () => setView("week"));
    add.addEventListener("click", () => openEditor(null, C.focus, add));

    days.addEventListener("click", e => {
      const ent = e.target.closest(".cal-entry");
      if (ent) { const x = C.all.get(ent.dataset.id); if (x) openEditor(x, null, ent); return; }
      const cell = e.target.closest(".cal-day");
      if (!cell || e.target.closest(".cal-list .cal-item")) return;
      C.focus = cell.dataset.day;
      // a phone's month shows posts in short: a day that has some opens its week, where they can be tapped
      if (C.view === "month" && narrow() && cell.querySelector(".cal-entry")) { C.view = "week"; C.cursor = parseDay(C.focus); load(); return; }
      openEditor(null, cell.dataset.day, cell.querySelector(".cal-daybtn"));
    });

    days.addEventListener("keydown", e => {
      const step = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7}[e.key];
      if (!step) return;
      const ent = e.target.closest(".cal-entry");
      if (ent && e.altKey) {
        e.preventDefault();
        const x = C.all.get(ent.dataset.id);
        if (x) moveTo(x, ymd(addDays(parseDay(splitWhen(x.when).day), step)));
        return;
      }
      const btn = e.target.closest(".cal-daybtn");
      if (btn && !e.altKey) { e.preventDefault(); focusDay(ymd(addDays(parseDay(btn.dataset.day), step))); }
    });

    /* ------------------------------------------------ drag: grid <-> grid, rail -> grid, grid -> rail */

    root.addEventListener("dragstart", e => {
      const src = e.target.closest && e.target.closest(".cal-entry, .cal-card");
      if (!src) return;
      C.dragId = src.dataset.id;
      e.dataTransfer.setData(DRAG, src.dataset.id);
      e.dataTransfer.setData("text/plain", (C.all.get(src.dataset.id) || {}).title || "");
      e.dataTransfer.effectAllowed = "move";
      src.classList.add("dragging");
      root.classList.add("cal-dragging");
    });
    /* the dragged element is often re-rendered away before dragend fires, so a drop ends the drag too */
    function endDrag() {
      C.dragId = null;
      root.classList.remove("cal-dragging");
      for (const c of root.querySelectorAll(".over, .dragging")) c.classList.remove("over", "dragging");
    }
    root.addEventListener("dragend", endDrag);
    root.addEventListener("drop", endDrag);
    const ours = e => [...e.dataTransfer.types].includes(DRAG);
    days.addEventListener("dragover", e => {
      const cell = e.target.closest && e.target.closest(".cal-day");
      if (!cell || !ours(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      for (const c of days.querySelectorAll(".cal-day.over")) if (c !== cell) c.classList.remove("over");
      cell.classList.add("over");
    });
    days.addEventListener("dragleave", e => {
      const cell = e.target.closest && e.target.closest(".cal-day");
      if (cell && !cell.contains(e.relatedTarget)) cell.classList.remove("over");
    });
    days.addEventListener("drop", e => {
      const cell = e.target.closest && e.target.closest(".cal-day");
      if (!cell) return;
      e.preventDefault();
      cell.classList.remove("over");
      const x = C.all.get(e.dataTransfer.getData(DRAG));
      if (x) moveTo(x, cell.dataset.day);
    });
    rail.addEventListener("dragover", e => {
      const x = C.dragId && C.all.get(C.dragId);
      if (!ours(e) || !x || !x.when) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      rail.classList.add("over");
    });
    rail.addEventListener("dragleave", e => { if (!rail.contains(e.relatedTarget)) rail.classList.remove("over"); });
    rail.addEventListener("drop", e => {
      rail.classList.remove("over");
      const x = C.all.get(e.dataTransfer.getData(DRAG));
      if (!x || !x.when) return;
      e.preventDefault();
      moveTo(x, null);
    });

    /* day null = back to the rail; time (optional) replaces the entry's time */
    async function moveTo(x, day, time) {
      const w = splitWhen(x.when);
      const t = time === undefined ? w.time : time;
      const when = day ? joinWhen(day, t, t === w.time ? w.tail : "") : null;
      if (when === (x.when || null)) return;
      const old = x;
      put(Object.assign({}, x, {when}));
      if (day) C.focus = day;
      render();
      say((day ? "Scheduling “" : "Unscheduling “") + x.title + "”…");
      try {
        put(await api("POST", "/api/calendar/" + encodeURIComponent(x.id), {rev: x.rev, when}));
        say(day ? "“" + x.title + "” is on " + longDay(parseDay(day)) + "." : "“" + x.title + "” is back in Unscheduled.");
      } catch (e) {
        if (e.status === 409 && e.body && e.body.current) { put(e.body.current); say("That post was changed elsewhere; showing the newer version.", "warn"); }
        else if (e.status === 404) { drop(x.id); say("That post was deleted elsewhere.", "warn"); }
        else { put(old); say(e.message, "err"); }
      }
      render();
    }

    /* ------------------------------------------------ schedule dialog (the keyboard and touch way to drag) */

    const s = {
      head: h("h2", {id: id("schtitle")}),
      date: h("input", {id: id("schdate"), type: "date", required: true}),
      time: h("input", {id: id("schtime"), type: "time"}),
      msg: h("div", {class: "status", role: "status"}),
      cancel: h("button", {class: "ghost", type: "button", text: "Cancel"}),
      ok: h("button", {type: "submit", text: "Schedule"}),
    };
    const sform = h("form", {class: "cal-form", novalidate: true}, s.head,
      h("div", {class: "cal-row2"},
        h("div", null, h("label", {for: s.date.id, text: "Day"}), s.date),
        h("div", null, h("label", {for: s.time.id, text: "Time (empty = all day)"}), s.time)),
      s.msg, h("div", {class: "ebtns"}, h("span", {class: "sp"}), s.cancel, s.ok));
    const sdlg = h("dialog", {class: "cal-dlg cal-sdlg", "aria-labelledby": s.head.id}, sform);
    document.body.append(sdlg);
    s.cancel.addEventListener("click", () => sdlg.close());
    sdlg.addEventListener("close", () => {
      const o = C.scheduling && C.scheduling.opener;
      C.scheduling = null;
      if (o && o.isConnected) o.focus();
    });
    function openSchedule(x, opener) {
      C.scheduling = {x, opener};
      s.head.textContent = "Schedule “" + x.title + "”";
      s.date.value = C.focus || ymd(new Date());
      s.time.value = "";
      s.msg.textContent = "";
      sdlg.showModal();
      s.date.focus();
    }
    sform.addEventListener("submit", async ev => {
      ev.preventDefault();
      const x = C.scheduling && C.scheduling.x;
      if (!x) return;
      if (!/^\d{4}-\d{2}-\d{2}$/.test(s.date.value)) { s.msg.className = "status err"; s.msg.textContent = "Pick a day."; return; }
      const day = s.date.value, time = s.time.value ? s.time.value.slice(0, 5) : "";
      C.scheduling.opener = null;
      sdlg.close();
      if (C.railOpen && narrow()) showRail(false);
      await moveTo(x, day, time);
      const d = parseDay(day), [a, b] = range();
      if (d < a || d > b) { C.cursor = d; await load(); }
      const back = days.querySelector('.cal-entry[data-id="' + x.id + '"]') || days.querySelector('.cal-daybtn[data-day="' + day + '"]');
      if (back) back.focus();
    });

    /* ------------------------------------------------ editor */

    const f = {
      date: h("input", {id: id("date"), type: "date"}),
      time: h("input", {id: id("time"), type: "time"}),
      unsched: h("input", {id: id("unsched"), type: "checkbox"}),
      dur: h("input", {id: id("dur"), type: "number", min: "0", max: "10080", step: "5", inputmode: "numeric"}),
      status: h("select", {id: id("status")}, STATUSES.map(s => h("option", {value: s, text: label(s)}))),
      stdot: h("span", {class: "cal-stnow"}, dot()),
      note: h("textarea", {id: id("note"), maxlength: String(MAXNOTE), rows: "2",
        placeholder: "e.g. Approved, caption checked"}),
      noteCount: h("span", {class: "hint", "aria-live": "off"}),
      channel: h("input", {id: id("channel"), maxlength: "40", list: id("channels"), autocomplete: "off",
        placeholder: "instagram_reel"}),
      title: h("input", {id: id("ttl"), maxlength: "120", placeholder: "Untitled"}),
      caption: h("textarea", {id: id("cap"), maxlength: String(MAXCAP), rows: "5"}),
      count: h("span", {class: "hint", id: id("count"), "aria-live": "off"}),
      project: h("select", {id: id("project")}),
      assetNow: h("span", {class: "hint cal-assetnow"}),
      assetNone: h("button", {class: "ghost tiny", type: "button", text: "None"}),
      strip: h("div", {class: "cal-pick", role: "group", "aria-label": "Recent pictures and clips"}),
      hist: h("ol", {class: "cal-hist"}),
      status2: h("div", {class: "status", role: "status"}),
      del: h("button", {class: "ghost danger", type: "button"}, icon("trash"), "Delete"),
      cancel: h("button", {class: "ghost", type: "button", text: "Cancel"}),
      save: h("button", {type: "submit", text: "Save"}),
      head: h("h2", {id: id("dlgtitle")}),
    };
    const lab = (el, text) => h("label", {for: el.id, text});
    const noteWrap = h("div", {class: "cal-note", hidden: true}, lab(f.note, "Note for this change (optional, kept in the history)"),
      f.note, f.noteCount);
    const histWrap = h("section", {class: "cal-histwrap", "aria-labelledby": id("histtitle")},
      h("h3", {id: id("histtitle"), text: "History"}), f.hist);
    const dayRow = h("div", {class: "cal-row3"},
      h("div", null, lab(f.date, "Day"), f.date),
      h("div", null, lab(f.time, "Time (empty = all day)"), f.time),
      h("div", null, lab(f.dur, "Length (min)"), f.dur));
    const form = h("form", {class: "cal-form", novalidate: true},
      f.head,
      h("div", {class: "cal-row2"},
        h("div", null, lab(f.status, "Status"), h("div", {class: "cal-strow"}, f.stdot, f.status)),
        h("div", null, lab(f.channel, "Channel"), f.channel,
          h("datalist", {id: id("channels")}, CHANNELS.map(c => h("option", {value: c}))))),
      noteWrap,
      h("label", {class: "cal-check", for: f.unsched.id}, f.unsched, "No date yet (keep it in Unscheduled)"),
      dayRow,
      h("div", null, lab(f.title, "Title"), f.title),
      h("div", null, lab(f.caption, "Caption"), f.caption, f.count),
      h("div", null, lab(f.project, "Linked cut"), f.project),
      h("div", null, h("span", {class: "label", id: id("assetlab"), text: "Linked picture or clip"}),
        h("div", {class: "cal-assetrow"}, f.assetNow, f.assetNone), f.strip),
      histWrap,
      f.status2,
      h("div", {class: "ebtns"}, f.del, h("span", {class: "sp"}), f.cancel, f.save));
    const dlg = h("dialog", {class: "cal-dlg", "aria-labelledby": id("dlgtitle")}, form);
    document.body.append(dlg);

    const counter = () => { f.count.textContent = f.caption.value.length + " / " + MAXCAP; };
    const noteCounter = () => { f.noteCount.textContent = f.note.value.length + " / " + MAXNOTE; };
    function syncStatus() {
      f.stdot.className = "cal-stnow st-" + f.status.value;
      const changed = C.editing ? f.status.value !== C.editing.status : false;
      noteWrap.hidden = !changed && !f.note.value;
    }
    function syncUnsched() {
      dayRow.classList.toggle("cal-off", f.unsched.checked);
      f.date.disabled = f.time.disabled = f.unsched.checked;
    }
    f.caption.addEventListener("input", counter);
    f.note.addEventListener("input", noteCounter);
    f.status.addEventListener("change", syncStatus);
    f.unsched.addEventListener("change", syncUnsched);
    f.cancel.addEventListener("click", () => dlg.close());
    f.assetNone.addEventListener("click", () => { C.asset = null; renderAsset(); });
    dlg.addEventListener("close", () => {
      const o = C.opener;
      C.editing = null; C.opener = null;
      if (o && o.isConnected) o.focus();
      else if (o && o.dataset && o.dataset.fk) {
        const back = [...root.querySelectorAll("[data-fk]")].find(x => x.dataset.fk === o.dataset.fk);
        if (back) back.focus();
      }
    });
    function say2(text, kind) {
      f.status2.className = "status" + (kind ? " " + kind : "");
      f.status2.setAttribute("role", kind === "err" ? "alert" : "status");
      f.status2.textContent = text || "";
    }

    function renderHistory(e) {
      const hist = (e && Array.isArray(e.history) ? e.history : []).slice().reverse();
      histWrap.hidden = !e;
      f.hist.replaceChildren(...(hist.length ? hist.map(r => h("li", {class: "st-" + r.status},
        h("span", {class: "cal-hdot"}, dot()),
        h("div", {class: "cal-hbody"},
          h("p", {class: "cal-hline"}, h("strong", {text: label(r.status)}), " · ", r.by || "you", " · ",
            h("time", {datetime: r.at || "", text: stamp(r.at)})),
          r.note ? h("p", {class: "cal-hnote", text: r.note}) : null)))
        : [h("li", {class: "cal-hnone hint", text: "No status changes recorded yet."})]));
    }

    const sameRef = (a, b) => !!(a && b && a.root === b.root && a.name === b.name);
    function renderAsset() {
      const a = C.asset, it = a && C.recent.find(r => sameRef(r.ref, a));
      f.assetNow.textContent = a ? (it && it.label ? it.label : a.root + "/" + a.name) : "None";
      f.assetNone.hidden = !a;
      for (const b of f.strip.querySelectorAll("button")) {
        const on = !!(a && b.dataset.root === a.root && b.dataset.name === a.name);
        b.setAttribute("aria-pressed", String(on));
      }
    }
    function renderPickers() {
      const cur = C.editing ? C.editing.project : null;
      const opts = [h("option", {value: "", text: "None"})];
      for (const p of C.projects) opts.push(h("option", {value: p.id, text: p.name || p.id}));
      if (cur && !C.projects.some(p => p.id === cur)) opts.push(h("option", {value: cur, text: "(deleted cut)"}));
      f.project.replaceChildren(...opts);
      f.project.value = cur || "";
      f.strip.replaceChildren(...C.recent.map(r => {
        const name = r.label || r.ref.name;
        const src = r.poster || (r.type === "image" ? r.url : "");
        return h("button", {class: "cal-thumb", type: "button", data: {root: r.ref.root, name: r.ref.name},
          title: name, "aria-label": (r.type === "video" ? "Clip: " : "Picture: ") + name,
          on: {click: () => { C.asset = {root: r.ref.root, name: r.ref.name}; renderAsset(); }}},
          src ? h("img", {src, alt: "", loading: "lazy"}) : icon("play"));
      }));
      if (!C.recent.length) f.strip.append(h("span", {class: "hint", text: "Nothing made yet."}));
      renderAsset();
    }
    async function loadPickers() {
      const [p, r] = await Promise.allSettled([api("GET", "/api/projects"), api("GET", "/api/recent?limit=40")]);
      if (p.status === "fulfilled" && Array.isArray(p.value)) C.projects = p.value;
      if (r.status === "fulfilled" && Array.isArray(r.value)) C.recent = r.value;
      if (dlg.open) renderPickers();
    }

    function fillForm(e, day) {
      const w = e ? splitWhen(e.when) : {day: day || "", time: "", tail: ""};
      f.head.textContent = e ? "Edit post" : "New post";
      f.unsched.checked = e ? !e.when : !day;
      f.date.value = w.day || C.focus || ymd(new Date()); f.time.value = w.time;
      f.dur.value = String(e ? e.duration_min || 0 : 0);
      f.status.value = e ? e.status : "idea";
      f.note.value = "";
      f.channel.value = e ? e.channel || "" : "";
      f.title.value = e && e.title !== "Untitled" ? e.title : "";
      f.caption.value = e ? e.caption || "" : "";
      f.del.hidden = !e;
      C.asset = e && e.asset ? {root: e.asset.root, name: e.asset.name} : null;
      counter(); noteCounter(); syncStatus(); syncUnsched(); renderHistory(e);
      renderPickers();
    }

    function openEditor(e, day, opener) {
      C.editing = e; C.opener = opener || document.activeElement;
      fillForm(e, day);
      say2("");
      if (!dlg.open) dlg.showModal();
      f.title.focus();
      loadPickers();
    }

    function payload() {
      let when = null;
      if (!f.unsched.checked) {
        const day = f.date.value;
        if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) throw new Error("Pick a day, or tick “No date yet”.");
        const old = C.editing ? splitWhen(C.editing.when) : {time: "", tail: ""};
        const time = f.time.value ? f.time.value.slice(0, 5) : "";
        const tail = time && time === old.time ? old.tail : (time && old.tail ? old.tail.replace(/^:\d\d/, "") : "");
        when = joinWhen(day, time, tail);
      }
      const dur = f.dur.value === "" ? 0 : Number(f.dur.value);
      if (!Number.isInteger(dur) || dur < 0 || dur > 10080) throw new Error("Length must be whole minutes, 0 to 10080.");
      if (f.note.value.length > MAXNOTE) throw new Error("The note is too long (" + MAXNOTE + " characters at most).");
      const body = {when, duration_min: dur, status: f.status.value, channel: f.channel.value,
        title: f.title.value, caption: f.caption.value, project: f.project.value || null, asset: C.asset};
      if (f.note.value.trim()) body.note = f.note.value;
      return body;
    }

    form.addEventListener("submit", async ev => {
      ev.preventDefault();
      let body;
      try { body = payload(); } catch (e) { say2(e.message, "err"); return; }
      const e0 = C.editing;
      f.save.disabled = true;
      say2("Saving…");
      try {
        const saved = e0 ? await api("POST", "/api/calendar/" + encodeURIComponent(e0.id), Object.assign({rev: e0.rev}, body))
          : await api("POST", "/api/calendar", body);
        put(saved);
        if (saved.when) { C.focus = splitWhen(saved.when).day; C.opener = {dataset: {fk: "e" + saved.id}}; }
        else C.opener = {dataset: {fk: "r" + saved.id}};
        dlg.close();
        render();
        const moved = e0 && e0.status !== saved.status ? " Now " + label(saved.status) + "." : "";
        say((e0 ? "Saved “" : "Added “") + saved.title + "”." + moved);
      } catch (e) {
        if (e.status === 409 && e.body && e.body.current) {
          put(e.body.current); C.editing = e.body.current; fillForm(e.body.current); render();
          say2("This post was changed elsewhere. The newer version is shown; make your change again.", "warn");
        } else if (e.status === 404 && e0) {
          drop(e0.id); render(); dlg.close(); say("That post was deleted elsewhere.", "warn");
        } else say2(e.message, "err");
      } finally { f.save.disabled = false; }
    });

    f.del.addEventListener("click", async () => {
      const e0 = C.editing;
      if (!e0 || !window.confirm("Delete “" + e0.title + "” from the calendar?")) return;
      f.del.disabled = true;
      try {
        await api("POST", "/api/calendar/" + encodeURIComponent(e0.id) + "/delete", {rev: e0.rev});
        drop(e0.id);
        C.opener = e0.when ? {dataset: {fk: "d" + splitWhen(e0.when).day}} : railNew;
        dlg.close(); render();
        say("Deleted “" + e0.title + "”.");
      } catch (e) {
        if (e.status === 409 && e.body && e.body.current) {
          put(e.body.current); C.editing = e.body.current; fillForm(e.body.current); render();
          say2("This post was changed elsewhere. Check the newer version before deleting it.", "warn");
        } else if (e.status === 404) { drop(e0.id); render(); dlg.close(); }
        else say2(e.message, "err");
      } finally { f.del.disabled = false; }
    });

    /* a new post with a file already attached (Posts › Add to calendar) */
    function addWith(o) {
      openEditor(null, ymd(new Date()));
      f.status.value = "draft"; syncStatus();
      if (o && o.title) f.title.value = String(o.title).slice(0, 120);
      if (o && o.caption) { f.caption.value = String(o.caption).slice(0, MAXCAP); counter(); }
      if (o && o.asset) { C.asset = {root: o.asset.root, name: o.asset.name}; renderPickers(); }
    }

    render();
    load();
    return {reload: load, root, add: addWith};
  }

  /* ---------------------------------------------------------------- public: mount + optional toggle buttons */

  const mounted = new WeakMap();
  function mount(el) {
    if (!el || !el.nodeType) throw new Error("MediaLabCalendar.mount needs an element");
    if (!mounted.has(el)) mounted.set(el, create(el));
    return mounted.get(el);
  }

  function wireToggles() {
    for (const b of document.querySelectorAll("[data-calendar-toggle]")) {
      const host = document.getElementById(b.dataset.calendarToggle);
      if (!host) continue;
      b.setAttribute("aria-controls", host.id);
      const show = open => {
        host.hidden = !open;
        // on <html>, not <body>: app.js rewrites body's class list when its tabs change
        document.documentElement.classList.toggle("cal-open", open);
        b.setAttribute("aria-expanded", String(open));
        b.classList.toggle("on", open);
        lsSet("ml-cal-open", open ? "1" : "0");
      };
      show(!host.hidden || lsGet("ml-cal-open") === "1");
      if (!host.hidden) mount(host);
      b.addEventListener("click", () => {
        const open = host.hidden;
        show(open);
        if (open) {
          const existed = mounted.has(host), c = mount(host);
          if (existed) c.reload();
          const t = host.querySelector(".cal-daybtn[tabindex='0']") || host.querySelector("button");
          if (t) t.focus({preventScroll: true});
        }
      });
    }
  }

  window.MediaLabCalendar = {mount};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wireToggles);
  else wireToggles();
})();
