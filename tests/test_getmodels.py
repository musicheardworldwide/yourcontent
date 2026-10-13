"""get-models: the shipped manifest, the downloader's verify rules and the health-check hook (MAP.md: models).
Every download here is a few bytes served from a local test server; nothing reaches the network."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import http.server
import io
import json
import os
import tempfile
import threading
import unittest

from medialab import getmodels, models

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

BODY = b"media-lab test weights " * 50          # 1150 bytes
SHA = hashlib.sha256(BODY).hexdigest()


def tiny_manifest(endpoint: str, body: bytes = BODY, sha: str = SHA) -> dict:
    return getmodels.parse_manifest({
        "schema": getmodels.SCHEMA, "endpoint": endpoint,
        "repos": {"r": {"id": "Example/repo", "rev": "0" * 40}},
        "files": {"tiny.safetensors": {"repo": "r", "path": "split_files/vae/tiny.safetensors", "folder": "vae",
                                       "bytes": len(body), "sha256": sha, "licence": "apache-2.0"}}})


class _Handler(http.server.BaseHTTPRequestHandler):
    body = BODY
    honour_range = True
    requests: list = []

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):  # noqa: N802
        type(self).requests.append((self.path, self.headers.get("Range"), self.headers.get("Authorization")))
        data, status = self.body, 200
        rng = self.headers.get("Range")
        if rng and self.honour_range:
            start = int(rng.split("=")[1].rstrip("-"))
            data, status = data[start:], 206
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextlib.contextmanager
def serve(body: bytes = BODY, honour_range: bool = True):
    handler = type("H", (_Handler,), {"body": body, "honour_range": honour_range, "requests": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", handler
    finally:
        srv.shutdown()
        srv.server_close()


class TestShippedManifest(unittest.TestCase):
    def test_parses_and_every_entry_is_complete(self):
        m = getmodels.load_manifest()
        self.assertEqual(len(m["files"]), 17)
        for name, f in m["files"].items():
            self.assertRegex(f["sha256"], r"^[0-9a-f]{64}$", name)
            self.assertGreater(f["bytes"], 0, name)
            self.assertIn(f["folder"], getmodels.FOLDERS, name)
            self.assertTrue(getmodels.url_for(m, name).startswith("https://huggingface.co/Comfy-Org/"))
            self.assertIn("/resolve/", getmodels.url_for(m, name))

    def test_every_default_model_file_is_in_the_manifest(self):
        """The downloader and the health check can't disagree: every file the shipped graphs need is known."""
        m = models.load(None)
        want = getmodels.wanted(m, getmodels.SETS)
        man = getmodels.load_manifest()
        names = {n for folders in want.values() for files in folders.values() for n in files}
        self.assertEqual(len(names), 13)   # 3 picture (Qwen-Image-2512 default) + 6 clip + 4 music
        self.assertEqual(sorted(names - set(man["files"])), [])
        for s, folders in want.items():
            for folder, files in folders.items():
                for n in files:
                    self.assertEqual(man["files"][n]["folder"], folder, n)
        for vid in m.image_variants():   # every shipped picture variant, not only the default
            w = getmodels.wanted(m, ["pictures"], vid)["pictures"]
            for folder, files in w.items():
                for n in files:
                    self.assertEqual(man["files"][n]["folder"], folder, (vid, n))
                    self.assertEqual(man["files"][n]["licence"], m.licence_for_file(n), (vid, n))

    def test_totals_match_the_release_table(self):
        man = getmodels.load_manifest()
        m = models.load(None)
        want = getmodels.wanted(m, getmodels.SETS)
        tot = {s: sum(man["files"][n]["bytes"] for fs in f.values() for n in fs) for s, f in want.items()}
        self.assertEqual(round(tot["pictures"] / 1e9, 2), 30.07)    # Qwen-Image-2512 fp8 + Qwen2.5-VL fp8 + VAE
        self.assertEqual(round(tot["clips"] / 1e9, 2), 38.03)
        self.assertEqual(round(tot["music"] / 1e9, 2), 10.03)
        w21 = getmodels.wanted(m, ["pictures"], "bf16")["pictures"]
        self.assertEqual(round(sum(man["files"][n]["bytes"] for fs in w21.values() for n in fs) / 1e9, 2), 24.26)


class TestManifestParse(unittest.TestCase):
    def good(self):
        return copy.deepcopy(tiny_manifest("http://example.invalid"))

    def test_rejects_bad_entries(self):
        cases = [
            ("schema", lambda m: m.update(schema="x")),
            ("sha256", lambda m: m["files"]["tiny.safetensors"].update(sha256="ABC")),
            ("bytes", lambda m: m["files"]["tiny.safetensors"].update(bytes=0)),
            ("bytes", lambda m: m["files"]["tiny.safetensors"].update(bytes=True)),
            ("folder", lambda m: m["files"]["tiny.safetensors"].update(folder="../etc")),
            ("path", lambda m: m["files"]["tiny.safetensors"].update(path="../x/tiny.safetensors")),
            ("path", lambda m: m["files"]["tiny.safetensors"].update(path="vae/other.safetensors")),
            ("repo", lambda m: m["files"]["tiny.safetensors"].update(repo="nope")),
            ("rev", lambda m: m["repos"]["r"].update(rev="main")),
            ("plain file name", lambda m: m["files"].update({"../evil": m["files"]["tiny.safetensors"]})),
        ]
        for word, mutate in cases:
            m = self.good()
            mutate(m)
            with self.assertRaises(getmodels.ManifestError, msg=word) as cm:
                getmodels.parse_manifest(m)
            self.assertIn(word, str(cm.exception))

    def test_load_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.json")
            with open(p, "w") as f:
                json.dump(self.good(), f)
            self.assertIn("tiny.safetensors", getmodels.load_manifest(p)["files"])
            with open(p, "w") as f:
                f.write("{nope")
            with self.assertRaises(getmodels.ManifestError):
                getmodels.load_manifest(p)


class TestFetch(unittest.TestCase):
    def test_download_verifies_and_lands_in_its_folder(self):
        with serve() as (url, h), tempfile.TemporaryDirectory() as d:
            p = getmodels.fetch(tiny_manifest(url), "tiny.safetensors", d)
            self.assertEqual(p, os.path.join(os.path.realpath(d), "vae", "tiny.safetensors"))
            with open(p, "rb") as f:
                self.assertEqual(f.read(), BODY)
            self.assertFalse(os.path.exists(p + ".part"))
            self.assertEqual(h.requests[0][0], "/Example/repo/resolve/" + "0" * 40 + "/split_files/vae/tiny.safetensors")

    def test_sha_mismatch_is_rejected_and_the_part_deleted(self):
        bad = BODY[:-1] + b"X"   # same size, different bytes
        with serve(bad) as (url, _), tempfile.TemporaryDirectory() as d:
            with self.assertRaises(getmodels.VerifyError) as cm:
                getmodels.fetch(tiny_manifest(url), "tiny.safetensors", d)
            self.assertIn("sha256 mismatch", str(cm.exception))
            self.assertEqual(os.listdir(os.path.join(d, "vae")), [])

    def test_oversize_is_rejected(self):
        with serve(BODY + b"extra") as (url, _), tempfile.TemporaryDirectory() as d:
            with self.assertRaises(getmodels.VerifyError):
                getmodels.fetch(tiny_manifest(url), "tiny.safetensors", d)
            self.assertEqual(os.listdir(os.path.join(d, "vae")), [])

    def test_resume_sends_range_and_hashes_the_whole_file(self):
        with serve() as (url, h), tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "vae"))
            with open(os.path.join(d, "vae", "tiny.safetensors.part"), "wb") as f:
                f.write(BODY[:400])
            p = getmodels.fetch(tiny_manifest(url), "tiny.safetensors", d)
            self.assertEqual(h.requests[0][1], "bytes=400-")
            with open(p, "rb") as f:
                self.assertEqual(f.read(), BODY)

    def test_resume_restarts_when_the_server_ignores_range(self):
        with serve(honour_range=False) as (url, _), tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "vae"))
            with open(os.path.join(d, "vae", "tiny.safetensors.part"), "wb") as f:
                f.write(b"garbage!" * 10)
            p = getmodels.fetch(tiny_manifest(url), "tiny.safetensors", d)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), BODY)

    def test_never_overwrites_a_finished_file(self):
        with serve() as (url, h), tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "vae"))
            target = os.path.join(d, "vae", "tiny.safetensors")
            with open(target, "wb") as f:
                f.write(b"owner's own file")
            with self.assertRaises(FileExistsError):
                getmodels.fetch(tiny_manifest(url), "tiny.safetensors", d)
            with open(target, "rb") as f:
                self.assertEqual(f.read(), b"owner's own file")
            self.assertEqual(h.requests, [])

    def test_token_only_when_set(self):
        with serve() as (url, h), tempfile.TemporaryDirectory() as d:
            getmodels.fetch(tiny_manifest(url), "tiny.safetensors", d, token="hf_test")
            self.assertEqual(h.requests[0][2], "Bearer hf_test")


class TestRun(unittest.TestCase):
    """The command over a stand-in model table whose music set names the tiny file."""

    def table(self, d):
        p = os.path.join(d, "models.json")
        with open(p, "w") as f:
            json.dump({"music": {"vae": "tiny.safetensors"}}, f)
        return models.load(None, local_path=p)

    def run_cmd(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = getmodels.run(**kw)
        return rc, out.getvalue()

    def test_check_lists_missing_with_sizes_then_download_then_clean(self):
        with serve() as (url, _), tempfile.TemporaryDirectory() as d:
            man = tiny_manifest(url)
            md = os.path.join(d, "models")
            m = self.table(d)
            rc, out = self.run_cmd(models=m, sets=["music"], models_dir=md, check=True, man=man)
            self.assertEqual(rc, 1)
            self.assertIn("tiny.safetensors (models/vae, 0.00 GB) · get it: python3 -m medialab get-models music", out)
            self.assertIn("acestep_v1.5_turbo.safetensors", out)   # not in the tiny manifest: listed as yours
            rc, out = self.run_cmd(models=m, sets=["music"], models_dir=md, dry_run=True, man=man)
            self.assertEqual(rc, 0)
            self.assertIn("yours, not in the manifest", out)
            self.assertFalse(os.path.exists(os.path.join(md, "vae")))   # dry run wrote nothing
            rc, out = self.run_cmd(models=m, sets=["music"], models_dir=md, man=man)
            self.assertEqual(rc, 0, out)
            self.assertIn("done, sha256 ok", out)
            rc, out = self.run_cmd(models=m, sets=["music"], models_dir=md, check=True, man=man)
            self.assertEqual(rc, 0, out)
            rc, out = self.run_cmd(models=m, sets=["music"], models_dir=md, verify=True, man=man)
            self.assertIn("verify tiny.safetensors: ok", out)

    def test_wrong_size_file_is_reported_and_left_alone(self):
        with serve() as (url, h), tempfile.TemporaryDirectory() as d:
            md = os.path.join(d, "models")
            os.makedirs(os.path.join(md, "vae"))
            with open(os.path.join(md, "vae", "tiny.safetensors"), "wb") as f:
                f.write(b"short")
            rc, out = self.run_cmd(models=self.table(d), sets=["music"], models_dir=md, man=tiny_manifest(url))
            self.assertEqual(rc, 0)
            self.assertIn("left as it is", out)
            self.assertEqual(h.requests, [])
            with open(os.path.join(md, "vae", "tiny.safetensors"), "rb") as f:
                self.assertEqual(f.read(), b"short")

    def test_sha_mismatch_fails_the_command(self):
        with serve(BODY[:-1] + b"X") as (url, _), tempfile.TemporaryDirectory() as d:
            md = os.path.join(d, "models")
            rc, out = self.run_cmd(models=self.table(d), sets=["music"], models_dir=md, man=tiny_manifest(url))
            self.assertEqual(rc, 1)
            self.assertIn("tiny.safetensors: FAILED", out)
            self.assertFalse(os.path.exists(os.path.join(md, "vae", "tiny.safetensors")))

    def test_unknown_variant_is_an_error(self):
        with self.assertRaises(ValueError):
            getmodels.wanted(models.load(None), ["pictures"], variant="nope")


class TestWrapper(unittest.TestCase):
    @unittest.skipUnless(os.path.isfile(os.path.join(ROOT, "scripts", "get-models")), "scripts/ is not in the image")
    def test_script_wraps_the_module(self):
        p = os.path.join(ROOT, "scripts", "get-models")
        self.assertTrue(os.access(p, os.X_OK))
        with open(p, encoding="utf-8") as f:
            self.assertIn("-m medialab get-models", f.read())


if __name__ == "__main__":
    unittest.main()
