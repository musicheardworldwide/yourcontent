# craft: photo and video recipes

YourContent's ComfyUI side makes pictures, clips and music. **craft** adds the editing a model can't do, by driving
four open-source desktop apps headlessly over MCP: PhotoCraft, LightCraft, FilmCraft and EffectCraft
(github.com/storytold, by the ArtCraft Team and contributors, MIT OR Apache-2.0). They are separate programs in their
own container; YourContent does not include their source and does not use their names or logos as its branding.

| recipe | app | what it makes |
|---|---|---|
| `photo.grade` | LightCraft | a look on one photo (derived from the brand kit's colours, a preset, or explicit develop values) -> JPEG/PNG |
| `photo.finish` | PhotoCraft | crop to 1:1 / 4:5 / 9:16 / 1.91:1 around a focus point, resize, curves, vibrance, warmth -> JPEG + layered PSD |
| `video.titlecard` | EffectCraft | animated title card in the brand kit's colours and fonts, with its logo and handle, over a still or text-only with alpha (ProRes 4444) |
| `video.reel` | FilmCraft | stills and clips in order, fill-frame, dissolves, captions, a music file, loudness -> H.264 MP4 + cover |

Every recipe renders only: nothing is posted or sent.

## How it fits

```
browser / agent                  app container (medialab/)                 craft container (craft/)
 POST /api/craft {recipe,      ─► craft_spec.validate ─► DATA/craft/queue/<job>.json
   params, brand}                                                            │ craftd polls (1 s)
                                                     claim: rename to DATA/craft/jobs/<job>/job.json
                                                     validate again (medialab/craft_spec.py)
                                                     copy inputs ─► DATA/craft/work/<job>/in (digit-free names)
                                                     craft-mcp <app> (bwrap: no network, only that folder writable)
                                                     outputs ─► lab root + sidecar each; jobs/<job>/status.json
 GET /api/craft/<job> ◄──────── reads status.json: outputs are {root: "lab", name} refs, served at /m/lab/<name>
```

- **No port.** The app and the runner share only the data folders; the craft container sits on an internal network
  with no way out. Its health is a heartbeat file (`DATA/craft/craftd.json`); `craft_routes.feature()` turns a missing
  or stale one into a plain reason, never an error.
- **The sandbox is the boundary.** Three of the four apps' MCP servers can read, write and delete any path they can
  reach, so each session runs inside `craft-mcp`: bubblewrap with `--unshare-all` (no network, own PID/IPC), read-only
  `/usr`, an empty read-only home, a cleared environment, and the job's own folder as the only writable path. Each
  job gets a fresh folder, removed when it ends. Bridge/GUI modes are refused.
- **The runner trusts nothing in the queue.** It re-validates every job, resolves inputs only to regular files inside
  their own root (no symlinks, no `..`; an owned ref from any root, or a file in the uploads root), copies them in
  under digit-free names (EffectCraft reads `page_07.jpg` as an image sequence), and copies back only whitelisted
  output names.
- **Provenance carries through** (`medialab/provenance.py`). Each output gets a sidecar whose inputs are its job's
  inputs: the worst licence wins (non-commercial beats unknown beats commercial), an upload with no sidecar counts as
  "rights not checked", and a text-only title card has no restricting input. The sidecar's `tool` names the app, its
  licence and commit. Outputs chain: a graded photo can be the background of a title card, which can be an item in a
  reel, and the licence follows.
- **Brand kits.** A request carries `brand: {colors, fonts, logo, handle}` (or `brand_kit: <id>` when this build has
  `medialab/brandkits.py`). `craft_spec.brand_kit()` validates the fields the recipes use and fills gaps from a neutral
  kit. Fonts are family names; put the matching TTF/OTF files in `DATA/craft/fonts/` (mounted read-only into the
  sandbox), otherwise the apps fall back to their built-in Inter.
- **AI masks are off.** LightCraft can select objects with Meta's SAM 3, whose weights are under the SAM License (a
  custom licence, not open source). Nothing loads it unless `YOURCONTENT_CRAFT_AI_MASKS=1` is set and
  `YOURCONTENT_CRAFT_SAM3_DIR` names a folder holding the model you downloaded yourself; only then is that folder
  mounted read-only into LightCraft's sandbox. No recipe uses masks yet.

## Run

```bash
docker compose --profile craft up -d --build     # builds the four apps from source at the pinned commits
```

The image (`craft/Dockerfile`) clones each app at a reviewed commit, builds only its CLI (`cargo build --release
--locked -p <app>-cli`), and copies the licences, NOTICE files and a crate/licence list into
`/usr/share/licenses/<app>/`. Bubblewrap inside Docker needs user namespaces, so the compose service relaxes the
default seccomp/AppArmor profiles and `/proc` masking for this container only (it still runs as a non-root user, with
no capabilities, a read-only root filesystem and no network).

| app | commit |
|---|---|
| photocraft | 6f39aba |
| lightcraft | 52f64ce |
| filmcraft | a4536bc |
| effectcraft | e38a92e |

**Updating an app:** upstream changes often, so never follow it automatically. Review the diff (build scripts,
`Cargo.lock` sources, new network/process/dlopen code, the MCP tool list), change the commit in `craft/Dockerfile`
and this table, rebuild, and run `tests/e2e_craft.py`.

Without Docker: build the four CLIs yourself, then `CRAFT_BIN_DIR=<folder> python3 -m craft.craftd` with the same
`YOURCONTENT_*` folders as the app (needs `bwrap`).

## Test

- `python3 -m unittest tests.test_craft`: stdlib only, no apps (recipes are faked): spec ranges and refusals, refs
  that try to leave their root, brand kits, provenance rules, a tampered queue file, bad queue names, the output
  whitelist, restart recovery, pruning, the queue cap, the sandbox environment and the wrapper's refusals, and the
  routes against a fake craft service (202/400/401/403/404, health reasons, Range on outputs).
- `CRAFT_BIN_DIR=<folder> python3 -m unittest tests.e2e_craft`: opt-in, the real apps through the sandbox (about
  10 s): every recipe once in throwaway temp folders, outputs probed with ffprobe.

## Files

| file | |
|---|---|
| `craftd.py` | the runner: claim, validate, copy in, run, file outputs with sidecars, status, prune, heartbeat |
| `recipes.py` | the four recipes: the exact MCP calls per app |
| `mcp_client.py` | stdio MCP client with per-call deadlines (a timeout kills the sandbox); the sandbox environment |
| `craft-mcp` | the bubblewrap wrapper |
| `Dockerfile`, `build-app.sh` | the craft image: four pinned source builds, licences, bubblewrap, non-root |
| `../medialab/craft_spec.py` | recipes, params, brand kits, refs: shared by the app and the runner |
| `../medialab/craft_routes.py` | the app's routes and health |
