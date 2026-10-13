"""Shared test helpers (WP0). Stdlib only; everything lives in temp dirs; no network beyond 127.0.0.1.

    from tests.util import TempRoots, FakeComfy, FakeJob, make_png, requires_ffmpeg

- TempRoots: a throwaway DATA / ComfyUI output / ComfyUI input tree, its MEDIA_LAB_* env and a Config.
- FakeComfy: an in-process fake ComfyUI HTTP server (the endpoints YourContent uses, with real response shapes).
- FakeJob: the jobs.Job interface, recording what a job function reports.
- make_png: a tiny valid PNG, optionally with tEXt chunks (ComfyUI stores its graph in a "prompt" chunk).
- requires_ffmpeg / ffmpeg_version: skip tests when ffmpeg is absent or too old.

Tests never touch real ComfyUI folders: copy what you need into a TempRoots tree.
"""
from __future__ import annotations

import email.parser
import email.policy
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the project folder


# --------------------------------------------------------------------------------------------- temp roots

class TempRoots:
    """A temp tree laid out like a real install (§3.1, §5.1):

        <base>/data                      MEDIA_LAB_DATA            (DATA; root "lab" = data/media)
        <base>/output                    MEDIA_LAB_COMFY_OUTPUT    (root "out", flat legacy files)
        <base>/output/<output_subdir>                              (root "gen")
        <base>/input/<input_subdir>      MEDIA_LAB_UPLOADS         (root "in")

    Use as a context manager, or call cleanup() (e.g. self.addCleanup(roots.cleanup)).
    Only the root folders are created; DATA's inner layout is the store's job.
    """

    def __init__(self, input_subdir: str = "media_lab", output_subdir: str = "media_lab",
                 comfy_url: str = "http://127.0.0.1:9"):
        self.base = tempfile.mkdtemp(prefix="medialab-test-")
        self.input_subdir, self.output_subdir, self.comfy_url = input_subdir, output_subdir, comfy_url
        self.data = os.path.join(self.base, "data")
        self.output = os.path.join(self.base, "output")
        self.input = os.path.join(self.base, "input")
        self.uploads = os.path.join(self.input, input_subdir)
        self.dirs = {"out": self.output, "gen": os.path.join(self.output, output_subdir),
                     "in": self.uploads, "lab": os.path.join(self.data, "media")}
        os.makedirs(self.data, mode=0o700)
        for d in self.dirs.values():
            os.makedirs(d, exist_ok=True)

    def __enter__(self) -> "TempRoots":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def dir(self, root: str) -> str:
        return self.dirs[root]

    def path(self, root: str, name: str) -> str:
        return os.path.join(self.dirs[root], name)

    def put(self, root: str, name: str, data: bytes = b"", mtime: float | None = None) -> str:
        """Write a file under a root; returns its path."""
        p = self.path(root, name)
        with open(p, "wb") as f:
            f.write(data)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    @property
    def env(self) -> dict[str, str]:
        return {
            "MEDIA_LAB_COMFY_URL": self.comfy_url,
            "MEDIA_LAB_HOST": "127.0.0.1",
            "MEDIA_LAB_PORT": "0",
            "MEDIA_LAB_DATA": self.data,
            "MEDIA_LAB_COMFY_OUTPUT": self.output,
            "MEDIA_LAB_UPLOADS": self.uploads,
            "MEDIA_LAB_INPUT_SUBDIR": self.input_subdir,
            "MEDIA_LAB_OUTPUT_SUBDIR": self.output_subdir,
        }

    def config(self, **overrides):
        """A medialab.config.Config for this tree, built directly (does not need config.load)."""
        from medialab.config import Config
        fields = dict(comfy_url=self.comfy_url, host="127.0.0.1", port=0, data=self.data,
                      comfy_output=self.output, uploads=self.uploads, input_subdir=self.input_subdir,
                      output_subdir=self.output_subdir)
        fields.update(overrides)
        return Config(**fields)

    def snapshot(self, top: str | None = None) -> dict[str, tuple[int, int, str]]:
        """{relative path: (size, mtime_ns, sha256)} for every file under `top` (default: the whole tree).
        Compare two snapshots to prove files were not changed, moved or deleted."""
        top = top or self.base
        out = {}
        for d, _, files in os.walk(top):
            for n in files:
                p = os.path.join(d, n)
                st = os.lstat(p)
                out[os.path.relpath(p, top)] = (st.st_size, st.st_mtime_ns, sha256_file(p))
        return out


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------------------------- fake job

class FakeJob:
    """Mirrors medialab.jobs.Job. Records progress; `cancel()` flips `.cancelled` for tests."""

    def __init__(self) -> None:
        self.id = "j_" + "0" * 12
        self.events: list[tuple[float | None, str]] = []
        self.procs: list = []
        self.comfy_pids: list[str] = []
        self._cancelled = False
        self.committed = False

    def progress(self, p: float | None, phase: str) -> None:
        self.events.append((p, phase))

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def commit(self, can_discard: bool = True) -> None:
        from medialab import jobs
        if can_discard and self._cancelled:
            raise jobs.Cancelled("cancelled")
        self.committed = True

    def set_proc(self, popen) -> None:
        self.procs.append(popen)

    def set_comfy(self, pid: str) -> None:
        self.comfy_pids.append(pid)

    @property
    def phases(self) -> list[str]:
        out: list[str] = []
        for _, ph in self.events:
            if not out or out[-1] != ph:
                out.append(ph)
        return out


# --------------------------------------------------------------------------------------------- png

def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def make_png(path: str | None = None, w: int = 2, h: int = 2, text: dict[str, str] | None = None,
             rgb: tuple[int, int, int] = (128, 128, 128)) -> bytes:
    """A valid 8-bit RGB PNG of w x h, with one tEXt chunk per `text` item (values: str, or dicts as JSON).
    Writes it to `path` when given; always returns the bytes."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    body = [_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))]
    for k, v in (text or {}).items():
        v = v if isinstance(v, str) else json.dumps(v)
        body.append(_chunk(b"tEXt", k.encode("latin-1") + b"\x00" + v.encode("latin-1")))
    body += [_chunk(b"IDAT", zlib.compress(raw)), _chunk(b"IEND", b"")]
    data = b"\x89PNG\r\n\x1a\n" + b"".join(body)
    if path:
        with open(path, "wb") as f:
            f.write(data)
    return data


# --------------------------------------------------------------------------------------------- ffmpeg

def ffmpeg_version() -> tuple[int, int] | None:
    """(major, minor) of the ffmpeg on PATH, or None when there is none (ffprobe must exist too)."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return None
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def requires_ffmpeg(min_version: tuple[int, int] | None = None):
    """Decorator: skip unless ffmpeg+ffprobe exist (and are at least min_version, e.g. (7, 1))."""
    v = ffmpeg_version()
    ok = v is not None and (min_version is None or v >= min_version)
    why = "ffmpeg not found" if v is None else f"needs ffmpeg {min_version}, found {v}"
    return unittest.skipUnless(ok, why)


# --------------------------------------------------------------------------------------------- fake ComfyUI

class FakeComfy:
    """An in-process fake ComfyUI (0.37.0 shapes) on 127.0.0.1:<ephemeral port>.

        with FakeComfy(object_info=fixture, output_dir=roots.output, input_dir=roots.input) as fc:
            ... point the client at fc.url ...
            fc.calls("POST", "/history")   # what was sent

    Endpoints: GET /system_stats, /object_info, /object_info/<cls>, /history, /history/<pid>, /queue, /view;
    POST /prompt, /history ({"delete": [...]} | {"clear": true}), /queue (same), /interrupt, /upload/image
    (multipart), /free (recorded so tests can assert it is never called). POST /history, /queue, /interrupt
    and /free answer 200 with an empty body, as ComfyUI does.

    Prompts:
    - auto_complete=True (default): a prompt finishes at once. Outputs come from `on_prompt(graph, pid)` if set
      (return the history "outputs" dict), else from default_outputs(): SaveImage -> images, VHS_VideoCombine
      -> gifs (+ poster .png), SaveAudio/SaveAudioAdvanced/SaveAudioMP3 -> audio. With output_dir set, those
      files are written there as small placeholders (a real PNG for pictures; dummy bytes for video/audio).
    - auto_complete=False: prompts wait in queue_pending; drive them with run(pid), complete(pid, outputs)
      and fail(pid, message).
    - node_errors = {...}: the next /prompt calls answer 400 with those node errors.
    - down(): stop answering (simulates ComfyUI off); start() again to bring it back on a new port.
    """

    def __init__(self, object_info: dict | None = None, output_dir: str | None = None,
                 input_dir: str | None = None, version: str = "0.37.0", auto_complete: bool = True):
        self.object_info = dict(object_info or {})
        self.output_dir, self.input_dir, self.version = output_dir, input_dir, version
        self.auto_complete = auto_complete
        self.on_prompt: Callable[[dict, str], dict] | None = None
        self.node_errors: dict | None = None
        self.history: dict[str, dict] = {}
        self.pending: list[list] = []   # [number, pid, graph, extra, output_node_ids]
        self.running: list[list] = []
        self.requests: list[tuple[str, str, object]] = []   # (method, path incl. query, json body | bytes | None)
        self.uploads: list[tuple[str, str, bytes]] = []     # (name, subfolder, data)
        self.lock = threading.RLock()
        self._number = 0
        self._counters: dict[str, int] = {}
        self.counter_from_disk = False   # True: number outputs like ComfyUI does, from the files on disk
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # lifecycle
    def start(self) -> "FakeComfy":
        fake = self

        class H(_FakeHandler):
            owner = fake

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    down = stop

    def __enter__(self) -> "FakeComfy":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def url(self) -> str:
        assert self._httpd, "FakeComfy is not running"
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    # inspection
    def calls(self, method: str | None = None, path: str | None = None) -> list[tuple[str, str, object]]:
        """Recorded requests, filtered by method and by path prefix (query string ignored)."""
        with self.lock:
            return [r for r in self.requests
                    if (method is None or r[0] == method) and (path is None or r[1].split("?")[0].startswith(path))]

    @property
    def free_called(self) -> bool:
        return bool(self.calls("POST", "/free"))

    def add_history(self, pid: str, outputs: dict, graph: dict | None = None, status: str = "success") -> None:
        """Seed a history entry (e.g. one made by another tool, or a legacy render)."""
        with self.lock:
            self._number += 1
            self.history[pid] = self._entry([self._number, pid, graph or {}, {}, list(outputs)], outputs, status)

    # prompt lifecycle
    def run(self, pid: str) -> None:
        with self.lock:
            item = self._take(self.pending, pid)
            if item:
                self.running.append(item)

    def complete(self, pid: str, outputs: dict | None = None) -> None:
        with self.lock:
            item = self._take(self.running, pid) or self._take(self.pending, pid)
            if item is None:
                raise KeyError(pid)
            if outputs is None:
                outputs = self.on_prompt(item[2], pid) if self.on_prompt else self.default_outputs(item[2])
            self.history[pid] = self._entry(item, outputs or {}, "success")

    def fail(self, pid: str, message: str = "render failed", node_id: str = "") -> None:
        with self.lock:
            item = self._take(self.running, pid) or self._take(self.pending, pid)
            if item is None:
                raise KeyError(pid)
            self.history[pid] = self._entry(item, {}, "error", [["execution_error", {
                "prompt_id": pid, "node_id": node_id, "exception_message": message, "exception_type": "RuntimeError",
                "traceback": []}]])

    def default_outputs(self, graph: dict) -> dict:
        """History outputs a real ComfyUI would report for the saver nodes in `graph`."""
        outs: dict[str, dict] = {}
        for nid, node in graph.items():
            ct, inp = node.get("class_type"), node.get("inputs", {})
            prefix = str(inp.get("filename_prefix", "ComfyUI"))
            sub, _, base = prefix.rpartition("/")
            if ct == "SaveImage":
                name = f"{base}_{self._next(prefix):05}_.png"
                self._write(sub, name, make_png(w=4, h=4))
                outs[nid] = {"images": [{"filename": name, "subfolder": sub, "type": "output"}]}
            elif ct == "VHS_VideoCombine":
                n = self._next(prefix)
                name, poster = f"{base}_{n:05}.mp4", f"{base}_{n:05}.png"
                self._write(sub, name, b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16)
                self._write(sub, poster, make_png(w=4, h=4))
                outs[nid] = {"gifs": [{"filename": name, "subfolder": sub, "type": "output",
                                       "format": inp.get("format", "video/h264-mp4"),
                                       "frame_rate": float(inp.get("frame_rate", 16)), "workflow": poster}]}
            elif ct in ("SaveAudio", "SaveAudioAdvanced", "SaveAudioMP3"):
                fmt = {"SaveAudioMP3": "mp3"}.get(ct, "flac")
                if ct == "SaveAudioAdvanced" and isinstance(inp.get("format"), str):
                    fmt = inp["format"]
                name = f"{base}_{self._next(prefix):05}.{fmt}"
                self._write(sub, name, b"fLaC" + b"\x00" * 32 if fmt == "flac" else b"\x00" * 36)
                outs[nid] = {"audio": [{"filename": name, "subfolder": sub, "type": "output"}]}
        return outs

    # internals
    def _next(self, prefix: str) -> int:
        if self.counter_from_disk and self.output_dir:
            # what ComfyUI really does (folder_paths.get_save_image_path): one past the highest file left under
            # the prefix, so a deleted newest file's number is handed out again
            sub, _, base = prefix.rpartition("/")
            d, pat = os.path.join(self.output_dir, sub), re.compile(re.escape(base) + r"_(\d{5})")
            nums = [int(m.group(1)) for f in (os.listdir(d) if os.path.isdir(d) else []) if (m := pat.match(f))]
            return max(nums, default=0) + 1
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return self._counters[prefix]

    def _write(self, sub: str, name: str, data: bytes) -> None:
        if not self.output_dir:
            return
        d = os.path.join(self.output_dir, sub)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)

    @staticmethod
    def _take(queue: list[list], pid: str) -> list | None:
        for i, item in enumerate(queue):
            if item[1] == pid:
                return queue.pop(i)
        return None

    @staticmethod
    def _entry(item: list, outputs: dict, status: str, messages: list | None = None) -> dict:
        return {"prompt": item, "outputs": outputs, "meta": {},
                "status": {"status_str": status, "completed": status == "success",
                           "messages": messages if messages is not None else
                           [["execution_start", {"prompt_id": item[1]}],
                            ["execution_success", {"prompt_id": item[1]}]]}}

    def _submit(self, body: dict) -> tuple[int, dict]:
        graph = body.get("prompt")
        if not isinstance(graph, dict):
            return 400, {"error": {"type": "invalid_prompt", "message": "Cannot execute because the prompt is "
                                   "not a dict", "details": "", "extra_info": {}}, "node_errors": {}}
        if self.node_errors:
            return 400, {"error": {"type": "prompt_outputs_failed_validation",
                                   "message": "Prompt outputs failed validation", "details": "", "extra_info": {}},
                         "node_errors": self.node_errors}
        with self.lock:
            pid = str(uuid.uuid4())
            item = [self._number, pid, graph, {"client_id": body.get("client_id")},
                    [k for k, n in graph.items() if str(n.get("class_type", "")).startswith(("Save", "VHS_"))]]
            number = self._number
            self._number += 1
            self.pending.append(item)
            if self.auto_complete:
                self.complete(pid)
        return 200, {"prompt_id": pid, "number": number, "node_errors": {}}


class _FakeHandler(BaseHTTPRequestHandler):
    owner: FakeComfy

    def log_message(self, fmt, *args):  # keep test output clean
        pass

    def _send(self, code: int, obj=None, raw: bytes | None = None, ctype: str = "application/json") -> None:
        body = raw if raw is not None else (b"" if obj is None else json.dumps(obj).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        fc = self.owner
        with fc.lock:
            fc.requests.append(("GET", self.path, None))
        path, _, query = self.path.partition("?")
        if path == "/system_stats":
            return self._send(200, {"system": {"os": "posix", "comfyui_version": fc.version,
                                               "python_version": "3.12", "embedded_python": False},
                                    "devices": []})
        if path == "/object_info":
            return self._send(200, fc.object_info)
        if path.startswith("/object_info/"):
            cls = urllib.parse.unquote(path[len("/object_info/"):])
            return self._send(200, {cls: fc.object_info[cls]} if cls in fc.object_info else {})
        if path == "/history":
            with fc.lock:
                return self._send(200, dict(fc.history))
        if path.startswith("/history/"):
            pid = urllib.parse.unquote(path[len("/history/"):])
            with fc.lock:
                return self._send(200, {pid: fc.history[pid]} if pid in fc.history else {})
        if path == "/queue":
            with fc.lock:
                return self._send(200, {"queue_running": list(fc.running), "queue_pending": list(fc.pending)})
        if path == "/view":
            q = urllib.parse.parse_qs(query)
            name, sub = q.get("filename", [""])[0], q.get("subfolder", [""])[0]
            base = {"output": fc.output_dir, "input": fc.input_dir}.get(q.get("type", ["output"])[0])
            if base and name and "/" not in name and ".." not in sub:
                p = os.path.join(base, sub, name)
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        return self._send(200, raw=f.read(), ctype="application/octet-stream")
            return self._send(404, raw=b"404: Not Found", ctype="text/plain")
        return self._send(404, raw=b"404: Not Found", ctype="text/plain")

    def do_POST(self):
        fc = self.owner
        raw = self._body()
        path = self.path.partition("?")[0]
        ctype = self.headers.get("Content-Type", "")
        if path == "/upload/image":
            with fc.lock:
                fc.requests.append(("POST", self.path, raw))
            return self._upload(raw, ctype)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = None
        with fc.lock:
            fc.requests.append(("POST", self.path, body if body is not None else raw))
        if body is None:
            return self._send(400, raw=b"bad json", ctype="text/plain")
        if path == "/prompt":
            return self._send(*fc._submit(body))
        if path in ("/history", "/queue"):
            with fc.lock:
                if body.get("clear"):
                    if path == "/history":
                        fc.history.clear()
                    else:
                        fc.pending.clear()
                for pid in body.get("delete", []) or []:
                    if path == "/history":
                        fc.history.pop(pid, None)
                    else:
                        fc._take(fc.pending, pid)
            return self._send(200)
        if path == "/interrupt":
            with fc.lock:
                target = body.get("prompt_id")
                for item in list(fc.running):
                    if target is None or item[1] == target:
                        fc.fail(item[1], "Interrupted")
            return self._send(200)
        if path == "/free":
            return self._send(200)
        return self._send(404, raw=b"404: Not Found", ctype="text/plain")

    def _upload(self, raw: bytes, ctype: str):
        fc = self.owner
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
        fields: dict[str, str] = {}
        image: tuple[str, bytes] | None = None
        for part in msg.iter_parts() if msg.is_multipart() else []:
            name = part.get_param("name", header="content-disposition")
            fname = part.get_filename()
            data = part.get_payload(decode=True) or b""
            if name == "image" and fname:
                image = (fname, data)
            elif name:
                fields[name] = data.decode(errors="replace")
        if not image:
            return self._send(400, raw=b"", ctype="text/plain")
        name, data = os.path.basename(image[0]), image[1]
        sub = fields.get("subfolder", "")
        if ".." in sub.split("/") or sub.startswith("/"):
            return self._send(400, raw=b"", ctype="text/plain")
        if fc.input_dir:
            d = os.path.join(fc.input_dir, sub)
            os.makedirs(d, exist_ok=True)
            stem, ext = os.path.splitext(name)
            i = 1
            while os.path.exists(os.path.join(d, name)) and fields.get("overwrite", "").lower() != "true":
                name, i = f"{stem} ({i}){ext}", i + 1
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
        with fc.lock:
            fc.uploads.append((name, sub, data))
        return self._send(200, {"name": name, "subfolder": sub, "type": fields.get("type", "input")})
