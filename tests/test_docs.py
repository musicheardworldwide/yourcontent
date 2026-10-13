"""The generated API docs match the live route table (regenerate: python3 -m medialab openapi [--md])."""
from __future__ import annotations

import json
import os
import unittest

from medialab import openapi, server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GeneratedDocs(unittest.TestCase):
    def test_openapi_and_api_md_are_current(self):
        spec = openapi.build(server.ROUTES)
        with open(os.path.join(ROOT, "docs", "openapi.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), spec, "docs/openapi.json is stale: python3 -m medialab openapi")
        with open(os.path.join(ROOT, "docs", "API.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), openapi.to_markdown(spec), "docs/API.md is stale: python3 -m medialab openapi --md")

    def test_every_route_has_a_summary(self):
        spec = openapi.build(server.ROUTES)
        bare = [f"{m.upper()} {p}" for p, ops in spec["paths"].items() for m, op in ops.items() if op["summary"] == p]
        self.assertEqual(bare, [])


if __name__ == "__main__":
    unittest.main()
