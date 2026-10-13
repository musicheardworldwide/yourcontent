"use strict";
/* Light / dark / system, remembered per device. Loaded in <head> so the saved choice applies before the first paint:
   "light" or "dark" sets <html data-theme>, which tokens.css honours over the system setting; "system" (the
   default) removes it. Every [data-theme-switch] button (the header's #themebtn, and one in About for
   narrow screens) cycles System -> Light -> Dark. */
(function () {
  const KEY = "ml-theme", ORDER = ["system", "light", "dark"];
  const LABEL = {system: "System", light: "Light", dark: "Dark"};
  const ICON = {system: "system", light: "sun", dark: "moon"};
  function get() {
    let v = null;
    try { v = localStorage.getItem(KEY); } catch (e) { /* private window or blocked storage */ }
    return ORDER.includes(v) ? v : "system";
  }
  function apply(v) {
    if (v === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", v);
  }
  function paint(btn, v) {
    btn.querySelector("use").setAttribute("href", "/static/icons.svg#" + ICON[v]);
    btn.querySelector(".lbltext").textContent = LABEL[v];
    btn.setAttribute("aria-label", "Theme: " + LABEL[v] + ". Change theme");
    btn.title = "Theme: " + LABEL[v] + " (tap to change)";
  }
  let cur = get();
  apply(cur);
  function wire() {
    const btns = [...document.querySelectorAll("[data-theme-switch]")];
    const paintAll = () => { for (const b of btns) { paint(b, cur); b.dataset.theme = cur; } };
    paintAll();
    for (const btn of btns) btn.addEventListener("click", () => {
      cur = ORDER[(ORDER.indexOf(cur) + 1) % ORDER.length];
      try { localStorage.setItem(KEY, cur); } catch (e) { /* still switches for this visit */ }
      apply(cur); paintAll();
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
  window.MediaLabTheme = {get: () => cur, apply};
})();
