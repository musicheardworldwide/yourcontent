# Set up YourContent with an AI agent

Paste the prompt below into any AI agent that can run commands on the computer where YourContent will live
(Claude Code, Codex, Gemini CLI, or the agent mode of Cursor, Windsurf or Cline). It downloads the code, fetches the
models, connects to your ComfyUI, starts the app and checks that it works. It asks you before anything big.

## The prompt

````text
Set up YourContent on this computer.

1. Clone https://github.com/musicheardworldwide/yourcontent into a folder named yourcontent in my home folder.
   If that folder already exists, stop and ask me.
2. Read docs/SETUP-WITH-AI.md in the clone and follow its procedure exactly. It says what to check, what to ask me
   first, and how to prove it works.
3. Ask me before you download model files (10 to 78 GB), install system packages, use sudo, or restart or change
   my ComfyUI.
4. Never print, commit or send a token or password. Change nothing outside the yourcontent folder and the ComfyUI
   folders I point you to.
5. When it is running, tell me the address to open, which features work, and anything still missing.
````

No agent? Follow the same steps yourself: they are the README's Quick start.

---

## Procedure (for the agent)

You are setting up a local app for its owner. The owner's computer, folders and ComfyUI are theirs: look, then ask.

**Rules**

- Ask first, and wait for a yes, before: downloading models, installing or upgrading anything with a package manager,
  using `sudo`, restarting ComfyUI or changing how it is launched, opening a firewall port, or making the app
  reachable from other computers.
- Don't print tokens or passwords, don't write them into files you commit, and don't paste them into chat. Model
  downloads need no account or token (`HF_TOKEN` is only used if the owner already has it set).
- Create files only in the `yourcontent` folder (`.env`, `data/`) and in the ComfyUI folders the owner named
  (`input/media_lab`). Delete nothing.
- Do not post to any social network or send messages for the owner. YourContent never does either.
- Report problems as they are. If a step fails, say which command, what it printed, and what you tried.

### 1. Look at the machine

```bash
uname -sm; id -u                                   # the container runs as user 1000 (see Troubleshooting)
git --version; docker --version; docker compose version
docker info --format '{{.OperatingSystem}}'         # "Docker Desktop" or the Linux name (Docker Engine)
python3 --version                                   # 3.12 or newer
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader   # an NVIDIA GPU and its memory
df -h .                                             # free disk
```

Docker with Compose v2, git and Python 3.12+ are required. If one is missing, tell the owner what to install for their
system and wait. Making pictures, clips and music needs an NVIDIA card: **24 GB or more runs every feature** (32 GB is
the only size measured; see `docs/GPU.md`). With no GPU, posts, recipes, the calendar and agents still work, so say
so and skip the model steps. YourContent was tested on Linux with Docker Desktop; other setups should work but are
untested, so say what you see rather than assuming.

### 2. Find ComfyUI

YourContent runs on top of a ComfyUI the owner runs (version 0.37.0 or newer).

```bash
curl -s http://127.0.0.1:8188/system_stats        # running? "comfyui_version" must be 0.37.0 or newer
```

Ask the owner for ComfyUI's folder (the one that holds `main.py`, `models/`, `input/` and `output/`); call it
`$COMFY` below. If you can't find one, say that generating pictures, clips and music needs it, and ask whether they
want to install it (<https://github.com/comfyanonymous/ComfyUI>) or continue without. Don't install it unasked.

### 3. Get the code

Skip if the prompt already cloned it.

```bash
git clone https://github.com/musicheardworldwide/yourcontent ~/yourcontent && cd ~/yourcontent
```

### 4. Get the models

```bash
python3 -m medialab get-models all --dry-run --models-dir "$COMFY/models"
```

This lists each file, its size, what is already there and the free disk space, and downloads nothing. Tell the owner
the sizes and ask which sets they want: **pictures 30.07 GB**, **clips 38.03 GB**, **music 10.03 GB**, all 78.13 GB.
Start with pictures if they are unsure. Then, for each set they chose:

```bash
python3 -m medialab get-models pictures --models-dir "$COMFY/models"     # or clips, music, all
```

Files are checked against pinned SHA-256 hashes before they are moved into place. If the download stops, run the same
command again: it resumes. It refuses to start when the disk is too small. These are large; run it in the background
and check on it rather than waiting on one command. Pictures made with the default model (Qwen-Image-2512) are
commercial-use OK; the licenses of every model are in `THIRD-PARTY-NOTICES.md`.

### 5. Point it at ComfyUI's folders

```bash
cd ~/yourcontent
cp .env.example .env
mkdir -p data/lab "$COMFY/input/media_lab"
```

Edit `.env` so that `COMFY_OUTPUT_DIR` is `$COMFY/output` and `COMFY_UPLOADS_DIR` is `$COMFY/input/media_lab` (absolute
paths). Leave `YOURCONTENT_COMFY_URL` as it is unless ComfyUI uses another port.

### 6. Start it and check

Make sure ComfyUI is running first.

```bash
docker compose up -d --build                  # first build takes a few minutes: it fetches fonts and a headless Chromium
docker compose ps                             # app and render: running
curl -s http://127.0.0.1:8190/api/health
docker compose exec app python3 -m medialab check
```

`check` prints whether ComfyUI is reachable and, per feature (`picture`, `clip`, `extend`, `music`, `export`), `ok` or the
exact file or node that is missing, with the command that fetches it. **Done when** ComfyUI is reachable and every
feature whose models were downloaded says `ok`. When you change `.env` later, run `docker compose up -d` (a plain
`restart` does not re-read it).

**If `check` says `ComfyUI: not reachable`**, fix it before going on. The container looks for ComfyUI at
`host.docker.internal:8188`:

- **Docker Desktop** (`docker info --format '{{.OperatingSystem}}'` says `Docker Desktop`): that name is the computer
  itself, loopback services included (tested: a service bound to `127.0.0.1` answered from inside the container). So
  ComfyUI is stopped, on another port, or `.env` names the wrong URL.
- **Docker Engine on Linux** (not tested here): a container cannot reach a service that listens on `127.0.0.1` only.
  Ask the owner to restart ComfyUI with Docker's bridge address added, which keeps it off the rest of the network:

  ```bash
  docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'     # usually 172.17.0.1
  # then start ComfyUI with:   python main.py --listen 127.0.0.1,172.17.0.1
  ```

  (`--listen` alone listens on every network interface; ComfyUI has no login, so only do that on a network the owner
  trusts.) A firewall such as `ufw` can also block the container; ask before opening anything.
- ComfyUI on another port or another computer: set `YOURCONTENT_COMFY_URL` in `.env` and run `docker compose up -d`.

Restart ComfyUI only after the owner says yes.

Optional, ask first: `docker compose --profile craft up -d --build` adds the photo and video recipes (grade a photo,
title cards, reels). The first build takes about ten minutes.

### 7. Open it

The address is **http://127.0.0.1:8190**. A first-run tour explains each view. The app listens on this computer only;
leave it that way unless the owner asks. Reaching it from other computers needs `YOURCONTENT_TOKEN` (README,
*Configure*).

### 8. Optional: connect yourself

If the owner wants you to make and schedule content for them, ask, then connect over MCP. Claude Code:

```bash
claude mcp add --transport http yourcontent http://127.0.0.1:8190/mcp
```

For any other agent, add a Streamable HTTP MCP server at that address. The agent has to run on the same computer, since
the app listens on loopback. Then read `docs/AGENTS.md` (how work flows, licenses, house rules).

### 9. Report to the owner

Say, in a few lines: the address; the output of `python3 -m medialab check` in one line per feature; which model sets
are installed and which are not; what you changed outside the `yourcontent` folder (it should be `input/media_lab`, and
ComfyUI's launch flag if they agreed); and anything they still have to do.

---

## Troubleshooting

| you see | do this |
|---|---|
| `ComfyUI: not reachable` | Step 6, the part under "If `check` says". Test from inside: `docker compose exec app python3 -c "import urllib.request as u; print(u.urlopen('http://host.docker.internal:8188/system_stats', timeout=5).status)"` prints 200 when it works. |
| `is not shared from the host and is not known to Docker` | Docker Desktop only mounts folders listed under Settings > Resources > File sharing (your home folder by default). Keep `yourcontent` and ComfyUI under it, or add the folder there. |
| `Permission denied` on `/data`, `/output` or `/uploads` | The containers run as user 1000. If `id -u` is not 1000, add a `compose.override.yaml` that sets `user: "<your uid>:<your gid>"` on the `app` service (not tested), or give user 1000 write access to those folders. |
| A feature says a model is missing | Run `python3 -m medialab get-models <set> --check --models-dir "$COMFY/models"`, then download the set (step 4). ComfyUI may need a restart to see new files. |
| `port is already allocated` on 8190 | Something else uses 8190 (`ss -ltnp \| grep 8190`). Stop it, or ask the owner. |
| Build fails while fetching fonts or Chromium | The build needs internet. Check the network or proxy and run `docker compose up -d --build` again. |
| Out of memory while making a picture or clip | `docs/GPU.md`: card sizes, what fits, smaller options. |

Update later: `git pull && docker compose up -d --build`. Stop: `docker compose down` (your files stay in `data/lab`
and in ComfyUI's `output/yourcontent/`).
