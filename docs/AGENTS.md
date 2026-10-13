# Driving YourContent from an AI agent

This guide is for AI agents (and the people who set them up) that use YourContent to make and plan content. It
is not a guide to working on YourContent's code; see `MAP.md` for that.

## 1. Connect

Open YourContent > **Connect** > *Your AI agent*, name the agent, pick its kind and press **Make setup prompt**.
Paste the prompt into the agent. It contains the address, a credential if this install needs one, the MCP
command for that kind of agent and the house rules.

- **MCP (preferred):** Streamable HTTP at `<address>/mcp`, JSON responses, no event stream.
  - Claude Code: `claude mcp add --transport http yourcontent http://127.0.0.1:8190/mcp` (add
    `--header "Authorization: Bearer <token>"` when the install needs a token).
  - Clients that speak only stdio can bridge with `npx -y mcp-remote <address>/mcp` (add `--allow-http` for a
    plain-HTTP address and `--header "Authorization: Bearer <token>"` when needed).
- **HTTP API:** every POST needs `X-Media-Lab: 1` and a JSON body; see `docs/API.md` and `docs/openapi.json`.

### Credentials

| install | what the agent needs |
|---|---|
| Listens only on this computer (the default) | nothing; any program on the computer can use it, so the agent must run there |
| Reachable from other machines (`YOURCONTENT_TOKEN` set) | an **agent token** (`mla_…`) from the setup prompt, sent as `Authorization: Bearer` |

An agent token can make, arrange and schedule. It **cannot** delete files, cuts, calendar entries or brand
kits, cannot make or list other agents, and cannot connect or disconnect Google. Only its SHA-256 is stored;
the owner revokes it in Connect.

## 2. How work flows

1. `app_status`: which features work, and the exact missing model or node if one does not.
2. Make material: `generate_picture`, `animate_picture`, `extend_clip`, `make_music`. Each returns `{job}`.
3. Wait: `job_status` with `wait_seconds` (up to 110) until `state` is `done` or `failed`. A picture takes about
   30 s on a 24 GB card, a 5 s clip a few minutes. Don't resubmit a job that is still running.
4. Shape it:
   - a styled post: `list_brandkits`, `list_styles`, then `render_post`;
   - a graded photo or a reel from stills and clips: `craft_recipes`, then `craft_run` and `craft_status`;
   - a cut with music: `create_project`, `save_project` (send the `rev` you read), `export_project`.
5. Plan it: `calendar_add` with `status: "ready"` and the file as `asset`. The owner publishes.
6. Optional: `google_upload` copies a finished file to the owner's Drive workspace.

Files are always references `{root, name}` as `list_media` returns them, never paths.

## 3. Licences

Every file has a sidecar with the models that made it and their licences, and anything made from other files
inherits the most restrictive one.

- `commercial_ok: false`: say so before suggesting the file for business or paid use (for example, anything made
  with the Qwen-Image 2.1 picture model is non-commercial; the default, Qwen-Image-2512, is commercial OK).
- `commercial_ok: null`: rights not checked (an upload with no record).

## 4. House rules

- Never post to a social network, never send messages for the owner, never delete. Finished work goes on the
  calendar as `ready`.
- Keep the credential secret: not in captions, files, the Sheet or chat replies.
- Don't depict real people without their consent; never sexualise minors. YourContent does not filter prompts, so
  this is on you and the owner.

## 5. Google workspace

When the owner connected Google, YourContent made a folder with `Inbox/`, `Rendered/`, `Exports/`, `Calendar/`
and a **Content Plan** Sheet; `google_status` returns the links. YourContent uses Google's `drive.file` access, so it
sees only files it made itself: a file someone drops into `Inbox/` on Google's own site is invisible to it, so
upload source material through YourContent. Plan posts with `calendar_add`; `plan_sync` brings the calendar and the
Sheet in step both ways (the side that changed since the last sync wins; when both did, YourContent's version is
kept). YourContent never deletes Sheet rows.

Content Plan columns: `id, when, channel, title, caption, status, project, asset, updated`. Status is one of
`idea, draft, ready, posted, skipped`.
