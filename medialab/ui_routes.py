"""Static files for the studio shell (view switching) and the first-run walkthrough. No routes of its own."""
from __future__ import annotations

STATIC = {"/static/studio.js": ("studio.js", "text/javascript; charset=utf-8"),
          "/static/studio.css": ("studio.css", "text/css; charset=utf-8"),
          "/static/tour.js": ("tour.js", "text/javascript; charset=utf-8")}


def routes(srv):
    return []
