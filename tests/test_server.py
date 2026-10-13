"""WP2: the HTTP server in-process on an ephemeral port, with temp roots and fakes for the other packages.

Covers: every §6 route, the §12 guard (Host, token, CSRF header, Origin, methods, body rules), Range/206,
error mapping, start-up refusal, jobs, config and the CLI's manifest/verify. Tests that need another
package's real code (WP1/WP3) skip themselves while that code is still a stub.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from unittest import mock

from medialab import (__main__ as cli, actions, comfy, config, export, features, jobs, legacy, media, models,
                      projects, provenance, server, store, timeline)
from medialab.types import Plan, Probe
from tests.util import FakeComfy, TempRoots, make_png, requires_ffmpeg

TOKEN = "correct-horse-battery"
ORIG_MODELS_LOAD = models.load


class FakeModels:
    aliases = {"old-int8": "int8"}
    default_variant = "int8"
    video = {"licence": "apache-2.0"}
    music = {"licence": "ace-step"}

    def image_variants(self):
        return {"int8": {"label": "int8 · balanced", "unet": "a.safetensors", "lora": None, "licence": "qwen-image-nc"}}

    def known(self, vid):
        return vid in (None, "") or vid in self.image_variants() or self.aliases.get(vid) in self.image_variants()

    def licence(self, lid):
        return {"qwen-image-nc": {"text": "Qwen-Image 2.1 licence (non-commercial research)", "commercial_ok": False},
                "apache-2.0": {"text": "Apache-2.0", "commercial_ok": True},
                "ace-step": {"text": "Apache-2.0 / MIT, outputs commercial OK", "commercial_ok": True}}.get(
            lid, {"text": "unknown", "commercial_ok": None})


def fake_plan(project, probes, wavs={}):
    n = len(project.get("items") or [])
    return Plan(F=24, items=(), T=60 * n, T_samples=120000 * n, preset="9:16", fit="auto", music=None,
                warnings=tuple(f"{r[1]} is missing; shown as black" for r, p in probes.items() if p is None),
                missing=())


def fake_plan_json(plan):
    return {"fps": plan.F, "frames": plan.T, "seconds": f"{plan.T / plan.F:.6f}", "samples": plan.T_samples,
            "items": [], "music": None, "warnings": list(plan.warnings)}


def fake_probe(path):
    if path.endswith(".png"):
        return Probe("image", 1104, 1472, Fraction(1), 0, Fraction(0), 0.0, False, 0, 0, "png", "", "unknown")
    return Probe("video", 624, 624, Fraction(1), 49, Fraction(16), 3.0625, False, 0, 0, "h264", "", "bt709")


def fake_input_entry(role, ref, sidecar, snap_ok=None):
    ok = sidecar.get("commercial_ok") if sidecar else snap_ok
    return {"role": role, "root": ref[0], "name": ref[1], "commercial_ok": ok, "missing": sidecar is None,
            "licences": (sidecar or {}).get("licences", [])}


def fake_aggregate(models_, inputs):
    oks = [i["commercial_ok"] for i in inputs]
    ok = False if False in oks else (None if None in oks else True)
    return {"commercial_ok": ok, "licences": [], "most_restrictive": "qwen-image-nc" if ok is False else None,
            "restricted_by": [f"{i['name']}" for i in inputs if i["commercial_ok"] is False]}


class Upload415(ValueError):
    status = 415


class ServerCase(unittest.TestCase):
    """Starts a server for each test. Override cfg_overrides() for token / hosts."""

    def cfg_overrides(self) -> dict:
        return {}

    def setUp(self):
        self.r = TempRoots()
        self.addCleanup(self.r.cleanup)
        self.calls: list = []
        self.comfy_up = True
        patches = [
            mock.patch.object(models, "load", lambda cfg: FakeModels()),
            mock.patch.object(timeline, "validate", lambda p: (p, [])),
            mock.patch.object(timeline, "plan", fake_plan),
            mock.patch.object(timeline, "plan_json", fake_plan_json),
            mock.patch.object(timeline, "ace_seconds", lambda ts, m: round(ts / 48000, 2)),
            mock.patch.object(media, "probe", fake_probe),
            mock.patch.object(media, "ffmpeg_caps", lambda: {"version": "7.1.5", "missing_filters": [],
                                                              "missing_encoders": []}),
            mock.patch.object(provenance, "input_entry", fake_input_entry),
            mock.patch.object(provenance, "aggregate", fake_aggregate),
            mock.patch.object(features, "compute", self._features),
            mock.patch.object(comfy, "request", self._comfy_request),
            mock.patch.object(comfy, "ensure", self._comfy_ensure),
            mock.patch.object(actions, "generate", self._action("generate")),
            mock.patch.object(actions, "animate", self._action("animate")),
            mock.patch.object(actions, "extend", self._action("extend")),
            mock.patch.object(actions, "music", self._action("music")),
            mock.patch.object(export, "run_render", self._render),
            mock.patch.object(server, "FAIL_DELAY", 0.3),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        jobs.reset()
        self.addCleanup(jobs.reset)
        cfg = self.r.config(**self.cfg_overrides())
        self.srv = server.make_server(cfg, adopt=False)
        self.port = self.srv.server_address[1]
        t = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    # fakes
    def _features(self, infos, models_, ff):
        return {"picture": {"ok": bool(infos), "variants": {"int8": {"ok": True, "missing": []}}},
                "clip": {"ok": True, "missing": []}, "extend": {"ok": True},
                "music": {"ok": True, "saver": "SaveAudio", "missing": []},
                "export": {"ok": True, "missing_filters": ff["missing_filters"]}}

    def _comfy_request(self, path, payload=None, timeout=30):
        self.calls.append(("comfy.request", path))
        if not self.comfy_up:
            raise OSError("connection refused")
        if path == "/system_stats":
            return {"system": {"comfyui_version": "0.37.0"}}
        if path == "/object_info":
            return {"KSampler": {}}
        return {}

    def _comfy_ensure(self):
        if not self.comfy_up:
            raise RuntimeError("ComfyUI is off")

    def _action(self, name):
        def fn(body, job):
            self.calls.append((name, body))
            job.progress(0.5, "render")
            return {"ref": {"root": "gen", "name": "img_00001_.png"}, "type": "image", "kind": name}
        return fn

    def _render(self, project, rev, mode, job):
        self.calls.append(("render", project["id"], rev, mode))
        job.progress(0.5, "video")
        return {"url": "/m/cache/" + "a" * 64 + ".mp4", "frames": 60, "seconds": 2.5, "mode": mode}

    # client
    def req(self, method, path, body=None, headers=None, raw=None, host=None, csrf=True, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host if host is not None else f"127.0.0.1:{self.port}"}
        if method == "POST" and csrf:
            h["X-Media-Lab"] = "1"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None and ctype:
            h["Content-Type"] = ctype
        h.update(headers or {})
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            if v is not None:
                conn.putheader(k, v)
        if data is not None and "Content-Length" not in h and "Transfer-Encoding" not in h:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders()
        if data is not None:
            try:
                conn.send(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
        resp = conn.getresponse()
        out = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        ctype_out = hdrs.get("content-type", "")
        parsed = json.loads(out) if ctype_out.startswith("application/json") and out else out
        return resp.status, hdrs, parsed

    def own(self, root, name, data=b"x", **sc):
        self.r.put(root, name, data)
        store.put_sidecar((root, name), {"schema": "media-lab/asset@1", "kind": sc.pop("kind", "image"),
                                         "created": sc.pop("created", "2026-10-07T10:00:00Z"), **sc})

    def wait_job(self, jid):
        return jobs.wait(jid, 5)


class GuardTest(ServerCase):
    def test_csp_and_security_headers_on_page_and_api(self):
        for path in ("/", "/static/app.js", "/static/style.css", "/api/config"):
            code, h, _ = self.req("GET", path)
            self.assertEqual(code, 200, path)
            self.assertEqual(h["content-security-policy"], server.CSP)
            self.assertIn("frame-ancestors 'none'", h["content-security-policy"])
            self.assertEqual(h["x-content-type-options"], "nosniff")
            self.assertEqual(h["referrer-policy"], "no-referrer")
            self.assertFalse([k for k in h if k.startswith("access-control-")])
        code, h, body = self.req("GET", "/")
        self.assertTrue(h["content-type"].startswith("text/html"))
        self.assertIn(b"/static/app.js", body)

    def test_tokens_and_fonts(self):
        """Visual pass: tokens.css is served like style.css; only the three named UI fonts are, from static/fonts/."""
        code, h, _ = self.req("GET", "/static/tokens.css")
        self.assertEqual((code, h["content-type"]), (200, "text/css; charset=utf-8"))
        self.assertEqual(self.req("GET", "/static/fonts/Other_wght.ttf")[0], 404)
        self.assertEqual(self.req("GET", "/static/fonts/../app.py")[0], 404)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "fonts"))
        with mock.patch.object(server, "STATIC_DIR", tmp):
            self.assertEqual(self.req("GET", "/static/fonts/Inter_opsz_wght.ttf")[0], 404)   # not fetched: 404
            with open(os.path.join(tmp, "fonts", "Inter_opsz_wght.ttf"), "wb") as f:
                f.write(b"\x00\x01\x00\x00font")
            code, h, body = self.req("GET", "/static/fonts/Inter_opsz_wght.ttf")
        self.assertEqual((code, h["content-type"], h["content-security-policy"]), (200, "font/ttf", server.CSP))

    def test_post_without_header_is_403(self):
        code, _, body = self.req("POST", "/api/projects", {}, csrf=False)
        self.assertEqual(code, 403)
        self.assertIn("X-Media-Lab", body["error"])
        code, _, _ = self.req("POST", "/api/projects", {}, headers={"X-Media-Lab": "yes"}, csrf=False)
        self.assertEqual(code, 403)

    def test_foreign_origin_is_403(self):
        for origin in ("http://evil.example", "null", f"http://127.0.0.1:{self.port + 1}", "file://"):
            code, _, _ = self.req("POST", "/api/projects", {}, headers={"Origin": origin})
            self.assertEqual(code, 403, origin)
        code, _, _ = self.req("POST", "/api/projects", {}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(code, 200)

    def test_bad_host_is_403(self):
        for host in ("evil.example", "evil.example:8190", "127.0.0.1.evil.example", "", "localhost:abc"):
            code, h, _ = self.req("GET", "/api/config", host=host)
            self.assertEqual(code, 403, host)
        for host in ("localhost", "localhost:8190", "127.0.0.1", "[::1]:8190", "[::1]"):
            code, _, _ = self.req("GET", "/api/config", host=host)
            self.assertEqual(code, 200, host)

    def test_other_methods_are_405_without_cors(self):
        for method in ("PUT", "OPTIONS", "DELETE", "PATCH", "HEAD"):
            code, h, _ = self.req(method, "/api/projects", headers={"Origin": "http://evil.example",
                                                                     "Access-Control-Request-Method": "POST"})
            self.assertEqual(code, 405, method)
            self.assertFalse([k for k in h if k.startswith("access-control-")], method)
        code, h, _ = self.req("GET", "/api/delete")
        self.assertEqual((code, h.get("allow")), (405, "GET, POST"))
        self.assertEqual(self.req("GET", "/nope")[0], 404)
        self.assertEqual(self.req("GET", "/static/../app.py")[0], 404)
        self.assertEqual(self.req("GET", "/static/util.js")[0], 404)
        # old routes are gone
        for path in ("/recent", "/models"):
            self.assertEqual(self.req("GET", path)[0], 404, path)

    def test_json_body_rules(self):
        self.assertEqual(self.req("POST", "/api/projects", raw=b'{"name":"x"}', ctype="text/plain")[0], 415)
        self.assertEqual(self.req("POST", "/api/projects", raw=b"{nope")[0], 400)
        self.assertEqual(self.req("POST", "/api/projects", raw=b"[1]")[0], 400)
        big = json.dumps({"name": "x" * (server.MAX_JSON + 10)}).encode()
        self.assertEqual(self.req("POST", "/api/projects", raw=big)[0], 413)
        code, _, body = self.req("POST", "/api/projects", raw=b'{"name":"x"}',
                                 headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(code, 400)
        self.assertIn("chunked", body["error"])

    def test_no_token_configured_means_open_and_login_is_a_noop(self):
        code, h, _ = self.req("POST", "/api/login", {"token": "anything"})
        self.assertEqual(code, 204)
        self.assertNotIn("set-cookie", h)
        c = self.req("GET", "/api/config")[2]
        self.assertFalse(c["auth"])
        self.assertEqual((c["token_required"], c["listens"], c["loopback_only"]), (False, "127.0.0.1", True))


class TokenTest(ServerCase):
    def cfg_overrides(self):
        return {"token": TOKEN}

    def test_token_required_except_page_and_config(self):
        for path in ("/login", "/static/app.js", "/static/style.css", "/static/login.js", "/api/config"):
            self.assertEqual(self.req("GET", path)[0], 200, path)
        code, h, _ = self.req("GET", "/")                  # launch UI: a signed-out browser goes to the sign-in page
        self.assertEqual((code, h.get("location")), (302, "/login"))
        self.assertTrue(self.req("GET", "/api/config")[2]["auth"])
        self.assertTrue(self.req("GET", "/api/config")[2]["token_required"])
        for method, path in (("GET", "/api/recent"), ("GET", "/api/health"), ("GET", "/m/gen/a.png"),
                             ("GET", "/out/a.png"), ("POST", "/api/projects"), ("GET", "/api/jobs")):
            code, _, body = self.req(method, path, {} if method == "POST" else None)
            self.assertEqual(code, 401, path)
            self.assertNotIn(TOKEN, json.dumps(body))

    def test_bearer_and_cookie(self):
        code, _, _ = self.req("GET", "/api/recent", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(code, 200)
        code, h, _ = self.req("POST", "/api/login", {"token": TOKEN})
        self.assertEqual(code, 204)
        cookie = h["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertNotIn(TOKEN, cookie)
        value = cookie.split(";")[0]
        self.assertEqual(self.req("GET", "/api/recent", headers={"Cookie": value})[0], 200)
        self.assertEqual(self.req("POST", "/api/projects", {}, headers={"Cookie": "a=b; " + value})[0], 200)
        # behind an https proxy the cookie is Secure
        _, h, _ = self.req("POST", "/api/login", {"token": TOKEN}, headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", h["set-cookie"])
        code, h, _ = self.req("POST", "/api/logout", {})
        self.assertEqual(code, 204)
        self.assertIn("Max-Age=0", h["set-cookie"])

    def test_sign_in_page_redirects(self):
        """Launch UI: signed out, / goes to /login; signed in, /login goes back to the app."""
        value = self.req("POST", "/api/login", {"token": TOKEN})[1]["set-cookie"].split(";")[0]
        self.assertEqual(self.req("GET", "/", headers={"Cookie": value})[0], 200)
        code, h, _ = self.req("GET", "/login", headers={"Cookie": value})
        self.assertEqual((code, h.get("location")), (302, "/"))
        code, h, _ = self.req("GET", "/", headers={"Cookie": "ml_session=" + "0" * 64})
        self.assertEqual((code, h.get("location")), (302, "/login"))

    def test_wrong_token_is_delayed_and_rate_limited(self):
        t0 = time.monotonic()
        code, _, _ = self.req("GET", "/api/recent", headers={"Authorization": "Bearer nope"})
        self.assertEqual(code, 401)
        self.assertGreaterEqual(time.monotonic() - t0, server.FAIL_DELAY * 0.9)
        t0 = time.monotonic()
        self.assertEqual(self.req("POST", "/api/login", {"token": "nope"})[0], 401)
        self.assertGreaterEqual(time.monotonic() - t0, server.FAIL_DELAY * 0.9)
        self.assertEqual(self.req("GET", "/api/recent", headers={"Cookie": "ml_session=forged"})[0], 401)
        with mock.patch.object(server, "FAIL_DELAY", 0):
            for _ in range(server.FAIL_LIMIT):
                self.req("POST", "/api/login", {"token": "nope"})
            # now even the right token waits out the minute
            self.assertEqual(self.req("POST", "/api/login", {"token": TOKEN})[0], 429)

    def test_limit_applies_to_bearer_even_when_right_but_not_to_a_session(self):
        """Once limited, a right Bearer token gets the same instant 429 as a wrong one (no verdict). A valid session
        cookie still passes (review r2): it is unguessable, and on Docker Desktop or behind NAT a guesser shares
        the owner's address, so limiting it would lock the owner out. A forged cookie is still limited."""
        value = self.req("POST", "/api/login", {"token": TOKEN})[1]["set-cookie"].split(";")[0]
        with mock.patch.object(server, "FAIL_DELAY", 0):
            for _ in range(server.FAIL_LIMIT):
                self.assertEqual(self.req("GET", "/api/recent", headers={"Authorization": "Bearer nope"})[0], 401)
            t0 = time.monotonic()
            self.assertEqual(self.req("GET", "/api/recent", headers={"Authorization": "Bearer nope"})[0], 429)
            self.assertEqual(self.req("GET", "/api/recent", headers={"Authorization": f"Bearer {TOKEN}"})[0], 429)
            self.assertEqual(self.req("GET", "/api/recent", headers={"Cookie": "ml_session=forged"})[0], 429)
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(self.req("GET", "/api/recent", headers={"Cookie": value})[0], 200)
            self.assertEqual(self.req("POST", "/api/login", {"token": TOKEN})[0], 429)

    def test_parallel_guesses_get_at_most_the_limit_of_verdicts(self):
        codes, lock = [], threading.Lock()

        def guess():
            c = self.req("GET", "/api/projects", headers={"Authorization": "Bearer wrong-guess"})[0]
            with lock:
                codes.append(c)
        ts = [threading.Thread(target=guess) for _ in range(3 * server.FAIL_LIMIT)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(15)
        self.assertEqual(codes.count(401), server.FAIL_LIMIT, codes)
        self.assertEqual(codes.count(429), 2 * server.FAIL_LIMIT, codes)

    def test_forwarded_for_ignored_without_a_trusted_proxy(self):
        with mock.patch.object(server, "FAIL_DELAY", 0):
            for i in range(server.FAIL_LIMIT):
                self.req("POST", "/api/login", {"token": "nope"}, headers={"X-Forwarded-For": f"203.0.113.{i}"})
            self.assertEqual(self.req("POST", "/api/login", {"token": TOKEN},
                                      headers={"X-Forwarded-For": "198.51.100.7"})[0], 429)

    def test_default_delay_is_one_second(self):
        with open(server.__file__, encoding="utf-8") as f:
            self.assertIn("FAIL_DELAY = 1.0 ", f.read())


class TrustedProxyTest(ServerCase):
    """Behind MEDIA_LAB_TRUSTED_PROXY the limiter keys on the address the proxy appended, so one client's
    wrong guesses cannot lock the owner out of login."""

    def cfg_overrides(self):
        return {"token": TOKEN, "trusted_proxies": ("127.0.0.1",)}

    def test_attacker_does_not_lock_out_the_owner(self):
        with mock.patch.object(server, "FAIL_DELAY", 0):
            for _ in range(server.FAIL_LIMIT):
                self.req("POST", "/api/login", {"token": "nope"}, headers={"X-Forwarded-For": "203.0.113.9"})
            self.assertEqual(self.req("POST", "/api/login", {"token": TOKEN},
                                      headers={"X-Forwarded-For": "203.0.113.9"})[0], 429)
            # a spoofed left-hand entry does not move the attacker to another key
            self.assertEqual(self.req("POST", "/api/login", {"token": "nope"},
                                      headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.9"})[0], 429)
            self.assertEqual(self.req("POST", "/api/login", {"token": TOKEN},
                                      headers={"X-Forwarded-For": "198.51.100.7"})[0], 204)

    def test_config_parses_trusted_proxies(self):
        from medialab import config as config_mod
        cfg = config_mod.load({"MEDIA_LAB_TRUSTED_PROXY": "127.0.0.1, ::1"})
        self.assertEqual(cfg.trusted_proxies, ("127.0.0.1", "::1"))
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load({"MEDIA_LAB_TRUSTED_PROXY": "proxy.example.test"})


class AllowedHostsTest(ServerCase):
    def cfg_overrides(self):
        return {"allowed_hosts": ("media.example.test:8190", "box.example.test")}

    def test_allowed_hosts(self):
        self.assertEqual(self.req("GET", "/api/config", host="media.example.test:8190")[0], 200)
        self.assertEqual(self.req("GET", "/api/config", host="MEDIA.example.test:8190")[0], 200)
        self.assertEqual(self.req("GET", "/api/config", host="media.example.test:9999")[0], 403)
        self.assertEqual(self.req("GET", "/api/config", host="box.example.test:1234")[0], 200)
        self.assertEqual(self.req("GET", "/api/config", host="other.example.test:8190")[0], 403)
        # Origin must match the Host it came in on
        code, _, _ = self.req("POST", "/api/projects", {}, host="media.example.test:8190",
                              headers={"Origin": "http://media.example.test:8190"})
        self.assertEqual(code, 200)


class MediaTest(ServerCase):
    def test_range_requests(self):
        data = bytes(range(256)) * 40   # 10240 bytes
        self.own("gen", "clip_00001.mp4", data, kind="clip")
        code, h, body = self.req("GET", "/m/gen/clip_00001.mp4")
        self.assertEqual((code, h["accept-ranges"], h["content-type"], body), (200, "bytes", "video/mp4", data))
        code, h, body = self.req("GET", "/m/gen/clip_00001.mp4", headers={"Range": "bytes=100-199"})
        self.assertEqual(code, 206)
        self.assertEqual(h["content-range"], "bytes 100-199/10240")
        self.assertEqual(h["content-length"], "100")
        self.assertEqual(body, data[100:200])
        code, h, body = self.req("GET", "/m/gen/clip_00001.mp4", headers={"Range": "bytes=10000-"})
        self.assertEqual((code, h["content-range"], body), (206, "bytes 10000-10239/10240", data[10000:]))
        code, h, body = self.req("GET", "/m/gen/clip_00001.mp4", headers={"Range": "bytes=-16"})
        self.assertEqual((code, h["content-range"], body), (206, "bytes 10224-10239/10240", data[-16:]))
        code, h, body = self.req("GET", "/m/gen/clip_00001.mp4", headers={"Range": "bytes=10000-99999"})
        self.assertEqual((code, h["content-range"]), (206, "bytes 10000-10239/10240"))
        code, h, _ = self.req("GET", "/m/gen/clip_00001.mp4", headers={"Range": "bytes=20000-"})
        self.assertEqual((code, h["content-range"]), (416, "bytes */10240"))
        code, _, body = self.req("GET", "/m/gen/clip_00001.mp4", headers={"Range": "bytes=0-1,5-6"})
        self.assertEqual((code, len(body)), (200, len(data)))   # multi-range: whole file
        code, h, _ = self.req("GET", "/m/gen/clip_00001.mp4?dl=1")
        self.assertEqual(h["content-disposition"], 'attachment; filename="clip_00001.mp4"')

    def test_ownership_and_aliases(self):
        png = make_png()
        self.own("out", "qwen_studio_00001_.png", png)
        self.r.put("out", "qwen2_t2i_00001_.png", png)              # another tool's file: never served
        self.own("in", "ref_20261007-120000_abcdef.png", png, kind="upload")
        for path in ("/m/out/qwen_studio_00001_.png", "/out/qwen_studio_00001_.png",
                     "/m/in/ref_20261007-120000_abcdef.png", "/ref/ref_20261007-120000_abcdef.png"):
            code, h, body = self.req("GET", path)
            self.assertEqual((code, h["content-type"], body), (200, "image/png", png), path)
        for path in ("/m/out/qwen2_t2i_00001_.png", "/out/qwen2_t2i_00001_.png", "/m/gen/qwen_studio_00001_.png",
                     "/m/out/..%2f..%2fetc%2fpasswd", "/m/out/%2e%2e", "/m/etc/passwd", "/out/.hidden"):
            code, _, _ = self.req("GET", path)
            self.assertIn(code, (400, 404), path)
        self.assertEqual(self.req("GET", "/m/out/qwen2_t2i_00001_.png")[0], 404)
        self.assertEqual(self.req("GET", "/m/out/%2e%2e%2fsecret")[0], 400)
        # owned but the file is gone
        store.put_sidecar(("gen", "img_00009_.png"), {"kind": "image"})
        self.assertEqual(self.req("GET", "/m/gen/img_00009_.png")[0], 404)

    def test_poster_only_through_parent(self):
        self.own("gen", "clip_00001.mp4", b"v", kind="clip", poster="clip_00001.png")
        self.r.put("gen", "clip_00001.png", make_png())
        self.r.put("gen", "clip_00002.png", make_png())
        self.assertEqual(self.req("GET", "/m/gen/clip_00001.png")[0], 200)
        self.assertEqual(self.req("GET", "/m/gen/clip_00002.png")[0], 404)
        self.assertEqual(self.req("GET", "/api/info/gen/clip_00001.png")[0], 404)   # not an asset itself
        self.assertEqual(self.req("POST", "/api/delete", {"ref": {"root": "gen", "name": "clip_00001.png"}})[0], 404)

    def test_symlink_is_refused(self):
        outside = os.path.join(self.r.base, "secret.txt")
        with open(outside, "w") as f:
            f.write("secret")
        os.symlink(outside, self.r.path("gen", "img_00001_.png"))
        store.put_sidecar(("gen", "img_00001_.png"), {"kind": "image"})
        code, _, body = self.req("GET", "/m/gen/img_00001_.png")
        self.assertEqual(code, 400)
        self.assertNotIn(b"secret", json.dumps(body).encode())

    def test_proxy_cache(self):
        key = "ab" * 32
        with open(store.data_path("cache", "proxy", key + ".mp4"), "wb") as f:
            f.write(b"proxy-bytes")
        code, h, body = self.req("GET", f"/m/cache/{key}.mp4", headers={"Range": "bytes=0-4"})
        self.assertEqual((code, body, h["content-range"]), (206, b"proxy", "bytes 0-4/11"))
        self.assertEqual(self.req("GET", f"/m/cache/{'cd' * 32}.mp4")[0], 404)
        self.assertEqual(self.req("GET", "/m/cache/abc.mp4")[0], 404)
        self.assertEqual(self.req("GET", "/m/cache/..%2f..%2fVERSION.mp4")[0], 404)

    def test_recent_tracks_info(self):
        self.own("gen", "img_00001_.png", make_png(), commercial_ok=False, created="2026-10-07T10:00:00Z",
                 probe={"w": 4, "h": 4})
        self.own("gen", "music_00001.flac", b"fLaC", kind="music", label="lofi", created="2026-10-07T11:00:00Z")
        code, _, rec = self.req("GET", "/api/recent")
        self.assertEqual(code, 200)
        self.assertEqual([x["ref"]["name"] for x in rec], ["img_00001_.png"])
        self.assertEqual(rec[0]["url"], "/m/gen/img_00001_.png")
        self.assertEqual(self.req("GET", "/api/recent?kind=video")[2], [])
        self.assertEqual(self.req("GET", "/api/recent?kind=bogus")[0], 400)
        self.assertEqual(self.req("GET", "/api/recent?limit=x")[0], 400)
        self.assertEqual([t["title"] for t in self.req("GET", "/api/tracks")[2]], ["lofi"])
        p = projects.create("Uses it")
        projects.save(p["id"], 1, dict(p, items=[{"id": "i_000001", "kind": "still",
                                                  "src": {"root": "gen", "name": "img_00001_.png"}}]))
        code, _, info = self.req("GET", "/api/info/gen/img_00001_.png")
        self.assertEqual((code, info["name"], info["root"], info["used_in"]), (200, "img_00001_.png", "gen", ["Uses it"]))
        self.assertEqual(self.req("GET", "/api/info/gen/nope.png")[0], 404)
        self.assertEqual(self.req("GET", "/api/info/etc/passwd")[0], 404)   # unknown root: no route
        for body in (rec, info):
            self.assertNotIn(self.r.base, json.dumps(body))


class UploadTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.ingested = []

        def ingest(stream, length, filename, ctype):
            data = stream.read(length)
            self.ingested.append((data, filename, ctype))
            if not data.startswith(b"\x89PNG"):
                raise Upload415("that file isn't a picture, video or audio file YourContent can use")
            return {"ref": {"root": "in", "name": "ref_x.png"}, "type": "image", "url": "/m/in/ref_x.png",
                    "poster": "/m/in/ref_x.png", "probe": {"w": 2, "h": 2}}

        p = mock.patch.object(actions, "ingest_upload", ingest)
        p.start()
        self.addCleanup(p.stop)

    def test_upload_passes_stream_and_label(self):
        png = make_png()
        code, _, body = self.req("POST", "/api/upload", raw=png, ctype="image/png",
                                 headers={"X-Filename": "My%20Holiday%0A.png"})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["ref"], {"root": "in", "name": "ref_x.png"})
        self.assertEqual(self.ingested[0], (png, "My Holiday.png", "image/png"))

    def test_upload_over_cap_is_413_before_reading(self):
        cfg_cap = self.srv.state.cfg.max_image_mb * 1024 * 1024
        code, _, body = self.req("POST", "/api/upload", raw=b"x" * 64, ctype="image/png",
                                 headers={"Content-Length": str(cfg_cap + 1)})
        self.assertEqual(code, 413)
        self.assertIn("30 MB", body["error"])
        self.assertEqual(self.ingested, [])
        code, _, body = self.req("POST", "/api/upload", raw=b"x" * 64, ctype="audio/mpeg",
                                 headers={"Content-Length": str(100 * 1024 * 1024 + 1)})
        self.assertEqual(code, 413)
        self.assertIn("100 MB", body["error"])

    def test_upload_needs_length_and_no_chunks(self):
        self.assertEqual(self.req("POST", "/api/upload", raw=b"", ctype="image/png")[0], 400)
        code, _, _ = self.req("POST", "/api/upload", raw=b"5\r\nhello\r\n0\r\n\r\n", ctype="image/png",
                              headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(code, 400)

    def test_low_disk_is_refused(self):
        with mock.patch.object(store, "free_gb", lambda root="lab": 1.0):
            code, _, body = self.req("POST", "/api/upload", raw=make_png(), ctype="image/png")
        self.assertEqual(code, 413)
        self.assertIn("disk", body["error"])

    def test_exe_named_png_is_415_via_status(self):
        code, _, body = self.req("POST", "/api/upload", raw=b"MZ\x90\x00" + b"\x00" * 60, ctype="image/png",
                                 headers={"X-Filename": "photo.png"})
        self.assertEqual(code, 415)
        self.assertIn("isn't a picture", body["error"])


class RealUploadTest(ServerCase):
    """The real actions.ingest_upload (WP3) and media.sniff (WP1); skipped while they are stubs."""

    def test_exe_named_png_is_415(self):
        exe = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 120
        with mock.patch.object(media, "probe", side_effect=AssertionError("must refuse before probing")):
            try:
                actions.ingest_upload(io.BytesIO(b""), 0, "x.png", "image/png")
            except NotImplementedError:
                self.skipTest("actions.ingest_upload is not built yet (WP3)")
            except Exception:  # noqa: BLE001 - built; it refused the empty body
                pass
            try:
                media.sniff(b"MZ")
            except NotImplementedError:
                self.skipTest("media.sniff is not built yet (WP1)")
            code, _, body = self.req("POST", "/api/upload", raw=exe, ctype="image/png",
                                     headers={"X-Filename": "photo.png"})
        self.assertEqual(code, 415, body)
        self.assertEqual(os.listdir(store.root_dir("in")), [])
        self.assertEqual([n for n in os.listdir(store.root_dir("lab"))], [])


class ActionRoutesTest(ServerCase):
    def test_generate_animate_extend_music_are_jobs(self):
        self.own("gen", "img_00001_.png", make_png())
        self.own("gen", "clip_00001.mp4", b"v", kind="clip")
        cases = [
            ("/api/generate", {"prompt": "a red fox", "steps": 30,
                               "refs": [{"root": "gen", "name": "img_00001_.png"}]},
             "generate"),
            ("/api/animate", {"start": {"root": "gen", "name": "img_00001_.png"}, "prompt": "", "seconds": "3",
                              "quality": "480p"}, "animate"),
            ("/api/extend", {"src": {"root": "gen", "name": "clip_00001.mp4"}, "prompt": "", "seconds": "5"},
             "extend"),
            ("/api/music", {"tags": "lofi", "seconds": 12}, "music"),
        ]
        for path, body, kind in cases:
            code, _, out = self.req("POST", path, body)
            self.assertEqual(code, 200, (path, out))
            self.assertRegex(out["job"], r"^j_[0-9a-f]{12}$")
            v = self.wait_job(out["job"])
            self.assertEqual((v["state"], v["kind"], v["result"]["kind"]), ("done", kind, kind), path)
            self.assertEqual(self.calls[-1], (kind, body))

    def test_known_model_and_size_are_accepted(self):
        _, _, cfg = self.req("GET", "/api/config")
        for vid in list(cfg["variants"]) + list(cfg["aliases"]):
            code, _, out = self.req("POST", "/api/generate", {"prompt": "x", "variant": vid, "size": "portrait"})
            self.assertEqual(code, 200, (vid, out))

    def test_validation(self):
        bad = [
            ("/api/generate", {"prompt": ""}, 400),
            ("/api/generate", {"prompt": "x" * 4001}, 400),
            ("/api/generate", {"prompt": "x", "steps": 100}, 400),
            ("/api/generate", {"prompt": "x", "refs": [{"root": "gen", "name": "nope.png"}]}, 404),
            ("/api/generate", {"prompt": "x", "refs": [{"root": "gen", "name": "../a"}]}, 400),
            ("/api/generate", {"prompt": "x", "refs": [{"root": "gen", "name": f"a{i}.png"} for i in range(7)]}, 400),
            ("/api/animate", {"start": {"root": "out", "name": "qwen_studio_00001_.png"}}, 404),
            ("/api/animate", {"start": None}, 400),
            ("/api/extend", {"src": {"root": "gen", "name": "x.mp4"}}, 404),
            ("/api/music", {"tags": ""}, 400),
            ("/api/music", {"tags": "x", "bpm": 5}, 400),
            ("/api/music", {"tags": "x", "seconds": 500}, 400),
            # audit 2026-10-11: an unknown model id ran the default model, an unknown size became square
            ("/api/generate", {"prompt": "x", "variant": "nope"}, 400),
            ("/api/generate", {"prompt": "x", "model": "nope"}, 400),
            ("/api/generate", {"prompt": "x", "size": "bogus"}, 400),
        ]
        for path, body, want in bad:
            code, _, out = self.req("POST", path, body)
            self.assertEqual(code, want, (path, body, out))
            self.assertIn("error", out)

    def test_music_length_defaults_to_the_cut(self):
        p = projects.create("Cut")
        projects.save(p["id"], 1, dict(p, items=[{"id": "i_1", "kind": "clip", "src": {"root": "gen", "name": "a.mp4"}}]))
        code, _, out = self.req("POST", "/api/music", {"tags": "lofi", "project_id": p["id"]})
        self.assertEqual(code, 200, out)
        self.wait_job(out["job"])
        self.assertEqual(self.calls[-1][1]["seconds"], 2.5)        # fake ace_seconds of 1 item
        code, _, out = self.req("POST", "/api/music", {"tags": "lofi"})
        self.wait_job(out["job"])
        self.assertEqual(self.calls[-1][1]["seconds"], 30.0)

    def test_comfy_off_is_503(self):
        self.comfy_up = False
        code, _, out = self.req("POST", "/api/generate", {"prompt": "x"})
        self.assertEqual(code, 503)
        self.assertIn("ComfyUI", out["error"])

    def test_queue_full_is_429(self):
        gate = threading.Event()
        with mock.patch.object(actions, "generate", lambda body, job: gate.wait(5) and {}):
            codes = [self.req("POST", "/api/generate", {"prompt": "x"})[0] for _ in range(jobs.MAX_QUEUED + 2)]
            gate.set()
        self.assertEqual(codes.count(429), 1)   # 1 running + 10 queued, then refused
        self.assertEqual(codes[-1], 429)

    def test_stub_action_is_501_job_error(self):
        with mock.patch.object(actions, "generate", side_effect=NotImplementedError("WP3")):
            out = self.req("POST", "/api/generate", {"prompt": "x"})[2]
            v = self.wait_job(out["job"])
        self.assertEqual((v["state"], v["error"]), ("error", "this feature isn't ready yet"))

    def test_job_errors_are_plain_and_have_no_paths(self):
        def boom(body, job):
            raise RuntimeError(f"could not read {self.r.path('gen', 'x.png')}")
        with mock.patch.object(actions, "generate", boom), contextlib.redirect_stderr(io.StringIO()) as err:
            out = self.req("POST", "/api/generate", {"prompt": "x"})[2]
            v = self.wait_job(out["job"])
        self.assertIn("RuntimeError", err.getvalue())     # the server log keeps the detail
        self.assertEqual(v["state"], "error")
        self.assertNotIn(self.r.base, v["error"])
        self.assertNotIn("/", v["error"].replace("x.png", ""))


class DeleteTest(ServerCase):
    def test_delete_route_adds_used_in(self):
        self.own("gen", "img_00001_.png", make_png())
        p = projects.create("Holiday")
        projects.save(p["id"], 1, dict(p, items=[{"id": "i_1", "kind": "still",
                                                  "src": {"root": "gen", "name": "img_00001_.png"}}]))
        seen = []
        with mock.patch.object(actions, "delete", lambda ref: seen.append(ref) or {"removed": [ref[1]],
                                                                                    "history_deleted": 1}):
            code, _, out = self.req("POST", "/api/delete", {"ref": {"root": "gen", "name": "img_00001_.png"}})
        self.assertEqual(code, 200)
        self.assertEqual(seen, [("gen", "img_00001_.png")])
        self.assertEqual(out, {"removed": ["img_00001_.png"], "history_deleted": 1, "used_in": ["Holiday"]})
        self.assertEqual(self.req("POST", "/api/delete", {"ref": {"root": "gen", "name": "nope.png"}})[0], 404)
        self.assertEqual(self.req("POST", "/api/delete", {"ref": "gen/img_00001_.png"})[0], 400)

    def test_every_delete_is_audited_on_stderr(self):
        """A delete leaves one stderr line (docker logs) with UTC time, ref and client address; so does a
        project delete. A refused ref (not owned) deletes nothing and is not logged as a delete."""
        import re as _re
        self.own("gen", "img_00001_.png", make_png())
        p = projects.create("Gone")
        err = io.StringIO()
        with mock.patch.object(actions, "delete", lambda ref: {"removed": [ref[1]], "history_deleted": 0}), \
                contextlib.redirect_stderr(err):
            self.assertEqual(self.req("POST", "/api/delete", {"ref": {"root": "gen", "name": "img_00001_.png"}})[0], 200)
            self.assertEqual(self.req("POST", "/api/delete", {"ref": {"root": "gen", "name": "nope.png"}})[0], 404)
            self.assertEqual(self.req("POST", f"/api/projects/{p['id']}/delete", {})[0], 200)
        lines = [l for l in err.getvalue().splitlines() if " audit " in l]
        self.assertEqual(len(lines), 2, err.getvalue())
        self.assertRegex(lines[0], r"^yourcontent: audit \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ delete gen/img_00001_\.png "
                                   r"client=127\.0\.0\.1 removed=1$")
        self.assertRegex(lines[1], rf"audit \S+Z delete-project {p['id']} client=127\.0\.0\.1 removed$")
        self.assertNotIn("nope.png", err.getvalue())

    def test_failed_delete_is_audited_too(self):
        self.own("gen", "img_00001_.png", make_png())
        err = io.StringIO()

        def boom(ref):
            raise OSError("disk")
        with mock.patch.object(actions, "delete", boom), contextlib.redirect_stderr(err):
            self.assertEqual(self.req("POST", "/api/delete", {"ref": {"root": "gen", "name": "img_00001_.png"}})[0], 500)
        self.assertRegex(err.getvalue(), r"audit \S+Z delete gen/img_00001_\.png client=\S+ failed \(OSError\)")

    def test_real_delete_forgets_only_ledger_pids(self):
        """actions.delete (WP3) with a FakeComfy: only this ref's ledger pids leave /history."""
        with FakeComfy(output_dir=self.r.output, input_dir=self.r.input) as fc:
            fc.add_history("ours-1", {"9": {"images": [{"filename": "img_00001_.png", "subfolder": "media_lab",
                                                         "type": "output"}]}})
            fc.add_history("ours-2", {"9": {"images": [{"filename": "img_00002_.png", "subfolder": "media_lab",
                                                         "type": "output"}]}})
            fc.add_history("other-tool", {"1": {"images": [{"filename": "img_00001_.png", "subfolder": "",
                                                             "type": "output"}]}})
            self.own("gen", "img_00001_.png", make_png())
            self.own("gen", "img_00002_.png", make_png())
            store.ledger_add("ours-1", "image", [("gen", "img_00001_.png")])
            store.ledger_add("ours-2", "image", [("gen", "img_00002_.png")])
            forgot = []
            real_forget = comfy.forget
            fn = getattr(comfy, "configure", None)
            with mock.patch.object(comfy, "request", self._passthrough(fc)), \
                    mock.patch.object(comfy, "forget", lambda pids: forgot.append(list(pids)) or real_forget(pids)):
                if callable(fn):
                    fn(self.r.config(comfy_url=fc.url))
                try:
                    code, _, out = self.req("POST", "/api/delete", {"ref": {"root": "gen", "name": "img_00001_.png"}})
                except Exception:  # noqa: BLE001
                    raise
                if code == 501:
                    self.skipTest("actions.delete is not built yet (WP3)")
            self.assertEqual(code, 200, out)
            self.assertEqual(forgot, [["ours-1"]])
            self.assertEqual(set(fc.history), {"ours-2", "other-tool"})
            self.assertFalse(os.path.exists(self.r.path("gen", "img_00001_.png")))
            self.assertTrue(os.path.exists(self.r.path("gen", "img_00002_.png")))
            self.assertFalse(fc.free_called)

    @staticmethod
    def _passthrough(fc):
        import urllib.request

        def request(path, payload=None, timeout=30):
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(fc.url + path, data, {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            return json.loads(body) if body.strip() else {}
        return request


class ProjectRoutesTest(ServerCase):
    def test_licence_row_lists_non_commercial_and_uploads_together(self):
        """Review r2: a cut with a Qwen-Image picture and an upload warns about both before export."""
        self.own("gen", "img_00001_.png", b"p", kind="image", commercial_ok=False, licences=["qwen-image-nc"],
                 most_restrictive="qwen-image-nc")
        self.own("in", "song.mp3", b"m", kind="upload", commercial_ok=None, licences=["upload"])
        proj = {"items": [{"id": "i_000001", "kind": "still", "src": {"root": "gen", "name": "img_00001_.png"}}],
                "music": {"src": {"root": "in", "name": "song.mp3"}}}
        lic = server.licence_summary(proj)
        self.assertIs(lic["commercial_ok"], False)
        self.assertEqual(lic["uploads"], 1)
        self.assertEqual(len(lic["warnings"]), 2, lic["warnings"])
        self.assertIn("non-commercial", lic["warnings"][0])
        self.assertIn("Includes uploads", lic["warnings"][1])

    def test_crud_and_conflict(self):
        self.own("out", "qwen_video_00001.mp4", b"v", kind="clip", commercial_ok=False)
        code, _, out = self.req("POST", "/api/projects", {"name": "First"})
        self.assertEqual(code, 200)
        proj, resolved = out["project"], out["resolved"]
        pid = proj["id"]
        self.assertEqual((proj["name"], proj["rev"], resolved["frames"]), ("First", 1, 0))
        self.assertEqual(resolved["licence"]["uploads"], 0)
        item = {"id": "i_9c2a1f", "kind": "clip", "src": {"root": "out", "name": "qwen_video_00001.mp4"},
                "in": 0, "out": None, "join": "fade"}
        missing = {"id": "i_0b7e44", "kind": "still", "src": {"root": "gen", "name": "gone.png"}, "dur": 2.5,
                   "snap": {"w": 10, "h": 10, "sar": "1/1", "commercial_ok": None}}
        code, _, out = self.req("POST", f"/api/projects/{pid}", {"rev": 1, "project": dict(proj, items=[item, missing])})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["rev"], 2)
        self.assertEqual(out["resolved"]["frames"], 120)
        self.assertEqual(out["resolved"]["warnings"], ["gone.png is missing; shown as black"])
        self.assertIs(out["resolved"]["licence"]["commercial_ok"], False)
        code, _, got = self.req("GET", f"/api/projects/{pid}")
        self.assertEqual(code, 200)
        saved = got["project"]["items"]
        # snap is rewritten from the probe for a present source and kept for a missing one
        self.assertEqual(saved[0]["snap"], {"frames": 49, "rate": "16/1", "w": 624, "h": 624, "sar": "1/1",
                                            "commercial_ok": False})
        self.assertEqual(saved[1]["snap"], missing["snap"])
        # stale rev
        code, _, out = self.req("POST", f"/api/projects/{pid}", {"rev": 1, "project": dict(proj, name="Stale")})
        self.assertEqual(code, 409)
        self.assertEqual(out["current"]["project"]["rev"], 2)
        self.assertIn("resolved", out["current"])
        self.assertIn("error", out)
        # list
        code, _, rows = self.req("GET", "/api/projects")
        self.assertEqual([(r["id"], r["items"], r["seconds"]) for r in rows], [(pid, 2, 5.0)])
        # validate errors and bad bodies
        with mock.patch.object(timeline, "validate", lambda p: (p, ["too many items (60 at most)"])):
            code, _, out = self.req("POST", f"/api/projects/{pid}", {"rev": 2, "project": proj})
        self.assertEqual((code, out["error"]), (400, "too many items (60 at most)"))
        self.assertEqual(self.req("POST", f"/api/projects/{pid}", {"rev": "2", "project": proj})[0], 400)
        self.assertEqual(self.req("POST", f"/api/projects/{pid}", {"rev": 2, "project": []})[0], 400)
        big = dict(proj, items=[dict(item, pad="x" * 300_000)])
        self.assertEqual(self.req("POST", f"/api/projects/{pid}", {"rev": 2, "project": big})[0], 413)
        # ids
        self.assertEqual(self.req("GET", "/api/projects/p_000000000000")[0], 404)
        self.assertEqual(self.req("GET", "/api/projects/nope")[0], 400)
        # delete leaves media alone
        before = self.r.snapshot(self.r.output)
        code, _, out = self.req("POST", f"/api/projects/{pid}/delete", {})
        self.assertEqual((code, out), (200, {"ok": True}))
        self.assertEqual(self.r.snapshot(self.r.output), before)
        self.assertEqual(self.req("GET", f"/api/projects/{pid}")[0], 404)

    def test_preview_export_jobs(self):
        out = self.req("POST", "/api/projects", {})[2]
        pid, proj = out["project"]["id"], out["project"]
        self.assertEqual(self.req("POST", f"/api/projects/{pid}/preview", {"rev": 1})[0], 400)   # empty cut
        item = {"id": "i_1", "kind": "clip", "src": {"root": "gen", "name": "a.mp4"}}
        rev = self.req("POST", f"/api/projects/{pid}", {"rev": 1, "project": dict(proj, items=[item])})[2]["rev"]
        self.assertEqual(self.req("POST", f"/api/projects/{pid}/preview", {"rev": 1})[0], 409)
        code, _, out = self.req("POST", f"/api/projects/{pid}/preview", {"rev": rev})
        self.assertEqual(code, 200)
        v = self.wait_job(out["job"])
        self.assertEqual((v["state"], v["kind"], v["result"]["mode"]), ("done", "preview", "proxy"))
        code, _, out = self.req("POST", f"/api/projects/{pid}/export", {"rev": rev})
        v = self.wait_job(out["job"])
        self.assertEqual((v["state"], v["kind"], v["result"]["mode"]), ("done", "export", "final"))
        self.assertEqual(self.calls[-1], ("render", pid, rev, "final"))

    def test_new_preview_supersedes_the_old_one(self):
        out = self.req("POST", "/api/projects", {})[2]
        pid, proj = out["project"]["id"], out["project"]
        item = {"id": "i_1", "kind": "clip", "src": {"root": "gen", "name": "a.mp4"}}
        rev = self.req("POST", f"/api/projects/{pid}", {"rev": 1, "project": dict(proj, items=[item])})[2]["rev"]
        started = threading.Event()

        def slow(project, rev, mode, job):
            started.set()
            for _ in range(500):
                if job.cancelled:
                    return {}
                time.sleep(0.01)
            return {"url": "late"}
        with mock.patch.object(export, "run_render", slow):
            first = self.req("POST", f"/api/projects/{pid}/preview", {"rev": rev})[2]["job"]
            started.wait(2)
            second = self.req("POST", f"/api/projects/{pid}/preview", {"rev": rev})[2]["job"]
            self.assertEqual(self.wait_job(first)["state"], "cancelled")
            jobs.cancel(second)
            self.assertEqual(self.wait_job(second)["state"], "cancelled")


class JobRoutesTest(ServerCase):
    def test_jobs_list_get_cancel(self):
        gate = threading.Event()
        with mock.patch.object(actions, "generate", lambda body, job: (job.progress(0.25, "sampling"),
                                                                        gate.wait(5), {})[2]):
            a = self.req("POST", "/api/generate", {"prompt": "a"})[2]["job"]
            b = self.req("POST", "/api/generate", {"prompt": "b"})[2]["job"]
            for _ in range(200):
                if jobs.get(a)["state"] == "running":
                    break
                time.sleep(0.01)
            listed = self.req("GET", "/api/jobs")[2]
            self.assertEqual([j["id"] for j in listed], [a, b])
            self.assertEqual([j["state"] for j in listed], ["running", "queued"])
            code, _, out = self.req("POST", f"/api/jobs/{b}/cancel", {})
            self.assertEqual((code, out), (200, {"state": "cancelled"}))
            gate.set()
            done = self.wait_job(a)
        self.assertEqual(done["state"], "done")
        code, _, got = self.req("GET", f"/api/jobs/{a}")
        self.assertEqual(set(got), {"id", "kind", "state", "phase", "progress", "label", "result", "error"})
        self.assertEqual((got["progress"], got["label"]), (1.0, "a"))
        self.assertEqual(self.req("GET", "/api/jobs/j_000000000000")[2]["state"], "error")   # lost in a restart
        self.assertEqual(self.req("GET", "/api/jobs/nope")[0], 400)
        self.assertEqual(self.req("POST", "/api/jobs/nope/cancel", {})[0], 400)


class ConfigHealthTest(ServerCase):
    def test_config_shape(self):
        code, _, c = self.req("GET", "/api/config")
        self.assertEqual(code, 200)
        self.assertEqual((c["app"], c["consent_version"], c["auth"]), ("YourContent", 1, False))
        self.assertEqual(c["variants"], {"int8": "int8 · balanced"})
        self.assertEqual((c["default_variant"], c["aliases"]), ("int8", {"old-int8": "int8"}))
        self.assertEqual(c["sizes"], ["square", "portrait", "landscape", "wide", "tall"])
        self.assertEqual(c["presets"][0], {"id": "9:16", "label": "9:16 Reels / Stories", "w": 1080, "h": 1920})
        self.assertEqual([p["id"] for p in c["presets"]], ["9:16", "4:5", "1:1", "16:9", "source"])
        self.assertEqual(c["presets"][-1], {"id": "source", "label": "Source shape"})
        self.assertEqual(c["limits"], {"image_mb": 30, "video_mb": 500, "audio_mb": 100})
        self.assertIn({"model": "int8 · balanced", "licence": "Qwen-Image 2.1 licence (non-commercial research)",
                       "commercial_ok": False}, c["licences"])
        self.assertEqual(c["licences"][0], {"model": "YourContent", "licence": "PolyForm Small Business 1.0.0", "commercial_ok": True})

    def test_config_with_the_real_model_table(self):
        with mock.patch.object(models, "load", ORIG_MODELS_LOAD):
            self.srv.state.models = None
            code, _, c = self.req("GET", "/api/config")
        self.assertEqual(code, 200)
        self.assertTrue(c["variants"], c)
        self.assertIn(c["default_variant"], c["variants"])
        self.assertTrue(any(row["commercial_ok"] is False for row in c["licences"]))
        self.assertNotIn(self.r.base, json.dumps(c))

    def test_config_survives_a_missing_model_table(self):
        with mock.patch.object(models, "load", side_effect=NotImplementedError):
            self.srv.state.models = None
            code, _, c = self.req("GET", "/api/config")
        self.assertEqual((code, c["variants"]), (200, {}))

    def test_health_cached_and_recheck(self):
        code, _, hl = self.req("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertEqual(hl["comfy"], {"ok": True, "version": "0.37.0"})
        self.assertTrue(hl["features"]["picture"]["ok"])
        n = len(self.calls)
        self.req("GET", "/api/health")
        self.assertEqual(len(self.calls), n)          # cached
        self.comfy_up = False
        hl = self.req("GET", "/api/health?recheck=1")[2]
        self.assertEqual(hl["comfy"]["ok"], False)
        self.assertEqual(hl["comfy"]["error"], "ComfyUI not reachable")
        self.assertFalse(hl["features"]["picture"]["ok"])

    def test_health_names_the_models_json_it_read(self):
        """/api/health shows the fingerprint of the models.json the loader read (models.load sets it)."""
        seen = {"sha256": "ab" * 32, "bytes": 27, "mtime": "2026-10-07T00:00:00Z"}
        real_load = models.load

        def load(cfg=None, **kw):
            m = real_load(cfg, **kw)
            m.local_file = seen
            return m
        with mock.patch.object(models, "load", load):
            hl = self.req("GET", "/api/health?recheck=1")[2]
        self.assertEqual(hl["models_json"], seen)
        hl = self.req("GET", "/api/health?recheck=1")[2]   # FakeModels: no local file
        self.assertEqual(hl["models_json"], {"loaded": False})

    def test_health_when_features_are_not_built(self):
        with mock.patch.object(features, "compute", side_effect=NotImplementedError):
            hl = self.req("GET", "/api/health?recheck=1")[2]
        self.assertEqual(hl["features"]["music"]["ok"], False)
        self.assertEqual(hl["features"]["music"]["missing"], ["not ready yet"])


class StartupTest(unittest.TestCase):
    def test_non_loopback_without_token_refuses_to_start(self):
        with TempRoots() as r:
            for host in ("0.0.0.0", "192.0.2.10", "::"):
                with self.subTest(host=host), self.assertRaises(server.StartupError) as cm:
                    server.make_server(r.config(host=host), adopt=False)
                self.assertIn("YOURCONTENT_TOKEN", str(cm.exception))
            with self.assertRaises(server.StartupError):
                server.make_server(r.config(require_token="1"), adopt=False)
            # explicit opt-out (the compose file: 0.0.0.0 inside, published on 127.0.0.1)
            self.assertIsNone(config.startup_problem(r.config(host="0.0.0.0", require_token="0")))
            self.assertIsNone(config.startup_problem(r.config(host="0.0.0.0", token="long-enough-token")))
            self.assertIsNone(config.startup_problem(r.config(host="127.0.0.1")))
            self.assertIsNone(config.startup_problem(r.config(host="localhost")))

    def test_published_host_decides_auto(self):
        """Review r2: in a container the bind is 0.0.0.0; "auto" judges the host-side publish address."""
        with TempRoots() as r:
            self.assertIsNone(config.startup_problem(r.config(host="0.0.0.0", published_host="127.0.0.1")))
            for pub in ("0.0.0.0", "192.0.2.10", "::"):
                with self.subTest(pub=pub):
                    self.assertIn("YOURCONTENT_TOKEN",
                                  config.startup_problem(r.config(host="0.0.0.0", published_host=pub)) or "")
            self.assertIsNone(config.startup_problem(r.config(host="0.0.0.0", published_host="0.0.0.0",
                                                              token="long-enough-token")))
            # a token-less non-loopback bind always prints a loud warning; a loopback bind or a token does not
            self.assertIn("NO token", config.startup_warning(r.config(host="0.0.0.0", published_host="127.0.0.1")))
            self.assertIn("NO token", config.startup_warning(r.config(host="0.0.0.0", require_token="0")))
            self.assertIsNone(config.startup_warning(r.config(host="127.0.0.1")))
            self.assertIsNone(config.startup_warning(r.config(host="0.0.0.0", token="long-enough-token")))

    def test_shipped_compose_keeps_the_token_check(self):
        """Review r2: compose.yaml must not switch the check off, and widening YOURCONTENT_PUBLISH (or the older
        MEDIA_LAB_PUBLISH) must trip it."""
        import re as _re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "compose.yaml"), encoding="utf-8") as f:
            text = f.read()
        block = text.split("environment:", 1)[1].split("extra_hosts:", 1)[0]
        raw = dict(_re.findall(r"^\s+((?:YOURCONTENT|MEDIA_LAB)_[A-Z_]+):\s*(.*?)\s*$", block, _re.M))
        for k in ("YOURCONTENT_REQUIRE_TOKEN", "MEDIA_LAB_REQUIRE_TOKEN"):
            self.assertNotIn(raw.get(k, "").strip('"'), ("0", "false", "no"))
        self.assertIn("${YOURCONTENT_PUBLISH:-${MEDIA_LAB_PUBLISH:-127.0.0.1}}:8190:8190", text)

        def expand(v, env):
            while True:
                m = _re.search(r"\$\{([A-Z_]+):-((?:[^${}]|\$\{(?:[^${}]|\$\{[^}]*\})*\})*)\}", v)
                if not m:
                    return v.strip('"')
                v = v[:m.start()] + (env.get(m.group(1)) or expand(m.group(2), env)) + v[m.end():]

        def startup(user_env, override=None):
            with TempRoots() as r:
                env = {k: expand(v, user_env) for k, v in raw.items()}
                env.update(override or {})
                env.update(YOURCONTENT_DATA=r.data, YOURCONTENT_COMFY_OUTPUT=r.output, YOURCONTENT_UPLOADS=r.uploads)
                return config.startup_problem(config.load(env))
        self.assertIsNone(startup({}))
        for pre in ("YOURCONTENT_", "MEDIA_LAB_"):
            self.assertIn("YOURCONTENT_TOKEN", startup({pre + "PUBLISH": "0.0.0.0"}) or "")
            self.assertIn("YOURCONTENT_TOKEN", startup({pre + "PUBLISHED_HOST": "192.0.2.10"}) or "")
            self.assertIsNone(startup({pre + "PUBLISH": "0.0.0.0", pre + "TOKEN": "long-enough-token"}))
        # an older compose.override.yaml that sets the old names in `environment:` still wins over the defaults
        self.assertIsNone(startup({}, {"MEDIA_LAB_PUBLISHED_HOST": "192.0.2.10", "MEDIA_LAB_REQUIRE_TOKEN": "0"}))
        self.assertIsNone(startup({}, {"YOURCONTENT_PUBLISHED_HOST": "192.0.2.10", "YOURCONTENT_REQUIRE_TOKEN": "0"}))
        env = {k: expand(v, {}) for k, v in raw.items()}
        env.update(MEDIA_LAB_COMFY_URL="http://192.0.2.5:8188")
        self.assertEqual(config.load(env).comfy_url, "http://192.0.2.5:8188")
        self.assertEqual(config.load({k: expand(v, {}) for k, v in raw.items()}).comfy_url,
                         "http://host.docker.internal:8188")

    def test_main_exits_with_a_plain_message(self):
        with TempRoots() as r:
            env = dict(r.env, MEDIA_LAB_HOST="0.0.0.0")
            err = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    server.main()
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("won't listen on 0.0.0.0 without a token", err.getvalue())

    def test_start_empties_tmp_and_adopts_when_enabled(self):
        with TempRoots() as r:
            store.configure(r.config())
            junk = store.tmp_path(".part")
            with open(junk, "w") as f:
                f.write("x")
            seen = []
            with mock.patch.object(legacy, "adopt_all", lambda cfg, st, cm=None: seen.append((st, cm)) or
                                   {"adopted": [1, 2], "skipped": [], "manifest": []}), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                srv = server.make_server(r.config(adopt_legacy=True))
                srv.server_close()
            self.assertFalse(os.path.exists(junk))
            self.assertEqual(seen, [(store, comfy)])
            self.assertIn("adopted this start: 2", out.getvalue())
            # adoption failures never stop the server
            with mock.patch.object(legacy, "adopt_all", side_effect=RuntimeError("boom")), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                server.make_server(r.config(adopt_legacy=True)).server_close()
            self.assertIn("legacy adoption failed", err.getvalue())


class ConfigLoadTest(unittest.TestCase):
    def test_defaults(self):
        c = config.load({})
        self.assertEqual((c.host, c.port, c.comfy_url, c.data, c.input_subdir, c.output_subdir),
                         ("127.0.0.1", 8190, "http://127.0.0.1:8188", "./data/lab", "media_lab", "yourcontent"))
        self.assertEqual((c.token, c.require_token, c.allowed_hosts, c.adopt_legacy), (None, "auto", (), False))
        self.assertEqual((c.max_image_mb, c.max_video_mb, c.max_audio_mb, c.min_free_gb), (30, 500, 100, 2))

    def test_new_names_and_aliases(self):
        old = {"COMFY_URL": "http://old:8188/", "STUDIO_HOST": "0.0.0.0", "STUDIO_PORT": "9000",
               "COMFY_OUTPUT": "/o", "STUDIO_UPLOADS": "/u"}
        c = config.load(old)
        self.assertEqual((c.comfy_url, c.host, c.port, c.comfy_output, c.uploads),
                         ("http://old:8188", "0.0.0.0", 9000, "/o", "/u"))
        new = dict(old, MEDIA_LAB_COMFY_URL="http://new:8188", MEDIA_LAB_HOST="127.0.0.1", MEDIA_LAB_PORT="8191",
                   MEDIA_LAB_COMFY_OUTPUT="/o2", MEDIA_LAB_UPLOADS="/u2", MEDIA_LAB_DATA="/d",
                   MEDIA_LAB_INPUT_SUBDIR="qwen_studio", MEDIA_LAB_ADOPT_LEGACY="1",
                   MEDIA_LAB_ALLOWED_HOSTS=" Box.Example.test:8190 , 192.0.2.1:8190 ,",
                   MEDIA_LAB_MAX_VIDEO_MB="800", MEDIA_LAB_REQUIRE_TOKEN="1")
        c = config.load(new)
        self.assertEqual((c.comfy_url, c.host, c.port, c.comfy_output, c.uploads, c.data, c.input_subdir),
                         ("http://new:8188", "127.0.0.1", 8191, "/o2", "/u2", "/d", "qwen_studio"))
        self.assertEqual(c.allowed_hosts, ("box.example.test:8190", "192.0.2.1:8190"))
        self.assertEqual((c.adopt_legacy, c.max_video_mb, c.require_token), (True, 800, "1"))

    def test_token_and_file_never_printed(self):
        with TempRoots() as r:
            tf = os.path.join(r.base, "token")
            with open(tf, "w") as f:
                f.write("from-a-file-123\n")
            c = config.load({"MEDIA_LAB_TOKEN_FILE": tf})
            self.assertEqual(c.token, "from-a-file-123")
            self.assertEqual(config.load({"MEDIA_LAB_TOKEN": "direct-token-1", "MEDIA_LAB_TOKEN_FILE": tf}).token,
                             "direct-token-1")
            self.assertNotIn("from-a-file-123", repr(c))
            self.assertEqual(config.describe(c)["token"], "set")
            self.assertIsNone(config.load({"MEDIA_LAB_TOKEN": ""}).token)
            with self.assertRaises(config.ConfigError):
                config.load({"MEDIA_LAB_TOKEN_FILE": os.path.join(r.base, "missing")})
            with self.assertRaises(config.ConfigError):
                config.load({"MEDIA_LAB_TOKEN": "short"})

    def test_bad_values_are_plain_errors(self):
        for env in ({"MEDIA_LAB_PORT": "x"}, {"MEDIA_LAB_PORT": "70000"}, {"MEDIA_LAB_REQUIRE_TOKEN": "maybe"},
                    {"MEDIA_LAB_INPUT_SUBDIR": "../x"}, {"MEDIA_LAB_OUTPUT_SUBDIR": "a/b"},
                    {"MEDIA_LAB_COMFY_URL": "ftp://x"}, {"MEDIA_LAB_COMFY_URL": "http://x/path"},
                    {"MEDIA_LAB_ADOPT_LEGACY": "perhaps"}, {"MEDIA_LAB_MAX_IMAGE_MB": "0"}):
            with self.subTest(env=env), self.assertRaises(config.ConfigError):
                config.load(env)

    def test_is_loopback(self):
        for h in ("127.0.0.1", "127.1.2.3", "localhost", "::1", "[::1]", "LOCALHOST"):
            self.assertTrue(config.is_loopback(h), h)
        for h in ("0.0.0.0", "::", "", "192.0.2.1", "localhost.example.test", "10.0.0.1"):
            self.assertFalse(config.is_loopback(h), h)


class JobsUnitTest(unittest.TestCase):
    def setUp(self):
        jobs.reset()
        self.addCleanup(jobs.reset)

    def test_one_worker_per_queue_runs_in_order(self):
        order, lock = [], threading.Lock()

        def fn(tag):
            def run(job):
                with lock:
                    order.append(("start", tag))
                time.sleep(0.05)
                with lock:
                    order.append(("end", tag))
                return {"tag": tag}
            return run
        ids = [jobs.submit("gpu", "t", fn(i), label=str(i)) for i in range(3)]
        for jid in ids:
            self.assertEqual(jobs.wait(jid, 5)["state"], "done")
        self.assertEqual(order, [(e, i) for i in range(3) for e in ("start", "end")])
        self.assertEqual(jobs.get(ids[2])["result"], {"tag": 2})
        with self.assertRaises(ValueError):
            jobs.submit("cpu", "t", fn(9), label="x")

    def test_cancel_kills_the_process_group(self):
        import subprocess
        import sys
        started = threading.Event()
        procs = []

        def run(job):
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            procs.append(p)
            job.set_proc(p)
            started.set()
            p.wait()
            return {}
        jid = jobs.submit("ffmpeg", "render", run, label="r")
        started.wait(5)
        jobs.cancel(jid)
        self.assertEqual(jobs.wait(jid, 5)["state"], "cancelled")
        self.assertIsNotNone(procs[0].poll())

    def test_cancel_calls_comfy_cancel_for_registered_pids(self):
        stopped = []
        gate = threading.Event()

        def run(job):
            job.set_comfy("pid-1")
            gate.wait(5)
            return {}
        with mock.patch.object(comfy, "cancel", lambda pid: stopped.append(pid)):
            jid = jobs.submit("gpu", "generate", run, label="g")
            for _ in range(200):
                if jobs.get(jid)["state"] == "running":
                    break
                time.sleep(0.01)
            time.sleep(0.05)
            jobs.cancel(jid)
            gate.set()
            self.assertEqual(jobs.wait(jid, 5)["state"], "cancelled")
        self.assertEqual(stopped, ["pid-1"])

    def test_job_finished_cancelled_never_flips_back_to_running(self):
        """Worker/cancel race (review r2): a job that cancel()/supersede finished as "cancelled" while the
        worker was dequeuing it must not be set "running" and executed. Modelled by a job whose state is
        already "cancelled" when the worker reaches it, with the cancel flag not yet visible."""
        gate, ran = threading.Event(), []
        blocker = jobs.submit("ffmpeg", "t", lambda job: gate.wait(5) and {}, label="block")
        victim = jobs.submit("ffmpeg", "t", lambda job: ran.append(1) or {}, label="victim")
        with jobs._lock:
            v = jobs._jobs[victim]
        fn = v._fn
        jobs._finish(v, "cancelled")
        v._fn = fn                      # the worker had already read fn when the cancel landed
        gate.set()
        self.assertEqual(jobs.wait(blocker, 5)["state"], "done")
        time.sleep(0.1)
        self.assertEqual(ran, [])
        self.assertEqual(jobs.get(victim)["state"], "cancelled")
        self.assertNotIn(victim, [j["id"] for j in jobs.active()])

    def test_cancel_of_a_queued_job_is_atomic_with_the_worker(self):
        """cancel() and the worker's queued->running step take the same lock: exactly one side wins."""
        for _ in range(50):
            ran = []
            jid = jobs.submit("ffmpeg", "t", lambda job: ran.append(1) or {}, label="x")
            state = jobs.cancel(jid)
            v = jobs.wait(jid, 5)
            if ran:
                self.assertIn(v["state"], ("done", "cancelled"))
            else:
                self.assertEqual((state, v["state"]), ("cancelled", "cancelled"))

    def test_late_cancel_after_commit_reports_done(self):
        """A cancel that arrives after the result was kept cannot undo it: the job says "done", not "cancelled"."""
        committed, gate = threading.Event(), threading.Event()

        def run(job):
            job.commit()
            committed.set()
            gate.wait(5)
            return {"ref": "kept"}
        jid = jobs.submit("ffmpeg", "export", run, label="x")
        committed.wait(5)
        jobs.cancel(jid)
        gate.set()
        v = jobs.wait(jid, 5)
        self.assertEqual((v["state"], v["result"]), ("done", {"ref": "kept"}))

    def test_cancel_before_commit_discards(self):
        started, gate, after = threading.Event(), threading.Event(), []

        def run(job):
            started.set()
            gate.wait(5)
            job.commit()
            after.append("kept")
            return {}
        jid = jobs.submit("ffmpeg", "export", run, label="x")
        started.wait(5)
        jobs.cancel(jid)
        gate.set()
        self.assertEqual(jobs.wait(jid, 5)["state"], "cancelled")
        self.assertEqual(after, [])

    def test_superseded_queued_jobs_finish_at_once(self):
        gate = threading.Event()
        blocker = jobs.submit("ffmpeg", "export", lambda job: (gate.wait(5), {})[1], label="long")
        try:
            ids = [jobs.submit("ffmpeg", "preview", lambda job: {}, label="p", supersede="preview:p_1")
                   for _ in range(jobs.MAX_QUEUED + 5)]           # never QueueFull: superseded ones are finished
            for jid in ids[:-1]:
                self.assertEqual(jobs.get(jid)["state"], "cancelled")
                self.assertIsNone(jobs._jobs[jid]._fn)            # the closure (and its project) is released
            self.assertEqual(jobs.get(ids[-1])["state"], "queued")
            self.assertEqual([v["id"] for v in jobs.active()], [blocker, ids[-1]])
        finally:
            gate.set()
        self.assertEqual(jobs.wait(ids[-1], 5)["state"], "done")

    def test_progress_is_clamped(self):
        def run(job):
            job.progress(7, "x")
            seen.append(jobs.get(job.id)["progress"])
            job.progress(None, "y")
            seen.append(jobs.get(job.id)["progress"])
            return {}
        seen: list = []
        jobs.wait(jobs.submit("gpu", "t", run, label="p"), 5)
        self.assertEqual(seen, [1.0, None])


class CliTest(unittest.TestCase):
    def test_manifest_and_verify(self):
        from medialab.legacy import Legacy
        with TempRoots(input_subdir="qwen_studio") as r:
            r.put("out", "qwen_studio_00001_.png", make_png(), mtime=1_700_000_000)
            r.put("out", "qwen_video_00001.mp4", b"video", mtime=1_700_000_100)
            r.put("out", "qwen_video_00001.png", make_png(), mtime=1_700_000_100)
            r.put("out", "someone_else.png", b"x")
            found = [Legacy("qwen_studio", "out", "qwen_studio_00001_.png", "image", None),
                     Legacy("qwen_video", "out", "qwen_video_00001.mp4", "clip", "qwen_video_00001.png")]
            cfg = r.config()
            with mock.patch.object(legacy, "scan", lambda c: found):
                m = cli.manifest(cfg)
                self.assertEqual((m["count"], m["output_entries"]), (3, 4))   # media_lab/ subfolder not counted
                self.assertEqual([f["kind"] for f in m["files"]], ["image", "clip", "poster"])
                self.assertEqual(cli.verify(cfg, m), [])
                before = r.snapshot()
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.cmd_migrate(cfg, dry_run=True, verify_path=None), 0)
                self.assertEqual(json.loads(out.getvalue())["count"], 3)
                self.assertEqual(r.snapshot(), before)      # dry run changes nothing
                os.utime(r.path("out", "qwen_video_00001.png"), (1, 1))
                r.put("out", "new_file.png", b"y")
                problems = cli.verify(cfg, m)
                self.assertEqual(problems, ["qwen_video_00001.png: mtime changed",
                                            "output folder entries: 4 before, 5 now"])

    def test_check_runs_with_comfy_off(self):
        with TempRoots() as r:
            out = io.StringIO()
            with mock.patch.object(comfy, "request", side_effect=OSError("refused")), \
                    mock.patch.object(models, "load", lambda cfg: FakeModels()), \
                    mock.patch.object(media, "ffmpeg_caps", lambda: {"version": "7.1.5", "missing_filters": []}), \
                    mock.patch.object(features, "compute", lambda i, m, f: {"export": {"ok": True, "missing_filters": []}}), \
                    mock.patch.object(legacy, "scan", lambda c: []), \
                    contextlib.redirect_stdout(out):
                code = cli.cmd_check(r.config(token="secret-token-xyz"))
            text = out.getvalue()
            self.assertEqual(code, 1)
            self.assertIn("ComfyUI: not reachable", text)
            self.assertIn("token: set", text)
            self.assertNotIn("secret-token-xyz", text)
            self.assertIn("legacy files: none", text)

    def test_dump_fixture_reduces_enums(self):
        infos = {
            "UNETLoader": {"input": {"required": {"unet_name": [["a.safetensors", "private-finetune.safetensors"], {}],
                                                  "weight_dtype": [["default", "fp8"], {}]}}},
            "LoadImage": {"input": {"required": {"image": [["holiday.png", "sub/x.png"], {"image_upload": True}]}}},
            "LoraLoaderModelOnly": {"input": {"required": {"lora_name": [["wan_high.safetensors", "x.safetensors"], {}]}}},
            "SomethingElse": {"input": {}},
        }

        class M(FakeModels):
            video = {"lora": "wan_{}.safetensors", "licence": "apache-2.0"}
        with TempRoots() as r, mock.patch.object(features, "fixture_subset", None):
            path = cli.dump_object_info(infos, M(), r.base, "0.37.0")
            with open(path) as f:
                got = json.load(f)
        self.assertEqual(os.path.basename(path), "object_info_0.37.0.json")
        self.assertEqual(set(got), {"UNETLoader", "LoadImage", "LoraLoaderModelOnly"})
        self.assertEqual(got["UNETLoader"]["input"]["required"]["unet_name"][0], ["a.safetensors"])
        self.assertEqual(got["UNETLoader"]["input"]["required"]["weight_dtype"][0], ["default", "fp8"])
        self.assertEqual(got["LoadImage"]["input"]["required"]["image"][0], [])
        self.assertEqual(got["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0], ["wan_high.safetensors"])


class RealFlowTest(unittest.TestCase):
    """No fakes except ComfyUI: upload a picture, make a cut, preview it, export it, fetch it with Range,
    delete it. Needs ffmpeg 7.1 (run in the container)."""

    @requires_ffmpeg((7, 1))
    def test_upload_cut_preview_export_delete(self):
        r = TempRoots()
        self.addCleanup(r.cleanup)
        jobs.reset()
        self.addCleanup(jobs.reset)
        fc = FakeComfy(output_dir=r.output, input_dir=r.input).start()
        self.addCleanup(fc.stop)
        srv = server.make_server(r.config(comfy_url=fc.url), adopt=False)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"127.0.0.1:{srv.server_address[1]}"

        def req(method, path, body=None, raw=None, headers=None):
            conn = http.client.HTTPConnection(base, timeout=60)
            h = {"X-Media-Lab": "1"} if method == "POST" else {}
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            if body is not None:
                h["Content-Type"] = "application/json"
            h.update(headers or {})
            conn.request(method, path, data, h)
            resp = conn.getresponse()
            out = resp.read()
            hd = {k.lower(): v for k, v in resp.getheaders()}
            conn.close()
            return resp.status, hd, (json.loads(out) if hd.get("content-type", "").startswith("application/json")
                                     and out else out)

        png = make_png(w=64, h=48, rgb=(200, 40, 40))
        code, _, up = req("POST", "/api/upload", raw=png, headers={"Content-Type": "image/png",
                                                                   "X-Filename": "red%20box.png"})
        self.assertEqual(code, 200, up)
        ref = up["ref"]
        self.assertEqual(ref["root"], "in")
        rec = req("GET", "/api/recent")[2]
        self.assertEqual([x["ref"] for x in rec], [ref])

        out = req("POST", "/api/projects", {"name": "Test cut"})[2]
        proj = out["project"]
        proj["items"] = [{"id": "i_000001", "kind": "still", "src": ref, "dur": 1.0, "join": "cut"}]
        proj["export"] = {"preset": "1:1", "fit": "fill", "fps": 24, "fade": 0.5}
        code, _, saved = req("POST", f"/api/projects/{proj['id']}", {"rev": 1, "project": proj})
        self.assertEqual(code, 200, saved)
        self.assertEqual((saved["rev"], saved["resolved"]["frames"]), (2, 24))
        stored = req("GET", f"/api/projects/{proj['id']}")[2]["project"]
        self.assertEqual(stored["items"][0]["snap"]["w"], 64)

        code, _, pj = req("POST", f"/api/projects/{proj['id']}/preview", {"rev": 2})
        self.assertEqual(code, 200, pj)
        v = jobs.wait(pj["job"], 120)
        self.assertEqual(v["state"], "done", v)
        code, h, body = req("GET", v["result"]["url"], headers={"Range": "bytes=0-99"})
        self.assertEqual((code, len(body)), (206, 100))
        self.assertTrue(h["content-range"].startswith("bytes 0-99/"))

        code, _, ej = req("POST", f"/api/projects/{proj['id']}/export", {"rev": 2})
        v = jobs.wait(ej["job"], 180)
        self.assertEqual(v["state"], "done", v)
        cut = v["result"]["ref"]
        self.assertEqual(cut["root"], "lab")
        self.assertEqual(req("GET", "/api/recent?kind=export")[2][0]["ref"], cut)
        code, h, body = req("GET", f"/m/lab/{cut['name']}?dl=1")
        self.assertEqual(code, 200)
        self.assertIn("attachment", h["content-disposition"])
        self.assertEqual(body[4:8], b"ftyp")
        info = req("GET", f"/api/info/lab/{cut['name']}")[2]
        self.assertEqual(info["kind"], "export")

        code, _, d = req("POST", "/api/delete", {"ref": cut})
        self.assertEqual(code, 200, d)
        self.assertIn(cut["name"], d["removed"])
        self.assertEqual(req("GET", f"/m/lab/{cut['name']}")[0], 404)
        for blob in (up, saved, v, d, info):
            self.assertNotIn(r.base, json.dumps(blob))
        self.assertFalse(fc.free_called)


if __name__ == "__main__":
    unittest.main()
