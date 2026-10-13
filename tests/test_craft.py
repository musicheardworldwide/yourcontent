"""Craft service: the shared spec, brand kits, the runner's queue, safety and provenance rules, the sandbox wrapper's
refusals, and the HTTP routes against a fake craft service. Stdlib only; no craft app runs (recipes are faked).

A real run of every recipe is opt-in: tests/e2e_craft.py (needs the four apps built and bubblewrap).
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from craft import craftd, mcp_client
from medialab import craft_routes, craft_spec, provenance, server, store
from tests.util import ROOT, TempRoots

JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xc0\x00\x11\x08\x02\x00\x03\x00" + b"\x00" * 64  # 768x512 header
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1080).to_bytes(4, "big") + (1920).to_bytes(4, "big")
       + b"\x08\x02\x00\x00\x00" + b"\x00" * 16)
KIT = {"name": "Studio", "colors": {"bg": "#0b1d3a", "fg": "#ffffff", "accent": "#e0b84a"},
       "fonts": {"display": "Playfair Display", "body": "Inter"}, "handle": "@studio"}
WRAPPER = os.path.join(ROOT, "craft", "craft-mcp")


class CraftCase(unittest.TestCase):
    """Temp roots, a configured store, and fake app binaries (never executed) so the runner sees them as built."""

    def setUp(self):
        self.r = TempRoots()
        self.addCleanup(self.r.cleanup)
        store.configure(self.r.config())
        self.bin = os.path.join(self.r.base, "bin")
        os.makedirs(self.bin)
        for a in craftd.APPS:
            p = os.path.join(self.bin, f"{a}-cli")
            with open(p, "w") as f:
                f.write("#!/bin/sh\nexit 1\n")
            os.chmod(p, 0o755)
        env = {"CRAFT_BIN_DIR": self.bin, "CRAFT_WORK_ROOT": os.path.join(self.r.base, "work")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("CRAFT_SRC", "CRAFT_VERSIONS_DIR", "MEDIA_LAB_CRAFT_AI_MASKS", "MEDIA_LAB_CRAFT_SAM3_DIR"):
            os.environ.pop(k, None)
        os.makedirs(craftd.queue_dir(), exist_ok=True)
        os.makedirs(craftd.jobs_dir(), exist_ok=True)

    def own(self, root, name, data=JPG, ok=None, licences=("unknown",), kind="image"):
        """A file YourContent owns, with a sidecar carrying this licence."""
        self.r.put(root, name, data)
        ref = (root, name)
        store.put_sidecar(ref, {"kind": kind, "commercial_ok": ok, "licences": list(licences),
                                "most_restrictive": list(licences)[0] if licences else None})
        return {"root": root, "name": name}

    def upload(self, name="photo.jpg", data=JPG):
        """A file in the uploads root with no sidecar: allowed, rights not checked."""
        self.r.put("in", name, data)
        return {"root": "in", "name": name}

    def v(self, recipe, params, brand=None):
        return craft_spec.validate({"recipe": recipe, "params": params, "brand": brand})

    def queue(self, params=None, recipe="photo.grade", brand=None):
        return craft_routes.enqueue({"recipe": recipe, "params": params or {"image": self.upload()},
                                     "brand": brand})["job"]


# --------------------------------------------------------------------------------------------- spec

class SpecTest(CraftCase):
    def test_grade_look_follows_the_brand_kit(self):
        up = self.upload()
        j = self.v("photo.grade", {"image": up}, KIT)
        self.assertEqual(j["params"]["look"], "brand")
        self.assertEqual(j["params"]["develop"]["grading.highlights.hue"], 44)  # the accent's hue
        self.assertEqual(j["inputs"], [{"role": "source", "ref": ["in", "photo.jpg"]}])
        self.assertEqual(self.v("photo.grade", {"image": up})["params"]["look"], "neutral")

    def test_unknown_recipe_and_brand_look_without_kit(self):
        with self.assertRaises(craft_spec.SpecError):
            craft_spec.validate({"recipe": "shell"})
        with self.assertRaises(craft_spec.SpecError):
            self.v("photo.grade", {"image": self.upload(), "look": "brand"})

    def test_refs_cannot_escape_their_root(self):
        self.r.put("lab", "x.jpg", JPG)   # exists but not owned
        for bad in ("../etc/passwd", "/etc/passwd", {"root": "in", "name": "../x.jpg"}, {"root": "etc", "name": "x.jpg"},
                    {"root": "in", "name": "a/b.jpg"}, {"root": "in", "name": "missing.jpg"},
                    {"root": "lab", "name": "x.jpg"}, {"root": "out", "name": "..%2fx.jpg"}, ["in"], None):
            with self.assertRaises(craft_spec.SpecError, msg=repr(bad)):
                self.v("photo.grade", {"image": bad})

    def test_symlinked_input_refused(self):
        os.symlink("/etc/hostname", self.r.path("in", "link.jpg"))
        with self.assertRaises(craft_spec.SpecError):
            self.v("photo.grade", {"image": {"root": "in", "name": "link.jpg"}})

    def test_values_must_be_control_ids_and_numbers(self):
        up = self.upload()
        with self.assertRaises(craft_spec.SpecError):
            self.v("photo.grade", {"image": up, "values": {"rm -rf": 1}})
        with self.assertRaises(craft_spec.SpecError):
            self.v("photo.grade", {"image": up, "values": {"light.exposure": "1"}})
        j = self.v("photo.grade", {"image": up, "look": "warm", "values": {"light.exposure": 0.4}})
        self.assertEqual(j["params"]["develop"]["light.exposure"], 0.4)
        self.assertEqual(j["params"]["develop"]["grading.highlights.hue"], 40)

    def test_ranges(self):
        up = self.upload()
        with self.assertRaises(craft_spec.SpecError):
            self.v("photo.grade", {"image": up, "long_edge": 99999})
        with self.assertRaises(craft_spec.SpecError):
            self.v("video.titlecard", {"headline": "x" * 81})
        with self.assertRaises(craft_spec.SpecError):
            self.v("video.titlecard", {"headline": "Hi", "background": up, "alpha": True})
        with self.assertRaises(craft_spec.SpecError):
            self.v("photo.finish", {"image": up, "focus": [True, 0.5]})

    def test_text_is_one_line(self):
        j = self.v("video.titlecard", {"headline": "Two\nlines\x00 here"})
        self.assertEqual(j["params"]["headline"], "Two lines here")

    def test_reel_length_cap(self):
        items = [{"ref": self.upload(), "seconds": 15}] * 7
        with self.assertRaises(craft_spec.SpecError):
            self.v("video.reel", {"items": items})
        self.assertEqual(len(self.v("video.reel", {"items": items[:6]})["params"]["items"]), 6)

    def test_reel_takes_a_music_file(self):
        song = self.own("gen", "song.mp3", b"ID3", ok=True, licences=("ace-step",), kind="music")
        j = self.v("video.reel", {"items": [{"ref": self.upload()}], "music": song})
        self.assertEqual(j["params"]["music"], ["gen", "song.mp3"])
        self.assertIn({"role": "music", "ref": ["gen", "song.mp3"]}, j["inputs"])
        with self.assertRaises(craft_spec.SpecError):
            self.v("video.reel", {"items": [{"ref": self.upload()}], "music": self.upload("cover.png", PNG)})

    def test_unowned_upload_is_accepted_but_other_roots_need_a_sidecar(self):
        self.assertEqual(self.v("photo.finish", {"image": self.upload()})["inputs"][0]["ref"], ["in", "photo.jpg"])
        owned = self.own("gen", "pic.png", PNG, ok=False, licences=("qwen-image-nc",))
        self.assertEqual(self.v("photo.finish", {"image": owned})["params"]["image"], ["gen", "pic.png"])
        self.r.put("gen", "loose.png", PNG)
        with self.assertRaises(craft_spec.SpecError):
            self.v("photo.finish", {"image": {"root": "gen", "name": "loose.png"}})


class BrandKitTest(CraftCase):
    def test_object_and_list_forms(self):
        a = craft_spec.brand_kit(KIT)
        self.assertEqual((a["bg"], a["fg"], a["ac"], a["display"], a["sans"], a["handle"]),
                         ("#0b1d3a", "#ffffff", "#e0b84a", "Playfair Display", "Inter", "@studio"))
        b = craft_spec.brand_kit({"colors": ["#000", {"hex": "#FAFAFA"}, "#ff0055"], "fonts": ["Oswald"]})
        self.assertEqual((b["bg"], b["fg"], b["ac"], b["display"], b["sans"]),
                         ("#000000", "#fafafa", "#ff0055", "Oswald", "Oswald"))
        self.assertEqual(craft_spec.brand_kit({})["bg"], craft_spec.NEUTRAL_KIT["bg"])
        self.assertIsNone(craft_spec.brand_kit(None))

    def test_bad_fields_are_refused(self):
        for bad in ({"colors": {"bg": "red"}}, {"colors": {"bg": "#12345"}}, {"colors": "#000000"},
                    {"fonts": {"display": "Inter; rm -rf /"}}, {"fonts": ["a", "b", "c"]}, {"handle": "x" * 41},
                    {"logo": {"root": "in", "name": "missing.png"}}, "kit"):
            with self.assertRaises(craft_spec.SpecError, msg=repr(bad)):
                craft_spec.brand_kit(bad)

    def test_logo_and_handle_join_the_title_card(self):
        logo = self.own("lab", "logo.png", PNG, ok=True, licences=("apache-2.0",))
        j = self.v("video.titlecard", {"headline": "Open late"}, dict(KIT, logo=logo, handle="Two\nlines"))
        self.assertTrue(j["params"]["logo"] and j["params"]["handle"])
        self.assertEqual(j["brand"]["handle"], "Two lines")
        self.assertIn({"role": "ref", "ref": ["lab", "logo.png"]}, j["inputs"])
        self.assertFalse(self.v("video.titlecard", {"headline": "x", "logo": False}, dict(KIT, logo=logo))["inputs"])
        with self.assertRaises(craft_spec.SpecError):
            self.v("video.titlecard", {"headline": "x", "logo": True}, KIT)   # no logo in the kit
        with self.assertRaises(craft_spec.SpecError):
            craft_spec.brand_kit({"logo": self.own("gen", "song.mp3", b"ID3", kind="music")})

    def test_monochrome_kit_gets_a_desaturated_look(self):
        look = craft_spec.brand_look(craft_spec.brand_kit({"colors": {"bg": "#ffffff", "fg": "#111111"}}))
        self.assertEqual(look["color.saturation"], -12)
        self.assertNotIn("grading.highlights.hue", look)


# --------------------------------------------------------------------------------------------- provenance

class ProvenanceTest(CraftCase):
    def entries(self, *refs):
        return craftd.input_entries([{"role": "item", "ref": [r["root"], r["name"]]} for r in refs])

    def test_non_commercial_wins_and_warns(self):
        a = self.own("gen", "a.png", PNG, ok=True, licences=("apache-2.0",))
        b = self.own("gen", "b.png", PNG, ok=False, licences=("qwen-image-nc",))
        entries, n_up = self.entries(a, b)
        agg = provenance.aggregate([], entries)
        self.assertIs(agg["commercial_ok"], False)
        self.assertEqual(agg["most_restrictive"], "qwen-image-nc")
        self.assertTrue(provenance.warnings(agg, n_up)[0].startswith("Contains Qwen-Image 2.1 output"))

    def test_unknown_when_an_upload_has_no_sidecar(self):
        a = self.own("gen", "a.png", PNG, ok=True, licences=("apache-2.0",))
        entries, n_up = self.entries(a, self.upload())
        agg = provenance.aggregate([], entries)
        self.assertIsNone(agg["commercial_ok"])
        self.assertEqual(n_up, 1)
        self.assertIn("your upload photo.jpg: rights not checked", agg["restricted_by"])

    def test_all_commercial_and_text_only(self):
        a = self.own("gen", "a.png", PNG, ok=True, licences=("apache-2.0",))
        self.assertIs(provenance.aggregate([], self.entries(a)[0])["commercial_ok"], True)
        self.assertIs(provenance.aggregate([], self.entries()[0])["commercial_ok"], True)

    def test_carries_through_an_earlier_craft_output(self):
        b = self.own("gen", "b.png", PNG, ok=False, licences=("qwen-image-nc",))
        jid = self.queue({"image": b})
        craftd.claim()
        st = craftd.run_job(jid, runner=lambda job, ctx: ((ctx.out / "graded.jpg").write_bytes(JPG), ["graded.jpg"])[1])
        first = st["outputs"][0]["ref"]
        jid2 = self.queue({"image": first}, recipe="photo.finish")
        craftd.claim()
        st2 = craftd.run_job(jid2, runner=lambda job, ctx: ((ctx.out / "finished.jpg").write_bytes(JPG), ["finished.jpg"])[1])
        self.assertIs(st2["provenance"]["commercial_ok"], False)
        side = store.sidecar(tuple(st2["outputs"][0]["ref"].values()))
        self.assertEqual(side["inputs"][0]["name"], first["name"])
        self.assertEqual(side["most_restrictive"], "qwen-image-nc")

    def test_owned_upload_counts_as_an_upload(self):
        up = self.own("in", "mine.jpg", JPG, ok=None, licences=("upload",), kind="upload")
        entries, n_up = self.entries(up)
        self.assertEqual(n_up, 1)
        self.assertIsNone(provenance.aggregate([], entries)["commercial_ok"])


# --------------------------------------------------------------------------------------------- runner

class RunnerTest(CraftCase):
    def test_claim_runs_a_fake_recipe_and_files_outputs_in_lab(self):
        jid = self.queue(brand=KIT)

        def fake(job, ctx):
            self.assertTrue(str(ctx.work).startswith(str(craftd.work_root())))
            self.assertEqual(sorted(p.name for p in (ctx.work / "in").iterdir()), ["in-a.jpg"])
            self.assertEqual(ctx.brand["ac"], "#e0b84a")
            (ctx.out / "graded.jpg").write_bytes(JPG)
            ctx.warn.append("a recipe warning")
            return ["graded.jpg"]

        self.assertEqual(craftd.claim(), jid)
        st = craftd.run_job(jid, runner=fake)
        self.assertEqual(st["state"], "done", st.get("error"))
        out = st["outputs"][0]
        self.assertEqual((out["ref"]["root"], out["role"], out["w"], out["h"]), ("lab", "main", 768, 512))
        self.assertTrue(out["ref"]["name"].startswith("grade_") and out["url"].startswith("/m/lab/"))
        self.assertFalse((craftd.work_root() / jid).exists(), "sandbox folder is removed after the job")
        self.assertIsNone(st["provenance"]["commercial_ok"])
        self.assertEqual(st["warnings"], ["Includes uploads: make sure you have the rights.", "a recipe warning"])
        side = store.sidecar(("lab", out["ref"]["name"]))
        self.assertEqual((side["kind"], side["tool"]["app"], side["tool"]["licence"]),
                         ("craft", "LightCraft", "MIT OR Apache-2.0"))
        self.assertEqual(side["inputs"][0]["licences"], ["upload"])
        self.assertEqual(side["params"]["brand"], "Studio")
        self.assertEqual(len(side["sha256"]), 64)
        self.assertIn(out["ref"]["name"], [i["ref"]["name"] for i in store.recent()])

    def test_video_poster_and_psd_follow_the_main_file(self):
        jid = self.queue({"headline": "Hi"}, recipe="video.titlecard")
        craftd.claim()

        def fake(job, ctx):
            (ctx.out / "titlecard.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
            (ctx.out / "poster.png").write_bytes(PNG)
            ctx.meta["titlecard.mp4"] = {"w": 1080, "h": 1920, "seconds": 4}
            return ["titlecard.mp4", "poster.png"]

        st = craftd.run_job(jid, runner=fake)
        self.assertEqual(st["state"], "done", st.get("error"))
        main, poster = st["outputs"]
        self.assertEqual(poster["ref"]["name"], main["ref"]["name"].replace(".mp4", ".png"))
        side = store.sidecar(("lab", main["ref"]["name"]))
        self.assertEqual((side["poster"], side["probe"]["seconds"]), (poster["ref"]["name"], 4))
        self.assertIs(side["commercial_ok"], True, "a text-only card has no restricting input")
        self.assertIsNone(store.sidecar(("lab", poster["ref"]["name"])), "the poster rides on the main sidecar")

    def test_output_outside_whitelist_fails_the_job(self):
        jid = self.queue()
        craftd.claim()
        st = craftd.run_job(jid, runner=lambda job, ctx: ["../../ledger.jsonl"])
        self.assertEqual(st["state"], "failed")
        st = craftd.run_job(jid, runner=lambda job, ctx: (
            ctx.out.joinpath("graded.jpg").symlink_to("/etc/hostname"), ["graded.jpg"])[1])
        self.assertEqual(st["state"], "failed")
        st = craftd.run_job(jid, runner=lambda job, ctx: [])
        self.assertEqual(st["state"], "failed")
        self.assertEqual(os.listdir(self.r.dir("lab")), [])

    def test_tampered_queue_file_is_revalidated(self):
        jid = self.queue()
        q = Path(craftd.queue_dir()) / f"{jid}.json"
        rec = json.loads(q.read_text())
        rec["params"]["image"] = {"root": "in", "name": "../../data/ledger.jsonl"}
        q.write_text(json.dumps(rec))
        craftd.claim()
        st = craftd.run_job(jid, runner=lambda job, ctx: self.fail("must not run"))
        self.assertEqual(st["state"], "failed")
        self.assertIn("image", st["error"])

    def test_bad_queue_names_are_dropped(self):
        qd = Path(craftd.queue_dir())
        (qd / "evil.json").write_text("{}")
        (qd / "c-1-x.json").write_text("{}")
        self.assertIsNone(craftd.claim())  # c-1-x.json is dropped; evil.json is never even looked at
        self.assertEqual([p.name for p in qd.iterdir()], ["evil.json"])
        self.assertEqual(list(Path(craftd.jobs_dir()).iterdir()), [])

    def test_restart_marks_running_jobs_failed(self):
        jid = self.queue()
        craftd.claim()
        p = Path(craftd.jobs_dir()) / jid / "status.json"
        p.write_text(json.dumps({"id": jid, "state": "running"}))
        craftd.recover()
        self.assertEqual(json.loads(p.read_text())["state"], "failed")

    def test_prune_keeps_the_newest(self):
        with mock.patch.object(craftd, "KEEP_JOBS", 2):
            for i in range(4):
                d = Path(craftd.jobs_dir()) / f"c-2026100800000{i}-aaaaaa"
                d.mkdir()
                (d / "status.json").write_text(json.dumps({"state": "done"}))
                os.utime(d, (1000 + i, 1000 + i))
            craftd.prune()
        self.assertEqual(sorted(p.name for p in Path(craftd.jobs_dir()).iterdir()),
                         ["c-20261008000002-aaaaaa", "c-20261008000003-aaaaaa"])

    def test_queue_cap(self):
        for _ in range(craft_routes.QUEUE_MAX):
            self.queue()
        with self.assertRaises(craft_routes.QueueFull):
            self.queue()

    def test_missing_app_fails_with_a_reason(self):
        os.remove(os.path.join(self.bin, "lightcraft-cli"))
        jid = self.queue()
        craftd.claim()
        st = craftd.run_job(jid, runner=lambda job, ctx: self.fail("must not run"))
        self.assertEqual(st["state"], "failed")
        self.assertIn("LightCraft is not installed", st["error"])

    def test_heartbeat_reports_apps_and_masks_off(self):
        craftd.heartbeat("idle")
        hb = json.loads((craftd.craft_dir() / "craftd.json").read_text())
        self.assertTrue(all(a["built"] for a in hb["apps"].values()))
        self.assertEqual(hb["ai_masks"]["enabled"], False)
        self.assertIn("SAM License", hb["ai_masks"]["licence"])


class SandboxTest(CraftCase):
    def test_sam3_folder_only_reaches_lightcraft_when_opted_in(self):
        sam = os.path.join(self.r.base, "sam3")
        os.makedirs(sam)
        env = mcp_client.sandbox_env("lightcraft", "/w", ai_masks=False, sam3_dir=sam, env={"PATH": "/usr/bin"})
        self.assertNotIn("CRAFT_SAM3_DIR", env)
        env = mcp_client.sandbox_env("lightcraft", "/w", ai_masks=True, sam3_dir=sam, env={"PATH": "/usr/bin"})
        self.assertEqual((env["CRAFT_AI_MASKS"], env["CRAFT_SAM3_DIR"]), ("1", sam))
        env = mcp_client.sandbox_env("filmcraft", "/w", ai_masks=True, sam3_dir=sam, env={"PATH": "/usr/bin"})
        self.assertNotIn("CRAFT_SAM3_DIR", env)
        self.assertFalse(craft_spec.ai_masks_enabled({}))
        self.assertTrue(craft_spec.ai_masks_enabled({"MEDIA_LAB_CRAFT_AI_MASKS": "1"}))

    def test_sandbox_env_passes_nothing_else(self):
        env = mcp_client.sandbox_env("photocraft", "/w", env={"PATH": "/usr/bin", "MEDIA_LAB_TOKEN": "secret-token",
                                                              "HOME": "/root", "CRAFT_BIN_DIR": "/b"})
        self.assertEqual(set(env), {"PATH", "CRAFT_BIN_DIR", "CRAFT_WORK"})

    def test_wrapper_refuses_bridge_mode_and_unknown_apps(self):
        """These exit before bubblewrap starts; the fake binary is never run."""
        env = {"PATH": os.environ.get("PATH", "/usr/bin"), "CRAFT_BIN_DIR": self.bin,
               "CRAFT_WORK": os.path.join(self.r.base, "w")}
        for args, code in ((["photocraft", "--bridge"], 2), (["filmcraft", "serve"], 2), (["effectcraft", "--port=1"], 2),
                           (["bash"], 2), (["lightcraft", "mcp", "--connect", "x"], 2)):
            p = subprocess.run([WRAPPER] + args, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(p.returncode, code, (args, p.stderr))
        os.remove(os.path.join(self.bin, "photocraft-cli"))
        p = subprocess.run([WRAPPER, "photocraft"], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual((p.returncode, p.stderr.strip()), (1, "not built: photocraft-cli"))

    def test_wrapper_is_executable_and_binds_one_writable_folder(self):
        self.assertTrue(os.stat(WRAPPER).st_mode & stat.S_IXUSR)
        with open(WRAPPER, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("--bind "), 1)
        self.assertIn("--unshare-all", text)
        self.assertIn("--clearenv", text)


# --------------------------------------------------------------------------------------------- routes

class RoutesTest(CraftCase):
    """The HTTP routes with a fake craft service: its heartbeat is written by the test, and jobs are run with
    craftd's own claim/run_job and a fake recipe."""

    TOKEN = None

    def setUp(self):
        super().setUp()
        # server.py installs craft_routes at import, like every feature module
        self.srv = server.make_server(self.r.config(token=self.TOKEN), adopt=False)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if method == "POST":
            h["X-Media-Lab"] = "1"
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        is_json = (r.getheader("Content-Type") or "").startswith("application/json")
        return r.status, (json.loads(data) if is_json else data), r

    def beat(self, age=0.0, apps=None):
        hb = {"epoch": time.time() - age, "at": "now", "state": "idle",
              "apps": apps or {a: {"built": True, "commit": "abc1234"} for a in craftd.APPS},
              "ai_masks": {"enabled": False}}
        with open(os.path.join(craft_routes.craft_dir(), "craftd.json"), "w") as f:
            json.dump(hb, f)


class RoutesOpenTest(RoutesTest):
    def test_queue_status_and_listing(self):
        code, out, _ = self.req("POST", "/api/craft", {"recipe": "video.titlecard", "params": {"headline": "Open late"},
                                                       "brand": KIT})
        self.assertEqual(code, 202, out)
        self.assertIn("craft service has not started", out.get("warning", ""))
        code, st, _ = self.req("GET", out["status"])
        self.assertEqual((code, st["state"], st["position"]), (200, "queued", 1))
        code, lst, _ = self.req("GET", "/api/craft")
        self.assertIn(out["job"], lst["queued"])
        code, doc, _ = self.req("GET", "/api/craft/recipes")
        self.assertEqual(set(doc["recipes"]), set(craft_spec.RECIPES))
        self.assertIn("SAM License", doc["service"]["ai_masks"]["licence"])

    def test_bad_requests(self):
        self.assertEqual(self.req("POST", "/api/craft", {"recipe": "x"})[0], 400)
        self.assertEqual(self.req("POST", "/api/craft", {"recipe": "photo.grade",
                                                         "params": {"image": {"root": "in", "name": "../x.jpg"}}})[0], 400)
        self.assertEqual(self.req("POST", "/api/craft", {"recipe": "video.titlecard", "params": {"headline": "x"}},
                                  headers={"X-Media-Lab": "0"})[0], 403)
        self.assertEqual(self.req("GET", "/api/craft/c-20261008000000-abcdef")[0], 404)
        self.assertEqual(self.req("GET", "/api/craft/../../etc/passwd")[0], 404)
        code, out, _ = self.req("POST", "/api/craft", {"recipe": "video.titlecard", "params": {"headline": "x"},
                                                       "brand_kit": "studio"})
        self.assertEqual(code, 400)
        self.assertIn("brand", out["error"])

    def test_service_down_is_reported_not_raised(self):
        f = craft_routes.feature()
        self.assertEqual((f["ok"], f["reason"]), (False, craft_routes.NOT_STARTED))
        self.beat(age=600)
        f = craft_routes.feature()
        self.assertFalse(f["ok"])
        self.assertIn("no heartbeat from the craft service for 600 s", f["reason"])
        with open(os.path.join(craft_routes.craft_dir(), "craftd.json"), "w") as fh:
            fh.write("{half a heartbeat")
        f = craft_routes.feature()
        self.assertFalse(f["ok"])
        self.assertIn("can't be read", f["reason"])
        with mock.patch.object(craft_routes, "runner", side_effect=RuntimeError("boom")):
            self.assertEqual(craft_routes.feature()["ok"], False)
        code, doc, _ = self.req("GET", "/api/craft/recipes")
        self.assertEqual((code, doc["service"]["ok"]), (200, False))

    def test_service_up_and_a_missing_app(self):
        self.beat()
        self.assertEqual(craft_routes.feature(), {"ok": True, "reason": None, "ai_masks": {
            "enabled": False, "licence": craft_spec.SAM_LICENCE},
            "recipes": {r: {"ok": True, "reason": None} for r in craft_spec.RECIPES}})
        apps = {a: {"built": a != "filmcraft"} for a in craftd.APPS}
        self.beat(apps=apps)
        f = craft_routes.feature()
        self.assertFalse(f["ok"])
        self.assertTrue(f["recipes"]["photo.grade"]["ok"])
        self.assertIn("FilmCraft is not installed", f["recipes"]["video.reel"]["reason"])
        code, out, _ = self.req("POST", "/api/craft", {"recipe": "video.titlecard", "params": {"headline": "x"}})
        self.assertNotIn("warning", out)

    def test_a_job_through_the_fake_service_and_its_output_served_with_range(self):
        self.beat()
        song = self.own("gen", "song.mp3", b"ID3", ok=True, licences=("ace-step",), kind="music")
        body = {"recipe": "video.reel", "params": {"items": [{"ref": self.upload(), "caption": "Hello"}], "music": song}}
        code, out, _ = self.req("POST", "/api/craft", body)
        self.assertEqual(code, 202, out)
        self.assertEqual(craftd.claim(), out["job"])
        code, st, _ = self.req("GET", out["status"])
        self.assertEqual(st["state"], "running")

        def fake(job, ctx):
            self.assertEqual(sorted(p.name for p in (ctx.work / "in").iterdir()), ["in-a.jpg", "in-b.mp3"])
            (ctx.out / "reel.mp4").write_bytes(bytes(range(256)) * 4)
            return ["reel.mp4"]

        craftd.run_job(out["job"], runner=fake)
        code, st, _ = self.req("GET", out["status"])
        self.assertEqual((code, st["state"]), (200, "done"), st)
        self.assertIsNone(st["provenance"]["commercial_ok"])
        url = st["outputs"][0]["url"]
        code, body, r = self.req("GET", url, headers={"Range": "bytes=10-19"})
        self.assertEqual((code, body, r.getheader("Content-Range")), (206, bytes(range(10, 20)), "bytes 10-19/1024"))
        self.assertEqual(self.req("GET", url, headers={"Range": "bytes=5000-"})[0], 416)
        code, lst, _ = self.req("GET", "/api/craft")
        self.assertEqual(lst["recent"][0]["state"], "done")

    def test_failed_job_reports_a_scrubbed_reason(self):
        jid = self.queue()
        craftd.claim()
        craftd.run_job(jid, runner=lambda job, ctx: (_ for _ in ()).throw(RuntimeError(f"boom in {ctx.work}")))
        code, st, _ = self.req("GET", f"/api/craft/{jid}")
        self.assertEqual((code, st["state"]), (200, "failed"))
        self.assertNotIn(self.r.base, st["error"])
        self.assertNotIn("trace", st)

    def test_source_files_are_not_served(self):
        for p in ("/craft/craftd.py", "/medialab/craft_spec.py", "/craft/craft-mcp", "/static/../craft/Dockerfile"):
            self.assertEqual(self.req("GET", p)[0], 404, p)


class RoutesTokenTest(RoutesTest):
    TOKEN = "correct-horse-battery"

    def test_craft_routes_need_the_token(self):
        self.assertEqual(self.req("GET", "/api/craft/recipes")[0], 401)
        self.assertEqual(self.req("POST", "/api/craft", {"recipe": "x"})[0], 401)
        code, _, _ = self.req("GET", "/api/craft/recipes", headers={"Authorization": f"Bearer {self.TOKEN}"})
        self.assertEqual(code, 200)


class NoticesTest(unittest.TestCase):
    def test_third_party_notices_name_the_craft_apps(self):
        with open(os.path.join(ROOT, "THIRD-PARTY-NOTICES.md"), encoding="utf-8") as f:
            text = f.read()
        for must in ("PhotoCraft", "LightCraft", "FilmCraft", "EffectCraft", "ArtCraft Team and contributors",
                     "MIT OR Apache-2.0", "SAM License", "symphonia", "MPL-2.0", "trademark"):
            self.assertIn(must, text)

    def test_dockerfile_pins_every_app(self):
        with open(os.path.join(ROOT, "craft", "Dockerfile"), encoding="utf-8") as f:
            text = f.read()
        for app, commit in (("photocraft", "6f39aba"), ("lightcraft", "52f64ce"), ("filmcraft", "a4536bc"),
                            ("effectcraft", "e38a92e")):
            self.assertRegex(text, rf"build-app {app} {commit}[0-9a-f]{{33}}\b")
        self.assertIn("USER 1000:1000", text)
        self.assertIn("bubblewrap", text)


class _FakeFilmCraft:
    """Stands in for a FilmCraft session: timeline.place answers like the real app (picture clip first, then the
    linked sound clip for a source with audio) and transitions refuse a clip of the wrong track kind."""
    with_sound = set()

    def __init__(self, *a, **k):
        self.calls, self.kind, self.next = [], {}, 100

    def __enter__(self):
        _FakeFilmCraft.last = self
        return self

    def __exit__(self, *exc):
        return False

    def call(self, tool, args, note="", check=True):
        self.calls.append((tool, args))
        if tool == "project_inspect":
            return {"root": {"children": [{"item": i, "duration": 2 * 254016000000} for i in range(1, 9)]}}, []
        if tool == "render_frame":
            return {}, []
        cid, prm = args.get("id"), args.get("params", {})
        if cid == "file.import":
            return {"items": list(range(1, len(prm["paths"]) + 1))}, []
        if cid == "timeline.place":
            ids = [self.next]
            self.kind[self.next] = "video" if prm["track"] == "V1" else "audio"
            if prm["track"] == "V1" and prm["item"] in self.with_sound:
                ids.append(self.next + 1)
                self.kind[self.next + 1] = "audio"
            self.next += 2
            return {"clips": ids}, []
        for want, k in (("sequence.applyVideoTransition", "video"), ("sequence.applyAudioTransition", "audio")):
            if cid == want and self.kind.get(prm["clip"]) != k:
                raise RuntimeError("no edit point at the playhead on targeted tracks")
        return {}, []


class ReelTransitionTest(unittest.TestCase):
    """Audit 2026-10-11 (high): a reel with any clip that has sound failed the default dissolve, because the
    linked sound clip got a video transition. Sound now crossfades where two clips with sound meet."""

    def run_reel(self, kinds, with_sound, music=False, transition="dissolve"):
        from types import SimpleNamespace
        from craft import recipes
        items = [{"ref": ("in", f"x{i}"), "kind": k, "seconds": 2, "caption": ""} for i, k in enumerate(kinds)]
        p = {"format": "9:16", "fps": 24, "items": items, "music": ("in", "song") if music else None,
             "transition": transition, "transition_frames": 10, "bitrate_kbps": 8000, "loudness": -14}
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        inp = {craft_spec.ref_key(it["ref"]): tmp / f"in{i}" for i, it in enumerate(items)}
        inp[craft_spec.ref_key(("in", "song"))] = tmp / "song"
        ctx = SimpleNamespace(work=tmp, fonts=None, sessions=[], inp=inp, out=tmp, warn=[], meta={},
                              brand=dict(craft_spec.NEUTRAL_KIT, sans="Inter"))
        _FakeFilmCraft.with_sound = set(with_sound)
        with mock.patch.object(recipes, "Session", _FakeFilmCraft):
            recipes.reel({"id": "c-test", "params": p}, ctx)
        fc = _FakeFilmCraft.last
        return [(a["id"], a["params"]["clip"]) for t, a in fc.calls
                if t == "command_run" and a["id"].startswith("sequence.apply")], fc

    def test_clips_with_sound_dissolve_and_crossfade(self):
        # still, clip with sound, clip with sound: two video dissolves, one audio crossfade (where sound meets)
        tr, fc = self.run_reel(["image", "video", "video"], with_sound={2, 3})
        self.assertEqual([t for t, _ in tr].count("sequence.applyVideoTransition"), 2)
        self.assertEqual([t for t, _ in tr].count("sequence.applyAudioTransition"), 1)
        self.assertTrue(all(fc.kind[c] == ("video" if "Video" in t else "audio") for t, c in tr))
        fill = [a["params"]["clips"] for t, a in fc.calls if t == "command_run" and a["id"] == "clip.fillFrame"][0]
        self.assertTrue(all(fc.kind[c] == "video" for c in fill))

    def test_music_replaces_clip_sound_so_no_crossfade(self):
        tr, _ = self.run_reel(["video", "video"], with_sound={1, 2}, music=True)
        self.assertEqual([t for t, _ in tr], ["sequence.applyVideoTransition"])

    def test_cut_has_no_transitions(self):
        tr, _ = self.run_reel(["video", "video"], with_sound={1, 2}, transition="cut")
        self.assertEqual(tr, [])


if __name__ == "__main__":
    unittest.main()
