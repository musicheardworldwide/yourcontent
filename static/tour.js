"use strict";
/* First-run walkthrough (studio merge). A small non-modal card that steps through what YourContent does, switching
   views and outlining the part it talks about. Starts once on a first visit (after the notice), again from the
   Tour button or #tour. Steps for features this install lacks are skipped. */
(function () {
  const DONE = "ml-tour-1";
  const STEPS = [
    {title: "Welcome to YourContent",
     text: "Make pictures, clips and music on your own GPU, turn them into posts and reels in your brand, plan " +
           "them on a calendar, and let an AI agent help. It never posts for you, and it sends nothing to an " +
           "outside service unless you connect one."},
    {view: "home", need: "MediaLabHome", spot: [".checklist", ".quick"], title: "Home",
     text: "Start here: one tap to a new post, picture, clip or reel, what's next on your calendar, and a short " +
           "setup list that ticks itself off."},
    {view: "settings", need: "MediaLabSettings", spot: [".setgrid"], title: "Is everything ready?",
     text: "The GPU light at the bottom of the sidebar is green when ComfyUI is up. Settings shows each feature's " +
           "status: a missing model names the file and the command that fetches it."},
    {view: "make", tap: "#ptabs [data-tab=picture]", spot: ["#pane-picture"], title: "Make a picture",
     text: "Describe it and press Generate. Add up to 6 reference pictures to keep a face, a product or a room " +
           "the same. Every file is labeled with the license of the model that made it."},
    {view: "make", spot: ["#ptabs", "#pane-clip"], title: "Clips and the cut",
     text: "Animate a picture into a 3–5 second clip, extend it, then arrange stills and clips in the Cut with " +
           "music, and export one MP4 for Reels, feed, square or wide."},
    {view: "posts", need: "MediaLabPosts", spot: [".mlp-gallery"], title: "Posts in your brand",
     text: "Make a brand kit once (colors, fonts, logo, handle) and type your words: every style is drawn in " +
           "your kit as you type. Open one to see it large, then render and save it. Photo grading and reel " +
           "recipes are under the gallery."},
    {view: "calendar", need: "MediaLabCalendar", title: "Plan it",
     text: "Put posts on the calendar as ideas, drafts or ready. Subscribe to the calendar feed from Google, " +
           "Apple or Outlook to see it on your phone."},
    {view: "connect", need: "MediaLabAgents", title: "Connect Google and your agent",
     text: "Connect Google to get a Drive folder and a Content Plan Sheet that YourContent keeps in sync. Then " +
           "make a setup prompt for your AI agent: it can make and schedule, and never posts. The panel says what " +
           "else it can do on this install."},
    {view: "library", need: "MediaLabLibrary", title: "Your library",
     text: "Everything you make or upload, with filters for pictures, clips, music and license. Open one to keep " +
           "working on it."},
    {view: "home", title: "That's the tour",
     text: "Run it again any time from Tour in the sidebar or from Settings. Settings › Help also opens the " +
           "3-minute setup video."},
  ];
  let i = 0, card = null, lastSpot = null;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } }
  const steps = () => STEPS.filter(s => !s.need || window[s.need]);

  function unspot() { if (lastSpot) lastSpot.classList.remove("tourspot"); lastSpot = null; }
  function render() {
    const list = steps(), s = list[i];
    if (s.view && window.MediaLabStudio) window.MediaLabStudio.show(s.view);
    unspot();
    const visible = e => e && e.getClientRects().length > 0;
    const tap = s.tap && document.querySelector(s.tap);
    if (visible(tap)) tap.click();
    const t = (s.spot || []).map(q => document.querySelector(q)).find(visible);
    if (t) { t.classList.add("tourspot"); t.scrollIntoView({block: "nearest"}); lastSpot = t; }
    card.querySelector(".tourn").textContent = (i + 1) + " / " + list.length;
    card.querySelector("h2").textContent = s.title;
    card.querySelector(".tourtext").textContent = s.text;
    card.querySelector(".tourback").disabled = i === 0;
    const next = card.querySelector(".tournext");
    next.textContent = i === list.length - 1 ? "Done" : "Next";
    next.focus({preventScroll: true});
  }
  function close() {
    unspot();
    lsSet(DONE, "1");
    if (card) card.remove();
    card = null;
  }
  function start() {
    if (card) return;
    i = 0;
    card = el("section", "tourcard");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-label", "YourContent tour");
    const head = el("div", "tourhead");
    head.append(el("span", "tourn"), el("span", "sp"));
    const skip = el("button", "ghost tiny", "Skip");
    skip.type = "button";
    skip.addEventListener("click", close);
    head.append(skip);
    const back = el("button", "ghost tourback", "Back");
    back.type = "button";
    back.addEventListener("click", () => { if (i > 0) { i--; render(); } });
    const next = el("button", "tournext", "Next");
    next.type = "button";
    next.addEventListener("click", () => { if (i < steps().length - 1) { i++; render(); } else close(); });
    const btns = el("div", "tourbtns");
    btns.append(back, el("span", "sp"), next);
    card.append(head, el("h2"), el("p", "tourtext"), btns);
    card.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
    document.body.append(card);
    render();
  }

  const btn = document.getElementById("tourbtn");
  if (btn) btn.addEventListener("click", start);
  window.addEventListener("hashchange", () => { if (location.hash === "#tour") start(); });
  window.MediaLabTour = {start};
  // first visit: wait until the welcome dialog is dismissed, so the two never stack (the welcome marks the tour
  // "later" unless its Take the tour button is used, so in practice the tour starts when asked)
  function maybeStart() {
    if (lsGet(DONE) || location.hash === "#google") return;
    if (!lsGet("ml-notice-1")) { setTimeout(maybeStart, 800); return; }  // app.js sets it when the welcome closes
    start();
  }
  if (location.hash === "#tour") start(); else setTimeout(maybeStart, 600);
})();
