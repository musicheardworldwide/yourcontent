/* Posts: the style gallery. Every style is drawn small in the chosen brand kit with the words typed on the left;
   a card opens large, where the post is rendered and saved. Brand kits are edited here too.
   Vanilla, strict CSP (no inline scripts, styles or HTML strings: DOM nodes only), every POST sends X-Media-Lab: 1.
   Thumbnails: 320 px previews from the render service, drawn only for cards on screen, at most two at a time (the
   service draws one at a time anyway), kept in memory and, where the browser allows it, in its Cache Storage for a
   day, keyed by everything that changes the picture. Typing re-draws the visible cards after a short pause.
   Use: window.MediaLabPosts.mount(document.getElementById("pane-posts")). */
(function () {
  "use strict";

  const FORMAT_LABELS = {"1:1": "1:1 square", "4:5": "4:5 feed", "9:16": "9:16 story", "1.91:1": "1.91:1 link"};
  const AR_CLASS = {"1:1": "ar-11", "4:5": "ar-45", "9:16": "ar-916", "1.91:1": "ar-191"};
  const SAMPLE = {kicker: "New this week", headline: "Your headline goes here",
                  body: "One or two short sentences about the post, in your own words.",
                  detail: "Sat 10:00", label: ""};
  const THUMB_W = 320, ZOOM_W = 1080, IN_FLIGHT = 2, PAUSE = 450, MEM_MAX = 240, STORE_MAX = 600;
  const STORE = "cc-post-thumbs-1", STORE_DAY = 86400000, PREFS = "ml-posts-view";

  const S = {kits: [], styles: [], fonts: [], formats: [], render: {ok: false, missing: []}, recent: [],
             kit: null, image: null, format: "4:5", palette: "brand", kind: "all", show: "all", size: "m",
             safe: true, mark: true, type: "png", zoom: null, slide: 1, busy: false, editing: null};
  const T = {queue: [], live: 0, flight: new Set(), mem: new Map(), seen: new Set(), io: null, store: null, puts: 0, timer: 0};
  let R = null;   /* the mounted root's parts */
  let zoomTok = 0, zoomUrl = null;

  /* ---------------------------------------------------------------- helpers */
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
      else if (k in el && k !== "list") el[k] = v;
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return el;
  }
  /* an icon from the sprite (icons.svg), always placed beside a text label */
  function icon(name) {
    const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg"), use = document.createElementNS(NS, "use");
    svg.setAttribute("class", "ic"); svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", "/static/icons.svg#" + name);
    svg.append(use);
    return svg;
  }
  /* colour dots as SVG: a fill attribute is not a style, so the CSP allows it */
  function swatches(colors) {
    const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "mlp-sw"); svg.setAttribute("viewBox", "0 0 " + (colors.length * 10 + 2) + " 12");
    svg.setAttribute("aria-hidden", "true");
    colors.forEach((c, i) => {
      const dot = document.createElementNS(NS, "circle");
      dot.setAttribute("cx", String(6 + i * 10)); dot.setAttribute("cy", "6"); dot.setAttribute("r", "5");
      dot.setAttribute("fill", /^#[0-9a-fA-F]{6}$/.test(c || "") ? c : "#888888");
      svg.append(dot);
    });
    return svg;
  }
  function field(label, input, hint) {
    const id = input.id || ("mlp-" + Math.random().toString(36).slice(2, 9));
    input.id = id;
    return h("div", {class: "mlp-field"}, h("label", {htmlFor: id, text: label}), input,
             hint ? h("span", {class: "hint", text: hint}) : null);
  }
  function option(value, label, selected) { return h("option", {value, text: label, selected: !!selected}); }
  function say(el, msg, kind) { el.textContent = msg || ""; el.classList.toggle("err", kind === "err"); el.classList.toggle("warn", kind === "warn"); }
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } }
  /* cyrb53: a short stable hash for cache keys (not security) */
  function hash(str) {
    let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
    for (let i = 0; i < str.length; i++) {
      const ch = str.charCodeAt(i);
      h1 = Math.imul(h1 ^ ch, 2654435761); h2 = Math.imul(h2 ^ ch, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return (h2 >>> 0).toString(16).padStart(8, "0") + (h1 >>> 0).toString(16).padStart(8, "0");
  }

  async function api(method, path, body, wantBlob) {
    const opt = {method, headers: {}};
    if (method === "POST") {
      opt.headers["X-Media-Lab"] = "1";
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body === undefined ? {} : body);
    }
    let r;
    try { r = await fetch(path, opt); } catch (e) { throw new Error("Can't reach YourContent. Is it running?"); }
    const isJson = (r.headers.get("Content-Type") || "").startsWith("application/json");
    if (!r.ok) {
      let j = null;
      if (isJson) { try { j = await r.json(); } catch (e) { j = null; } }
      const err = new Error((j && j.error) || ("Something went wrong (" + r.status + ")."));
      err.status = r.status;
      throw err;
    }
    if (wantBlob && !isJson) return r.blob();
    return r.json();
  }

  /* ---------------------------------------------------------------- data */
  async function loadAll() {
    const [st, kits, recent] = await Promise.all([
      api("GET", "/api/styles").catch(e => ({styles: [], fonts: [], formats: [], render: {ok: false, missing: [e.message]}})),
      api("GET", "/api/brandkits").catch(() => ({kits: []})),
      api("GET", "/api/recent?kind=image&limit=24").catch(() => []),
    ]);
    S.styles = st.styles || []; S.fonts = st.fonts || []; S.formats = st.formats || ["1:1", "4:5", "9:16", "1.91:1"];
    S.render = st.render || {ok: false, missing: []};
    S.kits = kits.kits || [];
    S.recent = Array.isArray(recent) ? recent : [];
    if (!S.formats.includes(S.format)) S.format = S.formats.includes("4:5") ? "4:5" : S.formats[0];
    if (!S.kit || !S.kits.some(k => k.id === S.kit.id)) S.kit = S.kits[0] || null;
    else S.kit = S.kits.find(k => k.id === S.kit.id);
    tellKit();
    renderAll();
  }
  function tellKit() { window.dispatchEvent(new CustomEvent("medialab:kit", {detail: S.kit ? S.kit.id : ""})); }
  const styleOf = id => S.styles.find(s => s.id === id);
  const picks = () => new Set((S.kit && S.kit.default_styles) || []);

  /* the words: what was typed, or the sample when nothing has been typed yet (previews only) */
  function words(forSave) {
    const t = {label: R.label.value, kicker: R.kicker.value, headline: R.headline.value, body: R.body.value,
               detail: R.detail.value, handle: R.handle.value};
    if (forSave || Object.values(t).some(v => v.trim())) {
      if (!forSave && !t.headline.trim()) t.headline = SAMPLE.headline;
      return t;
    }
    return Object.assign({}, SAMPLE, {handle: (S.kit && S.kit.handle) || "@yourbrand"});
  }
  function payload(st, preview, width, slide) {
    const p = {kit: S.kit && S.kit.id, style: st.id, format: S.format, palette: S.palette,
               image: st.kind === "image" ? S.image : null, preview, slide: slide || 1, mark: S.mark,
               safe_area: S.safe, type: preview ? "png" : S.type, text: words(!preview)};
    if (preview) p.width = width;
    else p.caption = R.caption.value;
    return p;
  }

  /* ---------------------------------------------------------------- sidebar */
  function renderAll() {
    const off = !S.render.ok;
    R.service.hidden = !off;
    R.service.textContent = off ? "Posts are off: " + ((S.render.missing || [])[0] || "the render service is not reachable") +
      ". Start the render service (compose service \"render\") and reload." : "";
    R.kits.replaceChildren(...(S.kits.length ? S.kits.map(k => h("button", {type: "button", class: "chip",
        "aria-pressed": String(!!S.kit && k.id === S.kit.id), title: k.handle || k.name,
        on: {click: () => { S.kit = k; tellKit(); renderAll(); }}},
      swatches([(k.colors || {}).bg, (k.colors || {}).accent, (k.colors || {}).fg]), k.name))
      : [h("span", {class: "hint", text: "No brand kits yet: make one with New kit."})]));
    R.editKit.disabled = !S.kit;
    R.handle.placeholder = (S.kit && S.kit.handle) || "@account or website";
    renderThumbs();
    renderTools();
    renderGallery();
  }

  function renderThumbs() {
    const none = h("button", {type: "button", class: "mlp-thumb mlp-none", "aria-pressed": String(!S.image),
                              text: "No picture", on: {click: () => { S.image = null; renderThumbs(); redraw(); }}});
    const items = S.recent.map(it => h("button", {type: "button", class: "mlp-thumb", title: it.label || it.ref.name,
        "aria-label": "Use " + (it.label || it.ref.name), "aria-pressed": String(!!S.image && S.image.name === it.ref.name && S.image.root === it.ref.root),
        on: {click: () => { S.image = it.ref; renderThumbs(); redraw(); }}},
      h("img", {src: it.poster || it.url, alt: "", loading: "lazy"})));
    R.thumbs.replaceChildren(none, ...items);
    R.noPics.hidden = S.recent.length > 0;
  }

  /* ---------------------------------------------------------------- toolbar: chips over the gallery */
  function chips(label, opts, get, set) {
    return h("div", {class: "mlp-tool"}, h("span", {class: "lbl", text: label}),
      h("div", {class: "chips", role: "group", "aria-label": label}, opts.map(([v, text]) =>
        h("button", {type: "button", class: "chip", "aria-pressed": String(get() === v), text,
                     on: {click: () => { set(v); savePrefs(); renderTools(); renderGallery(); }}}))));
  }
  function renderTools() {
    const tools = [
      chips("Kind", [["all", "All"], ["text", "Text"], ["image", "Photo + text"]], () => S.kind, v => { S.kind = v; }),
      chips("Format", S.formats.map(f => [f, FORMAT_LABELS[f] || f]), () => S.format, v => { S.format = v; }),
      chips("Colours", [["brand", "Kit"], ["invert", "Inverted"], ["own", "Style's own"]], () => S.palette, v => { S.palette = v; }),
      S.format === "9:16" ? chips("Story safe area", [[true, "On"], [false, "Off"]], () => S.safe, v => { S.safe = v; }) : null,
      chips("Size", [["s", "S"], ["m", "M"], ["l", "L"]], () => S.size, v => { S.size = v; }),
      chips("Show", [["all", "All"], ["picks", "Kit picks"]], () => S.show, v => { S.show = v; }),
    ];
    R.tools.replaceChildren(...tools.filter(Boolean), R.count);
  }
  function savePrefs() {
    lsSet(PREFS, JSON.stringify({format: S.format, palette: S.palette, kind: S.kind, show: S.show, size: S.size, safe: S.safe}));
  }
  function loadPrefs() {
    try {
      const p = JSON.parse(lsGet(PREFS) || "{}");
      if (typeof p.format === "string") S.format = p.format;
      if (["brand", "invert", "own"].includes(p.palette)) S.palette = p.palette;
      if (["all", "text", "image"].includes(p.kind)) S.kind = p.kind;
      if (["all", "picks"].includes(p.show)) S.show = p.show;
      if (["s", "m", "l"].includes(p.size)) S.size = p.size;
      if (typeof p.safe === "boolean") S.safe = p.safe;
    } catch (e) { /* ignore a bad saved value */ }
  }

  /* ---------------------------------------------------------------- the gallery */
  function cardFor(st) {
    let P = R.parts.get(st.id);
    if (!P) {
      P = {};
      P.img = h("img", {class: "mlp-shot", alt: "", hidden: true, draggable: false});
      P.wait = h("span", {class: "mlp-wait", text: "Waiting…"});
      P.frame = h("button", {type: "button", class: "mlp-frame", "aria-label": "Open " + st.name + " large",
                             on: {click: () => openZoom(st.id)}}, P.img, P.wait);
      P.star = h("button", {type: "button", class: "mlp-star", text: "★", title: "Kit pick",
                            "aria-label": "Kit pick: " + st.name, on: {click: () => togglePick(st.id, P.star)}});
      P.card = h("article", {class: "mlp-card", "data-style": st.id}, P.frame,
        h("div", {class: "mlp-meta"}, h("span", {class: "mlp-name", text: st.name}),
          h("span", {class: "mlp-tags", text: st.tags + (st.slides > 1 ? " · " + st.slides + " slides" : "")}), P.star));
      R.parts.set(st.id, P);
      if (T.io) T.io.observe(P.card);
    }
    P.frame.className = "mlp-frame " + (AR_CLASS[S.format] || "ar-45");
    P.frame.disabled = !S.render.ok || !S.kit;
    P.star.setAttribute("aria-pressed", String(picks().has(st.id)));
    P.star.disabled = !S.kit;
    return P.card;
  }
  function renderGallery() {
    const pk = picks();
    const list = S.styles.filter(s => (S.kind === "all" || s.kind === S.kind) && (S.show === "all" || pk.has(s.id)));
    R.gallery.className = "mlp-gallery size-" + S.size;
    R.count.textContent = S.styles.length ? list.length + " of " + S.styles.length + " styles" : "";
    const empty = !S.styles.length ? "No styles: the render service is off." :
      !S.kit ? "Make a brand kit first (New kit, on the left): every style is drawn in its colours and fonts." :
      !list.length ? (S.show === "picks" ? "No kit picks yet. Tap ★ on a style to pick it for this kit." : "No styles of this kind.") : "";
    R.gallery.replaceChildren(...list.map(cardFor), ...(empty ? [h("p", {class: "hint mlp-empty", text: empty})] : []));
    redraw(true);
  }

  /* ---------------------------------------------------------------- thumbnails: queue, memory and browser cache */
  function redraw(now) {
    clearTimeout(T.timer);
    if (now) { queueVisible(); return; }
    T.timer = setTimeout(queueVisible, PAUSE);
  }
  function queueVisible() {
    T.queue = [];
    if (!S.render.ok || !S.kit) return;
    const cards = [...R.gallery.querySelectorAll(".mlp-card")].filter(c => !T.io || T.seen.has(c.dataset.style));
    for (const c of cards) want(c.dataset.style);
    pump();
  }
  function want(id) {
    const st = styleOf(id), P = R.parts.get(id);
    if (!st || !P || !S.kit) return;
    const p = payload(st, true, THUMB_W, 1);
    const k = hash(JSON.stringify([S.kit, S.styles, p]));
    if (P.key === k && (P.shown === k || T.flight.has(k) || T.queue.some(j => j.k === k))) return;
    P.key = k;
    const url = T.mem.get(k);
    if (url) { show(P, k, url); return; }
    P.frame.classList.add("busy");
    if (!P.shown) say(P.wait, "Waiting…");
    T.queue.push({id, k, p});
  }
  function pump() {
    while (T.live < IN_FLIGHT && T.queue.length) {
      const job = T.queue.shift(), P = R.parts.get(job.id);
      if (!P || P.key !== job.k) continue;          /* the words changed since it was queued */
      T.live++; T.flight.add(job.k);
      if (!P.shown) say(P.wait, "Drawing…");
      draw(job).finally(() => { T.live--; T.flight.delete(job.k); pump(); });
    }
  }
  async function draw(job) {
    const P = R.parts.get(job.id);
    try {
      let url = await fromStore(job.k);
      if (!url) {
        const blob = await api("POST", "/api/posts/render", job.p, true);
        url = URL.createObjectURL(blob);
        toStore(job.k, blob);
      }
      remember(job.k, url);
      if (P.key === job.k) show(P, job.k, url);
    } catch (e) {
      if (P.key === job.k) { P.frame.classList.remove("busy"); if (!P.shown) say(P.wait, "Couldn't draw it: " + e.message, "err"); }
    }
  }
  function show(P, k, url) {
    P.img.src = url; P.img.hidden = false; P.shown = k;
    P.wait.hidden = true; P.frame.classList.remove("busy");
  }
  function remember(k, url) {
    T.mem.set(k, url);
    if (T.mem.size <= MEM_MAX) return;
    const inUse = new Set([...R.parts.values()].map(P => P.shown));
    for (const [old, u] of T.mem) {
      if (T.mem.size <= MEM_MAX) break;
      if (inUse.has(old)) continue;
      T.mem.delete(old); URL.revokeObjectURL(u);
    }
  }
  /* Cache Storage only exists on https or localhost; elsewhere the memory copy is all there is */
  async function store() {
    if (T.store === null) {
      try { T.store = window.caches ? await window.caches.open(STORE) : false; } catch (e) { T.store = false; }
    }
    return T.store;
  }
  const storeKey = k => location.origin + "/thumbs-cache/" + k;   /* a key in this browser only, never requested */
  async function fromStore(k) {
    const c = await store();
    if (!c) return null;
    try {
      const r = await c.match(storeKey(k));
      if (!r) return null;
      if (Date.now() - Number(r.headers.get("X-Saved") || 0) > STORE_DAY) { c.delete(storeKey(k)); return null; }
      return URL.createObjectURL(await r.blob());
    } catch (e) { return null; }
  }
  async function toStore(k, blob) {
    const c = await store();
    if (!c) return;
    try {
      await c.put(storeKey(k), new Response(blob, {headers: {"Content-Type": blob.type, "X-Saved": String(Date.now())}}));
      if (++T.puts % 50 === 0) {
        const keys = await c.keys();
        for (const old of keys.slice(0, Math.max(0, keys.length - STORE_MAX))) await c.delete(old);
      }
    } catch (e) { /* full or blocked: the memory copy still works */ }
  }

  /* ---------------------------------------------------------------- kit picks (★) */
  async function togglePick(id, btn) {
    const kit = S.kit;
    if (!kit) return;
    const cur = kit.default_styles || [];
    const next = cur.includes(id) ? cur.filter(x => x !== id) : [...cur, id];
    btn.disabled = true;
    try {
      S.kit = await api("POST", "/api/brandkits/" + encodeURIComponent(kit.id), Object.assign(bodyOf(kit), {default_styles: next}));
      S.kits = S.kits.map(k => k.id === S.kit.id ? S.kit : k);
      renderGallery();
    } catch (e) { say(R.status, e.message, "err"); }
    btn.disabled = false;
  }
  function bodyOf(k) {
    return {name: k.name, handle: k.handle || "", voice: k.voice || "", colors: k.colors || {}, fonts: k.fonts || {},
            logo: k.logo || null, default_styles: k.default_styles || []};
  }

  /* ---------------------------------------------------------------- one style, large */
  function openZoom(id) {
    const st = styleOf(id);
    if (!st || !S.kit) return;
    S.zoom = id; S.slide = 1;
    R.ztitle.textContent = st.name;
    R.ztags.textContent = st.tags + " · " + (FORMAT_LABELS[S.format] || S.format) + " · " + S.kit.name;
    R.zslides.hidden = st.slides < 2;
    R.zresult.replaceChildren();
    say(R.zstatus, "");
    const P = R.parts.get(id);
    R.zimg.src = P && P.shown ? P.img.src : "";
    R.zimg.hidden = !(P && P.shown);
    R.zframe.className = "mlp-zframe " + (AR_CLASS[S.format] || "ar-45");
    renderSlide(st);
    R.zoom.showModal();
    R.zsave.focus();
    drawZoom();
  }
  function renderSlide(st) { R.zslide.textContent = "Slide " + S.slide + " of " + st.slides; R.zprev.disabled = S.slide <= 1; R.znext.disabled = S.slide >= st.slides; }
  async function drawZoom() {
    const st = styleOf(S.zoom), tok = ++zoomTok;
    R.zframe.classList.add("busy");
    say(R.zstatus, "Drawing it large…");
    try {
      const blob = await api("POST", "/api/posts/render", payload(st, true, ZOOM_W, S.slide), true);
      if (tok !== zoomTok) return;
      if (zoomUrl) URL.revokeObjectURL(zoomUrl);
      zoomUrl = URL.createObjectURL(blob);
      R.zimg.src = zoomUrl; R.zimg.hidden = false;
      say(R.zstatus, R.headline.value.trim() ? "Preview, not saved yet." : "Preview with sample words. Type yours under Words.");
    } catch (e) {
      if (tok === zoomTok) say(R.zstatus, e.message, "err");
    } finally {
      if (tok === zoomTok) R.zframe.classList.remove("busy");
    }
  }
  async function save() {
    const st = styleOf(S.zoom);
    if (!R.headline.value.trim()) {
      say(R.zstatus, "Write a headline first, under Words. The preview uses sample words.", "err");
      return;
    }
    S.busy = true; R.zsave.disabled = true;
    say(R.zstatus, "Rendering and saving…");
    try {
      const out = await api("POST", "/api/posts/render", payload(st, false, 0, S.slide));
      R.zimg.src = out.item.url; R.zimg.hidden = false;
      const lic = out.commercial_ok === true ? "Commercial use OK." :
        out.commercial_ok === false ? "Not for commercial use." : "Licence unknown: check before commercial use.";
      const cal = window.MediaLabCalendar && window.MediaLabStudio;
      R.zresult.replaceChildren(
        h("p", {}, "Saved as ", h("a", {href: out.item.url + "?dl=1", text: out.item.ref.name, download: out.item.ref.name}),
          " (" + out.info.w + "×" + out.info.h + ")."),
        h("p", {class: out.commercial_ok === true ? "hint" : "status warn", text: lic}),
        ...(out.restricted_by || []).map(t => h("p", {class: "hint", text: t})),
        ...(out.warnings || []).filter(w => !/commercial/i.test(w) || out.commercial_ok !== true).map(t => h("p", {class: "hint", text: t})),
        h("div", {class: "mlp-row"},
          h("a", {class: "btn ghost", href: out.item.url + "?dl=1", download: out.item.ref.name}, icon("download"), "Download"),
          cal ? h("button", {type: "button", class: "ghost", on: {click: () => toCalendar(out)}}, icon("calendar"), "Add to calendar") : null));
      say(R.zstatus, "Saved. It's in your recent files too.");
      api("GET", "/api/recent?kind=image&limit=24").then(r => { S.recent = r; renderThumbs(); }).catch(() => {});
    } catch (e) {
      say(R.zstatus, e.message, "err");
    } finally {
      S.busy = false; R.zsave.disabled = false;
    }
  }
  function toCalendar(out) {
    const host = document.querySelector("#view-calendar .viewbody");
    if (!host || !window.MediaLabStudio || !window.MediaLabCalendar) return;
    R.zoom.close();
    window.MediaLabStudio.show("calendar");
    const cal = window.MediaLabCalendar.mount(host);
    if (cal && cal.add) cal.add({asset: out.item.ref, title: R.headline.value.trim(), caption: R.caption.value});
  }

  /* ---------------------------------------------------------------- brand kit editor (a dialog) */
  const COLORS = [["bg", "Background", true], ["fg", "Text", true], ["accent", "Accent", true],
                  ["accent2", "Second accent", false], ["muted", "Muted text", false], ["paper", "Paper (light panels)", false],
                  ["ink", "Ink (text on paper)", false]];
  const FONTS = [["display", "Display (headlines)", true], ["body", "Body", true], ["serif", "Serif", false],
                 ["mono", "Mono", false], ["hand", "Handwritten", false]];

  function openEditor(kit) {
    S.editing = kit ? kit.id : null;
    const k = kit || {name: "", handle: "", voice: "", colors: {bg: "#111111", fg: "#f5f5f5", accent: "#e8b14a"},
                      fonts: {display: "Space Grotesk", body: "Inter"}, logo: null, default_styles: []};
    const E = {picked: [...(k.default_styles || [])]};
    E.name = h("input", {value: k.name, maxLength: 60, required: true, autocomplete: "off"});
    E.handle = h("input", {value: k.handle || "", maxLength: 80, placeholder: "@account or example.com"});
    E.voice = h("textarea", {value: k.voice || "", maxLength: 500, rows: 3, placeholder: "How posts should sound (for whoever writes them)"});
    const colorRows = COLORS.map(([key, label, req]) => {
      const v = (k.colors || {})[key] || "";
      E["c_" + key] = h("input", {type: "color", id: "mlp-c-" + key, value: v || (req ? "#000000" : "#808080")});
      if (req) return field(label, E["c_" + key]);
      const auto = h("input", {type: "checkbox", checked: !v,
                               on: {change: () => { E["c_" + key].disabled = auto.checked; }}});
      E["a_" + key] = auto;
      E["c_" + key].disabled = !v;
      return h("div", {class: "mlp-field"}, h("label", {htmlFor: "mlp-c-" + key, text: label}),
        h("div", {class: "mlp-colorrow"}, E["c_" + key], h("label", {class: "mlp-check"}, auto, " Automatic")));
    });
    const famOpts = (cur, auto) => [...(auto ? [option("", "(automatic)", !cur)] : []),
      ...[...new Set([...S.fonts, ...(cur ? [cur] : [])])].sort().map(f => option(f, f, f === cur))];
    const fontRows = FONTS.map(([key, label, req]) => {
      E["f_" + key] = h("select", {}, famOpts((k.fonts || {})[key] || "", !req));
      return field(label, E["f_" + key]);
    });
    E.logo = h("select", {}, option("", "(none: the kit's name is the wordmark)", !k.logo),
      ...S.recent.map(it => option(it.ref.root + "/" + it.ref.name, it.label || it.ref.name,
        k.logo && k.logo.root === it.ref.root && k.logo.name === it.ref.name)));
    if (k.logo && !S.recent.some(it => it.ref.root === k.logo.root && it.ref.name === k.logo.name))
      E.logo.append(option(k.logo.root + "/" + k.logo.name, k.logo.name, true));
    const pickChips = h("div", {class: "chips", role: "group", "aria-label": "Kit picks"});
    const drawPicks = () => pickChips.replaceChildren(...S.styles.map(s => h("button", {type: "button", class: "chip",
      "aria-pressed": String(E.picked.includes(s.id)), text: s.name,
      on: {click: () => { E.picked = E.picked.includes(s.id) ? E.picked.filter(x => x !== s.id) : [...E.picked, s.id]; drawPicks(); }}})));
    drawPicks();
    E.status = h("p", {class: "status", role: "status", "aria-live": "polite"});
    const save = h("button", {type: "submit", text: kit ? "Save kit" : "Create kit"});
    const del = kit ? h("button", {type: "button", class: "ghost danger", text: "Delete kit", on: {click: () => deleteKit(kit, E)}}) : null;
    const cancel = h("button", {type: "button", class: "ghost", text: "Cancel", on: {click: closeEditor}});
    const form = h("form", {class: "mlp-kitform", on: {submit: ev => { ev.preventDefault(); saveKit(E); }}},
      h("h2", {text: kit ? "Edit brand kit" : "New brand kit"}),
      field("Name", E.name), field("Handle", E.handle),
      h("fieldset", {}, h("legend", {text: "Colours"}), h("div", {class: "mlp-cols"}, colorRows)),
      h("fieldset", {}, h("legend", {text: "Fonts"}), h("div", {class: "mlp-cols"}, fontRows)),
      field("Logo (optional)", E.logo, "One of your pictures; a transparent PNG works best."),
      field("Voice", E.voice),
      h("div", {class: "mlp-field"}, h("span", {class: "label", text: "Kit picks"}), pickChips,
        h("span", {class: "hint", text: "Styles that suit this kit. Show › Kit picks lists only these."})),
      h("div", {class: "mlp-row"}, save, cancel, h("span", {class: "sp"}), del), E.status);
    R.editor.replaceChildren(form);
    R.editor.showModal();
    E.name.focus();
  }
  function closeEditor() { if (R.editor.open) R.editor.close(); R.editor.replaceChildren(); S.editing = null; R.editKit.focus(); }

  function kitFrom(E) {
    const colors = {}, fonts = {};
    for (const [key, , req] of COLORS) if (req || !E["a_" + key].checked) colors[key] = E["c_" + key].value;
    for (const [key] of FONTS) { const v = E["f_" + key].value; if (v) fonts[key] = v; }
    const lg = E.logo.value;
    return {name: E.name.value, handle: E.handle.value, voice: E.voice.value, colors, fonts,
            logo: lg ? {root: lg.split("/")[0], name: lg.slice(lg.indexOf("/") + 1)} : null,
            default_styles: E.picked};
  }
  async function saveKit(E) {
    say(E.status, "Saving…");
    try {
      const body = kitFrom(E);
      const kit = S.editing ? await api("POST", "/api/brandkits/" + encodeURIComponent(S.editing), body)
                            : await api("POST", "/api/brandkits", body);
      S.kit = kit;
      closeEditor();
      await loadAll();
      say(R.status, "Brand kit \"" + kit.name + "\" saved.");
    } catch (e) { say(E.status, e.message, "err"); }
  }
  async function deleteKit(kit, E) {
    if (!window.confirm("Delete the brand kit \"" + kit.name + "\"? Posts already made keep their look.")) return;
    try {
      await api("POST", "/api/brandkits/" + encodeURIComponent(kit.id) + "/delete", {});
      S.kit = null; closeEditor(); await loadAll();
      say(R.status, "Brand kit deleted.");
    } catch (e) { say(E.status, e.message, "err"); }
  }

  /* ---------------------------------------------------------------- drawer (narrow screens) */
  function drawer(open) {
    R.root.classList.toggle("mlp-open", open);
    R.drawerBtn.setAttribute("aria-expanded", String(open));
    if (open) R.closeSide.focus(); else R.drawerBtn.focus({preventScroll: true});
  }

  /* ---------------------------------------------------------------- mount */
  function mount(el) {
    if (!el || el.dataset.mlpMounted) return;
    el.dataset.mlpMounted = "1";
    loadPrefs();
    R = {parts: new Map()};
    R.service = h("p", {class: "status warn mlp-service", role: "status", hidden: true});
    R.kits = h("div", {class: "chips mlp-kits", role: "group", "aria-label": "Brand kit"});
    R.editKit = h("button", {type: "button", class: "ghost tiny", text: "Edit kit", on: {click: () => S.kit && openEditor(S.kit)}});
    R.newKit = h("button", {type: "button", class: "ghost tiny", on: {click: () => openEditor(null)}}, icon("plus"), "New kit");
    const typing = {input: () => redraw()};
    R.headline = h("textarea", {maxLength: 300, rows: 2, placeholder: SAMPLE.headline, on: typing});
    R.kicker = h("input", {maxLength: 120, placeholder: SAMPLE.kicker, on: typing});
    R.body = h("textarea", {maxLength: 600, rows: 3, placeholder: SAMPLE.body, on: typing});
    R.detail = h("input", {maxLength: 80, placeholder: "date, price, number…", on: typing});
    R.handle = h("input", {maxLength: 80, on: typing});
    R.label = h("input", {maxLength: 60, placeholder: "e.g. Sponsored", on: typing});
    R.caption = h("textarea", {maxLength: 2200, rows: 3, placeholder: "The text that goes with the post"});
    R.thumbs = h("div", {class: "mlp-thumbs", role: "group", "aria-label": "Picture"});
    R.noPics = h("p", {class: "hint", text: "No pictures yet: make or upload one in Make, or use a text style.", hidden: true});
    R.markChip = h("button", {type: "button", class: "chip", "aria-pressed": String(S.mark), text: "Logo or wordmark",
      on: {click: () => { S.mark = !S.mark; R.markChip.setAttribute("aria-pressed", String(S.mark)); redraw(true); }}});
    R.typeChips = h("div", {class: "chips", role: "group", "aria-label": "File"});
    const drawType = () => R.typeChips.replaceChildren(...[["png", "PNG"], ["jpg", "JPEG"]].map(([v, t]) =>
      h("button", {type: "button", class: "chip", "aria-pressed": String(S.type === v), text: t, on: {click: () => { S.type = v; drawType(); }}})));
    drawType();
    R.status = h("p", {class: "status", role: "status", "aria-live": "polite"});
    R.closeSide = h("button", {type: "button", class: "ghost tiny mlp-close", text: "Close", "aria-label": "Close the controls",
                               on: {click: () => drawer(false)}});
    R.side = h("aside", {class: "mlp-side", id: "mlp-side", "aria-label": "Post controls"},
      h("div", {class: "mlp-sidehead"}, h("h2", {class: "mlp-title", text: "Posts"}), R.closeSide),
      h("p", {class: "hint mlp-sub", text: "Pick a kit, type your words, then open any style to render and save it."}),
      R.service,
      h("section", {class: "mlp-sec"}, h("span", {class: "lbl", text: "Brand kit"}), R.kits,
        h("div", {class: "mlp-row mlp-kitbtns"}, R.editKit, R.newKit)),
      h("section", {class: "mlp-sec"}, h("span", {class: "lbl", text: "Words"}),
        field("Headline", R.headline), field("Small line above the headline", R.kicker), field("Body", R.body),
        field("Extra detail (date, price…)", R.detail), field("@account or website", R.handle),
        field("Tag above the small line (optional)", R.label)),
      h("section", {class: "mlp-sec"}, h("span", {class: "lbl", text: "Picture (photo styles)"}), R.thumbs, R.noPics),
      h("section", {class: "mlp-sec"}, h("span", {class: "lbl", text: "On the post"}), h("div", {class: "chips"}, R.markChip)),
      h("section", {class: "mlp-sec"}, h("span", {class: "lbl", text: "When saving"}),
        field("Caption", R.caption, "Not drawn on the picture; saved with the post."),
        h("span", {class: "label", text: "File"}), R.typeChips),
      R.status);
    R.drawerBtn = h("button", {type: "button", class: "ghost mlp-drawerbtn", text: "Kit and words",
                               "aria-controls": "mlp-side", "aria-expanded": "false", on: {click: () => drawer(true)}});
    R.count = h("span", {class: "mlp-count"});
    R.tools = h("div", {class: "mlp-tools"});
    R.gallery = h("div", {class: "mlp-gallery"});
    R.canvas = h("div", {class: "mlp-canvas"}, R.drawerBtn, R.tools, R.gallery);
    R.scrim = h("div", {class: "mlp-scrim", on: {click: () => drawer(false)}});
    R.root = h("div", {class: "mlp", on: {keydown: e => { if (e.key === "Escape" && R.root.classList.contains("mlp-open")) drawer(false); }}},
      R.side, R.scrim, R.canvas);

    /* the zoom dialog */
    R.ztitle = h("h2", {class: "mlp-ztitle"});
    R.ztags = h("p", {class: "hint mlp-ztags"});
    R.zimg = h("img", {class: "mlp-zimg", alt: "The post, large", hidden: true});
    R.zframe = h("div", {class: "mlp-zframe"}, R.zimg);
    R.zprev = h("button", {type: "button", class: "ghost tiny", text: "‹", "aria-label": "Previous slide",
                           on: {click: () => { S.slide--; renderSlide(styleOf(S.zoom)); drawZoom(); }}});
    R.znext = h("button", {type: "button", class: "ghost tiny", text: "›", "aria-label": "Next slide",
                           on: {click: () => { S.slide++; renderSlide(styleOf(S.zoom)); drawZoom(); }}});
    R.zslide = h("span", {class: "hint"});
    R.zslides = h("div", {class: "mlp-row"}, R.zprev, R.zslide, R.znext);
    R.zsave = h("button", {type: "button", on: {click: save}}, icon("save"), "Render and save");
    R.zstatus = h("p", {class: "status", role: "status", "aria-live": "polite"});
    R.zresult = h("div", {class: "mlp-result", "aria-live": "polite"});
    const zclose = h("button", {type: "button", class: "ghost", text: "Close", on: {click: () => R.zoom.close()}});
    R.zoom = h("dialog", {class: "mlp-zoom", "aria-label": "Post, large"},
      h("div", {class: "mlp-zbody"}, R.zframe,
        h("div", {class: "mlp-zside"}, h("div", {}, R.ztitle, R.ztags), R.zslides,
          h("div", {class: "mlp-row"}, R.zsave, zclose), R.zstatus, R.zresult)));
    R.zoom.addEventListener("click", e => { if (e.target === R.zoom) R.zoom.close(); });
    R.zoom.addEventListener("close", () => { zoomTok++; S.zoom = null; });
    R.editor = h("dialog", {class: "mlp-editor", "aria-label": "Brand kit editor"});
    R.editor.addEventListener("close", () => { R.editor.replaceChildren(); S.editing = null; });

    if ("IntersectionObserver" in window) {
      T.io = new IntersectionObserver(entries => {
        let more = false;
        for (const e of entries) {
          const id = e.target.dataset.style;
          if (e.isIntersecting) { if (!T.seen.has(id)) { T.seen.add(id); want(id); more = true; } }
          else T.seen.delete(id);
        }
        if (more) pump();
      }, {rootMargin: "300px 0px"});
    }
    el.replaceChildren(R.root, R.zoom, R.editor);
    /* the page may hang more panels under the gallery (the recipes) */
    if (el.parentElement) for (const n of el.parentElement.querySelectorAll(":scope > [data-mlp-canvas]")) R.canvas.append(n);
    renderTools();
    loadAll().catch(e => say(R.status, e.message, "err"));
  }

  window.MediaLabPosts = {mount};
})();
