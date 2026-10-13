# MAP — media-lab

Design: `docs/DESIGN.md` (v0.2). This map is the short version.

```
browser: static/index.html + app.js + style.css (vanilla, strict CSP, no inline JS)
   │  JSON over HTTP; every POST carries X-Media-Lab: 1
   ▼
app.py → medialab/server.py (ThreadingHTTPServer)
   │  guard: method, Host allowlist, optional token (cookie or Bearer), CSRF header + Origin, body limits
   ├─ projects.py   project JSON (rev, autosave, .bak)        ├─ jobs.py     two workers: gpu | ffmpeg
   ├─ actions.py    generate / animate / extend / music / upload / delete (writes sidecars + ledger)
   ├─ timeline.py   PURE planner: integer frames and samples ├─ export.py   PURE graph builders + render runner
   ├─ graphs.py     PURE ComfyUI graphs                       ├─ comfy.py    ComfyUI HTTP client
   ├─ provenance.py PURE licence maths + sidecar shape        ├─ legacy.py   adoption of pre-v1 files
   ├─ models.py     models.default.json + licences.json + DATA/config/models.json
   ├─ features.py   PURE: which features work, and why not    ├─ config.py   YOURCONTENT_* env (MEDIA_LAB_* fallback)
   ├─ store.py      roots, refs, path safety, sidecar index, ledger, scoped delete
   ├─ media.py      ffprobe/ffmpeg helpers (probe cache, audio conform, posters, last frame, stitch)
   ├─ studio extensions (server.install_extension: routes + static files, injected into index.html):
   │    calendar.py + calendar_routes.py   content calendar, .ics feed, secret subscribe link
   │    google.py + google_routes.py       Connect Google (drive.file only): OAuth PKCE, workspace, upload, Inbox
   │    plan_sync.py                       calendar <-> Content Plan Sheet, both ways
   │    render_routes.py                   posts: brandkits.py (PURE + DATA/brandkits), render_client.py
   │    agents.py + agent_routes.py        scoped agent tokens + agent_setup.py (setup prompt)
   │    mcp_routes.py, openapi.py          MCP at /mcp (tools proxy the API), OpenAPI at /api/openapi.json
   └─   ui_routes.py                       studio.js (views) + tour.js (first-run walkthrough)
ComfyUI (GPU, HTTP /prompt /history /queue /interrupt /upload/image /object_info)
ffmpeg/ffprobe (subprocess argv lists, never a shell)
render/ (own container, internal network only): server.py stdlib HTTP, POST /render + GET /styles → headless
   Chromium (Playwright) draws one of 35 styles (styles/styles.json + styles/templates.js, engine.js) with the
   brand kit's colours and fonts; fonts fetched at build time, hash-pinned (fonts.json, FONTS.md)
```

Posts: `POST /api/posts/render` sends the kit, text and pictures (as data URLs) to the render service; a saved post
lands in `lab` as `post_*.png|jpg` with a sidecar whose inputs are its pictures and the kit's logo (licence: worst
of those; text-only = commercial OK; an unknown input = unknown). `preview: true` returns the image unsaved.

Folders (a file is always a ref `{root, name}`, never a path):

| root | folder | holds |
|---|---|---|
| `out` | ComfyUI `output/` (flat) | adopted legacy files (`qwen_studio_*`, `qwen_video_*`) |
| `gen` | ComfyUI `output/yourcontent/` (+ `output/media_lab/`, read for files made before the rename) | new pictures, clips, extensions, music |
| `in` | ComfyUI `input/<subdir>/` | picture uploads, temporary last frames |
| `lab` | `DATA/media/` | video/audio uploads, exports |

`DATA` (`data/lab/` on the host, `/data` in the container) holds the sidecars (`sidecars/<root>/<name>.json`: a
file is owned iff its sidecar exists), the ledger of ComfyUI prompt ids, projects, caches (probe, conformed music,
mixes, proxies) and `config/models.json`.

Flows:
- **Picture:** default Qwen-Image-2512 (`graphs.image_graph_2512`, ComfyUI's own template: CLIPTextEncode with the
  Qwen2.5-VL encoder, ModelSamplingAuraFlow 3.1, EmptySD3LatentImage, 50 steps at cfg 4; text only, refs refused).
  Qwen-Image 2.1 (variant `bf16`): `TextEncodeQwenImage21` takes up to 6 references as `images.image_N` plus the VAE.
  A variant's `graph` in models.default.json picks the family; one without it is a 2.1 variant.
- **Clip:** two-expert Wan 2.2 (high noise steps 0–2, then low noise), 16 fps; VideoHelperSuite writes the mp4 and
  a poster png.
- **Extend:** ffmpeg grabs the last frame, Wan animates from it at the same size, ffmpeg joins the two without the
  repeated frame; the segment and its history entry are removed.
- **Music:** ACE-Step 1.5 turbo, 10-node graph, lossless FLAC via SaveAudio (SaveAudioAdvanced as fallback).
- **Preview / export:** `timeline.plan` (integer frames, crossfade clamp, music window) → one two-pass-normalised
  48 kHz mix shared by proxy and final → one `filter_complex` → verify frames and audio length → sidecar with the
  render block and inherited licence.
- **Delete:** file + poster + sidecar, then `POST /history {delete: [ledger pids]}`; nothing else in ComfyUI.
- **Start:** empty `DATA/tmp`, configure modules, adopt legacy files when `YOURCONTENT_ADOPT_LEGACY=1` (idempotent;
  originals are only read).
