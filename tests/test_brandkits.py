"""Brand kits: validation (pure) and the DATA/brandkits store."""
from __future__ import annotations

import json
import os
import unittest

from medialab import brandkits, store
from medialab.brandkits import KitError
from tests.util import TempRoots


def kit(**over):
    k = {"name": "Test Kit", "colors": {"bg": "#101010", "fg": "#FAFAFA", "accent": "#e8b14a"},
         "fonts": {"display": "Space Grotesk", "body": "Inter"}, "handle": "@test", "voice": "Plain.",
         "default_styles": ["editorial", "scrim"]}
    k.update(over)
    return k


class ValidateTest(unittest.TestCase):
    def test_good_kit(self):
        k = brandkits.validate(kit(extra="dropped"))
        self.assertEqual(k["id"], "test-kit")
        self.assertEqual(k["colors"], {"bg": "#101010", "fg": "#fafafa", "accent": "#e8b14a"})
        self.assertEqual(k["fonts"], {"display": "Space Grotesk", "body": "Inter"})
        self.assertIsNone(k["logo"])
        self.assertEqual(k["default_styles"], ["editorial", "scrim"])
        self.assertNotIn("extra", k)

    def test_id_from_url_wins(self):
        self.assertEqual(brandkits.validate(kit(id="other"), kit_id="mine")["id"], "mine")
        self.assertEqual(brandkits.slug("  Café & Co!! "), "caf-co")
        self.assertEqual(brandkits.slug("!!!"), "kit")

    def test_refusals(self):
        bad = [
            ("not a dict", []),
            ("no name", kit(name="  ")),
            ("long name", kit(name="x" * 61)),
            ("bad id", kit(id="Bad Id")),
            ("no colors", kit(colors=None)),
            ("missing accent", kit(colors={"bg": "#000000", "fg": "#ffffff"})),
            ("short hex", kit(colors={"bg": "#000", "fg": "#ffffff", "accent": "#ffffff"})),
            ("css in a colour", kit(colors={"bg": "#000000;x", "fg": "#ffffff", "accent": "#ffffff"})),
            ("no body font", kit(fonts={"display": "Inter"})),
            ("quote in a font", kit(fonts={"display": "Inter',serif;}", "body": "Inter"})),
            ("font not text", kit(fonts={"display": 3, "body": "Inter"})),
            ("styles not a list", kit(default_styles="editorial")),
            ("bad style id", kit(default_styles=["<b>"])),
            ("too many styles", kit(default_styles=[f"s{i}" for i in range(13)])),
            ("long voice", kit(voice="v" * 501)),
            ("control chars", kit(handle="a\x00b")),
            ("logo not a ref", kit(logo="logo.png")),
            ("logo traversal", kit(logo={"root": "in", "name": "../x.png"})),
            ("logo is a video", kit(logo={"root": "lab", "name": "clip.mp4"})),
        ]
        for what, obj in bad:
            with self.subTest(what), self.assertRaises(KitError):
                brandkits.validate(obj)

    def test_optional_parts(self):
        k = brandkits.validate(kit(colors={"bg": "#000000", "fg": "#ffffff", "accent": "#ff0000", "muted": "",
                                           "paper": "#EEEEEE"},
                                   fonts={"display": "Bebas Neue", "body": "Inter", "mono": " JetBrains Mono "},
                                   logo={"root": "in", "name": "ref_1.png"}, default_styles=["a", "a", "b"]))
        self.assertEqual(k["colors"]["paper"], "#eeeeee")
        self.assertNotIn("muted", k["colors"])
        self.assertEqual(k["fonts"]["mono"], "JetBrains Mono")
        self.assertEqual(k["logo"], {"root": "in", "name": "ref_1.png"})
        self.assertEqual(k["default_styles"], ["a", "b"])

    def test_render_brand(self):
        b = brandkits.render_brand(brandkits.validate(kit()))
        self.assertEqual(set(b), {"name", "handle", "colors", "fonts"})

    def test_examples_are_valid_and_neutral(self):
        ex = brandkits.load_examples()
        self.assertTrue(2 <= len(ex) <= 3)
        self.assertEqual(len({k["id"] for k in ex}), len(ex))
        for k in ex:
            self.assertTrue(k["default_styles"])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.r = TempRoots()
        self.addCleanup(self.r.cleanup)
        store.configure(self.r.config())

    def test_seeded_once(self):
        kits = brandkits.list_kits()
        self.assertEqual(len(kits), len(brandkits.load_examples()))
        d = store.data_path("brandkits")
        self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)
        brandkits.delete(kits[0]["id"])
        self.assertEqual(len(brandkits.list_kits()), len(kits) - 1)   # a deleted example stays deleted

    def test_create_update_delete(self):
        made = brandkits.create(kit())
        self.assertIn("updated", made)
        with self.assertRaises(KitError) as cm:
            brandkits.create(kit())
        self.assertEqual(cm.exception.status, 409)
        got = brandkits.update("test-kit", kit(name="Renamed", id="ignored"))
        self.assertEqual((got["id"], got["name"]), ("test-kit", "Renamed"))
        self.assertEqual(brandkits.get("test-kit")["name"], "Renamed")
        with open(store.data_path("brandkits", "test-kit.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["name"], "Renamed")
        brandkits.delete("test-kit")
        for fn in (lambda: brandkits.get("test-kit"), lambda: brandkits.delete("test-kit"),
                   lambda: brandkits.update("test-kit", kit()), lambda: brandkits.get("../etc")):
            with self.assertRaises(KitError) as cm:
                fn()
            self.assertEqual(cm.exception.status, 404)

    def test_junk_files_are_ignored(self):
        brandkits.list_kits()
        d = store.data_path("brandkits")
        with open(os.path.join(d, "broken.json"), "w") as f:
            f.write("{nope")
        with open(os.path.join(d, "mismatch.json"), "w") as f:
            json.dump({"id": "other", "name": "x"}, f)
        ids = {k["id"] for k in brandkits.list_kits()}
        self.assertNotIn("broken", ids)
        self.assertNotIn("mismatch", ids)


if __name__ == "__main__":
    unittest.main()
