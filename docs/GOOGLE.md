# Connect Google

YourContent can keep a workspace in your Google Drive: a folder you name (default "YourContent") with `Inbox`,
`Rendered`, `Exports` and `Calendar` inside, and a Google Sheet "Content Plan" with two tabs ("Content Plan" and
"Assets"). It uploads exports there and writes a row per upload.

It asks for one permission only: **`https://www.googleapis.com/auth/drive.file`**, "see, edit, create and delete
only the specific Google Drive files you use with this app". It cannot read the rest of your Drive. Google lists
this scope as non-sensitive, so it needs no security audit.

Each self-hosted YourContent uses its **own** Google OAuth client. Nothing goes through a shared server.

## Setup (about 5 minutes)

1. Open the Google Cloud console (console.cloud.google.com) and create a project, or pick one you already have.
2. **APIs & Services → Library**: turn on **Google Drive API** and **Google Sheets API**.
3. **Google Auth Platform** (called "OAuth consent screen" in older consoles):
   - Branding: an app name (for example "My YourContent") and your email address.
   - Audience: **External**. While the app is in **Testing**, add your own Google address under *Test users*.
   - Data access: add the scope `.../auth/drive.file`. Add nothing else.
4. **Clients → Create client → Application type: Desktop app**. Copy the client ID (it ends in
   `.apps.googleusercontent.com`) and the client secret.
5. Give them to YourContent and restart it:
   ```
   YOURCONTENT_GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com
   YOURCONTENT_GOOGLE_CLIENT_SECRET=...
   ```
   With compose, put them in the service's `environment:` block or in an env file that git ignores. Keep the
   secret out of git. A desktop client's secret is not truly secret, but it is still yours.
6. Open YourContent at **http://127.0.0.1:8190** (a loopback address), go to the Google panel, press
   **Connect Google**, approve, then press **Create workspace**.

If `YOURCONTENT_GOOGLE_CLIENT_ID` is not set, the panel says so and points here.

### Addresses

A Desktop-app client only redirects back to a loopback address (`http://127.0.0.1:<any port>`, `http://[::1]:…`
or `http://localhost:…`). YourContent builds the redirect URI from the address in your browser bar, plus
`/api/google/callback`. So connect from the machine YourContent runs on, or through an SSH tunnel that maps a local
port to it.

If you reach YourContent by another name (a LAN or VPN host name), a Desktop client won't work. Create a **Web
application** client instead and register the exact redirect URI, for example
`https://media.example.com/api/google/callback`. Google's rules for web clients apply: https, and a real public
domain except for localhost. Behind a reverse proxy, YourContent uses `https` in the redirect URI when the proxy
sends `X-Forwarded-Proto: https` and the address is not loopback.

### Testing vs. production

While the consent screen is in **Testing**, Google lets refresh tokens expire after 7 days, and you will need to
press Connect again once a week. Publishing the app ("In production") removes that limit. Because the only scope
is non-sensitive, Google should not ask for an app review, but it may show an "unverified app" notice. Check
what your console says before you rely on it.

## What drive.file lets YourContent see (and what it doesn't)

- **It sees** files and folders YourContent created: the workspace, the Sheet and every upload. Each one is tagged
  with an `appProperties` entry (`mediaLab = root | inbox | … | sheet | upload`). That lets "Create workspace"
  find the folders again if YourContent's local record is lost.
- **It sees** files you open with YourContent through the **Google Picker**. YourContent does not include the Picker
  yet: it needs Google's script from `apis.google.com` and an API key, and YourContent's strict Content Security
  Policy blocks that script today.
- **It does not see** other files in your Drive. **This includes files you drag into the workspace's `Inbox`
  folder yourself** in Drive on the web or on your desktop. Google's documentation describes drive.file access
  per file and says nothing about it extending to a folder's other contents. We understand that it does not, and
  have not tested it against a live account. So the Inbox list shows only files YourContent can see, and the
  panel says so when it is empty.
- Our understanding is that access belongs to the **OAuth client**, not to one copy of the app. A script or agent
  signed in to the same Google account **with the same client ID** can put files in `Inbox`, and YourContent will
  see them. This is the dependable way to feed the Inbox until the Picker exists.
- The account address shown in the panel comes from Drive's `about.get` (`fields=user`). The Drive API reference
  lists `drive.file` among the scopes that call accepts. YourContent does not ask for `openid` or `email`.
- Google shows each permission as a separate checkbox. If you untick the Drive box, YourContent refuses the
  connection and asks you to try again with the box ticked.

## No OAuth: pasting IDs

`POST /api/google/manual {folders: {root, inbox?, rendered?, exports?, calendar?}, sheet?}` saves IDs you paste
(they are checked for shape only). They give you **links** in the panel and nothing more. Without OAuth there is
no access, and even with OAuth, drive.file does not reach a folder or Sheet you made by hand. Treat pasted IDs as
bookmarks. The one route that works is a Sheet another tool created with **the same OAuth client**. Pasting the
IDs of files you made yourself only becomes useful once the Picker exists (you would open each file with it
once). Until then this is a known limitation.

## Where things are kept

- `DATA/config/google.json`: the tokens, mode 0600. They are never logged and never returned by any API. Treat
  the data folder as private.
- `DATA/config/google_workspace.json`: folder and Sheet IDs (mode 0600, not secret).
- **Disconnect** revokes the token at Google (`oauth2.googleapis.com/revoke`) and deletes `google.json`. The
  workspace record stays, so reconnecting reuses it. "Create workspace" checks each ID and recreates only what is
  gone or invisible. You can also remove access at myaccount.google.com → Security → Third-party connections.

## API

| Route | Does |
|---|---|
| `GET /api/google/status` | configured?, reason, connected?, email, workspace IDs and links, last error |
| `POST /api/google/connect` | returns `{url}` (the consent page); sets a one-use binding cookie |
| `GET /api/google/callback` | Google sends the browser back here; then redirects to `/#google` |
| `POST /api/google/bootstrap` | `{name?}` creates or repairs the workspace (idempotent) |
| `POST /api/google/disconnect` | revoke and forget |
| `POST /api/google/upload` | `{ref, folder: "exports" \| "rendered"}` streams a file YourContent owns, adds an Assets row |
| `GET /api/google/inbox` | files in `Inbox/` that YourContent can see |
| `POST /api/google/inbox/import` | `{file_id}` downloads into YourContent's uploads (same checks as a browser upload) |
| `POST /api/google/manual` | pasted IDs (see above) |

The callback is a GET that changes state, so it guards itself:

- The `state` value is single use, expires after 10 minutes and is tied to the browser that pressed Connect,
  through an HttpOnly, SameSite=Lax cookie limited to the callback path.
- Sign-in uses PKCE (S256).
- The server's Host allowlist still applies to the callback.
- When `YOURCONTENT_TOKEN` is set, the callback is the one API route that needs no session. The Strict session
  cookie does not come back from Google's page, and the binding cookie can only be obtained by an
  authenticated `POST /connect`.
