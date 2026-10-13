#!/bin/sh
# build-app.sh <app> <full commit>: used by craft/Dockerfile's build stages.
# Clones github.com/storytold/<app> at exactly that commit, builds its CLI alone (`-p <app>-cli`, locked), and
# collects the binary, the licence and NOTICE files and a dependency/licence list into /out.
set -eu
app="$1"; commit="$2"
case "$app" in photocraft|lightcraft|filmcraft|effectcraft) ;; *) echo "unknown app: $app" >&2; exit 2;; esac
mkdir -p "/src/$app" && cd "/src/$app"
git init -q .
git remote add origin "https://github.com/storytold/$app"
git fetch -q --depth 1 origin "$commit"
git checkout -q FETCH_HEAD
[ "$(git rev-parse HEAD)" = "$commit" ] || { echo "$app: fetched $(git rev-parse HEAD), wanted $commit" >&2; exit 1; }
cargo build --release --locked -p "$app-cli"
install -D -m 0755 "target/release/$app-cli" "/out/bin/$app-cli"
lic="/out/licenses/$app"
mkdir -p "$lic"
cp LICENSE-MIT LICENSE-APACHE NOTICE "$lic/"
[ -f ATTRIBUTION.md ] && cp ATTRIBUTION.md "$lic/"
[ -f assets/ATTRIBUTION.md ] && cp assets/ATTRIBUTION.md "$lic/ATTRIBUTION-assets.md"
# Licence texts of bundled fonts, icons and word lists (OFL-*.txt, LICENSE-*.txt under assets/).
find assets -type f \( -name 'OFL-*.txt' -o -name 'LICENSE-*.txt' \) -exec cp {} "$lic/" \;
# Every crate compiled into the CLI, with its declared licence (MPL-2.0 crates such as symphonia: see
# THIRD-PARTY-NOTICES.md for where their source is available).
cargo tree --locked -p "$app-cli" -e normal --prefix none -f '{p} {l}' | sort -u > "$lic/DEPENDENCIES.txt"
mkdir -p /out/versions
printf '{"%s": "%s"}\n' "$app" "$commit" > "/out/versions/$app.json"
