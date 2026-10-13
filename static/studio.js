"use strict";
/* Studio shell: the left sidebar (a bottom tab bar on a phone) and the views it switches between, by hash
   (#home, #create, #posts, #calendar, #library, #connect, #settings; #make and #google still work). A view's module
   mounts on first open. A feature whose script did not load (its module is absent on this install) has no button.
   With no hash the app opens on Home. The sidebar's collapsed state is remembered per device. */
(function () {
  const VIEWS = [
    {id: "home", label: "Home", icon: "home", mod: "MediaLabHome"},
    {id: "make", label: "Create", icon: "sparkle"},
    {id: "posts", label: "Posts", icon: "layers", mods: ["MediaLabPosts", "MediaLabCraft"]},
    {id: "calendar", label: "Calendar", icon: "calendar", mod: "MediaLabCalendar"},
    {id: "library", label: "Library", icon: "folder", mod: "MediaLabLibrary"},
    {id: "connect", label: "Connect", icon: "link", mods: ["MediaLabGoogle", "MediaLabAgents"]},
    {id: "settings", label: "Settings", icon: "gear", mod: "MediaLabSettings"},
  ];
  const ALIAS = {create: "make", google: "connect", agents: "connect"};
  const HOME = "home";
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
    id = ALIAS[id] || id;
    const v = VIEWS.find(x => x.id === id && has(x)) || VIEWS.find(x => x.id === HOME && has(x)) || VIEWS[1];
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
    window.scrollTo(0, 0);
    window.dispatchEvent(new CustomEvent("medialab:view", {detail: v.id}));
  }
  function fromHash() {
    const raw = (location.hash || "").slice(1).split(/[?&]/)[0];
    show(raw || HOME);
  }
  const hashOf = id => id === "make" ? "create" : id;

  const svgNS = "http://www.w3.org/2000/svg";
  for (const v of VIEWS) {
    if (!has(v)) continue;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "vtab";
    b.dataset.view = v.id;
    b.title = v.label;
    const ic = document.createElementNS(svgNS, "svg");
    ic.setAttribute("class", "ic");
    ic.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(svgNS, "use");
    use.setAttribute("href", "/static/icons.svg#" + v.icon);
    ic.append(use);
    const t = document.createElement("span");
    t.className = "vlabel";
    t.textContent = v.label;
    b.append(ic, t);
    b.addEventListener("click", () => {
      if (location.hash.slice(1) === hashOf(v.id)) show(v.id); else location.hash = hashOf(v.id);
    });
    nav.append(b);
  }
  nav.hidden = nav.children.length < 2;

  // collapse to icons (desktop); remembered
  const KEY = "ml-side";
  const cb = document.getElementById("collapsebtn");
  const setCollapsed = on => {
    document.body.classList.toggle("side-min", on);
    if (cb) { cb.setAttribute("aria-expanded", String(!on)); cb.setAttribute("aria-label", on ? "Expand sidebar" : "Collapse sidebar"); }
  };
  let saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* private window */ }
  setCollapsed(saved === "min");
  if (cb) cb.addEventListener("click", () => {
    const on = !document.body.classList.contains("side-min");
    setCollapsed(on);
    try { localStorage.setItem(KEY, on ? "min" : "full"); } catch (e) { /* still collapses for this visit */ }
  });

  window.addEventListener("hashchange", fromHash);
  // switch now (callers such as the tour read the view straight after), and keep the address in step
  window.MediaLabStudio = {show: id => { show(id); history.replaceState(null, "", "#" + hashOf(ALIAS[id] || id)); }};
  fromHash();
})();
