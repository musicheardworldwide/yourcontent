"use strict";
/* YourContent page (§10 of docs/DESIGN.md). Vanilla JS, no build step, no inline handlers (strict CSP).
   The server does every timing and licence calculation; this file only shows what `resolved` says. */

const $ = id => document.getElementById(id);
const MAXREFS = 6, MAXITEMS = 60;
const SHAPES = {"9:16": "9:16 Reels / Stories", "4:5": "4:5 Feed", "1:1": "1:1 Square", "16:9": "16:9 Wide",
  "source": "Same as first item"};
const SLOT = {generate: "status", animate: "vstatus", extend: "vstatus", music: "mstatus", preview: "xstatus",
  export: "xstatus"};
const LABEL = {generate: "Painting…", animate: "Animating…", extend: "Extending…", music: "Making music…",
  preview: "Making preview…", export: "Exporting…"};
const BUTTON = {generate: "go", animate: "vgo", extend: "vgo", music: "mgo", export: "xexport"};

const S = {
  config: null, health: null, recent: [], byKey: new Map(), tracks: [], refs: [], current: null, vstart: null,
  project: null, rev: 0, resolved: null, projects: [], sel: null, conflict: null, saveState: "",
  save: {timer: null, inflight: null, dirty: false}, info: new Map(), infoP: new Map(),
  tab: "picture", card: "clip", jobs: {}, creating: null, adding: null, dragged: 0, lastSeed: null, seedMode: "",
};

/* ------------------------------------------------------------------ small helpers */

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
/* replaceChildren that skips null/false (replaceChildren itself would print "null") */
/* Rebuilding a list must not drop keyboard focus: an element marked data-fk gets it back after the rebuild. */
function fill(el, ...kids) {
  const a = document.activeElement;
  const fk = a && a !== el && el.contains(a) && a.dataset ? a.dataset.fk : null;
  el.replaceChildren(...kids.flat().filter(k => k != null && k !== false));
  if (fk) {
    const back = [...el.querySelectorAll("[data-fk]")].find(x => x.dataset.fk === fk);
    if (back) back.focus({preventScroll: true});
  }
  return el;
}
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { /* private window */ } }
const refKey = r => r.root + "/" + r.name;
const sameRef = (a, b) => !!(a && b && a.root === b.root && a.name === b.name);
const fmt1 = x => (Math.round(Number(x) * 10) / 10).toFixed(1);
const fmt2 = x => (Math.round(Number(x) * 100) / 100).toFixed(2);
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
function rhex(n) { const b = new Uint8Array(n); crypto.getRandomValues(b); return [...b].map(x => (x % 16).toString(16)).join(""); }
function parseRate(r) {
  if (typeof r === "number") return r;
  const m = /^(\d+)(?:\/(\d+))?$/.exec(String(r || ""));
  return m ? (+m[1]) / (+(m[2] || 1)) : 16;
}
function mediaUrl(ref) { return "/m/" + ref.root + "/" + encodeURIComponent(ref.name); }
function nameOf(it) { return (it && (it.label || (it.ref && it.ref.name))) || "this file"; }

function say(id, text, kind, onStop) {
  const el = $(id);
  if (!el) return;
  el.className = "status" + (id === "tstatus" ? " tstatus" : "") + (kind ? " " + kind : "");
  el.setAttribute("role", kind === "err" ? "alert" : "status");   // screen readers announce results and errors
  el.textContent = text || "";
  if (onStop) el.append(h("button", {class: "link", type: "button", text: "Stop", on: {click: onStop}}));
}
function fail(id, e) { say(id, (e && e.message) || String(e), "err"); }

/* ------------------------------------------------------------------ server calls */

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
  if (r.status !== 204) { try { j = await r.json(); } catch (e) { j = null; } }
  if (r.status === 401 && path !== "/api/login") needLogin();
  if (!r.ok) {
    const err = new Error((j && j.error) || ("Something went wrong (" + r.status + ")."));
    err.status = r.status; err.body = j;
    throw err;
  }
  return j;
}

async function upload(file) {
  const lim = (S.config && S.config.limits) || {};
  const kind = (file.type || "").split("/")[0];
  const mb = {image: lim.image_mb, video: lim.video_mb, audio: lim.audio_mb}[kind];
  if (mb && file.size > mb * 1048576) throw new Error(file.name + " is over " + mb + " MB.");
  let r;
  try {
    r = await fetch("/api/upload", {method: "POST", body: file, headers: {"X-Media-Lab": "1",
      "Content-Type": file.type || "application/octet-stream", "X-Filename": encodeURIComponent(file.name)}});
  } catch (e) { throw new Error("Upload failed: can't reach YourContent."); }
  let j = null;
  try { j = await r.json(); } catch (e) { j = null; }
  if (r.status === 401) needLogin();
  if (!r.ok) throw new Error((j && j.error) || "Upload failed.");
  const it = {ref: j.ref, type: j.type, kind: "upload", url: j.url, poster: j.poster || null,
    label: file.name.slice(0, 60), w: (j.probe && j.probe.w) || 0, h: (j.probe && j.probe.h) || 0,
    seconds: j.seconds || (j.probe && j.probe.seconds) || 0, commercial_ok: null};
  remember(it);
  return it;
}

function poll(jid, onTick) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    let misses = 0;
    const step = async () => {
      let j;
      try { j = await api("GET", "/api/jobs/" + jid); misses = 0; } catch (e) {
        if (e.status === 404) { reject(new Error("That job is gone (YourContent may have restarted).")); return; }
        if (++misses > 30) { reject(e); return; }
        setTimeout(step, 2000);
        return;
      }
      if (onTick) onTick(j, (Date.now() - t0) / 1000);
      if (j.state === "done") resolve(j.result);
      else if (j.state === "error") reject(new Error(j.error || "It didn't work."));
      else if (j.state === "cancelled") { const e = new Error("Stopped."); e.cancelled = true; reject(e); }
      else setTimeout(step, 700);
    };
    setTimeout(step, 250);
  });
}

function tickText(j, t) {
  if (j.state === "queued") return "waiting for a free slot";
  if (j.progress != null) return Math.round(j.progress * 100) + " %";
  return Math.round(t) + " s";
}

const DONE = {generate: onPictureDone, animate: onClipDone, extend: onClipDone, music: onMusicDone,
  preview: onPreviewDone, export: onExportDone};

/* Follow a job to its end in its status line. A newer job of the same kind takes the line over. */
async function follow(jid, kind, ctx) {
  const slot = SLOT[kind];
  S.jobs[kind] = jid;
  busy(kind, true);
  try {
    const res = await poll(jid, (j, t) => {
      if (S.jobs[kind] === jid) say(slot, LABEL[kind] + " " + tickText(j, t), "", () => stopJob(jid));
    });
    if (S.jobs[kind] === jid) await DONE[kind](res, ctx || {});
  } catch (e) {
    if (S.jobs[kind] === jid) { if (e.cancelled) say(slot, "Stopped."); else fail(slot, e); }
  } finally {
    if (S.jobs[kind] === jid) { delete S.jobs[kind]; busy(kind, false); }
  }
}
function busy(kind, on) {
  const b = BUTTON[kind];
  if (b && $(b)) $(b).disabled = on;
  if (kind === "music" || kind === "export") renderCut();
}
async function stopJob(jid) { try { await api("POST", "/api/jobs/" + jid + "/cancel", {}); } catch (e) { /* already over */ } }
async function resumeJobs() {
  let list = [];
  try { list = await api("GET", "/api/jobs"); } catch (e) { return; }
  for (const j of list || []) if (DONE[j.kind] && !S.jobs[j.kind]) follow(j.id, j.kind, {resumed: true});
}

async function fetchInfo(ref, fresh) {
  const key = refKey(ref);
  if (!fresh && S.info.has(key)) return S.info.get(key);
  if (!fresh && S.infoP.has(key)) return S.infoP.get(key);
  const p = api("GET", "/api/info/" + ref.root + "/" + encodeURIComponent(ref.name))
    .then(j => { S.info.set(key, j); return j; }, () => null)
    .finally(() => S.infoP.delete(key));
  S.infoP.set(key, p);
  return p;
}
function remember(it) { if (it && it.ref) S.byKey.set(refKey(it.ref), it); }

/* ------------------------------------------------------------------ login */

function needLogin() {
  if (!S.config || !S.config.auth) return;
  $("login").hidden = false;
  // sign in first: the welcome comes back after the page reloads signed in
  if ($("notice").open) { S.welcomeLater = true; $("notice").close(); }
}
async function login(e) {
  e.preventDefault();
  try {
    await api("POST", "/api/login", {token: $("token").value});
    $("token").value = "";
    location.reload();
  } catch (err) { fail("loginstatus", err); }
}

/* ------------------------------------------------------------------ health */

async function loadHealth(recheck) {
  try { S.health = await api("GET", "/api/health" + (recheck ? "?recheck=1" : "")); } catch (e) { S.health = null; }
  renderHealth(S.health);
}
function featureReason(name) {
  const hh = S.health;
  if (!hh || !hh.features) return null;
  const f = hh.features[name];
  if (!f || f.ok !== false) return null;
  if (name !== "export" && !(hh.comfy && hh.comfy.ok)) return "ComfyUI isn't reachable right now.";
  const miss = (f.missing_filters || f.missing || []).filter(Boolean);
  if (name === "export") return "Export needs ffmpeg filters that are missing" + (miss.length ? ": " + miss.join(", ") : "") + ".";
  return "This needs files that are missing" + (miss.length ? ": " + miss.join(", ") : "") + ".";
}
function renderHealth(hh) {
  const ok = !!(hh && hh.comfy && hh.comfy.ok);
  const dot = $("dot");
  dot.classList.toggle("ok", ok);
  const t = ok ? "ComfyUI is ready" : (hh ? "ComfyUI not reachable; editing and export still work" : "Checking ComfyUI…");
  dot.title = t; dot.setAttribute("aria-label", t);
  const vars = (hh && hh.features && hh.features.picture && hh.features.picture.variants) || {};
  const sel = $("model");
  for (const o of sel.options) {
    const v = vars[o.value];
    const bad = ok && v && v.ok === false;
    o.disabled = !!bad;
    o.textContent = ((S.config && S.config.variants[o.value]) || o.value) + (bad ? " (missing file)" : "");
  }
  if (sel.selectedOptions[0] && sel.selectedOptions[0].disabled) {
    const first = [...sel.options].find(o => !o.disabled);
    if (first) sel.value = first.value;
  }
  const lines = [["ComfyUI", ok ? "reachable" + (hh.comfy.version ? " (" + hh.comfy.version + ")" : "") : "not reachable"]];
  for (const [k, label] of [["picture", "Pictures"], ["clip", "Clips"], ["extend", "Extend"], ["music", "Music"], ["export", "Export"]]) {
    const why = featureReason(k);
    lines.push([label, hh && hh.features && hh.features[k] ? (why || "ready") : "unknown", !!why]);
  }
  fill($("feat"), ...lines.map(([a, b, bad]) => h("li", {class: bad ? "bad" : null, text: a + ": " + b})));
  renderCut();
}

/* ------------------------------------------------------------------ tabs and notice */

function setTab(t) {
  if (!["picture", "clip", "cut"].includes(t)) t = "picture";
  S.tab = t;
  if (t !== "picture") S.card = t;
  document.body.className = "tab-" + S.tab + " card-" + S.card;
  for (const b of document.querySelectorAll(".ptabs button, .ctabs button")) {
    const on = b.closest(".ptabs") ? b.dataset.tab === S.tab : b.dataset.tab === S.card;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  }
  lsSet("ml-tab", S.tab); lsSet("ml-card", S.card);
}

/* ------------------------------------------------------------------ recent */

async function loadRecent() {
  try { S.recent = await api("GET", "/api/recent?limit=60"); } catch (e) { return; }
  for (const it of S.recent) remember(it);
  renderRecent();
}
function thumbEl(it, onPick, withRef) {
  const vid = it.type === "video";
  const src = vid ? it.poster : it.url;
  const on = S.current && sameRef(S.current.ref, it.ref);
  const el = h("div", {class: "thumb" + (on ? " on" : ""), draggable: true, title: nameOf(it)},
    src ? h("img", {src, loading: "lazy", alt: nameOf(it), draggable: false, tabindex: "0", role: "button",
      data: {fk: "t:" + refKey(it.ref)}})
      : h("div", {class: "vid", tabindex: "0", role: "button", "aria-label": nameOf(it), data: {fk: "t:" + refKey(it.ref)}}),
    vid && h("span", {class: "play", text: "▶"}),
    it.commercial_ok === false && h("span", {class: "nc", title: "Non-commercial: not for business use", text: "NC"}),
    withRef && !vid && h("button", {type: "button", text: "+ ref", title: "Use as reference",
      data: {fk: "r:" + refKey(it.ref)}, on: {click: e => { e.stopPropagation(); addRef(it); }}}));
  const pick = el.firstChild;
  pick.addEventListener("click", () => onPick(it));
  pick.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(it); } });
  el.addEventListener("dragstart", e => {
    e.dataTransfer.setData("text/x-media-lab", refKey(it.ref));
    e.dataTransfer.effectAllowed = "copy";
  });
  return el;
}
function renderRecent() {
  const list = S.recent.filter(it => it.type === "image" || it.type === "video");
  fill($("strip"), ...(list.length ? list.map(it => thumbEl(it, show, true))
    : [h("div", {class: "none", text: "Nothing yet. Make a picture to start."})]));
}
const ourDrag = e => [...(e.dataTransfer ? e.dataTransfer.types : [])].includes("text/x-media-lab");

/* ------------------------------------------------------------------ stage and tools */

function stageMedia(it, opts) {
  opts = opts || {};
  let el;
  if (it.type === "video") {
    el = h("video", {src: it.url, controls: true, autoplay: !opts.paused, loop: true, playsInline: true,
      preload: "auto", poster: it.poster || null});
  } else el = h("img", {src: it.url, alt: nameOf(it)});
  fill($("stage"), ...[el, opts.tag ? h("span", {class: "tag", text: opts.tag}) : null].filter(Boolean));
  if (el.play && !opts.paused) el.play().catch(() => { /* autoplay blocked; controls are there */ });
  return el;
}
function show(it) {
  S.current = it;
  stageMedia(it);
  renderTools(); renderRecent();
  // phones stack Recent below the fold: bring the result and its tools ('Add to cut') on screen
  const st = $("stage");
  if (st && window.matchMedia && window.matchMedia("(max-width:799px)").matches) {
    const b = st.getBoundingClientRect();
    if (b.top < 0 || b.top > window.innerHeight * 0.5) st.scrollIntoView({behavior: "smooth", block: "start"});
  }
}
function clearStage(text) {
  S.current = null;
  fill($("stage"), h("div", {class: "empty", text: text || "Your picture shows up here."}));
  renderTools(); renderRecent();
}
function renderTools() {
  const it = S.current;
  $("tools").hidden = !(it && it.ref);
  if (!it || !it.ref) return;
  const vid = it.type === "video";
  $("dl").href = it.url + (it.url.includes("?") ? "&" : "?") + "dl=1";
  $("dl").setAttribute("download", it.ref.name);
  $("useref").hidden = vid; $("tovideo").hidden = vid; $("extend").hidden = !vid;
}

async function showInfo(it) {
  const inf = await fetchInfo(it.ref, true);
  if (!inf) { say("tstatus", "No details for this file.", "err"); return; }
  const pr = inf.probe || {};
  const ok = inf.commercial_ok;
  const rows = [
    ["File", inf.name], ["Made", (inf.created || "").replace("T", " ").replace("Z", " UTC")],
    ["Size", pr.w ? pr.w + " × " + pr.h : ""], ["Length", pr.seconds ? fmt2(pr.seconds) + " s" : ""],
    ["Licence", ok === false ? "Non-commercial use only" : ok === true ? "Commercial use OK" : "Unknown: check your rights"],
    ["Prompt", inf.prompt || ""], ["Seed", inf.seed != null ? String(inf.seed) : ""],
  ].filter(r => r[1]);
  const why = inf.restricted_by || [];
  $("infotitle").textContent = nameOf(inf);
  fill($("infobody"), 
    h("dl", {class: "kv"}, ...rows.flatMap(([a, b]) => [h("dt", {text: a}), h("dd", {text: b})])),
    why.length ? h("ul", {class: "why"}, ...why.map(w => h("li", {text: w}))) : null,
    inf.legacy ? h("p", {class: "hint", text: "Made before YourContent tracked files; these details were recovered from the file."}) : null);
  $("infodlg").showModal();
}
function showWhy() {
  const L = S.resolved && S.resolved.licence;
  $("infotitle").textContent = "Licence of this cut";
  fill($("infobody"), 
    h("p", {text: L && L.commercial_ok === false ? "Something in this cut is non-commercial, so the whole cut is."
      : "Some parts have no known licence. Make sure you have the rights."}),
    h("ul", {class: "why"}, ...((L && L.restricted_by) || []).map(w => h("li", {text: w}))));
  $("infodlg").showModal();
}

/* ------------------------------------------------------------------ delete */

async function usedIn(ref) {
  let list = [];
  try { list = await api("GET", "/api/projects"); } catch (e) { return 0; }
  const has = p => (p.items || []).some(i => sameRef(i.src, ref)) || !!(p.music && sameRef(p.music.src, ref));
  let n = 0;
  await Promise.all((list || []).slice(0, 40).map(async row => {
    let p = S.project && S.project.id === row.id ? S.project : null;
    if (!p) { try { p = (await api("GET", "/api/projects/" + row.id)).project; } catch (e) { return; } }
    if (has(p)) n++;
  }));
  return n;
}
async function wipe(it) {
  const inf = await fetchInfo(it.ref, true);
  const n = inf && Array.isArray(inf.used_in) ? inf.used_in.length : await usedIn(it.ref);
  let msg = "Delete " + it.ref.name + " forever?";
  msg += n ? " Used in " + n + (n === 1 ? " cut" : " cuts") + "; they'll show it as missing." : " This can't be undone.";
  if (inf && inf.legacy && !(inf.comfy && inf.comfy.prompt_ids && inf.comfy.prompt_ids.length))
    msg += " ComfyUI may still list it in its history until it restarts.";
  if (!confirm(msg)) return;
  let j;
  try { j = await api("POST", "/api/delete", {ref: it.ref}); } catch (e) { fail("tstatus", e); return; }
  const key = refKey(it.ref);
  S.byKey.delete(key); S.info.delete(key);
  S.refs = S.refs.filter(r => !sameRef(r.ref, it.ref)); renderRefs();
  if (S.vstart && sameRef(S.vstart.ref, it.ref)) setStart(null);
  if (S.current && sameRef(S.current.ref, it.ref)) clearStage("Deleted.");
  say("tstatus", "Deleted " + ((j && j.removed) || [it.ref.name]).join(", ") + ".");
  if (S.project && j && j.used_in && j.used_in.length) await openProject(S.project.id);
  loadRecent(); loadTracks();
}

/* ------------------------------------------------------------------ first-run welcome */

/* A dialog shown once per device (ml-notice-1), leading to making a post. Every way out of it (a button, Escape,
   a click outside) counts as seen; About › Welcome screen opens it again. The tour then only starts when asked. */
function welcome() {
  const dlg = $("notice");
  const go = view => {
    dlg.close();
    if (view && window.MediaLabStudio) window.MediaLabStudio.show(view);
  };
  const hasPosts = !!window.MediaLabPosts;
  $("noticeoktext").textContent = hasPosts ? "Make a post" : "Get started";
  dlg.addEventListener("close", () => { if (S.welcomeLater) return; lsSet("ml-notice-1", "1"); if (!lsGet("ml-tour-1")) lsSet("ml-tour-1", "later"); });
  $("noticeok").addEventListener("click", () => go(hasPosts ? "posts" : null));
  $("noticemake").addEventListener("click", () => { go("make"); setTab("picture"); $("prompt").focus(); });
  $("noticetour").addEventListener("click", () => { go(null); if (window.MediaLabTour) window.MediaLabTour.start(); });
  $("noticetour").hidden = !$("tourbtn");
  $("welcomeagain").addEventListener("click", () => { $("about").close(); if (!dlg.open) dlg.showModal(); });
  $("abouttour").addEventListener("click", () => { $("about").close(); if (window.MediaLabTour) window.MediaLabTour.start(); });
  $("abouttour").hidden = !window.MediaLabTour;
  if (!lsGet("ml-notice-1") && !dlg.open && $("login").hidden) dlg.showModal();
}

/* ------------------------------------------------------------------ chips for the picture's choices */

/* The <select>s stay the source of truth (app code and agents read them); chips are their face. A chip group
   follows its select: options added, renamed or disabled (a model's missing file) redraw it. */
function chipsFor(sel, host) {
  const draw = () => fill(host, ...[...sel.options].map(o => h("button", {type: "button", class: "chip",
    "aria-pressed": String(o.value === sel.value), disabled: o.disabled, text: o.textContent, data: {fk: "c:" + sel.id + ":" + o.value},
    title: o.disabled ? "Not available: see About" : null,
    on: {click: () => { if (sel.value === o.value) return; sel.value = o.value; sel.dispatchEvent(new Event("change")); draw(); }}})));
  new MutationObserver(draw).observe(sel, {childList: true, subtree: true, attributes: true, characterData: true});
  sel.addEventListener("change", draw);
  draw();
  return draw;
}
/* Seed: Random (empty), Keep this one (the last picture's seed), or a number of your own. */
function renderSeed() {
  const inp = $("seed"), v = inp.value.trim();
  const mode = !v ? "random" : S.lastSeed != null && v === String(S.lastSeed) && S.seedMode !== "custom" ? "keep" : "custom";
  const set = (m) => {
    S.seedMode = m;
    if (m === "random") inp.value = "";
    if (m === "keep" && S.lastSeed != null) inp.value = String(S.lastSeed);
    renderSeed();
    if (m === "custom") inp.focus();
  };
  const shown = S.seedMode === "custom" ? "custom" : mode;
  fill($("seedchips"),
    h("button", {type: "button", class: "chip", "aria-pressed": String(shown === "random"), text: "Random", data: {fk: "s:r"},
      on: {click: () => set("random")}}),
    h("button", {type: "button", class: "chip", "aria-pressed": String(shown === "keep"), disabled: S.lastSeed == null,
      text: S.lastSeed == null ? "Keep this one" : "Keep " + S.lastSeed, data: {fk: "s:k"},
      title: S.lastSeed == null ? "Make a picture first; its seed can then be kept" : "Use the last picture's seed again",
      on: {click: () => set("keep")}}),
    h("button", {type: "button", class: "chip", "aria-pressed": String(shown === "custom"), text: "Your own", data: {fk: "s:c"},
      on: {click: () => set("custom")}}));
  inp.hidden = shown !== "custom";
}

/* ------------------------------------------------------------------ picture */

function initModels() {
  const c = S.config, sel = $("model");
  c.variants = c.variants || {};
  sel.replaceChildren(...Object.entries(c.variants).map(([id, label]) => h("option", {value: id, text: label})));
  let want = lsGet("ml-model");
  if (!want) {                       // one-time move of the old page's preference
    const old = lsGet("qs-model");
    if (old) { want = (c.aliases && c.aliases[old]) || old; if (c.variants[want]) lsSet("ml-model", want); }
  }
  sel.value = want && c.variants[want] ? want : c.default_variant;
  if (!sel.value && sel.options.length) sel.selectedIndex = 0;
}
function renderRefs() {
  fill($("refs"), 
    ...S.refs.map((r, i) => h("div", {class: "ref"}, h("img", {src: r.url, alt: "reference " + (i + 1)}),
      h("b", {text: String(i + 1)}),
      h("button", {type: "button", text: "×", title: "Remove from references", "aria-label": "Remove reference " + (i + 1),
        on: {click: () => { S.refs.splice(i, 1); renderRefs(); }}}))),
    S.refs.length < MAXREFS ? h("button", {class: "ghost add", type: "button", text: "+", title: "Upload pictures",
      "aria-label": "Upload reference pictures", on: {click: () => $("file").click()}}) : null,
    S.refs.length ? null : h("span", {class: "tip", text: "No references: plain text-to-image."}));
}
function addRef(it) {
  if (!it || it.type !== "image") { say("status", "References must be pictures.", "err"); return; }
  if (S.refs.length >= MAXREFS) { say("status", "Up to " + MAXREFS + " references.", "err"); return; }
  if (S.refs.some(r => sameRef(r.ref, it.ref))) return;
  S.refs.push({ref: it.ref, url: it.url, w: it.w, h: it.h});
  renderRefs();
  say("status", "Added reference " + S.refs.length + ".");
}
async function uploadRefs(files) {
  for (const f of files) {
    if (!/^image\/(png|jpeg|webp)$/.test(f.type)) { say("status", f.name + ": pictures must be PNG, JPEG or WebP.", "err"); continue; }
    say("status", "Uploading " + f.name + "…");
    try { addRef(await upload(f)); } catch (e) { fail("status", e); }
  }
  loadRecent();
}
function dims(src) {
  return new Promise(res => {
    const im = new Image();
    im.addEventListener("load", () => res([im.naturalWidth, im.naturalHeight]));
    im.addEventListener("error", () => res([1024, 1024]));
    im.src = src;
  });
}
async function generate() {
  const prompt = $("prompt").value.trim();
  if (!prompt) { $("prompt").focus(); say("status", "Write a prompt first.", "err"); return; }
  const why = featureReason("picture");
  if (why) { say("status", why, "err"); return; }
  const size = $("size").value;
  if (size === "match" && !S.refs.length) { say("status", "“Match ref 1” needs a reference.", "err"); return; }
  const body = {prompt, negative: $("negative").value, variant: $("model").value, size,
    steps: clamp(Math.round(+$("steps").value || 30), 4, 60), refs: S.refs.map(r => r.ref)};
  const seed = $("seed").value.trim();
  if (seed) {
    if (!/^\d+$/.test(seed)) { say("status", "The seed must be a whole number (or empty for random).", "err"); return; }
    body.seed = Number(seed);
  }
  if (size === "match") {
    const r = S.refs[0];
    if (!r.w || !r.h) [r.w, r.h] = await dims(r.url);
    body.ref_w = r.w; body.ref_h = r.h;
  }
  $("go").disabled = true;
  try {
    const {job} = await api("POST", "/api/generate", body);
    await follow(job, "generate", {refs: S.refs.length});
  } catch (e) { fail("status", e); $("go").disabled = false; }
}
function onPictureDone(res, ctx) {
  remember(res); show(res);
  if (res.seed != null) { S.lastSeed = res.seed; renderSeed(); }
  say("status", "Done in " + res.seconds + " s · " + res.size + " · seed " + res.seed + (ctx.refs ? " · " + ctx.refs + " ref" : ""));
  loadRecent();
}

/* ------------------------------------------------------------------ make clip */

function noStart() { return h("div", {class: "none"}, "no start", h("br"), "image"); }
function setStart(it) {
  S.vstart = it ? {mode: "animate", ref: it.ref, url: it.url, w: it.w, h: it.h} : null;
  $("vgo").textContent = "Make video"; $("vq").disabled = false;
  fill($("vthumb"), it ? h("img", {src: it.url, alt: "start image"}) : noStart());
  if (it) say("vstatus", "Start image set.");
}
function setExtend(it) {
  S.vstart = {mode: "extend", ref: it.ref, poster: it.poster};
  fill($("vthumb"), it.poster ? h("img", {src: it.poster, alt: "clip"}) : h("div", {class: "none", text: "clip"}));
  $("vgo").textContent = "Extend clip"; $("vq").disabled = true;
  say("vstatus", "Extending " + nameOf(it) + ": it continues from the last frame at the same size. The original is kept.");
}
async function makeClip() {
  const v = S.vstart;
  if (!v) { say("vstatus", "Pick a start image first (Animate this).", "err"); return; }
  const why = featureReason(v.mode === "extend" ? "extend" : "clip");
  if (why) { say("vstatus", why, "err"); return; }
  const prompt = $("vprompt").value, seconds = $("vsecs").value;
  let body;
  if (v.mode === "extend") body = {src: v.ref, prompt, seconds};
  else {
    if (!v.w || !v.h) [v.w, v.h] = await dims(v.url);
    body = {start: v.ref, prompt, seconds, quality: $("vq").value, w: v.w, h: v.h};
  }
  $("vgo").disabled = true;
  try {
    const {job} = await api("POST", "/api/" + (v.mode === "extend" ? "extend" : "animate"), body);
    await follow(job, v.mode === "extend" ? "extend" : "animate", {mode: v.mode});
  } catch (e) { fail("vstatus", e); $("vgo").disabled = false; }
}
function onClipDone(res, ctx) {
  remember(res);
  if (ctx.mode === "extend") setExtend(res);
  show(res);
  const took = res.elapsed != null ? res.elapsed : res.seconds;
  say("vstatus", "Done in " + took + " s" + (res.size ? " · " + res.size : ""));
  loadRecent();
}

/* ------------------------------------------------------------------ cut: project */

function setProject(p, resolved) {
  S.project = p || null; S.rev = p ? p.rev : 0; S.resolved = resolved || null; S.conflict = null;
  S.save.dirty = false; clearTimeout(S.save.timer); S.save.timer = null;
  if (S.sel && !(p && p.items.some(i => i.id === S.sel))) S.sel = null;
  lsSet("ml-project", p ? p.id : null);
  setSaveState(p ? "saved" : "");
  renderCut();
}
async function openProject(id) {
  await flush();
  try {
    const j = await api("GET", "/api/projects/" + id);
    setProject(j.project, j.resolved);
  } catch (e) {
    if (e.status === 404) { if (S.project && S.project.id === id) setProject(null); lsSet("ml-project", null); }
    else fail("xstatus", e);
  }
}
async function ensureProject() {
  if (S.project) return S.project;
  if (!S.creating) {
    S.creating = (async () => {
      try {
        const j = await api("POST", "/api/projects", {name: "Untitled cut"});
        setProject(j.project, j.resolved);
        loadProjects();
        return S.project;
      } finally { S.creating = null; }
    })();
  }
  return S.creating;
}
async function loadProjects() {
  try { S.projects = await api("GET", "/api/projects") || []; } catch (e) { S.projects = []; }
  renderProjectMenu();
}
function renderProjectMenu() {
  const cur = S.project && S.project.id;
  fill($("plist"), ...S.projects.map(p => h("button", {type: "button", class: "ghost" + (p.id === cur ? " on" : ""),
    text: (p.id === cur && S.project ? S.project.name : p.name) + " · " + fmt1(p.seconds || 0) + " s",
    on: {click: () => { menu(false); if (p.id !== cur) openProject(p.id); }}})));
  $("pdel").hidden = !S.project;
}
function menu(open) {
  $("pmenu").hidden = !open;
  $("pmenubtn").setAttribute("aria-expanded", open ? "true" : "false");
  if (open) loadProjects();
}
async function newProject() {
  menu(false);
  await flush();
  try {
    const j = await api("POST", "/api/projects", {name: "Untitled cut"});
    setProject(j.project, j.resolved); loadProjects(); setTab("cut");
  } catch (e) { fail("xstatus", e); }
}
async function deleteProject() {
  menu(false);
  const p = S.project;
  if (!p || !confirm("Delete the cut “" + p.name + "”? Your pictures, clips and exports stay.")) return;
  try { await api("POST", "/api/projects/" + p.id + "/delete", {}); } catch (e) { fail("xstatus", e); return; }
  setProject(null); loadProjects();
  say("xstatus", "Cut deleted.");
}

/* ------------------------------------------------------------------ cut: autosave */

function setSaveState(st, msg) {
  S.saveState = st;
  $("psave").textContent = {saved: "Saved", saving: "Saving…", conflict: "Conflict ·", error: "Not saved ·"}[st] || "";
  $("psave").title = msg || "";
  $("preload").hidden = st !== "conflict";
  $("pretry").hidden = st !== "error";
}
function autosave() {
  if (!S.project) return;
  S.save.dirty = true;
  if (S.saveState !== "conflict") setSaveState("saving");
  clearTimeout(S.save.timer);
  S.save.timer = setTimeout(() => { S.save.timer = null; flush(); }, 800);
}
async function flush() {
  clearTimeout(S.save.timer); S.save.timer = null;
  while (S.save.inflight) await S.save.inflight;
  const p = S.project;
  if (!p || !S.save.dirty || S.conflict) return;
  S.save.dirty = false;
  p.rev = S.rev;
  const run = (async () => {
    try {
      const j = await api("POST", "/api/projects/" + p.id, {rev: S.rev, project: p});
      if (S.project !== p) return;
      S.rev = j.rev; p.rev = j.rev; S.resolved = j.resolved;
      if (j.project && Array.isArray(j.project.items)) {   // keep the snaps the server recorded
        const snaps = new Map(j.project.items.map(x => [x.id, x.snap]));
        for (const x of p.items || []) if (snaps.get(x.id)) x.snap = snaps.get(x.id);
      }
      setSaveState(S.save.dirty ? "saving" : "saved");
      renderCut();
    } catch (e) {
      if (S.project !== p) return;
      if (e.status === 409 && e.body && e.body.current) { S.conflict = e.body.current; setSaveState("conflict"); }
      else { S.save.dirty = true; setSaveState("error", e.message); }
    }
  })();
  S.save.inflight = run;
  try { await run; } finally { S.save.inflight = null; }
}
async function saveNow() {
  await flush();
  if (S.conflict) throw new Error("This cut was changed somewhere else. Press “Reload newer” first.");
  if (S.saveState === "error") throw new Error("The cut isn't saved yet. Press Retry.");
}
function reloadNewer() {
  const c = S.conflict;
  if (c) setProject(c.project, c.resolved);
}

/* ------------------------------------------------------------------ cut: items */

const selItem = () => (S.project && S.sel ? S.project.items.find(i => i.id === S.sel) : null) || null;
function rmap() { return new Map(((S.resolved && S.resolved.items) || []).map(x => [x.id, x])); }

function clipMeta(it) {
  const inf = S.info.get(refKey(it.src));
  const pr = (inf && inf.probe) || {};
  const r = rmap().get(it.id);
  const n = pr.frames || (it.snap && it.snap.frames) || (it.out == null && r && r.src_out) || 49;
  return {n, rate: parseRate(pr.fps || (it.snap && it.snap.rate) || "16/1"), audio: !!pr.has_audio};
}
function posterFor(it) {
  const r = rmap().get(it.id);
  if (r && r.poster) return r.poster;
  const a = S.byKey.get(refKey(it.src));
  if (it.kind === "still") return (a && a.url) || mediaUrl(it.src);
  if (a && a.poster) return a.poster;
  const inf = S.info.get(refKey(it.src));
  if (inf) return inf.poster ? mediaUrl({root: it.src.root, name: inf.poster}) : null;
  fetchInfo(it.src).then(j => { if (j) renderStrip(); });
  return null;
}
async function newItem(it) {
  const base = {id: "i_" + rhex(6), src: {root: it.ref.root, name: it.ref.name}, join: "cut"};
  const ok = it.commercial_ok === undefined ? null : it.commercial_ok;
  if (it.type === "video") {
    const inf = await fetchInfo(it.ref);
    const pr = (inf && inf.probe) || {};
    const snap = {w: pr.w || it.w || 0, h: pr.h || it.h || 0, sar: "1/1", commercial_ok: ok};
    if (pr.frames) snap.frames = pr.frames;
    if (pr.fps) snap.rate = pr.fps;
    return Object.assign(base, {kind: "clip", in: 0, out: null, snap});
  }
  return Object.assign(base, {kind: "still", dur: 2.5, snap: {w: it.w || 0, h: it.h || 0, sar: "1/1", commercial_ok: ok}});
}
function addToCut(it, at) {
  const run = (async () => {
    if (!it || !it.ref || (it.type !== "image" && it.type !== "video")) { say("xstatus", "Only pictures and clips can go in a cut.", "err"); return; }
    let p;
    try { p = await ensureProject(); } catch (e) { fail("xstatus", e); return; }
    if (p.items.length >= MAXITEMS) { say("xstatus", "A cut can hold " + MAXITEMS + " items at most.", "err"); return; }
    const item = await newItem(it);
    if (S.project !== p) return;
    const i = at == null ? p.items.length : clamp(at, 0, p.items.length);
    p.items.splice(i, 0, item);
    setTab("cut");
    renderCut(); autosave();
    say("xstatus", "Added " + nameOf(it) + ".");
  })();
  S.adding = run.finally(() => { if (S.adding === run) S.adding = null; });
  return run;
}
function removeItem(id) {
  if (!S.project) return;
  S.project.items = S.project.items.filter(i => i.id !== id);
  if (S.sel === id) S.sel = null;
  renderCut(); autosave();
}
function moveItem(id, to) {
  const items = S.project.items, from = items.findIndex(i => i.id === id);
  if (from < 0) return;
  if (to > from) to--;
  to = clamp(to, 0, items.length - 1);
  if (to === from) return;
  items.splice(to, 0, items.splice(from, 1)[0]);
  renderCut(); autosave();
}

async function selectItem(id) {
  S.sel = S.sel === id ? null : id;
  renderStrip(); renderEditor();
  const it = selItem();
  if (!it) return;
  if (it.kind === "clip") { await fetchInfo(it.src); if (S.sel !== it.id) return; renderEditor(); }
  playItem(it);
}
function playItem(it) {
  const r = rmap().get(it.id);
  if (r && r.missing) {
    S.current = null; renderTools();
    fill($("stage"), h("div", {class: "empty", text: "This file is missing. The cut shows black for the same length."}));
    return;
  }
  const a = S.byKey.get(refKey(it.src)) || {ref: it.src, type: it.kind === "clip" ? "video" : "image",
    url: mediaUrl(it.src), poster: posterFor(it), label: it.src.name};
  S.current = a; renderTools(); renderRecent();
  const el = stageMedia(a, {tag: "In the cut"});
  if (it.kind !== "clip") return;
  const startAt = () => { const x = selItem(); if (x && x.id === it.id) el.currentTime = ((x.in || 0) + 0.5) / clipMeta(x).rate; };
  el.addEventListener("loadedmetadata", startAt);
  el.addEventListener("timeupdate", () => {
    const x = selItem();
    if (!x || x.id !== it.id || el.paused || el.seeking) return;
    const m = clipMeta(x), end = (x.out == null ? m.n : x.out) / m.rate;
    if (el.currentTime >= end - 0.02) startAt();
  });
}
function scrub(it, k, rate) {
  const v = $("stage").querySelector("video");
  if (!v || !S.current || !sameRef(S.current.ref, it.src)) return;
  v.pause();
  v.currentTime = (k + 0.5) / rate;
}
function readout(it, m) {
  const a = (it.in || 0) / m.rate, b = (it.out == null ? m.n : it.out) / m.rate;
  $("ereadout").textContent = fmt2(a) + " s → " + fmt2(b) + " s (" + fmt2(b - a) + " s)";
}

/* ------------------------------------------------------------------ cut: render */

function renderCut() {
  const p = S.project, R = S.resolved, n = p ? p.items.length : 0;
  $("ctabcut").textContent = "Cut · " + n;
  $("ptabcut").textContent = n ? "Cut · " + n : "Cut";
  $("pname").hidden = !p;
  if (p && document.activeElement !== $("pname")) $("pname").value = p.name || "";
  $("ptotal").textContent = p && R && n ? fmt1(R.seconds) + " s" : "";
  if (!p) setSaveState("");
  renderProjectMenu();
  renderStrip(); renderEditor(); renderMusic(); renderExport();
}
function renderStrip() {
  const items = S.project ? S.project.items : [], rm = rmap(), kids = [];
  items.forEach((it, k) => {
    if (k) kids.push(jointEl(items[k - 1], rm.get(items[k - 1].id)));
    kids.push(cardEl(it, rm.get(it.id)));
  });
  kids.push(h("button", {class: "ccard add", type: "button", text: "+", title: "Add to the cut", "aria-label": "Add to the cut",
    data: {fk: "add"}, on: {click: openPicker}}));
  fill($("cutstrip"), ...kids);
  $("cutempty").hidden = items.length > 0;
}
function cardEl(it, r) {
  const missing = !!(r && r.missing);
  const len = r ? fmt2(r.seconds) + " s" : "…";
  const what = missing ? "Missing" : it.kind === "still" ? "Still" : "Clip";
  const el = h("div", {class: "ccard" + (missing ? " missing" : "") + (S.sel === it.id ? " sel" : ""), role: "button",
    tabindex: "0", "aria-label": what + ", " + len, "aria-pressed": S.sel === it.id ? "true" : "false", data: {id: it.id, fk: "c:" + it.id}});
  if (missing) {
    el.append(h("span", {text: "Missing"}), h("button", {class: "x", type: "button", text: "×", "aria-label": "Remove missing item",
      data: {fk: "x:" + it.id}, on: {click: e => { e.stopPropagation(); removeItem(it.id); }}}));
  } else {
    const src = posterFor(it);
    if (src) el.append(h("img", {src, alt: "", draggable: false}));
    if (it.kind === "still") el.append(h("span", {class: "badge", text: "▣"}));
  }
  el.append(h("span", {class: "len", text: len}));
  return el;
}
function jointEl(item, r) {
  const fade = item.join === "fade", clamped = fade && r && r.xfade_after === 0;
  const tip = clamped ? "Crossfade, but the item is too short so it plays as a cut. Tap for a cut."
    : fade ? "Crossfade. Tap for a cut." : "Cut. Tap for a crossfade.";
  return h("button", {type: "button", class: "joint" + (fade ? " fade" : "") + (clamped ? " clamped" : ""),
    text: fade ? "◇" : "|", title: tip, "aria-label": tip, data: {fk: "j:" + item.id},
    on: {click: () => { item.join = fade ? "cut" : "fade"; renderStrip(); autosave(); }}});
}
function renderEditor() {
  const it = selItem();
  $("editor").hidden = !it;
  if (!it) return;
  const r = rmap().get(it.id), missing = !!(r && r.missing), clip = it.kind === "clip";
  $("emissing").hidden = !missing;
  $("eclip").hidden = !clip || missing;
  $("estill").hidden = clip || missing;
  if (clip && !missing) {
    const m = clipMeta(it), a = $("ein"), b = $("eout");
    a.max = String(Math.max(0, m.n - 2)); b.min = "2"; b.max = String(m.n);
    a.value = String(it.in || 0); b.value = String(it.out == null ? m.n : it.out);
    readout(it, m);
    $("esound").hidden = !m.audio;
  }
  if (!clip && document.activeElement !== $("edur")) $("edur").value = String(it.dur == null ? 2.5 : it.dur);
  const i = S.project.items.indexOf(it);
  $("eleft").disabled = i <= 0;
  $("eright").disabled = i >= S.project.items.length - 1;
}

/* Pointer reorder: immediate with a mouse, a 250 ms press on touch. ◀ ▶ always work too. */
function onDrag() {
  const strip = $("cutstrip");
  let d = null;
  const cards = () => [...strip.querySelectorAll(".ccard[data-id]")];
  const dropIndex = x => {
    const cs = cards();
    for (let i = 0; i < cs.length; i++) { const b = cs[i].getBoundingClientRect(); if (x < b.left + b.width / 2) return i; }
    return cs.length;
  };
  const caret = i => {
    let c = strip.querySelector(".caret");
    if (!c) { c = h("div", {class: "caret"}); strip.append(c); }
    const cs = cards();
    const left = !cs.length ? 4 : i < cs.length ? cs[i].offsetLeft - 3 : cs[cs.length - 1].offsetLeft + cs[cs.length - 1].offsetWidth + 3;
    c.style.left = left + "px";
  };
  const noCaret = () => { const c = strip.querySelector(".caret"); if (c) c.remove(); };
  const begin = () => {
    if (!d) return;
    d.on = true; d.card.classList.add("dragging");
    try { strip.setPointerCapture(d.pid); } catch (e) { /* pointer already gone */ }
    caret(dropIndex(d.x));
  };
  strip.addEventListener("pointerdown", e => {
    const card = e.target.closest(".ccard[data-id]");
    if (!card || e.target.closest("button") || e.button > 0 || !S.project) return;
    d = {id: card.dataset.id, card, x: e.clientX, y: e.clientY, pid: e.pointerId, touch: e.pointerType !== "mouse", on: false, lp: null};
    if (d.touch) d.lp = setTimeout(begin, 250);
  });
  strip.addEventListener("pointermove", e => {
    if (!d || e.pointerId !== d.pid) return;
    const dist = Math.hypot(e.clientX - d.x, e.clientY - d.y);
    if (!d.on) {
      if (d.touch) { if (dist > 10) { clearTimeout(d.lp); d = null; } return; }
      if (dist < 5) return;
      begin();
    }
    e.preventDefault();
    caret(dropIndex(e.clientX));
    const b = strip.getBoundingClientRect();
    if (e.clientX < b.left + 30) strip.scrollLeft -= 12; else if (e.clientX > b.right - 30) strip.scrollLeft += 12;
  });
  const end = e => {
    if (!d || e.pointerId !== d.pid) return;
    clearTimeout(d.lp);
    const was = d; d = null;
    if (!was.on) return;
    was.card.classList.remove("dragging"); noCaret();
    S.dragged = Date.now();
    if (e.type === "pointerup") moveItem(was.id, dropIndex(e.clientX));
  };
  strip.addEventListener("pointerup", end);
  strip.addEventListener("pointercancel", end);
  strip.addEventListener("touchmove", e => { if (d && d.on) e.preventDefault(); }, {passive: false});
  strip.addEventListener("contextmenu", e => { if (e.target.closest(".ccard")) e.preventDefault(); });
  strip.addEventListener("click", e => {
    const card = e.target.closest(".ccard[data-id]");
    if (!card || e.target.closest("button") || Date.now() - S.dragged < 300) return;
    selectItem(card.dataset.id);
  });
  strip.addEventListener("keydown", e => {
    const card = e.target.closest(".ccard[data-id]");
    if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); selectItem(card.dataset.id); }
  });
  /* drops from Recent (HTML5 drag) */
  strip.addEventListener("dragover", e => {
    if (!ourDrag(e)) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "copy";
    strip.classList.add("over"); caret(dropIndex(e.clientX));
  });
  strip.addEventListener("dragleave", e => { if (!strip.contains(e.relatedTarget)) { strip.classList.remove("over"); noCaret(); } });
  strip.addEventListener("drop", e => {
    if (!ourDrag(e)) return;
    e.preventDefault(); strip.classList.remove("over"); noCaret();
    const it = S.byKey.get(e.dataTransfer.getData("text/x-media-lab"));
    if (it) addToCut(it, dropIndex(e.clientX));
  });
}

function openPicker() {
  const list = S.recent.filter(it => it.type === "image" || it.type === "video");
  fill($("pickstrip"), ...(list.length ? list.map(it => thumbEl(it, x => { $("picker").close(); addToCut(x); }, false))
    : [h("div", {class: "none", text: "Nothing in Recent yet. Upload a picture or a clip."})]));
  say("pickstatus", "");
  $("picker").showModal();
}

/* ------------------------------------------------------------------ cut: music */

async function loadTracks() {
  try { S.tracks = await api("GET", "/api/tracks") || []; } catch (e) { return; }
  renderMusic();
}
function trackSeconds(ref) {
  const t = S.tracks.find(x => sameRef(x.ref, ref));
  return t ? t.seconds : (S.resolved && S.resolved.music && S.resolved.music.seconds) || 0;
}
function genFromInfo(inf) {
  if (!inf || inf.kind !== "music") return null;
  const pr = inf.params || {};
  const g = {tags: pr.tags != null ? pr.tags : (inf.prompt || ""), lyrics: pr.lyrics || "", bpm: pr.bpm || 120,
    seconds: pr.seconds || (inf.probe && inf.probe.seconds) || 0, keyscale: pr.keyscale || "C major",
    timesig: String(pr.timesig || "4"), language: pr.language || "en"};
  if (inf.seed != null) g.seed = inf.seed;
  return g;
}
async function setMusic(p, ref, gen) {
  const old = p.music || {};
  const g = gen || genFromInfo(await fetchInfo(ref));
  p.music = {src: {root: ref.root, name: ref.name}, volume: old.volume != null ? old.volume : 1.0,
    fade_in: old.fade_in != null ? old.fade_in : 0.5, fade_out: old.fade_out != null ? old.fade_out : 1.5,
    offset: sameRef(old.src, ref) && old.offset != null ? old.offset : 0};
  if (g) p.music.gen = g;
}
function renderMusic() {
  const p = S.project, m = p && p.music, sel = $("msel");
  if (document.activeElement !== sel) {
    const opts = [h("option", {value: "none", text: "No music"})];
    for (const t of S.tracks) opts.push(h("option", {value: "t:" + refKey(t.ref), text: "♪ " + (t.title || t.ref.name) + " (" + fmt1(t.seconds) + " s)"}));
    if (m && m.src && !S.tracks.some(t => sameRef(t.ref, m.src))) opts.push(h("option", {value: "t:" + refKey(m.src), text: "♪ " + m.src.name + " (missing)"}));
    opts.push(h("option", {value: "upload", text: "Upload…"}), h("option", {value: "make", text: "Make music…"}));
    sel.replaceChildren(...opts);
    sel.value = m && m.src ? "t:" + refKey(m.src) : "none";
  }
  $("madjust").hidden = !(m && m.src);
  if (m && m.src) {
    const set = (id, v) => { if (document.activeElement !== $(id)) $(id).value = String(v); };
    set("mvol", Math.round((m.volume != null ? m.volume : 1) * 100));
    $("mvolout").textContent = Math.round((m.volume != null ? m.volume : 1) * 100) + "%";
    set("moff", m.offset || 0); set("mfin", m.fade_in != null ? m.fade_in : 0.5); set("mfout", m.fade_out != null ? m.fade_out : 1.5);
  }
  const why = featureReason("music");
  $("mgo").disabled = !!why || !!S.jobs.music;
  $("mreason").textContent = why || "";
  const R = S.resolved, cut = p && R && p.items.length ? Number(R.seconds) : 0;
  $("msecs").placeholder = cut ? "matches cut: " + fmt1(cut) + " s" : "matches cut";
  const longer = !!(m && m.src && m.gen && R && R.music && R.samples && R.music.end < R.samples - 24000);
  $("mlonger").hidden = !longer;
  $("mlongerbtn").disabled = !!why || !!S.jobs.music;
  if (longer) $("mlongertext").textContent = "Music is " + fmt1(trackSeconds(m.src)) + " s, cut is " + fmt1(R.seconds) + " s";
}
async function onMusicSelect() {
  const v = $("msel").value;
  if (v === "upload") { $("msel").blur(); renderMusic(); $("mfile").click(); return; }
  if (v === "make") { $("msel").blur(); $("mmake").hidden = false; renderMusic(); $("mtags").focus(); return; }
  let p;
  try { p = await ensureProject(); } catch (e) { fail("mstatus", e); return; }
  if (v === "none") p.music = null;
  else {
    const t = S.tracks.find(x => "t:" + refKey(x.ref) === v);
    if (!t) return;
    await setMusic(p, t.ref);
  }
  renderCut(); autosave();
}
function musicNum(id, lo, hi, apply) {
  $(id).addEventListener("input", () => {
    const m = S.project && S.project.music, x = parseFloat($(id).value);
    if (!m || !isFinite(x)) return;
    apply(m, clamp(x, lo, hi));
    autosave();
  });
  $(id).addEventListener("change", () => renderMusic());
}
async function makeMusic(body) {
  const why = featureReason("music");
  if (why) { say("mstatus", why, "err"); return; }
  if (!String(body.tags || "").trim()) { $("mtags").focus(); say("mstatus", "Describe the style first, e.g. “warm lo-fi, mellow piano”.", "err"); return; }
  let p;
  try { p = await ensureProject(); await saveNow(); } catch (e) { fail("mstatus", e); return; }
  body.project_id = p.id;
  $("mgo").disabled = true;
  try {
    const {job} = await api("POST", "/api/music", body);
    await follow(job, "music", {pid: p.id, body});
  } catch (e) { fail("mstatus", e); $("mgo").disabled = false; }
}
function musicBodyFromForm() {
  const body = {tags: $("mtags").value.trim(), lyrics: $("mlyrics").value, bpm: clamp(Math.round(+$("mbpm").value || 120), 10, 300)};
  const s = parseFloat($("msecs").value);
  if (isFinite(s)) body.seconds = clamp(s, 5, 240);
  return body;
}
async function onMusicDone(res, ctx) {
  await loadTracks();
  const inf = await fetchInfo(res.ref, true);
  const b = ctx.body || {};
  const g = genFromInfo(inf) || {tags: b.tags || "", lyrics: b.lyrics || "", bpm: b.bpm || 120, seconds: res.seconds,
    keyscale: b.keyscale || "C major", timesig: b.timesig || "4", language: b.language || "en"};
  if (g.seed == null && b.seed != null) g.seed = b.seed;
  if (S.project && (!ctx.pid || ctx.pid === S.project.id)) {
    await setMusic(S.project, res.ref, g);
    $("mmake").hidden = true;
    renderCut(); autosave();
    say("mstatus", "Music ready · " + fmt1(res.seconds) + " s.");
  } else say("mstatus", "Music ready. Pick it from the Music list.");
}
function makeLonger() {
  const g = S.project && S.project.music && S.project.music.gen;
  if (!g) return;
  const body = {tags: g.tags, lyrics: g.lyrics || "", bpm: g.bpm || 120, keyscale: g.keyscale, timesig: g.timesig, language: g.language};
  if (g.seed != null) body.seed = g.seed;
  makeMusic(body);
}

/* ------------------------------------------------------------------ cut: export */

function initShapes() {
  const list = (S.config.presets && S.config.presets.length) ? S.config.presets : Object.keys(SHAPES).map(id => ({id}));
  fill($("xshape"), ...list.map(p => h("option", {value: p.id, text: "Shape: " + (p.label || SHAPES[p.id] || p.id)})));
}
// The export row's licence line: every warning that applies (non-commercial AND uploads), never just the first.
function licenceText(L) {
  const icon = L.commercial_ok === false ? "⚠ " : "ⓘ ";
  if (Array.isArray(L.warnings)) return L.warnings.length ? icon + L.warnings.join(" · ") : "";
  const parts = [];
  if (L.commercial_ok === false) parts.push(L.most_restrictive === "qwen-image-nc" ? "Contains Qwen-Image 2.1 output: non-commercial use only." : "Non-commercial use only.");
  if (L.uploads > 0) parts.push("Includes uploads: make sure you have the rights.");
  else if (L.commercial_ok == null) parts.push("Some parts have an unknown licence.");
  return parts.length ? icon + parts.join(" · ") : "";
}
function renderExport() {
  const p = S.project, ex = (p && p.export) || {preset: "9:16", fit: "auto", fps: 24, fade: 0.5};
  const set = (id, v) => { if (document.activeElement !== $(id)) $(id).value = String(v); };
  set("xshape", ex.preset); set("xfit", ex.fit); set("xfps", ex.fps); set("xfade", ex.fade);
  const n = p ? p.items.length : 0;
  $("xpreview").disabled = !n;
  $("xexport").disabled = !n || !!S.jobs.export;
  const R = S.resolved, L = R && R.licence;
  const t = n && L ? licenceText(L) : "";
  $("xlic").hidden = !t;
  $("xlictext").textContent = t;
  $("xlici").hidden = !(L && L.restricted_by && L.restricted_by.length);
  const w = n && R && R.warnings ? R.warnings : [];
  $("xwarn").hidden = !w.length;
  $("xwarn").textContent = w.join(" · ");
}
async function setExport(k, v) {
  let p;
  try { p = await ensureProject(); } catch (e) { fail("xstatus", e); return; }
  p.export = Object.assign({preset: "9:16", fit: "auto", fps: 24, fade: 0.5}, p.export || {}, {[k]: v});
  renderCut(); autosave();
}
async function readyToRender() {
  if (S.adding) await S.adding;
  if (S.creating) await S.creating;
  if (!S.project || !S.project.items.length) throw new Error("Add something to the cut first.");
  await saveNow();
  return S.project;
}
function staleRev(e) {
  if (e.body && e.body.current) { S.conflict = e.body.current; setSaveState("conflict"); }
  say("xstatus", "This cut was changed somewhere else. Press “Reload newer”, then try again.", "err");
}
async function preview() {
  try {
    const p = await readyToRender();
    const {job} = await api("POST", "/api/projects/" + p.id + "/preview", {rev: S.rev});
    await follow(job, "preview", {pid: p.id});
  } catch (e) { if (e.status === 409) staleRev(e); else fail("xstatus", e); }
}
function onPreviewDone(res) {
  S.current = null; renderTools(); renderRecent();
  stageMedia({type: "video", url: res.url, label: "Preview"}, {tag: "Preview"});
  say("xstatus", "Preview · " + fmt1(res.seconds) + " s (low resolution; the export is sharp).");
}
async function exportCut() {
  const why = featureReason("export");
  if (why) { say("xstatus", why, "err"); return; }
  $("xexport").disabled = true;
  try {
    const p = await readyToRender();
    const {job} = await api("POST", "/api/projects/" + p.id + "/export", {rev: S.rev});
    await follow(job, "export", {pid: p.id});
  } catch (e) { if (e.status === 409) staleRev(e); else fail("xstatus", e); }
  finally { if (!S.jobs.export) renderExport(); }
}
function onExportDone(res) {
  remember(res); show(res);
  const notes = [...(res.warnings || [])];
  if (res.checks && res.checks.ok === false) notes.push("the file doesn't fully match the plan; check it before posting");
  say("xstatus", "Exported " + fmt1(res.seconds) + " s. Use Download to save it." + (notes.length ? " Note: " + notes.join(" · ") : ""),
    notes.length ? "warn" : "");
  loadRecent();
}

/* ------------------------------------------------------------------ wiring */

function bind() {
  const on = (id, ev, fn) => $(id).addEventListener(ev, fn);
  // header, notice, dialogs
  const about = () => { renderHealth(S.health); $("about").showModal(); };
  on("aboutbtn", "click", about); on("dot", "click", about);
  on("aboutclose", "click", () => $("about").close());
  on("recheck", "click", () => loadHealth(true));
  on("infoclose", "click", () => $("infodlg").close());
  on("pickclose", "click", () => $("picker").close());
  for (const d of document.querySelectorAll("dialog")) d.addEventListener("click", e => { if (e.target === d) d.close(); });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", welcome); else welcome();
  chipsFor($("size"), $("sizechips")); chipsFor($("model"), $("modelchips")); renderSeed();
  on("login", "submit", login);
  // tabs
  for (const b of document.querySelectorAll(".ptabs button, .ctabs button")) {
    b.addEventListener("click", () => setTab(b.dataset.tab));
    if (b.dataset.tab === "cut") b.addEventListener("dragenter", e => { if (ourDrag(e)) setTab("cut"); });
  }
  // picture
  on("model", "change", () => lsSet("ml-model", $("model").value));
  on("go", "click", generate);
  on("prompt", "keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) generate(); });
  on("file", "change", e => { uploadRefs([...e.target.files]); e.target.value = ""; });
  const zone = $("refs");
  zone.addEventListener("dragover", e => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", e => {
    e.preventDefault(); zone.classList.remove("over");
    const key = e.dataTransfer.getData("text/x-media-lab");
    if (key) { addRef(S.byKey.get(key)); return; }
    uploadRefs([...e.dataTransfer.files]);
  });
  document.addEventListener("paste", e => {
    const fs = [...((e.clipboardData && e.clipboardData.files) || [])].filter(f => f.type.startsWith("image/"));
    if (fs.length) { e.preventDefault(); uploadRefs(fs); }
  });
  // tools
  on("useref", "click", () => { if (S.current) addRef(S.current); });
  on("tovideo", "click", () => { if (S.current) { setStart(S.current); setTab("clip"); $("vprompt").focus(); } });
  on("extend", "click", () => { if (S.current && S.current.type === "video") { setExtend(S.current); setTab("clip"); $("vprompt").focus(); } });
  on("tocut", "click", () => { if (S.current) addToCut(S.current); });
  on("info", "click", () => { if (S.current) showInfo(S.current); });
  on("del", "click", () => { if (S.current) wipe(S.current); });
  on("toolsmore", "click", () => {
    const open = $("tools").classList.toggle("open");
    $("toolsmore").setAttribute("aria-expanded", open ? "true" : "false");
  });
  // make clip
  on("vgo", "click", makeClip);
  on("vprompt", "keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) makeClip(); });
  on("vupload", "click", e => { e.preventDefault(); $("vfile").click(); });
  on("vfile", "change", async e => {
    const f = e.target.files[0]; e.target.value = "";
    if (!f) return;
    say("vstatus", "Uploading…");
    try { setStart(await upload(f)); loadRecent(); } catch (err) { fail("vstatus", err); }
  });
  // cut: project row
  on("pname", "input", () => {
    if (!S.project) return;
    S.project.name = $("pname").value.replace(/[\u0000-\u001f\u007f]/g, "").slice(0, 80);
    autosave();
  });
  on("pmenubtn", "click", e => { e.stopPropagation(); menu($("pmenu").hidden); });
  document.addEventListener("click", e => { if (!$("pmenu").hidden && !e.target.closest("#pmenu, #pmenubtn")) menu(false); });
  on("pnew", "click", newProject);
  on("pdel", "click", deleteProject);
  on("preload", "click", reloadNewer);
  on("pretry", "click", () => { S.save.dirty = true; flush(); });
  // cut: editor
  on("ein", "input", () => {
    const it = selItem(); if (!it) return;
    const m = clipMeta(it), out = it.out == null ? m.n : it.out;
    let v = +$("ein").value;
    if (v > out - 2) { v = out - 2; $("ein").value = String(v); }
    it.in = Math.max(0, v); readout(it, m); scrub(it, it.in, m.rate); autosave();
  });
  on("eout", "input", () => {
    const it = selItem(); if (!it) return;
    const m = clipMeta(it);
    let v = +$("eout").value;
    if (v < (it.in || 0) + 2) { v = (it.in || 0) + 2; $("eout").value = String(v); }
    it.out = v >= m.n ? null : v; readout(it, m); scrub(it, v - 1, m.rate); autosave();
  });
  for (const id of ["ein", "eout"]) on(id, "change", () => {
    const it = selItem(), v = $("stage").querySelector("video");
    if (it && v) { v.currentTime = ((it.in || 0) + 0.5) / clipMeta(it).rate; v.play().catch(() => {}); }
  });
  on("edur", "input", () => {
    const it = selItem(), x = parseFloat($("edur").value);
    if (!it || !isFinite(x)) return;
    it.dur = clamp(x, 0.5, 30); autosave();
  });
  on("edur", "change", () => renderEditor());
  on("eleft", "click", () => { const it = selItem(); if (it) moveItem(it.id, S.project.items.indexOf(it) - 1); });
  on("eright", "click", () => { const it = selItem(); if (it) moveItem(it.id, S.project.items.indexOf(it) + 2); });
  on("eremove", "click", () => { const it = selItem(); if (it) removeItem(it.id); });
  // cut: picker
  on("pickup", "click", () => $("pickfile").click());
  on("pickfile", "change", async e => {
    const f = e.target.files[0]; e.target.value = "";
    if (!f) return;
    say("pickstatus", "Uploading " + f.name + "…");
    try { const it = await upload(f); $("picker").close(); addToCut(it); loadRecent(); } catch (err) { fail("pickstatus", err); }
  });
  // cut: music
  on("msel", "change", onMusicSelect);
  on("mfile", "change", async e => {
    const f = e.target.files[0]; e.target.value = "";
    if (!f) return;
    say("mstatus", "Uploading " + f.name + "…");
    try {
      const it = await upload(f);
      const p = await ensureProject();
      await setMusic(p, it.ref);
      await loadTracks();
      renderCut(); autosave();
      say("mstatus", "Added " + f.name + ".");
    } catch (err) { fail("mstatus", err); }
  });
  on("mvol", "input", () => {
    const m = S.project && S.project.music; if (!m) return;
    m.volume = clamp(+$("mvol").value, 5, 100) / 100;
    $("mvolout").textContent = Math.round(m.volume * 100) + "%";
    autosave();
  });
  musicNum("moff", -600, 600, (m, x) => { m.offset = x; });
  musicNum("mfin", 0, 10, (m, x) => { m.fade_in = x; });
  musicNum("mfout", 0, 10, (m, x) => { m.fade_out = x; });
  on("mgo", "click", () => makeMusic(musicBodyFromForm()));
  on("mclose", "click", () => { $("mmake").hidden = true; });
  on("mlongerbtn", "click", makeLonger);
  // cut: export
  on("xshape", "change", () => setExport("preset", $("xshape").value));
  on("xfit", "change", () => setExport("fit", $("xfit").value));
  on("xfps", "change", () => setExport("fps", +$("xfps").value));
  on("xfade", "change", () => setExport("fade", +$("xfade").value));
  on("xlici", "click", showWhy);
  on("xpreview", "click", preview);
  on("xexport", "click", exportCut);
  // keep a pending edit when the page goes away
  window.addEventListener("pagehide", () => {
    if (!S.project || S.conflict || !(S.save.dirty || S.save.timer)) return;
    try {
      fetch("/api/projects/" + S.project.id, {method: "POST", keepalive: true,
        headers: {"X-Media-Lab": "1", "Content-Type": "application/json"},
        body: JSON.stringify({rev: S.rev, project: S.project})});
    } catch (e) { /* best effort */ }
  });
}

async function boot() {
  bind(); onDrag();
  const tab = lsGet("ml-tab"), card = lsGet("ml-card");
  S.card = card === "cut" ? "cut" : "clip";
  setTab(tab || "picture");
  renderRefs(); renderCut();
  try { S.config = await api("GET", "/api/config"); } catch (e) {
    fill($("stage"), h("div", {class: "empty", text: "Can't reach YourContent. Is it running?"}));
    return;
  }
  initModels(); initShapes();
  $("version").textContent = (S.config.app || "YourContent") + " " + (S.config.version || "");
  const pid = lsGet("ml-project");
  await Promise.all([loadHealth(), loadRecent(), loadTracks(), loadProjects(),
    pid && /^p_[0-9a-f]{12}$/.test(pid) ? openProject(pid) : null]);
  resumeJobs();
  setInterval(() => loadHealth(false), 30000);
}
boot();
