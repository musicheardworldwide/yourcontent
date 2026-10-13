"use strict";
/* Studio shell (studio merge): switches between Make (the original page) and the feature views, by hash
   (#make, #posts, #calendar, #connect; #google lands on Connect). A view's module mounts on first open.
   A feature whose script did not load (its module is absent on this install) has no button. With no hash the app
   opens on Posts, the job most people come for; Make is one tap away (and the first screen when Posts is absent). */
(function () {
  const VIEWS = [
    {id: "make", label: "Make"},
    {id: "posts", label: "Posts", mods: ["MediaLabPosts", "MediaLabCraft"]},
    {id: "calendar", label: "Calendar", mod: "MediaLabCalendar"},
    {id: "connect", label: "Connect", mods: ["MediaLabGoogle", "MediaLabAgents"]},
  ];
  const ALIAS = {google: "connect", agents: "connect"};
  const HOME = "posts";
  const mounted = new Set();
  const nav = document.getElementById("viewnav");
  const main = document.getElementById("main");
  if (!nav || !main) return;

  function has(v) {
    if (v.mod) return !!window[v.mod];
    if (v.mods) return v.mods.some(m => !!window[m]);
    return true;
  }
  function section(id) { return document.getElementById("view-" + id); }

  function show(id) {
    const v = VIEWS.find(x => x.id === id && has(x)) || VIEWS[0];
    main.hidden = v.id !== "make";
    for (const x of VIEWS) {
      const s = section(x.id);
      if (s) s.hidden = x.id !== v.id;
      const b = nav.querySelector(`[data-view="${x.id}"]`);
      if (b) b.setAttribute("aria-current", x.id === v.id ? "page" : "false");
    }
    if (v.id !== "make" && !mounted.has(v.id)) {
      mounted.add(v.id);
      const s = section(v.id);
      if (v.mod) window[v.mod].mount(s.querySelector(".viewbody"));
      if (v.mods) for (const m of v.mods) {
        const slot = s.querySelector(`[data-mod="${m}"]`);
        if (window[m] && slot) window[m].mount(slot);
        else if (slot) (slot.closest(".vpanel") || slot).hidden = true;
      }
    }
    document.body.dataset.view = v.id;
    window.dispatchEvent(new CustomEvent("medialab:view", {detail: v.id}));
  }
  function fromHash() {
    const raw = (location.hash || "").slice(1).split(/[?&]/)[0];
    show(ALIAS[raw] || raw || HOME);
  }

  for (const v of VIEWS) {
    if (!has(v)) continue;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "vtab";
    b.dataset.view = v.id;
    b.textContent = v.label;
    b.addEventListener("click", () => {
      if (location.hash.slice(1) === v.id) show(v.id); else location.hash = v.id;
    });
    nav.append(b);
  }
  nav.hidden = nav.children.length < 2;
  window.addEventListener("hashchange", fromHash);
  // switch now (callers such as the tour read the view straight after), and keep the address in step
  window.MediaLabStudio = {show: id => { show(id); history.replaceState(null, "", "#" + id); }};
  fromHash();
})();
