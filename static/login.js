"use strict";
/* The sign-in page (/login): one field, the access key. A good key sets the session cookie (POST /api/login) and
   goes back to the app, to the view the visitor was on (?next=#view). */
(function () {
  const $ = id => document.getElementById(id);
  const form = $("loginform"), tok = $("token"), st = $("loginstatus"), btn = $("signin"), peek = $("peek");
  const next = new URLSearchParams(location.search).get("next") || "";
  const back = "/" + (/^#[a-z]{1,20}$/.test(next) ? next : "");
  tok.focus();
  peek.addEventListener("click", () => {
    const show = tok.type === "password";
    tok.type = show ? "text" : "password";
    peek.setAttribute("aria-pressed", String(show));
    peek.setAttribute("aria-label", show ? "Hide key" : "Show key");
  });
  form.addEventListener("submit", async e => {
    e.preventDefault();
    if (!tok.value) { st.textContent = "Enter your access key."; st.className = "status err"; tok.focus(); return; }
    btn.disabled = true; st.className = "status"; st.textContent = "Signing in…";
    let r;
    try {
      r = await fetch("/api/login", {method: "POST", headers: {"X-Media-Lab": "1", "Content-Type": "application/json"},
        body: JSON.stringify({token: tok.value})});
    } catch (err) {
      btn.disabled = false; st.className = "status err"; st.textContent = "Can't reach YourContent. Is it running?"; return;
    }
    if (r.ok) { st.textContent = "Signed in."; location.replace(back); return; }
    btn.disabled = false; st.className = "status err";
    st.textContent = r.status === 429 ? "Too many tries. Wait a minute and try again." : "That key isn't right. Check it and try again.";
    tok.select();
  });
})();
