#!/usr/bin/env python3
"""YourContent's logo: a page with its corner folded and a play button (direction B, chosen 2026-10-12).

    python3 brand/build.py           SVG masters in brand/ and static/favicon.svg (tracked)
    python3 brand/build.py --png     also PNG exports in brand/export/ (git-ignored), rendered by headless Firefox

The lockup's lettering is Space Grotesk Bold, outlined, so no SVG needs a font. The font comes from the app's
pinned, hash-checked set: python3 render/fetch_fonts.py --out ~/.cache/brand-fonts --family "Space Grotesk".
Needs fontTools (pip install fonttools) for the outlines; the app itself does not.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPORT = ROOT / "export"
FONT = Path.home() / ".cache/brand-fonts/SpaceGrotesk_wght.ttf"

# The app's own theme tokens (static/tokens.css): the fold is the accent, the page is the text colour.
LIGHT = {"page": "#1b1a17", "fold": "#ad4f19", "ground": "#f4f3ef"}
DARK = {"page": "#ecebe7", "fold": "#e8b14a", "ground": "#121313"}

PAGE = "M12 8h27l13 13v31a4 4 0 0 1-4 4H16a4 4 0 0 1-4-4z"
FOLD = "M39 8v13h13z"
PLAY = "M27 29l14 9-14 9z"


def mark(c, x=0, y=0, size=64):
    k = size / 64
    return (f'<g transform="translate({x} {y}) scale({k})"><path d="{PAGE}" fill="{c["page"]}"/>'
            f'<path d="{FOLD}" fill="{c["fold"]}"/><path d="{PLAY}" fill="{c["ground"]}"/></g>')


def svg(w, h, body, title="YourContent"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<title>{title}</title>{body}</svg>\n')


def tile(size, c=DARK, radius=0.22, inset=0.16):
    """App icon / favicon: the mark on a rounded tile of the dark ground."""
    m = size * (1 - 2 * inset)
    return (f'<rect width="{size}" height="{size}" rx="{size * radius:.1f}" fill="{c["ground"]}"/>'
            + mark(c, size * inset, size * inset * 1.05, m))


class Lettering:
    def __init__(self):
        from fontTools.ttLib import TTFont
        from fontTools.varLib.instancer import instantiateVariableFont
        if not FONT.exists():
            sys.exit(f"missing {FONT}: python3 render/fetch_fonts.py --out ~/.cache/brand-fonts --family 'Space Grotesk'")
        self.font = instantiateVariableFont(TTFont(FONT), {"wght": 700})
        self.glyphs, self.cmap = self.font.getGlyphSet(), self.font.getBestCmap()
        self.upm = self.font["head"].unitsPerEm

    def line(self, text, size, x, y, fill, tracking=0.0):
        from fontTools.pens.svgPathPen import SVGPathPen
        s, out = size / self.upm, []
        for ch in text:
            g = self.glyphs[self.cmap[ord(ch)]]
            pen = SVGPathPen(self.glyphs)
            g.draw(pen)
            if pen.getCommands():
                out.append(f'<path transform="translate({x:.2f} {y:.2f}) scale({s:.5f} {-s:.5f})" d="{pen.getCommands()}"/>')
            x += g.width * s + tracking
        return f'<g fill="{fill}">{"".join(out)}</g>', x


def lockup(c, letters):
    words, end = letters.line("YourContent", 44, 78, 47, c["page"], tracking=-0.6)
    return svg(round(end) + 4, 64, mark(c) + words)


def masters():
    letters = Lettering()
    return {
        "yourcontent-mark-light.svg": svg(64, 64, mark(LIGHT)),
        "yourcontent-mark-dark.svg": svg(64, 64, mark(DARK)),
        "yourcontent-icon.svg": svg(64, 64, tile(64)),
        "yourcontent-lockup-light.svg": lockup(LIGHT, letters),
        "yourcontent-lockup-dark.svg": lockup(DARK, letters),
    }


def exports(letters):
    out = {"yourcontent-icon-512.svg": svg(512, 512, tile(512)),
           "yourcontent-icon-1024.svg": svg(1024, 1024, tile(1024)),
           "yourcontent-avatar-1080.svg": svg(1080, 1080, f'<rect width="1080" height="1080" fill="{DARK["ground"]}"/>'
                                                 + mark(DARK, 250, 240, 580))}
    words, end = letters.line("YourContent", 112, 0, 0, DARK["page"], tracking=-1.5)
    tag, _ = letters.line("make · post · plan", 34, 0, 0, DARK["fold"], tracking=3)
    w = 160 + 40 + end
    x0 = (1200 - w) / 2
    card = (f'<rect width="1200" height="630" fill="{DARK["ground"]}"/>' + mark(DARK, x0, 235, 160)
            + f'<g transform="translate({x0 + 200} 345)">{words}</g><g transform="translate({x0 + 204} 400)">{tag}</g>')
    out["yourcontent-og-1200x630.svg"] = svg(1200, 630, card)
    return out


def _shoot(stage, name, text, w, h, bg):
    page = stage / f"{name}.{bg}.html"
    page.write_text(f'<!doctype html><style>html,body{{margin:0;overflow:hidden;background:{bg}}}'
                    f'svg{{display:block}}</style>{text}')
    png = stage / f"{name}.{bg}.png"
    subprocess.run(["firefox", "--headless", "--no-remote", "--profile", str(stage / "prof"),
                    f"--window-size={w},{h}", "--screenshot", str(png), page.as_uri()],
                   check=True, capture_output=True, timeout=120)
    return png


def render_png(svgs, transparent=()):
    """Headless Firefox (Chrome screenshots hang on the dev machine; snap Firefox only reads ~/snap). Screenshots have no
    alpha, so a transparent master is shot on black and on white and the alpha is the difference."""
    from PIL import Image, ImageChops
    stage = Path.home() / "snap/firefox/common/yourcontent-brand"
    (stage / "prof").mkdir(parents=True, exist_ok=True)
    for name, text in svgs.items():
        w, h = int(text.split('width="')[1].split('"')[0]), int(text.split('height="')[1].split('"')[0])
        out = EXPORT / name.replace(".svg", ".png")
        if name not in transparent:
            shutil.copy(_shoot(stage, name, text, w, h, "black"), out)
        else:
            black = Image.open(_shoot(stage, name, text, w, h, "black")).convert("RGB")
            white = Image.open(_shoot(stage, name, text, w, h, "white")).convert("RGB")
            alpha = ImageChops.invert(ImageChops.subtract(white, black).convert("L"))
            rgba = Image.new("RGBA", (w, h))
            pb, pa, po = black.load(), alpha.load(), rgba.load()
            for y in range(h):
                for x in range(w):
                    a = pa[x, y]
                    if a:
                        r, g, b = pb[x, y]
                        po[x, y] = (min(255, r * 255 // a), min(255, g * 255 // a), min(255, b * 255 // a), a)
            rgba.save(out)
        print("brand/export/" + out.name)


def at_size(text, w, h):
    old = text.split(" viewBox")[0].split("<svg ")[1]
    return text.replace(old, f'xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"', 1)


def main():
    ms = masters()
    for name, text in ms.items():
        (ROOT / name).write_text(text)
        print("brand/" + name)
    (ROOT.parent / "static/favicon.svg").write_text(ms["yourcontent-icon.svg"])
    print("static/favicon.svg")
    if "--png" in sys.argv:
        EXPORT.mkdir(exist_ok=True)
        sized = {k: at_size(v, 1024, 1024) for k, v in ms.items() if "mark" in k}
        for k in ("yourcontent-lockup-light.svg", "yourcontent-lockup-dark.svg"):
            w = int(ms[k].split('width="')[1].split('"')[0])
            sized[k] = at_size(ms[k], w * 8, 64 * 8)
        render_png({**sized, **exports(Lettering())}, transparent=set(sized))


if __name__ == "__main__":
    main()
