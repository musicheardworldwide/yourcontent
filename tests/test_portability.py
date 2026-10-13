"""WP3: public-release deny-scan (§14 test_portability). Shipped code and data name no machine, no private
path, no network address and no owner-specific model file. Machine values live only in gitignored files."""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_DIRS = ("medialab", "static", "render", "craft")
SHIPPED_FILES = ("Dockerfile", "compose.yaml")
SKIP_DIRS = {"__pycache__"}
TEXT_EXT = {".py", ".json", ".js", ".css", ".html", ".md", ".txt", ".yaml", ".yml", ".svg", ".sh", ""}

# The names are split so this file does not itself trip a repo-wide scan.
_J = "".join
DENY = [
    ("a home folder path", re.compile(_J(["/ho", "me/"]))),
    ("the dev machine's name", re.compile(_J(["crea", "ture"]), re.I)),
    ("the private network name", re.compile(_J(["tailc", "39d0c"]), re.I)),
    ("the hub's name", re.compile(_J(["sin", "-pi"]), re.I)),
    # The public repository itself lives under that account (Wes, 2026-10-12), so its own address is allowed.
    ("the GitHub account", re.compile(_J(["musicheard", "worldwide"]) + r"(?!/yourcontent(?:[\s/)>`\]]|\.git|$))",
                                      re.I)),
    ("a private network address", re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.")),
    ("the word 'un" "censored'", re.compile(_J(["un", "censored"]), re.I)),
    ("an owner model name", re.compile(r"\b" + _J(["u", "c-"]), re.I)),
    # The owner's estate (merge plan t:mg-08): the private posting back office, its agents and the fleet.
    ("the private posting service", re.compile(_J(["post", "forge"]), re.I)),
    ("the owner's agent", re.compile(r"\b" + _J(["Mu", "se"]) + r"\b|\b" + _J(["Ro", "se"]) + r"\b")),
    ("the owner's media brand", re.compile(r"\b" + _J(["The ", "Connect"]) + r"\b|(?i:" + _J(["connect", "-la"]) + ")")),
    ("the fleet's agent home", re.compile(_J([r"\.her", "mes/"]) + "|" + _J(["watch", "tower"]), re.I)),
    ("a trusted identity header", re.compile(_J(["Tailscale-", "User-Login"]) + "|" + _J(["X-Style", "-Lab"]), re.I)),
    ("the fleet share", re.compile(_J(["/srv/", "fleet"]))),
    ("a private network domain", re.compile(_J([r"\.ts", r"\.net\b"]), re.I)),
]


def shipped_files() -> list[str]:
    out = []
    for d in SHIPPED_DIRS:
        for base, dirs, files in os.walk(os.path.join(ROOT, d)):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for n in files:
                if os.path.splitext(n)[1].lower() in TEXT_EXT:
                    out.append(os.path.join(base, n))
    for f in SHIPPED_FILES:
        p = os.path.join(ROOT, f)
        if os.path.isfile(p):
            out.append(p)
    return sorted(out)


def scan(paths) -> list[str]:
    hits = []
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                for what, rx in DENY:
                    if rx.search(line):
                        hits.append(f"{os.path.relpath(p, ROOT)}:{i}: {what}")
    return hits


class TestDenyScan(unittest.TestCase):
    def test_scan_covers_the_shipped_tree(self):
        files = [os.path.relpath(p, ROOT) for p in shipped_files()]
        for must in ("medialab/models.default.json", "medialab/licences.json", "medialab/graphs.py",
                     "static/index.html", "Dockerfile", "compose.yaml", "render/server.py",
                     "render/styles/templates.js", "render/styles/styles.json", "render/Dockerfile",
                     "render/fonts.json", "static/posts.js", "craft/craftd.py", "craft/craft-mcp",
                     "craft/Dockerfile", "craft/build-app.sh"):
            self.assertIn(must, files)

    def test_shipped_files_are_clean(self):
        hits = scan(shipped_files())
        self.assertEqual(hits, [], "shipped files must stay machine-neutral:\n" + "\n".join(hits))

    def test_the_scan_bites(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write("URL = 'http://100." + "101.1.2:8188'\nNAME = 'qwen-image-2.1-" + "U" + "C-int8'\nP = '/ho" + "me/x'\n")
        try:
            self.assertEqual(len(scan([f.name])), 3)
        finally:
            os.remove(f.name)

    def test_test_fixtures_are_clean_too(self):
        """Fixtures are committed: they must not carry local model names or paths either."""
        fx = os.path.join(ROOT, "tests", "fixtures")
        paths = [os.path.join(fx, n) for n in sorted(os.listdir(fx)) if n.endswith(".json")]
        self.assertEqual(scan(paths), [])


if __name__ == "__main__":
    unittest.main()


class TestNotices(unittest.TestCase):
    def test_third_party_fixtures_are_declared(self):
        """Review r2: a fixture copied from another project is named in THIRD-PARTY-NOTICES.md."""
        with open(os.path.join(ROOT, "THIRD-PARTY-NOTICES.md"), encoding="utf-8") as f:
            notices = f.read()
        for n in os.listdir(os.path.join(ROOT, "tests", "fixtures")):
            if n.startswith("object_info_"):
                self.assertIn(f"tests/fixtures/{n}", notices)
        self.assertNotIn("contains no third-party code and", notices)
