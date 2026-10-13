"""The YourContent rename (2026-10-12): YOURCONTENT_* settings with MEDIA_LAB_* fallbacks, and the output folder
moving from output/media_lab/ to output/yourcontent/ without losing files made before."""
from __future__ import annotations

import os
import unittest

from medialab import actions, config, legacy, store
from tests.util import TempRoots, make_png


class EnvFallbackTest(unittest.TestCase):
    def test_new_name_old_name_and_precedence(self):
        self.assertEqual(config.load({"YOURCONTENT_COMFY_URL": "http://192.0.2.1:8188"}).comfy_url,
                         "http://192.0.2.1:8188")
        self.assertEqual(config.load({"MEDIA_LAB_COMFY_URL": "http://192.0.2.2:8188"}).comfy_url,
                         "http://192.0.2.2:8188")
        both = {"YOURCONTENT_COMFY_URL": "http://192.0.2.1:8188", "MEDIA_LAB_COMFY_URL": "http://192.0.2.2:8188"}
        self.assertEqual(config.load(both).comfy_url, "http://192.0.2.1:8188")   # the new name wins
        # an empty new name counts as unset, so the old one is used
        self.assertEqual(config.load({"YOURCONTENT_COMFY_URL": "", "MEDIA_LAB_COMFY_URL": "http://192.0.2.2:8188"})
                         .comfy_url, "http://192.0.2.2:8188")
        # the pre-v1 alias still comes last
        self.assertEqual(config.load({"COMFY_URL": "http://192.0.2.3:8188"}).comfy_url, "http://192.0.2.3:8188")

    def test_every_kind_of_setting_reads_both_names(self):
        for pre in ("YOURCONTENT_", "MEDIA_LAB_"):
            c = config.load({pre + "PORT": "8201", pre + "TOKEN": "long-enough-token", pre + "ADOPT_LEGACY": "1",
                             pre + "ALLOWED_HOSTS": "a.example:1", pre + "OUTPUT_SUBDIR": "mine",
                             pre + "REQUIRE_TOKEN": "1", pre + "TRUSTED_PROXY": "192.0.2.9"})
            self.assertEqual((c.port, c.token, c.adopt_legacy, c.allowed_hosts, c.output_subdir, c.require_token,
                              c.trusted_proxies),
                             (8201, "long-enough-token", True, ("a.example:1",), "mine", "1", ("192.0.2.9",)), pre)

    def test_env_helper_and_messages_use_the_new_name(self):
        self.assertEqual(config.env("RENDER_URL", {"MEDIA_LAB_RENDER_URL": "http://r:1"}), "http://r:1")
        self.assertEqual(config.env("YOURCONTENT_RENDER_URL", {"YOURCONTENT_RENDER_URL": "http://n:1",
                                                               "MEDIA_LAB_RENDER_URL": "http://r:1"}), "http://n:1")
        self.assertIsNone(config.env("RENDER_URL", {}))
        with self.assertRaises(config.ConfigError) as cm:
            config.load({"MEDIA_LAB_PORT": "nope"})
        self.assertIn("YOURCONTENT_PORT", str(cm.exception))

    def test_other_modules_read_both_names(self):
        from medialab import craft_spec
        self.assertTrue(craft_spec.ai_masks_enabled({"MEDIA_LAB_CRAFT_AI_MASKS": "1"}))
        self.assertTrue(craft_spec.ai_masks_enabled({"YOURCONTENT_CRAFT_AI_MASKS": "1"}))
        self.assertFalse(craft_spec.ai_masks_enabled({"YOURCONTENT_CRAFT_AI_MASKS": "0", "MEDIA_LAB_CRAFT_AI_MASKS": "1"}))


class OutputFolderTest(unittest.TestCase):
    def setUp(self):
        self.r = TempRoots(output_subdir="yourcontent")
        self.addCleanup(self.r.cleanup)
        self.legacy_dir = os.path.join(self.r.output, "media_lab")
        os.makedirs(self.legacy_dir)
        store.configure(self.r.config())
        actions._cfg = None

    def _old(self, name: str, data: bytes | None = None) -> str:
        p = os.path.join(self.legacy_dir, name)
        with open(p, "wb") as f:
            f.write(data if data is not None else make_png(None, 8, 8))
        store.put_sidecar(("gen", name), {"schema": "media-lab/asset@1", "kind": "image",
                                          "created": "2026-10-01T00:00:00Z", "commercial_ok": False})
        return p

    def test_default_folder_is_yourcontent(self):
        self.assertEqual(config.Config().output_subdir, "yourcontent")
        self.assertEqual(store.root_dir("gen"), os.path.join(self.r.output, "yourcontent"))

    def test_files_in_the_old_folder_stay_listed_served_and_usable(self):
        p = self._old("img_00001_.png")
        self.assertEqual(store.resolve(("gen", "img_00001_.png")), p)
        self.assertIn("img_00001_.png", [i["ref"]["name"] for i in store.recent()])
        self.assertEqual(store.gen_subdir("img_00001_.png"), "media_lab")
        self.assertEqual(actions.load_value(("gen", "img_00001_.png")), "media_lab/img_00001_.png [output]")
        # a LoadImage value naming the old folder maps back to the same ref (lineage of older files)
        self.assertEqual(legacy.load_ref("media_lab/img_00001_.png [output]", self.r.config()), ("gen", "img_00001_.png"))
        self.assertEqual(legacy.load_ref("yourcontent/img_00002_.png [output]", self.r.config()),
                         ("gen", "img_00002_.png"))
        self.assertNotIn(self.r.base, store.scrub(f"can't read {p}"))

    def test_new_files_go_to_the_new_folder(self):
        new = store.resolve(("gen", "img_20261012-000000_abcdef_00001_.png"))
        self.assertEqual(os.path.dirname(new), os.path.join(self.r.output, "yourcontent"))
        self.r.put("gen", "img_00003_.png", make_png(None, 8, 8))
        self.assertEqual(store.gen_subdir("img_00003_.png"), "yourcontent")
        self.assertEqual(actions.load_value(("gen", "img_00003_.png")), "yourcontent/img_00003_.png [output]")
        # a name in both folders: the new folder's file wins
        self._old("img_00003_.png", b"old")
        self.assertEqual(os.path.dirname(store.resolve(("gen", "img_00003_.png"))),
                         os.path.join(self.r.output, "yourcontent"))

    def test_delete_removes_the_old_file_and_nothing_else(self):
        self._old("img_00004_.png")
        self._old("img_00005_.png")
        self.assertEqual(store.remove(("gen", "img_00004_.png")), ["img_00004_.png"])
        self.assertEqual(sorted(os.listdir(self.legacy_dir)), ["img_00005_.png"])
        self.assertFalse(store.owned(("gen", "img_00004_.png")))

    def test_no_legacy_lookup_when_the_old_name_is_configured(self):
        r = TempRoots(output_subdir="media_lab")
        self.addCleanup(r.cleanup)
        store.configure(r.config())
        self.assertEqual(store.root_dir("gen"), os.path.join(r.output, "media_lab"))
        self.assertEqual(store.gen_subdir("x.png"), "media_lab")


if __name__ == "__main__":
    unittest.main()
