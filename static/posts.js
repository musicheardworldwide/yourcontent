/* Posts: a composer. The editor on the left (brand kit chips, a gallery of style cards, the words, the picture,
   the look and the caption); a live preview on the right that stays in view while you edit (sticky), and that
   follows the fields on a narrow screen, where Preview scrolls to it. Brand kits are edited in a dialog.
   Vanilla, strict CSP (no inline scripts, styles or HTML strings: DOM nodes only; colours drawn as SVG fill
   attributes), every POST sends X-Media-Lab: 1.
   Style cards: <img loading="lazy"> from GET /api/posts/thumb (320 px, drawn in the kit with sample words and
   cached on disk by the server; v= changes with the kit and the style's version so the browser can keep it a
   day). Under every picture sits a swatch drawn from the style's or the kit's colours and font, which is all
   that shows when the render service is off.
   Use: window.MediaLabPosts.mount(el); panels beside it marked [data-mlp-canvas] are moved under the composer. */
(function () {
  "use strict";

  const CHANNELS = {"4:5": "Instagram feed", "1:1": "Square", "9:16": "Story / Reel", "1.91:1": "Link / LinkedIn"};
  const AR_CLASS = {"1:1": "ar-11", "4:5": "ar-45", "9:16": "ar-916", "1.91:1": "ar-191"};
  const AR_BOX = {"1:1": [100, 100], "4:5": [100, 125], "9:16": [100, 178], "1.91:1": [191, 100]};
  const KINDS = [["all", "All"], ["text", "Text"], ["image", "Photo + text"], ["picks", "Kit picks"]];
  const SAMPLE = {kicker: "New this week", headline: "Your headline goes here",
                  body: "One or two short sentences about the post, in your own words.",
                  detail: "Sat 10:00", label: ""};
  const PREVIEW_W = 540, PAUSE = 450, PREFS = "ml-posts-composer";
  const HEX = /^#[0-9a-fA-F]{6}$/;

  const S = {kits: [], styles: [], fonts: [], formats: [], render: {ok: false, missing: []}, recent: [],
             kit: null, style: null, image: null, format: "4:5", palette: "brand", kind: "all", q: "",
             safe: true, mark: true, type: "png", slide: 1, busy: false, editing: null, loaded: false};
  let R = null;            /* the mounted root's parts */
  let tok = 0, url = null, timer = 0;

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
  const NS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
    return el;
  }
  /* an icon from the sprite (icons.svg), always placed beside a text label */
  function icon(name) {
    const s = svg("svg", {class: "ic", "aria-hidden": "true"}), use = svg("use", {href: "/static/icons.svg#" + name});
    s.append(use);
    return s;
  }
  const hex = (c, fb) => HEX.test(c || "") ? c : fb;
  /* colour dots as SVG: a fill attribute is not a style, so the CSP allows it */
  function swatches(colors) {
    const s = svg("svg", {class: "mlp-sw", viewBox: "0 0 " + (colors.length * 10 + 2) + " 12", "aria-hidden": "true"});
    colors.forEach((c, i) => s.append(svg("circle", {cx: 6 + i * 10, cy: 6, r: 5, fill: hex(c, "#888888")})));
    return s;
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
  /* cyrb53: a short stable hash for cache-busting (not security) */
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
  /* one line and one action: every empty state */
  function empty(line, label, iconName, fn) {
    return h("div", {class: "mlp-empty"}, h("p", {text: line}),
      fn ? h("button", {type: "button", class: "ghost", on: {click: fn}}, iconName ? icon(iconName) : null, label) : null);
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
    S.styles = st.styles || []; S.fonts = st.fonts || []; S.formats = (st.formats && st.formats.length) ? st.formats : ["4:5", "1:1", "9:16", "1.91:1"];
    S.render = st.render || {ok: false, missing: []};
    S.kits = kits.kits || [];
    S.recent = Array.isArray(recent) ? recent : [];
    S.loaded = true;
    if (!S.formats.includes(S.format)) S.format = S.formats.includes("4:5") ? "4:5" : S.formats[0];
    if (!S.kit || !S.kits.some(k => k.id === S.kit.id)) S.kit = S.kits[0] || null;
    else S.kit = S.kits.find(k => k.id === S.kit.id);
    pickDefaultStyle();
    tellKit();
    renderAll();
  }
  function tellKit() { window.dispatchEvent(new CustomEvent("medialab:kit", {detail: S.kit ? S.kit.id : ""})); }
  const styleOf = id => S.styles.find(s => s.id === id);
  const picks = () => new Set((S.kit && S.kit.default_styles) || []);
  function pickDefaultStyle() {
    if (S.style && styleOf(S.style)) return;
    const pk = [...picks()].find(styleOf);
    S.style = pk || (S.styles[0] && S.styles[0].id) || null;
    S.slide = 1;
  }

  /* the words: what was typed, or the sample when no headline has been typed yet (previews only) */
  function words(forSave) {
    const t = {label: R.label.value, kicker: R.kicker.value, headline: R.headline.value, body: R.body.value,
               detail: R.detail.value, handle: R.handle.value};
    if (forSave || Object.values(t).some(v => v.trim())) {
      if (!forSave && !t.headline.trim()) t.headline = SAMPLE.headline;
      return t;
    }
    return Object.assign({}, SAMPLE, {handle: (S.kit && S.kit.handle) || "@yourbrand"});
  }
  function payload(st, preview) {
    const p = {kit: S.kit && S.kit.id, style: st.id, format: S.format, palette: S.palette,
               image: st.kind === "image" ? S.image : null, preview, slide: S.slide, mark: S.mark,
               safe_area: S.safe, type: preview ? "png" : S.type, text: words(!preview)};
    if (preview) p.width = PREVIEW_W;
    else p.caption = R.caption.value;
    return p;
  }

  /* ---------------------------------------------------------------- the editor */
  function renderAll() {
    const off = S.loaded && !S.render.ok;
    R.service.hidden = !off;
    R.service.textContent = off ? "Previews are off: " + ((S.render.missing || [])[0] || "the render service is not reachable") +
      ". Start the render service (compose service \"render\") and reload." : "";
    R.kits.replaceChildren(...(S.kits.length ? S.kits.map(k => h("button", {type: "button", class: "chip",
        "aria-pressed": String(!!S.kit && k.id === S.kit.id), title: k.handle || k.name,
        on: {click: () => { S.kit = k; S.style = styleOf(S.style) ? S.style : null; pickDefaultStyle(); tellKit(); renderAll(); }}},
      swatches([(k.colors || {}).bg, (k.colors || {}).accent, (k.colors || {}).fg]), k.name)) : []));
    R.editKit.hidden = !S.kit;
    R.handle.placeholder = (S.kit && S.kit.handle) || "@account or website";
    renderPictures();
    renderFilters();
    renderLook();
    renderGallery();
    drawPreview(true);
  }

  function renderPictures() {
    const none = h("button", {type: "button", class: "mlp-pic mlp-none", "aria-pressed": String(!S.image),
                              text: "No picture", on: {click: () => { S.image = null; renderPictures(); drawPreview(true); }}});
    const items = S.recent.map(it => h("button", {type: "button", class: "mlp-pic", title: it.label || it.ref.name,
        "aria-label": "Use " + (it.label || it.ref.name), "aria-pressed": String(!!S.image && S.image.name === it.ref.name && S.image.root === it.ref.root),
        on: {click: () => { S.image = it.ref; renderPictures(); drawPreview(true); }}},
      h("img", {src: it.poster || it.url, alt: "", loading: "lazy"})));
    R.pics.replaceChildren(none, ...items);
    const make = window.MediaLabStudio ? () => window.MediaLabStudio.show("make") : null;
    R.noPics.replaceChildren(...(S.recent.length ? [] : [empty("No pictures yet. Photo styles show a placeholder until you add one.",
                                                              "Make a picture", "image", make)]));
  }

  function chipGroup(label, opts, get, set, cls) {
    return h("div", {class: "chips " + (cls || ""), role: "group", "aria-label": label}, opts.map(([v, text]) =>
      h("button", {type: "button", class: "chip", "aria-pressed": String(get() === v), text,
                   on: {click: () => { set(v); savePrefs(); }}})));
  }
  function renderFilters() {
    R.channels.replaceChildren(chipGroup("Channel", S.formats.map(f => [f, (CHANNELS[f] || f) + " " + f]),
      () => S.format, v => { S.format = v; renderFilters(); renderLook(); renderGallery(); drawPreview(true); }, "mlp-scrollchips"));
    R.kinds.replaceChildren(chipGroup("Kind of style", KINDS, () => S.kind,
      v => { S.kind = v; renderFilters(); renderGallery(); }, "mlp-scrollchips"));
  }
  function renderLook() {
    const rows = [
      h("div", {class: "mlp-look"}, h("span", {class: "label", text: "Colors"}),
        chipGroup("Colors", [["brand", "Kit"], ["invert", "Inverted"], ["own", "Style's own"]], () => S.palette,
          v => { S.palette = v; renderLook(); renderGallery(); drawPreview(true); })),
      S.format === "9:16" ? h("div", {class: "mlp-look"}, h("span", {class: "label", text: "Story safe area"}),
        chipGroup("Story safe area", [[true, "On"], [false, "Off"]], () => S.safe,
          v => { S.safe = v; renderLook(); drawPreview(true); })) : null,
      h("div", {class: "mlp-look"}, h("span", {class: "label", text: "On the post"}),
        chipGroup("On the post", [[true, "Logo or wordmark"], [false, "No mark"]], () => S.mark,
          v => { S.mark = v; renderLook(); drawPreview(true); })),
    ];
    R.look.replaceChildren(...rows.filter(Boolean));
  }
  function savePrefs() {
    lsSet(PREFS, JSON.stringify({format: S.format, palette: S.palette, kind: S.kind, safe: S.safe, style: S.style}));
  }
  function loadPrefs() {
    try {
      const p = JSON.parse(lsGet(PREFS) || "{}");
      if (typeof p.format === "string") S.format = p.format;
      if (["brand", "invert", "own"].includes(p.palette)) S.palette = p.palette;
      if (KINDS.some(k => k[0] === p.kind)) S.kind = p.kind;
      if (typeof p.safe === "boolean") S.safe = p.safe;
      if (typeof p.style === "string") S.style = p.style;
    } catch (e) { /* ignore a bad saved value */ }
  }

  /* ---------------------------------------------------------------- the style gallery */
  function kindLabel(st) { return st.kind === "image" ? "Photo" : "Text"; }
  function tagOf(st) {
    const first = String(st.tags || "").split("·")[0].trim();
    return kindLabel(st) + (first ? " · " + first : "") + (st.slides > 1 ? " · " + st.slides + " slides" : "");
  }
  /* the swatch under every card: the colours and font the card would be drawn in */
  function swatch(st) {
    const [w, hh] = AR_BOX[S.format] || AR_BOX["4:5"];
    const k = (S.kit && S.kit.colors) || {}, kf = (S.kit && S.kit.fonts) || {}, own = st.own || {};
    let bg, fg, ac, font;
    if (S.palette === "own" && own.bg) { bg = own.bg; fg = own.fg; ac = own.ac || own.ac2; font = Object.values(st.fonts || {})[0]; }
    else if (S.palette === "invert") { bg = k.fg; fg = k.bg; ac = k.accent; font = kf.display; }
    else { bg = k.bg || own.bg; fg = k.fg || own.fg; ac = k.accent || own.ac; font = kf.display || Object.values(st.fonts || {})[0]; }
    const s = svg("svg", {class: "mlp-swatch", viewBox: "0 0 " + w + " " + hh, preserveAspectRatio: "xMidYMid slice", "aria-hidden": "true"});
    s.append(svg("rect", {x: 0, y: 0, width: w, height: hh, fill: hex(bg, "#2a2a2a")}));
    if (st.kind === "image") s.append(svg("rect", {x: 0, y: 0, width: w, height: hh * 0.55, fill: hex(fg, "#dddddd"), "fill-opacity": ".18"}));
    const t = svg("text", {x: 12, y: hh * 0.72, fill: hex(fg, "#f5f5f5"), "font-size": 30, "font-weight": 700,
                           "font-family": String(font || "sans-serif").replace(/[^\w\s',-]/g, "")});
    t.textContent = "Aa";
    s.append(t, svg("rect", {x: 12, y: hh * 0.72 + 8, width: 28, height: 4, fill: hex(ac, "#e8b14a")}));
    return s;
  }
  function thumbUrl(st) {
    const v = hash(JSON.stringify([S.kit, st.version || ""]));
    return "/api/posts/thumb?" + new URLSearchParams([["kit", S.kit.id], ["style", st.id], ["format", S.format],
                                                      ["palette", S.palette], ["v", v]]).toString();
  }
  function card(st) {
    const box = h("span", {class: "mlp-cardpic " + (AR_CLASS[S.format] || "ar-45")}, swatch(st));
    if (S.render.ok && S.kit) {
      const img = h("img", {class: "mlp-shot", alt: "", loading: "lazy", decoding: "async", draggable: false,
                            src: thumbUrl(st), on: {error: () => img.remove(), load: () => img.classList.add("in")}});
      box.append(img);
    }
    const selected = st.id === S.style;
    return h("button", {type: "button", class: "mlp-card", "data-style": st.id, "aria-pressed": String(selected),
                        tabindex: selected ? "0" : "-1", "aria-label": st.name + ", " + tagOf(st),
                        on: {click: () => choose(st.id)}},
      box, h("span", {class: "mlp-name", text: st.name}), h("span", {class: "mlp-tag", text: tagOf(st)}));
  }
  function filtered() {
    const pk = picks(), q = S.q.trim().toLowerCase();
    return S.styles.filter(s => (S.kind === "all" || (S.kind === "picks" ? pk.has(s.id) : s.kind === S.kind)) &&
      (!q || (s.name + " " + s.tags + " " + kindLabel(s)).toLowerCase().includes(q)));
  }
  function renderGallery() {
    const list = filtered();
    R.count.textContent = S.styles.length ? list.length + " of " + S.styles.length : "";
    let kids;
    if (!S.loaded) kids = [h("p", {class: "hint", text: "Loading styles…"})];
    else if (!S.styles.length) kids = [empty("No styles: the render service is off.", "Try again", null, () => loadAll())];
    else if (!list.length) kids = [S.kind === "picks" && !S.q
      ? empty("This kit has no picks yet.", "Choose picks", null, () => S.kit && openEditor(S.kit))
      : empty("No styles match.", "Clear filters", null, () => { S.q = ""; R.search.value = ""; S.kind = "all"; savePrefs(); renderFilters(); renderGallery(); })];
    else kids = list.map(card);
    R.gallery.replaceChildren(...kids);
    if (list.length && !list.some(s => s.id === S.style)) {
      const first = R.gallery.querySelector(".mlp-card");
      if (first) first.tabIndex = 0;
    }
  }
  function choose(id) {
    if (!styleOf(id)) return;
    S.style = id; S.slide = 1;
    for (const c of R.gallery.querySelectorAll(".mlp-card")) {
      const on = c.dataset.style === id;
      c.setAttribute("aria-pressed", String(on)); c.tabIndex = on ? 0 : -1;
    }
    savePrefs();
    drawPreview(true);
  }
  /* arrows move through the cards (a grid on wide screens, one row on narrow ones); Home and End jump */
  function galleryKeys(e) {
    const cards = [...R.gallery.querySelectorAll(".mlp-card")];
    const i = cards.indexOf(document.activeElement);
    if (i < 0) return;
    const top = cards[0].offsetTop;
    let cols = cards.findIndex(c => c.offsetTop !== top);
    if (cols < 0) cols = cards.length;
    const row = cols >= cards.length ? 1 : cols;
    const step = {ArrowRight: 1, ArrowLeft: -1, ArrowDown: row, ArrowUp: -row, Home: -i, End: cards.length - 1 - i}[e.key];
    if (step === undefined) return;
    e.preventDefault();
    const j = Math.max(0, Math.min(cards.length - 1, i + step));
    for (const c of cards) c.tabIndex = -1;
    cards[j].tabIndex = 0;
    cards[j].focus();
    cards[j].scrollIntoView({block: "nearest", inline: "nearest"});
  }

  /* ---------------------------------------------------------------- the live preview */
  function drawPreview(now) {
    clearTimeout(timer);
    if (now) { timer = 0; paint(); return; }
    timer = setTimeout(paint, PAUSE);
  }
  function setFrame(content) {
    R.pframe.className = "mlp-pframe " + (AR_CLASS[S.format] || "ar-45");
    R.pframe.replaceChildren(...content);
  }
  async function paint() {
    const st = styleOf(S.style), my = ++tok;
    R.slides.hidden = true;
    R.save.disabled = !(st && S.kit && S.render.ok) || S.busy;
    R.previewBtn.disabled = !(st && S.kit && S.render.ok);
    if (!S.loaded) { setFrame([h("span", {class: "mlp-wait", text: "Loading…"})]); return; }
    if (!S.kit) {
      R.ptitle.textContent = "Your post";
      R.ptags.textContent = "";
      setFrame([empty("Make a brand kit to see your post in its colors.", "New kit", "plus", () => openEditor(null))]);
      say(R.pstatus, "");
      return;
    }
    if (!st) { R.ptitle.textContent = "Your post"; setFrame([empty("Pick a style on the left.", null)]); return; }
    R.ptitle.textContent = st.name;
    R.ptags.textContent = (CHANNELS[S.format] || S.format) + " " + S.format + " · " + S.kit.name;
    if (st.slides > 1) {
      R.slides.hidden = false;
      S.slide = Math.min(S.slide, st.slides);
      R.slide.textContent = "Slide " + S.slide + " of " + st.slides;
      R.sprev.disabled = S.slide <= 1; R.snext.disabled = S.slide >= st.slides;
    }
    if (!S.render.ok) {
      setFrame([swatch(st), empty("Previews are off: the render service isn't running.", "Try again", null, () => loadAll())]);
      R.pframe.classList.add("off");
      say(R.pstatus, "");
      return;
    }
    setFrame([swatch(st), R.pimg]);
    R.pframe.classList.add("busy");
    say(R.pstatus, "Drawing the preview…");
    try {
      const blob = await api("POST", "/api/posts/render", payload(st, true), true);
      if (my !== tok) return;
      if (url) URL.revokeObjectURL(url);
      url = URL.createObjectURL(blob);
      R.pimg.src = url; R.pimg.hidden = false;
      R.pimg.alt = "Preview of the post in " + st.name;
      say(R.pstatus, R.headline.value.trim() ? "Live preview. Not saved yet." : "Sample words. Type a headline to make it yours.");
    } catch (e) {
      if (my === tok) say(R.pstatus, "Couldn't draw the preview: " + e.message, "err");
    } finally {
      if (my === tok) R.pframe.classList.remove("busy");
    }
  }
  /* Preview: draw now and bring the preview into view (it follows the fields on a narrow screen) */
  function previewNow() {
    drawPreview(true);
    showPreview();
  }
  function showPreview() {
    const r = R.preview.getBoundingClientRect();
    if (r.top < 0 || r.top > window.innerHeight - 120) {
      R.preview.scrollIntoView({block: "start"});   /* instant: a smooth scroll can be dropped mid-way */
      R.preview.focus({preventScroll: true});
    }
  }

  async function save() {
    const st = styleOf(S.style);
    if (!st || !S.kit) return;
    if (!R.headline.value.trim()) {
      say(R.status, "Write a headline first. The preview uses sample words.", "err");
      R.headline.focus();
      return;
    }
    S.busy = true; R.save.disabled = true;
    say(R.status, "Rendering and saving…");
    say(R.pstatus, "Rendering and saving…");
    showPreview();
    try {
      const out = await api("POST", "/api/posts/render", payload(st, false));
      tok++;
      R.pimg.src = out.item.url; R.pimg.hidden = false;
      R.pframe.classList.remove("busy");
      const lic = out.commercial_ok === true ? "Commercial use OK." :
        out.commercial_ok === false ? "Not for commercial use." : "License unknown: check before commercial use.";
      const cal = window.MediaLabCalendar && window.MediaLabStudio;
      R.result.replaceChildren(
        h("p", {}, "Saved as ", h("a", {href: out.item.url + "?dl=1", text: out.item.ref.name, download: out.item.ref.name}),
          " (" + out.info.w + "×" + out.info.h + ")."),
        h("p", {class: out.commercial_ok === true ? "hint" : "status warn", text: lic}),
        ...(out.restricted_by || []).map(t => h("p", {class: "hint", text: t})),
        ...(out.warnings || []).filter(w => !/commercial/i.test(w) || out.commercial_ok !== true).map(t => h("p", {class: "hint", text: t})),
        h("div", {class: "mlp-row"},
          h("a", {class: "btn ghost", href: out.item.url + "?dl=1", download: out.item.ref.name}, icon("download"), "Download"),
          cal ? h("button", {type: "button", class: "ghost", on: {click: () => toCalendar(out)}}, icon("calendar"), "Add to calendar") : null));
      say(R.status, "Saved. It's in your recent files too.");
      say(R.pstatus, "Saved.");
      api("GET", "/api/recent?kind=image&limit=24").then(r => { S.recent = r; renderPictures(); }).catch(() => {});
    } catch (e) {
      say(R.status, e.message, "err");
      say(R.pstatus, e.message, "err");
    } finally {
      S.busy = false; R.save.disabled = false;
    }
  }
  function toCalendar(out) {
    const host = document.querySelector("#view-calendar .viewbody");
    if (!host || !window.MediaLabStudio || !window.MediaLabCalendar) return;
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
    const saveBtn = h("button", {type: "submit", text: kit ? "Save kit" : "Create kit"});
    const del = kit ? h("button", {type: "button", class: "ghost danger", text: "Delete kit", on: {click: () => deleteKit(kit, E)}}) : null;
    const cancel = h("button", {type: "button", class: "ghost", text: "Cancel", on: {click: closeEditor}});
    const form = h("form", {class: "mlp-kitform", on: {submit: ev => { ev.preventDefault(); saveKit(E); }}},
      h("h2", {text: kit ? "Edit brand kit" : "New brand kit"}),
      field("Name", E.name), field("Handle", E.handle),
      h("fieldset", {}, h("legend", {text: "Colors"}), h("div", {class: "mlp-cols"}, colorRows)),
      h("fieldset", {}, h("legend", {text: "Fonts"}), h("div", {class: "mlp-cols"}, fontRows)),
      field("Logo (optional)", E.logo, "One of your pictures; a transparent PNG works best."),
      field("Voice", E.voice),
      h("div", {class: "mlp-field"}, h("span", {class: "label", text: "Kit picks"}), pickChips,
        h("span", {class: "hint", text: "Styles that suit this kit. The Kit picks filter shows only these."})),
      h("div", {class: "mlp-row"}, saveBtn, cancel, h("span", {class: "sp"}), del), E.status);
    R.editor.replaceChildren(form);
    R.editor.showModal();
    E.name.focus();
  }
  function closeEditor() { if (R.editor.open) R.editor.close(); R.editor.replaceChildren(); S.editing = null; R.newKit.focus(); }

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

  /* ---------------------------------------------------------------- mount */
  function sec(label, ...kids) { return h("section", {class: "mlp-sec"}, h("h3", {class: "lbl", text: label}), ...kids); }

  function mount(el) {
    if (!el || el.dataset.mlpMounted) return;
    el.dataset.mlpMounted = "1";
    loadPrefs();
    R = {};
    R.service = h("p", {class: "status warn mlp-service", role: "status", hidden: true});

    /* brand kit */
    R.kits = h("div", {class: "chips mlp-kits", role: "group", "aria-label": "Brand kit"});
    R.editKit = h("button", {type: "button", class: "ghost", text: "Edit kit", on: {click: () => S.kit && openEditor(S.kit)}});
    R.newKit = h("button", {type: "button", class: "ghost", on: {click: () => openEditor(null)}}, icon("plus"), "New kit");

    /* style picker */
    R.search = h("input", {type: "search", placeholder: "Search styles", "aria-label": "Search styles", autocomplete: "off",
                           on: {input: () => { S.q = R.search.value; renderGallery(); }}});
    R.count = h("span", {class: "mlp-count", "aria-live": "polite"});
    R.channels = h("div", {class: "mlp-filter"});
    R.kinds = h("div", {class: "mlp-filter"});
    R.gallery = h("div", {class: "mlp-gallery", role: "group", "aria-label": "Styles", on: {keydown: galleryKeys}});

    /* words */
    const typing = {input: () => drawPreview()};
    R.headline = h("textarea", {maxLength: 300, rows: 2, placeholder: SAMPLE.headline, on: typing});
    R.kicker = h("input", {maxLength: 120, placeholder: SAMPLE.kicker, on: typing});
    R.body = h("textarea", {maxLength: 600, rows: 3, placeholder: SAMPLE.body, on: typing});
    R.detail = h("input", {maxLength: 80, placeholder: "date, price, number…", on: typing});
    R.handle = h("input", {maxLength: 80, on: typing});
    R.label = h("input", {maxLength: 60, placeholder: "e.g. Sponsored", on: typing});
    R.caption = h("textarea", {maxLength: 2200, rows: 3, placeholder: "The text that goes with the post"});

    /* picture, look, file */
    R.pics = h("div", {class: "mlp-pics", role: "group", "aria-label": "Picture"});
    R.noPics = h("div");
    R.look = h("div", {class: "mlp-looks"});
    R.typeChips = h("div", {class: "chips", role: "group", "aria-label": "File"});
    const drawType = () => R.typeChips.replaceChildren(...[["png", "PNG"], ["jpg", "JPEG"]].map(([v, t]) =>
      h("button", {type: "button", class: "chip", "aria-pressed": String(S.type === v), text: t, on: {click: () => { S.type = v; drawType(); }}})));
    drawType();

    R.previewBtn = h("button", {type: "button", class: "ghost", on: {click: previewNow}}, icon("play"), "Preview");
    R.save = h("button", {type: "button", on: {click: save}}, icon("save"), "Render and save");
    R.status = h("p", {class: "status", role: "status", "aria-live": "polite"});

    R.edit = h("div", {class: "mlp-editor-col"},
      h("div", {class: "mlp-head"}, h("h2", {class: "mlp-title", text: "Posts"}),
        h("p", {class: "hint", text: "Pick a kit and a style, write your words, and watch the preview."})),
      R.service,
      sec("Brand kit", h("div", {class: "mlp-kitrow"}, R.kits, h("div", {class: "mlp-row"}, R.editKit, R.newKit))),
      sec("Style",
        h("div", {class: "mlp-searchrow"}, R.search, R.count),
        R.channels, R.kinds, R.gallery),
      sec("Words",
        field("Headline", R.headline), field("Small line above the headline", R.kicker), field("Body", R.body),
        h("details", {class: "mlp-more"}, h("summary", {text: "More text: detail, handle, tag"}),
          field("Extra detail (date, price…)", R.detail), field("@account or website", R.handle),
          field("Tag above the small line (optional)", R.label))),
      sec("Picture", h("p", {class: "hint mlp-subhint", text: "Photo styles use it; text styles leave it out."}), R.pics, R.noPics),
      sec("Look", R.look),
      sec("Caption and file",
        field("Caption", R.caption, "Not drawn on the picture; saved with the post."),
        h("span", {class: "label", text: "File"}), R.typeChips),
      h("div", {class: "mlp-actions"}, R.previewBtn, R.save),
      R.status);

    /* the live preview */
    R.ptitle = h("h3", {class: "mlp-ptitle", text: "Your post"});
    R.ptags = h("p", {class: "hint mlp-ptags"});
    R.pimg = h("img", {class: "mlp-pimg", alt: "", hidden: true});
    R.pframe = h("div", {class: "mlp-pframe ar-45"});
    R.sprev = h("button", {type: "button", class: "ghost", text: "‹", "aria-label": "Previous slide",
                           on: {click: () => { S.slide--; drawPreview(true); }}});
    R.snext = h("button", {type: "button", class: "ghost", text: "›", "aria-label": "Next slide",
                           on: {click: () => { S.slide++; drawPreview(true); }}});
    R.slide = h("span", {class: "hint"});
    R.slides = h("div", {class: "mlp-row", hidden: true}, R.sprev, R.slide, R.snext);
    R.pstatus = h("p", {class: "status", role: "status", "aria-live": "polite"});
    R.result = h("div", {class: "mlp-result", "aria-live": "polite"});
    R.preview = h("aside", {class: "mlp-preview", "aria-label": "Preview", tabindex: "-1"},
      h("div", {class: "mlp-pcard"}, h("div", {class: "mlp-phead"}, R.ptitle, R.ptags), R.pframe, R.slides, R.pstatus, R.result));

    R.more = h("div", {class: "mlp-after"});
    R.root = h("div", {class: "mlp"}, h("div", {class: "mlp-compose"}, R.edit, R.preview), R.more);
    R.editor = h("dialog", {class: "mlp-kitdialog", "aria-label": "Brand kit editor"});
    R.editor.addEventListener("close", () => { R.editor.replaceChildren(); S.editing = null; });

    el.replaceChildren(R.root, R.editor);
    /* the page may hang more panels beside the composer (the recipes): they go under it */
    if (el.parentElement) for (const n of el.parentElement.querySelectorAll(":scope > [data-mlp-canvas]")) R.more.append(n);
    renderFilters();
    renderLook();
    renderGallery();
    paint();
    loadAll().catch(e => say(R.status, e.message, "err"));
  }

  window.MediaLabPosts = {mount};
})();
