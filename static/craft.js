"use strict";
/* Photo and video recipes (studio merge): grade a photo, finish it to a post format, make an animated title
   card, cut a reel from stills and clips with music. Each runs as a craft job (POST /api/craft, poll
   GET /api/craft/<job>); finished files land in the library with their licence carried through.
   Strict CSP: no inline handlers or styles. Exposes window.MediaLabCraft = {mount(el)}. */
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
  const isImage = n => /\.(png|jpe?g|webp)$/i.test(n);
  const isVideo = n => /\.(mp4|mov|webm)$/i.test(n);
  const isAudio = n => /\.(mp3|wav|m4a|flac|ogg|aac|opus)$/i.test(n);
  let uid = 0;
  const nid = p => "cr-" + p + "-" + (++uid);

  function field(label, input) {
    if (!input.id) input.id = nid("f");
    return h("div", {class: "cr-field"}, h("label", {for: input.id, text: label}), input);
  }
  function select(options, value) {
    const s = h("select", {});
    for (const o of options) {
      const [v, t] = Array.isArray(o) ? o : [o, o];
      s.append(h("option", {value: v, text: t}));
    }
    if (value != null) s.value = value;
    return s;
  }

  function mount(root) {
    const S = {media: [], tracks: [], kits: [], doc: null};
    const svc = h("p", {class: "hint"});
    const kit = select([["", "No brand kit (neutral)"]]);
    // follow the kit picked in Posts above, until this selector is changed by hand
    let followKit = "", ownKit = false;
    kit.addEventListener("change", () => { ownKit = true; });
    window.addEventListener("medialab:kit", e => {
      followKit = e.detail || "";
      if (!ownKit && [...kit.options].some(o => o.value === followKit)) { kit.value = followKit; kit.dispatchEvent(new Event("kitset")); }
    });
    const tabs = h("div", {class: "cr-tabs", role: "tablist"});
    const body = h("div", {class: "cr-body"});
    const status = h("p", {class: "status", role: "status"});
    const out = h("div", {class: "cr-out"});

    function mediaPicker(filter, allowNone) {
      const s = select(allowNone ? [["", "None"]] : []);
      for (const m of S.media.filter(m => filter(m.ref.name))) {
        s.append(h("option", {value: m.ref.root + "/" + m.ref.name, text: m.label || m.ref.name}));
      }
      return s;
    }
    const refOf = v => { if (!v) return null; const i = v.indexOf("/"); return {root: v.slice(0, i), name: v.slice(i + 1)}; };

    const forms = {
      "photo.grade": () => {
        // "brand" needs a brand kit: without one the default is a look that runs on its own
        const looks = (S.doc && S.doc.looks || []).filter(l => l !== "custom");
        const plain = looks.includes("natural") ? "natural" : looks.find(l => l !== "brand") || "brand";
        const image = mediaPicker(isImage), look = select(looks.map(l => [l, l[0].toUpperCase() + l.slice(1)]), kit.value ? "brand" : plain);
        let lookTouched = false;
        look.addEventListener("change", () => { lookTouched = true; });
        const sync = () => { if (!lookTouched && look.isConnected) look.value = kit.value ? "brand" : plain; };
        kit.addEventListener("change", sync); kit.addEventListener("kitset", sync);
        const fmt = select([["jpeg", "JPEG"], ["png", "PNG"]]);
        return {nodes: [field("Photo", image), field("Look", look), field("File", fmt)],
          params: () => ({image: refOf(image.value), look: look.value, format: fmt.value})};
      },
      "photo.finish": () => {
        const image = mediaPicker(isImage), fmt = select((S.doc && S.doc.formats) || ["4:5"], "4:5");
        const contrast = select(["none", "soft", "strong"], "soft");
        const psd = h("input", {type: "checkbox"});
        return {nodes: [field("Photo", image), field("Format", fmt), field("Contrast", contrast),
          h("label", {class: "cr-check"}, psd, " Also a layered PSD")],
          params: () => ({image: refOf(image.value), format: fmt.value, contrast: contrast.value, psd: psd.checked})};
      },
      "video.titlecard": () => {
        const headline = h("input", {maxlength: "80", placeholder: "Open studio night"});
        const kicker = h("input", {maxlength: "60", placeholder: "Friday 7 pm"});
        const sub = h("input", {maxlength: "120"});
        const bg = mediaPicker(isImage, true), motion = select(["push", "pan", "still"]);
        const fmt = select(["9:16", "4:5", "1:1"]), secs = h("input", {type: "number", min: "2", max: "10", value: "4"});
        const alpha = h("input", {type: "checkbox"});
        return {nodes: [field("Headline", headline), field("Kicker (small, above)", kicker), field("Line below", sub),
          field("Background picture", bg), field("Motion", motion), field("Format", fmt), field("Seconds", secs),
          h("label", {class: "cr-check"}, alpha, " Transparent background (ProRes .mov to lay over video)")],
          params: () => {
            const p = {headline: headline.value, kicker: kicker.value, sub: sub.value, motion: motion.value,
              format: fmt.value, seconds: Number(secs.value) || 4, alpha: alpha.checked};
            if (bg.value) p.background = refOf(bg.value);
            return p;
          }};
      },
      "video.reel": () => {
        const list = h("ol", {class: "cr-items"});
        const pick = mediaPicker(n => isImage(n) || isVideo(n));
        const addBtn = h("button", {class: "ghost", type: "button", text: "+ Add"});
        const items = [];
        function draw() {
          list.replaceChildren(...items.map((it, i) => {
            const secs = h("input", {type: "number", min: "0.5", max: "15", step: "0.5", value: String(it.seconds),
              "aria-label": "Seconds for item " + (i + 1)});
            secs.addEventListener("change", () => { it.seconds = Number(secs.value) || 3; });
            const cap = h("input", {maxlength: "80", placeholder: "Caption (optional)", value: it.caption,
              "aria-label": "Caption for item " + (i + 1)});
            cap.addEventListener("input", () => { it.caption = cap.value; });
            const rm = h("button", {class: "ghost tiny", type: "button", text: "✕", "aria-label": "Remove item " + (i + 1)});
            rm.addEventListener("click", () => { items.splice(i, 1); draw(); });
            return h("li", {}, h("span", {class: "cr-name", text: it.title}), secs, cap, rm);
          }));
          if (!items.length) list.append(h("li", {class: "hint", text: "Add stills and clips in order."}));
        }
        addBtn.addEventListener("click", () => {
          const m = S.media.find(m => m.ref.root + "/" + m.ref.name === pick.value);
          if (m && items.length < 20) { items.push({ref: m.ref, title: m.label || m.ref.name, seconds: 3, caption: ""}); draw(); }
        });
        draw();
        const music = select([["", "No music"]]);
        for (const t of S.tracks.concat(S.media.filter(m => isAudio(m.ref.name)))) {
          music.append(h("option", {value: t.ref.root + "/" + t.ref.name, text: t.title}));
        }
        const fmt = select(["9:16", "4:5", "1:1"]), tr = select(["dissolve", "cut"]);
        return {nodes: [h("div", {class: "cr-field"}, h("span", {class: "label", text: "Items"}), list,
            h("div", {class: "cr-row"}, pick, addBtn)),
          field("Music", music), field("Format", fmt), field("Transition", tr)],
          params: () => {
            const p = {items: items.map(it => ({ref: it.ref, seconds: it.seconds, caption: it.caption})),
              format: fmt.value, transition: tr.value};
            if (music.value) p.music = refOf(music.value);
            return p;
          }};
      },
    };
    const LABELS = {"photo.grade": "Grade a photo", "photo.finish": "Finish for a post",
      "video.titlecard": "Title card", "video.reel": "Reel"};
    const RUN = {"photo.grade": "Grade photo", "photo.finish": "Finish for a post", "video.titlecard": "Make title card",
      "video.reel": "Make reel"};

    let current = null, form = null;
    function show(recipe) {
      current = recipe;
      for (const b of tabs.children) b.setAttribute("aria-selected", b.dataset.r === recipe ? "true" : "false");
      form = forms[recipe]();
      const run = h("button", {type: "button", text: RUN[recipe] || "Run"});
      run.addEventListener("click", () => submit(run));
      body.replaceChildren(...form.nodes, h("div", {class: "cr-row"}, run));
    }
    async function poll(jid) {
      for (let i = 0; i < 600; i++) {
        const st = await api("GET", "/api/craft/" + encodeURIComponent(jid));
        if (st.state === "done" || st.state === "failed") return st;
        status.textContent = st.state === "queued" ? "Waiting (" + st.position + " ahead)…" : "Working…";
        await new Promise(r => setTimeout(r, 1500));
      }
      throw new Error("Still running; check back later.");
    }
    async function submit(btn) {
      btn.disabled = true; status.className = "status"; status.textContent = "Starting…";
      try {
        const body = {recipe: current, params: form.params()};
        if (kit.value) body.brand_kit = kit.value;
        const j = await api("POST", "/api/craft", body);
        const st = await poll(j.job || j.id);
        if (st.state === "failed") throw new Error(st.error || "The recipe failed.");
        status.textContent = "Done." + (st.warnings && st.warnings.length ? " " + st.warnings.join(" ") : "");
        out.replaceChildren(...(st.outputs || []).map(o => {
          const media = isImage(o.ref.name) ? h("img", {src: o.url, alt: o.role || o.ref.name})
            : isVideo(o.ref.name) && !/\.mov$/i.test(o.ref.name) ? h("video", {src: o.url, controls: true}) : null;
          return h("figure", {}, media, h("figcaption", {}, h("a", {href: o.url, download: o.ref.name, text: o.ref.name}),
            o.commercial_ok === false ? h("span", {class: "warn", text: " · non-commercial"}) : null));
        }));
      } catch (e) { status.className = "status err"; status.textContent = e.message; }
      btn.disabled = false;
    }

    root.replaceChildren(h("p", {text: "Photo and video recipes run on this computer with open-source editors. " +
      "Results go to your library, ready for posts and cuts."}), svc, field("Brand kit", kit), tabs, body, status, out);
    (async () => {
      try {
        const [doc, recent, tracks, kits] = await Promise.all([api("GET", "/api/craft/recipes"),
          api("GET", "/api/recent?limit=200"), api("GET", "/api/tracks").catch(() => []),
          api("GET", "/api/brandkits").catch(() => ({kits: []}))]);
        S.doc = doc; S.media = recent; S.tracks = Array.isArray(tracks) ? tracks : [];
        for (const k of kits.kits || []) kit.append(h("option", {value: k.id, text: k.name}));
        if (followKit && [...kit.options].some(o => o.value === followKit)) kit.value = followKit;
        svc.textContent = doc.service && doc.service.ok ? "" :
          "The recipe service isn't running: " + ((doc.service && doc.service.reason) || "start it with " +
          "docker compose --profile craft up -d") + ".";
        for (const r of Object.keys(forms)) {
          const b = h("button", {type: "button", role: "tab", class: "chip", text: LABELS[r], "data-r": r});
          b.dataset.r = r;
          b.addEventListener("click", () => show(r));
          tabs.append(b);
        }
        show("photo.grade");
      } catch (e) { status.className = "status err"; status.textContent = e.message; }
    })();
  }
  window.MediaLabCraft = {mount};
})();
