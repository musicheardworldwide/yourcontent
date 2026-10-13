"""WP2: ref and id validation, path safety, ownership (§3.1, §12.5)."""
from __future__ import annotations

import os
import unittest

from medialab import jobs, projects, server, store
from medialab.types import BadRef, NotOwned
from tests.util import TempRoots, make_png

BAD_NAMES = ["../x", "a/b", ".x", "a\\b", "..", "x..y", "a\x00b", "", " a", "a b", "a\nb", "abc\n",
             "аbc.png",            # Cyrillic a
             "ab∕c.png",           # division slash look-alike
             "ab／c.png",           # fullwidth solidus
             "café.png", "a%2fb", "%2e%2e%2f", "-a.png", "_a.png", "a" * 161]


class PathsTest(unittest.TestCase):
    def setUp(self):
        self.r = TempRoots()
        self.addCleanup(self.r.cleanup)
        store.configure(self.r.config())

    def test_check_ref_accepts_good_refs(self):
        self.assertEqual(store.check_ref({"root": "gen", "name": "clip_00001.mp4"}), ("gen", "clip_00001.mp4"))
        self.assertEqual(store.check_ref(["out", "qwen_studio_00001_.png"]), ("out", "qwen_studio_00001_.png"))
        self.assertEqual(store.check_ref(("lab", "a" * 160)), ("lab", "a" * 160))

    def test_check_ref_rejects(self):
        for name in BAD_NAMES:
            with self.subTest(name=name), self.assertRaises(BadRef):
                store.check_ref({"root": "gen", "name": name})
        for bad in [{"root": "etc", "name": "a.png"}, {"root": "OUT", "name": "a.png"}, {"name": "a.png"},
                    {"root": "gen"}, {"root": "gen", "name": 5}, ["gen"], ["gen", "a", "b"], "gen/a.png", None, 3]:
            with self.subTest(bad=bad), self.assertRaises(BadRef):
                store.check_ref(bad)

    def test_url_names_are_decoded_once_then_checked(self):
        for q in ["%2e%2e%2f", "%2E%2E", "..%2fx", "a%2fb", "a%5cb", "%00", "%c3%28", "%252e%252e"]:
            with self.subTest(q=q), self.assertRaises(BadRef):
                store.ref_from_url("gen", q)
        self.assertEqual(store.ref_from_url("gen", "img_00001_.png"), ("gen", "img_00001_.png"))
        self.assertEqual(store.ref_from_url("gen", "img%5f00001_.png"), ("gen", "img_00001_.png"))

    def test_resolve_stays_inside_the_root(self):
        p = store.resolve(("gen", "img_00001_.png"))
        self.assertEqual(os.path.dirname(p), os.path.realpath(self.r.dir("gen")))

    def test_resolve_refuses_a_symlink_escaping_its_root(self):
        outside = os.path.join(self.r.base, "secret.txt")
        with open(outside, "w") as f:
            f.write("secret")
        os.symlink(outside, self.r.path("gen", "link.png"))
        with self.assertRaises(BadRef):
            store.resolve(("gen", "link.png"))
        # even when it is owned
        store.put_sidecar(("gen", "link.png"), {"kind": "image"})
        with self.assertRaises(BadRef):
            store.resolve(("gen", "link.png"))

    def test_resolve_refuses_a_symlinked_sidecar(self):
        target = os.path.join(self.r.base, "fake.json")
        with open(target, "w") as f:
            f.write("{}")
        os.symlink(target, store.data_path("sidecars", "gen", "x.png.json"))
        self.assertFalse(store.owned(("gen", "x.png")))

    def test_unowned_valid_name_is_not_found(self):
        self.r.put("out", "qwen2_t2i_00001_.png", make_png())
        self.assertFalse(store.owned(("out", "qwen2_t2i_00001_.png")))
        with self.assertRaises(NotOwned):
            store.require_owned(("out", "qwen2_t2i_00001_.png"))
        with self.assertRaises(NotOwned):
            store.remove(("out", "qwen2_t2i_00001_.png"))
        self.assertTrue(os.path.exists(self.r.path("out", "qwen2_t2i_00001_.png")))
        self.assertEqual(NotOwned.status, 404)

    def test_poster_is_reachable_only_through_its_owned_parent(self):
        self.r.put("gen", "clip_00001.mp4", b"\x00\x00\x00\x18ftypisom")
        self.r.put("gen", "clip_00001.png", make_png())
        self.r.put("gen", "clip_00002.png", make_png())       # a png with no owner
        self.assertIsNone(store.poster_parent(("gen", "clip_00001.png")))
        store.put_sidecar(("gen", "clip_00001.mp4"), {"kind": "clip", "poster": "clip_00001.png"})
        self.assertEqual(store.poster_parent(("gen", "clip_00001.png")), ("gen", "clip_00001.mp4"))
        self.assertIsNone(store.poster_parent(("gen", "clip_00002.png")))
        self.assertIsNone(store.poster_parent(("out", "clip_00001.png")))   # other root
        self.assertFalse(store.owned(("gen", "clip_00001.png")))            # never owned in its own right
        # a non-same-stem poster (e.g. an extension's copied poster) is found by the scan
        self.r.put("gen", "ext_20261007-120000_abcdef.mp4", b"x")
        self.r.put("gen", "ext_poster.png", make_png())
        store.put_sidecar(("gen", "ext_20261007-120000_abcdef.mp4"), {"kind": "extension", "poster": "ext_poster.png"})
        self.assertEqual(store.poster_parent(("gen", "ext_poster.png")), ("gen", "ext_20261007-120000_abcdef.mp4"))

    def test_id_patterns(self):
        self.assertEqual(projects.check_pid("p_0123456789ab"), "p_0123456789ab")
        for bad in ["p_0123456789AB", "p_0123456789a", "p_0123456789abc", "x_0123456789ab", "p_../../etc",
                    "p_0123456789ab\n", 7, None]:
            with self.subTest(bad=bad), self.assertRaises(BadRef):
                projects.check_pid(bad)
        self.assertTrue(jobs.JID_RE.fullmatch("j_0123456789ab"))
        self.assertFalse(jobs.JID_RE.fullmatch("j_0123456789ab\n"))
        with self.assertRaises(BadRef):
            jobs.get("j_../x")
        self.assertTrue(server.PROXY_RE.fullmatch("a" * 64))
        for bad in ["a" * 63, "A" * 64, "g" * 64, "a" * 65]:
            self.assertFalse(server.PROXY_RE.fullmatch(bad))


if __name__ == "__main__":
    unittest.main()
