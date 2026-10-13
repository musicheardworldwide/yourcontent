"""Static files for the studio shell (sidebar and view switching), Home/Library/Settings and the first-run
walkthrough. No routes of its own. home.js goes before studio.js: the shell only shows views whose module loaded."""
from __future__ import annotations

STATIC = {"/static/home.js": ("home.js", "text/javascript; charset=utf-8"),
          "/static/studio.js": ("studio.js", "text/javascript; charset=utf-8"),
          "/static/studio.css": ("studio.css", "text/css; charset=utf-8"),
          "/static/tour.js": ("tour.js", "text/javascript; charset=utf-8")}


def routes(srv):
    return []
