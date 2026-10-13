# YourContent

Make pictures, clips and music on your own GPU, turn them into posts and reels in your brand, plan them on a
calendar, and let an AI agent help: one app in the browser, on top of a
[ComfyUI](https://github.com/comfyanonymous/ComfyUI) you run. Nothing leaves your computer unless you connect it.

**What it does**

| view | what you do there |
|---|---|
| **Make** | Pictures (Qwen-Image-2512, commercial use OK; or Qwen-Image 2.1, non-commercial, with up to 6 reference pictures), clips (Wan 2.2 image-to-video, Extend), music (ACE-Step 1.5); a cut with one video track, music, fades and crossfades; export one MP4 for 9:16, 4:5, 1:1 or 16:9 |
| **Posts** | Brand kits (colours, fonts, logo, handle, voice) and 35 post styles rendered to PNG/JPG; photo and video recipes: grade a photo, finish it to a post format with a layered PSD, an animated title card, a reel from stills and clips with captions and music |
| **Calendar** | Plan posts as ideas, drafts, ready or posted, by month or week; drag to move; a private subscribe link for Google, Apple or Outlook Calendar |
| **Connect** | Google Drive and Sheets (a workspace folder and a Content Plan Sheet, synced both ways with the calendar); a setup prompt for your AI agent (Claude, ChatGPT, Hermes or any MCP client) |

**Labels:** every file gets a sidecar saying which models made it and under which licence, and anything made from
other files inherits the most restrictive one. YourContent warns; it never blocks. **It never posts** to a social
network: finished work goes on the calendar and you publish it.

A first-run tour walks through all of this; run it again from **Tour** in the header.

Stack: Python 3.12 standard library, ffmpeg 7.1 and vanilla JavaScript for the app (no build step, no pip or npm
packages); a headless-Chromium container draws posts; an optional container runs the photo and video editors
([PhotoCraft, LightCraft, FilmCraft, EffectCraft](https://github.com/storytold), MIT OR Apache-2.0) in a sandbox.

## Quick start

You need Docker, git, Python 3.12+, a ComfyUI 0.37+ with an NVIDIA card (24 GB for every feature; see the GPU
guide in `docs/GPU.md`), and the models:

```bash
git clone https://github.com/musicheardworldwide/yourcontent && cd yourcontent
python3 -m medialab get-models pictures --models-dir /path/to/ComfyUI/models   # also: clips, music, all
mkdir -p data/lab
docker compose up -d --build                     # app + post renderer; open http://127.0.0.1:8190
docker compose --profile craft up -d --build     # optional: photo/video recipes (first build ~10 min)
```

`get-models pictures` fetches the default picture model, Qwen-Image-2512 (30.07 GB). For Qwen-Image 2.1 (reference
pictures, non-commercial) add `--variant bf16`. The containers are `yourcontent`, `yourcontent-render` and
`yourcontent-craft` (`docker logs yourcontent`).

No GPU? Everything except making pictures, clips and music works without one: posts, recipes, the calendar,
Google and agents. A hosted version for people without a GPU is planned: join the waitlist at
<https://yourcontent.online/#waitlist>.

**Agents:** open Connect > *Your AI agent*, or read `docs/AGENTS.md`. MCP is served at `/mcp`, the HTTP API is
described at `/api/openapi.json`, and `llms.txt` lists the docs for language models.

## Configure

```bash
mkdir -p data/lab                 # so the data folder belongs to you, not to root
docker compose up -d --build      # then open http://127.0.0.1:8190
```

Point it at your ComfyUI and its folders with a `compose.override.yaml` next to `compose.yaml` (gitignored):

| variable | what |
|---|---|
| `YOURCONTENT_COMFY_URL` | your ComfyUI, e.g. `http://host.docker.internal:8188` |
| volumes `/output`, `/uploads` | ComfyUI's `output/` folder and a subfolder of its `input/` |
| `YOURCONTENT_INPUT_SUBDIR` | the name of that `input/` subfolder (default `media_lab`) |
| `YOURCONTENT_ALLOWED_HOSTS` | extra `host:port` names the page is opened by; other `Host` headers get 403. This stops DNS rebinding through a browser only: it is **not** access control, since any other client can send `Host: localhost` |
| `YOURCONTENT_PUBLISH` | the host address the port is published on (default `127.0.0.1`). Anything else needs `YOURCONTENT_TOKEN`, or the app refuses to start. If you replace `ports` in the override instead, set `YOURCONTENT_PUBLISHED_HOST` to the same address |
| `YOURCONTENT_TOKEN` | required whenever the port is reachable from other machines; when set, the page asks for it once |
| `YOURCONTENT_TRUSTED_PROXY` | your reverse proxy's IP address(es): wrong-token limits then count per client (last `X-Forwarded-For` entry) instead of per proxy |
| `YOURCONTENT_ADOPT_LEGACY` | `1` adopts files made by the older single-file app (`qwen_studio_*`, `qwen_video_*`) |

**Older names still work.** Before it was YourContent the app was called Media Lab, and its settings were
`MEDIA_LAB_*`. Every `YOURCONTENT_*` setting is also read under its `MEDIA_LAB_*` name, in `.env`, in the shell or in
a `compose.override.yaml`; when both are set, the `YOURCONTENT_*` one wins.

Your own model files (fine-tunes, other quantisations) go in `data/lab/config/models.json`, which is merged over
the shipped `medialab/models.default.json`. A variant set to `null` there hides a shipped one. Edit it **in place**
(`cp new.json models.json` or save in an editor that overwrites the file): on Docker Desktop a save that renames a
new file over the old one can leave the container reading the old content. `/api/health` (`models_json`) and
`python3 -m medialab check` show the sha256 of the file actually read; compare it with `sha256sum` on the host.

`python3 -m medialab check` prints the configuration (token hidden), whether ComfyUI is reachable, which features
work and which files or nodes are missing. Without Docker: `python3 app.py` (defaults to 127.0.0.1:8190).

New ComfyUI outputs go to `output/yourcontent/`. Files an earlier version made in `output/media_lab/` stay where they
are and stay listed, usable and deletable; nothing is moved. YourContent only lists, serves and deletes files it made or adopted;
**Delete** removes the file for good and drops its ComfyUI history entry by prompt id.

**Plain HTTP.** The server binds 127.0.0.1 by default and refuses a non-loopback bind without a token. In the
container it listens on 0.0.0.0, so it judges the publish address (`YOURCONTENT_PUBLISH` / `YOURCONTENT_PUBLISHED_HOST`)
instead, and prints a warning at every start while it runs without a token. Only the token is access control. Beyond your
own machine or a private network, put it behind an HTTPS reverse proxy and set `YOURCONTENT_TOKEN` and
`YOURCONTENT_TRUSTED_PROXY`.

Every delete is written to the server's stderr (`docker logs yourcontent`) with the UTC time, the file and the client
address.

## Test

```bash
python3 -m unittest discover -s tests                     # stdlib only; real-ffmpeg tests skip without ffmpeg 7.1
docker run --rm --user 1000:1000 -e HOME=/tmp -v "$PWD":/app -w /app yourcontent:latest \
  python3 -m unittest discover -s tests                   # everything, with ffmpeg 7.1.5
python3 -m py_compile app.py medialab/*.py                # typecheck-equivalent
```

`tests/e2e_comfy.py` runs real GPU jobs against a real ComfyUI and is opt-in only (see its docstring). The UI
tests use Playwright when it is installed and skip otherwise.

## Deploy

None: it is a local app.

## Models and licences

YourContent's code is source-available under the **PolyForm Small Business License 1.0.0** (`LICENSE`). You may
use, change and share it for the benefit of a company or other organisation (a sole proprietorship or a non-profit
counts) that had fewer than 100 people working for it, employees and contractors together, and less than
US$1,000,000 revenue (2019 dollars, adjusted for inflation) in the prior tax year; organisations it controls or is
controlled by count too. Bigger organisations need a separate licence from the author (see below). It is not an
OSI open-source licence. Model weights are not included; each keeps its own licence. Full list:
`THIRD-PARTY-NOTICES.md`.

| model | used for | licence | commercial use |
|---|---|---|---|
| **Qwen-Image-2512** (default; fp8 file from the Comfy-Org repack) | pictures | Apache-2.0 | **yes**, outputs commercial OK |
| Qwen2.5-VL 7B text encoder (fp8), Qwen-Image VAE | pictures (2512) | Apache-2.0 | yes |
| Qwen-Image 2.1 (selectable; every variant, fine-tune and LoRA) | pictures with references | Qwen Research Licence | **No** (non-commercial only) |
| Qwen3-VL 8B text encoder (int8 file from the Comfy-Org repack) | pictures (2.1) | Qwen research licence (the repack's) | **No** |
| Wan 2.2 I2V A14B, Wan 2.1 VAE | clips | Apache-2.0 | yes |
| lightx2v Wan 2.2 4-step LoRAs | clips | Apache-2.0 | yes |
| umt5-xxl | clips | Apache-2.0 | yes |
| ACE-Step 1.5 turbo (+ its 0.6B/1.7B encoders, VAE) | music | Apache-2.0 / MIT | yes, outputs commercial OK |

Pictures made with Qwen-Image-2512 are labelled commercial OK. Anything that contains a Qwen-Image 2.1 picture,
directly or through a clip or cut made from it, is marked non-commercial. Uploads are marked "rights not checked".
Qwen-Image-2512 is text-to-image only; reference pictures need Qwen-Image 2.1 (Make > Advanced > Model).

## Licences for larger companies and the hosted version

- **Larger companies** (100 people or more, or US$1M revenue or more) need a commercial licence: see
  <https://yourcontent.online/licence>.
- **No GPU?** A hosted version is on the way; join the waitlist at <https://yourcontent.online/#waitlist>.

## Responsible use

YourContent doesn't filter prompts. You're responsible for what you make: don't depict real people without their
consent, never sexualise minors, and follow the law where you live. Some models are non-commercial; every file is
labelled. The page shows this notice once and keeps a one-line reminder in its footer.

## Where things are

- `app.py`: entrypoint shim. `medialab/`: the server and modules (map in `MAP.md`). `static/`: the page.
- `render/`: the post renderer (headless Chromium). `craft/`: the photo/video recipe runner and its sandbox.
- `docs/AGENTS.md`: driving YourContent from an AI agent. `docs/GOOGLE.md`: Google setup. `docs/API.md`: the HTTP
  API. `docs/GPU.md`: cards, VRAM, download sizes. `docs/MEASUREMENTS.md`: timings.
