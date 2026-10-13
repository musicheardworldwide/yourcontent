/* Post render engine: runs inside the render service's headless Chromium (never in the app's own page).
   server.py loads page.html, then calls window.renderPost(spec) once per post and screenshots #root > .post.
   spec = {meta, t, palette, ar, safe, mark, markText, logo, name, label, copy:{kicker, head, body, detail, handle},
           images:[data URLs], slide, width}; meta is the style's entry from styles.json, t the brand tokens. */
"use strict";
(function () {
const esc = s => String(s ?? "").replace(/[&<>"']/g, m => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[m]));
const br = s => esc(s).replace(/\n/g, "<br>");
const words = s => String(s).replace(/[.!?,;:]/g, "").split(/\s+/).filter(Boolean);
window.esc = esc; window.br = br; window.words = words;

/* ---------- colour helpers: keep every pairing readable (WCAG AA, 4.5:1) ---------- */
const hex2rgb = h => { h = h.replace("#", ""); if (h.length === 3) h = h.split("").map(x => x + x).join(""); return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)); };
const rgb2hex = a => "#" + a.map(v => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("");
const lum = h => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); }; const [r, g, b] = hex2rgb(h); return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
const cr = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
const mix = (a, b, t) => { const A = hex2rgb(a), B = hex2rgb(b); return rgb2hex(A.map((v, i) => v + (B[i] - v) * t)); };
/* Nudge c toward black or white until it reads on bg. Keeps the hue where it can. */
function ensure(c, bg, min = 4.5) {
  if (cr(c, bg) >= min) return c;
  const to = lum(bg) > .18 ? "#000000" : "#ffffff";
  for (let t = .06; t <= 1; t += .06) { const m = mix(c, to, t); if (cr(m, bg) >= min) return m; }
  return to;
}
/* Text colour for a filled block: brand colours first, then near-white or near-black. */
function onFill(fill, cands) { for (const c of cands) if (cr(c, fill) >= 4.5) return c; return cr("#ffffff", fill) >= cr("#0b0b0b", fill) ? "#ffffff" : "#0b0b0b"; }

function resolveTokens(t, mode, own) {
  let o;
  if (mode === "own") { o = Object.assign({bg: "#111111", fg: "#ffffff", ac: "#e0243f", ac2: "#14b8a6", mut: "#999999"}, own); o.paper = o.paper || o.bg; o.ink = o.ink || o.fg; o.ac2 = o.ac2 || o.ac; }
  else { o = Object.assign({}, t); if (mode === "invert") { [o.bg, o.paper] = [t.paper, t.bg]; [o.fg, o.ink] = [t.ink, t.fg]; } }
  const v = {bg: o.bg, fg: o.fg, paper: o.paper, ink: o.ink, ac: o.ac, ac2: o.ac2, mut: o.mut};
  v["ac-t"] = ensure(o.ac, o.bg); v["ac-p"] = ensure(o.ac, o.paper); v["ac-d"] = ensure(o.ac, "#121212");
  v["ac2-t"] = ensure(o.ac2, o.bg); v["ac2-p"] = ensure(o.ac2, o.paper); v["ac2-d"] = ensure(o.ac2, "#121212");
  v.night = lum(o.bg) < .05 ? o.bg : mix("#07070a", o.ac, .1);
  v["mut-t"] = ensure(o.mut, o.bg); v["mut-p"] = ensure(mix(o.ink, o.paper, .35), o.paper);
  v["on-ac"] = onFill(o.ac, [o.fg, o.ink, o.bg, o.paper]); v["on-ac2"] = onFill(o.ac2, [o.bg, o.fg, o.ink, o.paper]);
  v["on-bg"] = onFill(o.bg, [o.fg, o.ink]);
  return v;
}
function fontVars(t, meta, mode) {
  const f = mode === "own" ? Object.assign({disp: "'Space Grotesk',sans-serif", serif: "'Playfair Display',serif", sans: "Inter,sans-serif", mono: "'JetBrains Mono',monospace", hand: "Caveat,cursive"}, meta.fonts || {}) : t;
  return {"f-disp": f.disp, "f-serif": f.serif, "f-sans": f.sans, "f-mono": f.mono, "f-hand": f.hand};
}
function cssVars(meta, t, ar, mode) {
  const v = Object.assign(resolveTokens(t, mode, meta.own), fontVars(t, meta, mode));
  v["mk-bg"] = v.bg; v["mk-fg"] = v["on-bg"];
  return Object.entries(v).map(([k, x]) => `--${k}:${x}`).join(";") + `;--ar:${ar}`;
}

/* An image style without a picture still renders: a gradient in the kit's own colours. */
function placeholder(t) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1500" viewBox="0 0 1200 1500"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${t.bg}"/><stop offset="1" stop-color="${t.ac}"/></linearGradient></defs><rect width="1200" height="1500" fill="url(#g)"/><circle cx="860" cy="420" r="260" fill="${t.ac2}" opacity=".35"/></svg>`;
  return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}

const MK_POS = {tl: "top:4cqmin;left:4cqmin", tr: "top:4cqmin;right:4cqmin", bl: "bottom:4cqmin;left:4cqmin", br: "bottom:4cqmin;right:4cqmin"};
function ctx(spec, ar) {
  const [a, b] = ar.split("/").map(Number); const ratio = a / b;
  const cp = Object.assign({kicker: "", head: "", body: "", detail: "", handle: ""}, spec.copy || {});
  if (spec.label) cp.kicker = cp.kicker ? `${spec.label} · ${cp.kicker}` : spec.label;
  const imgs = (spec.images || []).slice();
  const img = imgs[0] || placeholder(spec.t);
  return Object.assign(cp, {mark: spec.markText || spec.name || "", name: spec.name || "", img, imgs: imgs.length ? imgs : [img],
    land: ratio > 1.2, ratio, slide: spec.slide || 0});
}
function markHTML(spec, corner) {
  const pos = MK_POS[corner || "tr"] || MK_POS.tr;
  if (spec.logo) return `<div class="mark logo" style="${pos}"><img src="${spec.logo}" alt=""></div>`;
  return spec.markText ? `<div class="mark" style="${pos}">${esc(spec.markText)}</div>` : "";
}
/* In 9:16 "safe" mode the design sits inside the band a story's UI leaves clear, and the frame is filled around it. */
function postHTML(spec) {
  const meta = spec.meta, tpl = window.TEMPLATES[meta.id];
  const story = spec.ar === "9/16" && spec.safe;
  const vars = cssVars(meta, spec.t, spec.ar, spec.palette || "brand");
  const c = ctx(spec, story ? "1080/1306" : spec.ar);
  const mark = spec.mark && meta.mk !== "none" ? markHTML(spec, meta.mk) : "";
  const body = tpl(c) + mark;
  if (story) {
    const fb = {bg: "var(--bg)", paper: "var(--paper)", ac: "var(--ac)", ac2: "var(--ac2)", night: "var(--night)", black: "#000"}[meta.fb] || "var(--bg)";
    const fill = meta.fb === "img" ? `<img class="bg" src="${c.img}" alt="" style="filter:blur(4cqmin) brightness(.55);transform:scale(1.15)">` : "";
    return `<div class="post" style="${vars};background:${fb}" data-style="${meta.id}">${fill}<div class="post inner">${body}</div></div>`;
  }
  return `<div class="post" style="${vars}" data-style="${meta.id}">${body}</div>`;
}

/* Shrink the headline until nothing spills out of the post (long words, long copy, small formats). */
function textEls(post) { return [...post.querySelectorAll("*")].filter(e => [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())); }
function spills(post) {
  const pr = post.getBoundingClientRect();
  for (const el of textEls(post)) {
    const r = el.getBoundingClientRect(); if (!r.width) continue;
    if (r.right > pr.right + 1 || r.left < pr.left - 1 || r.bottom > pr.bottom + 1 || r.top < pr.top - 1) return true;
    if (el.tagName === "H2" && el.scrollWidth > el.clientWidth + 2) return true;
    let a = el.parentElement;
    while (a && a !== post) { if (getComputedStyle(a).overflow !== "visible") { const ar = a.getBoundingClientRect(); if (r.bottom > ar.bottom + 2 || r.right > ar.right + 2) return true; } a = a.parentElement; }
  }
  return false;
}
function fitPost(outer) {
  const post = outer.querySelector(".post.inner") || outer;
  const h = post.querySelector("h2"); if (!h) return;
  const min = Math.min(post.clientWidth, post.clientHeight); if (!min) return;
  for (let i = 0; i < 24 && spills(post); i++) {
    const px = parseFloat(getComputedStyle(h).fontSize) * .92;
    h.style.fontSize = (px / min * 100).toFixed(3) + "cqmin";
  }
}
/* Version stamp per style, so a render can be traced back to the exact template. */
const hash = s => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0).toString(16).padStart(8, "0"); };

window.renderPost = async spec => {
  const tpl = (window.TEMPLATES || {})[spec.meta && spec.meta.id];
  if (!tpl) return {error: `unknown style "${spec.meta && spec.meta.id}"`};
  const root = document.getElementById("root");
  root.style.width = spec.width + "px";
  root.innerHTML = postHTML(spec);
  void root.offsetHeight;            /* lay out first, so every font the post uses starts loading */
  await document.fonts.ready;
  const bad = [];
  await Promise.all([...root.querySelectorAll("img")].map(i => i.complete && i.naturalWidth ? null :
    new Promise(r => { i.onload = r; i.onerror = () => { bad.push(i.getAttribute("src").slice(0, 40)); r(); }; })));
  if (bad.length) return {error: "a picture could not be decoded"};
  if (root.querySelector("[style*='background:url']")) {   /* Image-in-Type paints the picture as a background */
    const src = (spec.images || [])[0];
    if (src) await new Promise(r => { const i = new Image(); i.onload = i.onerror = r; i.src = src; });
  }
  fitPost(root.querySelector(".post"));
  const post = root.querySelector(".post");
  return {w: post.offsetWidth, h: post.offsetHeight,
          ver: hash(tpl.toString() + JSON.stringify(spec.meta.own) + JSON.stringify(spec.meta.fonts || {}))};
};
window.templateIds = () => Object.keys(window.TEMPLATES || {});
window.engineReady = true;
})();
