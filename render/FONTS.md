# Fonts in the render service

The render service draws posts with 19 font families (29 files). Every one may be redistributed and embedded:
18 are under the SIL Open Font License 1.1 and one (Permanent Marker) under the Apache License 2.0. Neither
licence restricts the pictures made with the fonts, so fonts never make a post non-commercial.

The files are **not** in this repository. `fetch_fonts.py` downloads them while the image is built, from the
official Google Fonts repository at one pinned commit (`commit` in `fonts.json`), checks every file against the
sha256 in `fonts.json` and stops the build on any mismatch. The licence text of each family is fetched the same way
and installed next to the fonts (`/opt/fonts/licences/` in the image), as OFL 1.1 asks.

Checked 2026-10-10 against each family's `METADATA.pb` (`license:` field) and licence file at that commit.

| Family | Licence | Files (weight axis) | Upstream folder |
|---|---|---|---|
| Archivo Black | OFL-1.1 | Regular (400) | `ofl/archivoblack` |
| Bebas Neue | OFL-1.1 | Regular (400) | `ofl/bebasneue` |
| Big Shoulders Display | OFL-1.1 | variable (100-900) | `ofl/bigshouldersdisplay` |
| Caveat | OFL-1.1 | variable (400-700) | `ofl/caveat` |
| Cormorant Garamond | OFL-1.1 | variable (300-700), italic | `ofl/cormorantgaramond` |
| DM Serif Display | OFL-1.1 | Regular, Italic (400) | `ofl/dmserifdisplay` |
| Geist Mono | OFL-1.1 | variable (100-900) | `ofl/geistmono` |
| Inter | OFL-1.1 | variable (100-900), italic | `ofl/inter` |
| JetBrains Mono | OFL-1.1 | variable (100-800) | `ofl/jetbrainsmono` |
| Libre Caslon Text | OFL-1.1 | variable (400-700), italic | `ofl/librecaslontext` |
| Libre Franklin | OFL-1.1 | variable (100-900), italic | `ofl/librefranklin` |
| Limelight | OFL-1.1 | Regular (400) | `ofl/limelight` |
| Monoton | OFL-1.1 | Regular (400) | `ofl/monoton` |
| Old Standard TT | OFL-1.1 | Regular, Italic (400), Bold (700) | `ofl/oldstandardtt` |
| Overpass | OFL-1.1 | variable (100-900), italic | `ofl/overpass` |
| Permanent Marker | Apache-2.0 | Regular (400) | `apache/permanentmarker` |
| Playfair Display | OFL-1.1 | variable (400-900), italic | `ofl/playfairdisplay` |
| Source Serif 4 | OFL-1.1 | variable (200-900), italic | `ofl/sourceserif4` |
| Space Grotesk | OFL-1.1 | variable (300-700) | `ofl/spacegrotesk` |

A brand kit may name any of these families (the editor lists them). A family the service does not ship falls back
to the browser's generic serif, sans-serif, monospace or cursive face.

To add a family: add its files and licence to `fonts.json` with their upstream paths and sha256 (a family is only
added when its licence allows redistribution), then rebuild the image.
