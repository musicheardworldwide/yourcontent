"""Environment -> frozen Config, aliases, limits, presets (§5.1). Owner: WP2.

Every MEDIA_LAB_* variable has a default; the pre-v1 names (COMFY_URL, STUDIO_HOST, STUDIO_PORT, COMFY_OUTPUT,
STUDIO_UPLOADS) are still read when the new name is unset. The token is never printed (repr=False) and
`describe()` redacts it.
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Mapping

# Output frame size per export preset (§8.1); "source" is derived from the first item.
PRESETS: dict[str, tuple[int, int] | None] = {
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "source": None,
}

# Labels for /api/config (§6).
PRESET_LABELS: dict[str, str] = {
    "9:16": "9:16 Reels / Stories",
    "4:5": "4:5 Feed",
    "1:1": "1:1 Square",
    "16:9": "16:9 Wide",
    "source": "Source shape",
}

SUBDIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ConfigError(ValueError):
    """A setting that cannot be used. The message is a plain sentence for the person starting the app."""


@dataclass(frozen=True)
class Config:
    comfy_url: str = "http://127.0.0.1:8188"   # MEDIA_LAB_COMFY_URL (alias COMFY_URL)
    host: str = "127.0.0.1"                     # MEDIA_LAB_HOST (alias STUDIO_HOST)
    port: int = 8190                            # MEDIA_LAB_PORT (alias STUDIO_PORT)
    data: str = "./data/lab"                    # MEDIA_LAB_DATA
    comfy_output: str = "./data/output"         # MEDIA_LAB_COMFY_OUTPUT (alias COMFY_OUTPUT): root "out"
    uploads: str = "./data/uploads"             # MEDIA_LAB_UPLOADS (alias STUDIO_UPLOADS): root "in"
    input_subdir: str = "media_lab"             # MEDIA_LAB_INPUT_SUBDIR
    output_subdir: str = "media_lab"            # MEDIA_LAB_OUTPUT_SUBDIR: root "gen" = comfy_output/output_subdir
    token: str | None = field(default=None, repr=False)  # MEDIA_LAB_TOKEN / _FILE; never printed
    require_token: str = "auto"                 # MEDIA_LAB_REQUIRE_TOKEN: "auto" | "0" | "1"
    published_host: str = ""                    # MEDIA_LAB_PUBLISHED_HOST: the address a container port is
                                                # published on (decides "auto" in place of host when set)
    allowed_hosts: tuple[str, ...] = ()         # MEDIA_LAB_ALLOWED_HOSTS (comma-separated)
    trusted_proxies: tuple[str, ...] = ()       # MEDIA_LAB_TRUSTED_PROXY (comma-separated IPs): use X-Forwarded-For
    adopt_legacy: bool = False                  # MEDIA_LAB_ADOPT_LEGACY
    max_image_mb: int = 30                      # MEDIA_LAB_MAX_IMAGE_MB
    max_video_mb: int = 500                     # MEDIA_LAB_MAX_VIDEO_MB
    max_audio_mb: int = 100                     # MEDIA_LAB_MAX_AUDIO_MB
    min_free_gb: float = 2                      # MEDIA_LAB_MIN_FREE_GB


def _get(env: Mapping[str, str], name: str, alias: str | None = None) -> str | None:
    v = env.get(name)
    if (v is None or v == "") and alias:
        v = env.get(alias)
    return None if v is None or v == "" else v


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int, alias: str | None = None) -> int:
    v = _get(env, name, alias)
    if v is None:
        return default
    try:
        n = int(v.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a whole number (got {v!r}).") from None
    if not lo <= n <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi} (got {n}).")
    return n


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    v = _get(env, name)
    if v is None:
        return default
    s = v.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be 1 or 0 (got {v!r}).")


def load(env: Mapping[str, str] = os.environ) -> Config:
    """Read the environment into a Config. Raises ConfigError with a plain sentence on a bad value."""
    token = _get(env, "MEDIA_LAB_TOKEN")
    token_file = _get(env, "MEDIA_LAB_TOKEN_FILE")
    if token is None and token_file:
        try:
            with open(token_file, encoding="utf-8") as f:
                token = f.read().strip() or None
        except OSError:
            raise ConfigError("MEDIA_LAB_TOKEN_FILE is set but the file can't be read.") from None
    if token is not None:
        token = token.strip() or None
    if token is not None and len(token) < 8:
        raise ConfigError("MEDIA_LAB_TOKEN must be at least 8 characters.")

    require = (_get(env, "MEDIA_LAB_REQUIRE_TOKEN") or "auto").strip().lower()
    require = {"true": "1", "yes": "1", "on": "1", "false": "0", "no": "0", "off": "0"}.get(require, require)
    if require not in ("auto", "0", "1"):
        raise ConfigError("MEDIA_LAB_REQUIRE_TOKEN must be auto, 1 or 0.")

    hosts = tuple(h.strip().lower() for h in (_get(env, "MEDIA_LAB_ALLOWED_HOSTS") or "").split(",") if h.strip())

    proxies = []
    for p in (_get(env, "MEDIA_LAB_TRUSTED_PROXY") or "").split(","):
        if p.strip():
            try:
                proxies.append(str(ipaddress.ip_address(p.strip())))
            except ValueError:
                raise ConfigError("MEDIA_LAB_TRUSTED_PROXY must be IP addresses, comma-separated.") from None

    comfy_url = (_get(env, "MEDIA_LAB_COMFY_URL", "COMFY_URL") or Config.comfy_url).strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+$", comfy_url):
        raise ConfigError("MEDIA_LAB_COMFY_URL must look like http://host:8188 (no path).")

    subdirs = {}
    for key, attr in (("MEDIA_LAB_INPUT_SUBDIR", "input_subdir"), ("MEDIA_LAB_OUTPUT_SUBDIR", "output_subdir")):
        v = (_get(env, key) or getattr(Config, attr)).strip()
        if not SUBDIR_RE.match(v) or ".." in v:
            raise ConfigError(f"{key} must be one folder name (letters, digits, . _ -).")
        subdirs[attr] = v

    try:
        min_free = float(_get(env, "MEDIA_LAB_MIN_FREE_GB") or Config.min_free_gb)
    except ValueError:
        raise ConfigError("MEDIA_LAB_MIN_FREE_GB must be a number.") from None

    return Config(
        comfy_url=comfy_url,
        host=(_get(env, "MEDIA_LAB_HOST", "STUDIO_HOST") or Config.host).strip(),
        port=_int(env, "MEDIA_LAB_PORT", Config.port, 0, 65535, "STUDIO_PORT"),
        data=_get(env, "MEDIA_LAB_DATA") or Config.data,
        comfy_output=_get(env, "MEDIA_LAB_COMFY_OUTPUT", "COMFY_OUTPUT") or Config.comfy_output,
        uploads=_get(env, "MEDIA_LAB_UPLOADS", "STUDIO_UPLOADS") or Config.uploads,
        input_subdir=subdirs["input_subdir"],
        output_subdir=subdirs["output_subdir"],
        token=token,
        require_token=require,
        published_host=(_get(env, "MEDIA_LAB_PUBLISHED_HOST") or "").strip(),
        allowed_hosts=hosts,
        trusted_proxies=tuple(proxies),
        adopt_legacy=_bool(env, "MEDIA_LAB_ADOPT_LEGACY", False),
        max_image_mb=_int(env, "MEDIA_LAB_MAX_IMAGE_MB", Config.max_image_mb, 1, 4096),
        max_video_mb=_int(env, "MEDIA_LAB_MAX_VIDEO_MB", Config.max_video_mb, 1, 65536),
        max_audio_mb=_int(env, "MEDIA_LAB_MAX_AUDIO_MB", Config.max_audio_mb, 1, 4096),
        min_free_gb=max(0.0, min_free),
    )


def is_loopback(host: str) -> bool:
    """True for localhost, 127.0.0.0/8 and ::1 (with or without brackets). 0.0.0.0 and '' are not loopback."""
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def token_required(cfg: Config) -> bool:
    """Whether this bind must have a token (§12.1): always with "1", never with "0", off-loopback with "auto".
    In a container, "auto" judges MEDIA_LAB_PUBLISHED_HOST (the host-side publish address) when it is set,
    since the bind inside is always 0.0.0.0."""
    if cfg.require_token == "1":
        return True
    if cfg.require_token == "0":
        return False
    return not is_loopback(cfg.published_host or cfg.host)


def reach(cfg: Config) -> dict:
    """How this running app is reached, as the page and the agent setup prompt state it (read-only facts):
    listens: the address the port is reachable on (MEDIA_LAB_PUBLISHED_HOST in a container, else the bind host;
    0.0.0.0 / :: = every address of the machine); loopback_only: only this computer can connect;
    token_required: a credential is asked for (any MEDIA_LAB_TOKEN set). Without one, every client that can
    reach `listens` has the owner's full access, delete included."""
    addr = (cfg.published_host or cfg.host or "").strip() or "0.0.0.0"
    return {"listens": addr, "loopback_only": is_loopback(addr), "token_required": bool(cfg.token)}


def startup_problem(cfg: Config) -> str | None:
    """A plain sentence when the server must refuse to start, else None."""
    if token_required(cfg) and not cfg.token:
        return (f"YourContent won't listen on {cfg.host} without a token. Set MEDIA_LAB_TOKEN (or "
                "MEDIA_LAB_TOKEN_FILE), or listen on 127.0.0.1, or set MEDIA_LAB_REQUIRE_TOKEN=0 if a "
                "firewall or container port mapping already keeps it private.")
    return None


def startup_warning(cfg: Config) -> str | None:
    """A loud warning for a non-loopback bind that runs without a token because something outside (a port
    mapping, a firewall) is trusted to keep it private. The Host allowlist is no substitute: any non-browser
    client can send `Host: localhost`."""
    if cfg.token or is_loopback(cfg.host):
        return None
    why = (f"MEDIA_LAB_PUBLISHED_HOST says the port is published on {cfg.published_host} only"
           if cfg.published_host and cfg.require_token != "0" else "MEDIA_LAB_REQUIRE_TOKEN=0")
    return (f"WARNING: YourContent is listening on {cfg.host} with NO token ({why}). Anyone who can reach this "
            "port can list, download and delete your files and queue GPU jobs. If you publish the port on "
            "anything but 127.0.0.1 (a compose override, a LAN, a VPN), set MEDIA_LAB_TOKEN.")


def describe(cfg: Config) -> dict:
    """The config as a dict for `check`, with the token redacted."""
    out = {k: getattr(cfg, k) for k in cfg.__dataclass_fields__}
    out["token"] = "set" if cfg.token else "not set"
    out["allowed_hosts"] = list(cfg.allowed_hosts)
    out["trusted_proxies"] = list(cfg.trusted_proxies)
    return out
