"""A small stdio MCP client for one sandboxed craft app session (craft-mcp <app>).

Each call has a deadline; on timeout the whole sandbox is killed (the job fails, nothing is left running).
Every call is appended to `log` as {app, tool, note, ok, seconds, result} so a job's status can show its steps.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, "craft-mcp")
# Only these reach the wrapper (which clears the environment again inside the sandbox).
PASS_ENV = ("PATH", "LANG", "CRAFT_BIN_DIR", "CRAFT_SRC", "CRAFT_GPU")


class CraftError(RuntimeError):
    pass


def sandbox_env(app: str, work: str, fonts: str | None = None, ai_masks: bool = False,
                sam3_dir: str | None = None, env=None) -> dict:
    """The environment for craft-mcp. The SAM 3 model folder is passed only to LightCraft, only when AI masks
    were turned on explicitly, and only when the folder exists."""
    env = os.environ if env is None else env
    out = {k: env[k] for k in PASS_ENV if env.get(k)}
    out.setdefault("PATH", "/usr/bin:/bin")
    out["CRAFT_WORK"] = str(work)
    if fonts:
        out["CRAFT_FONTS"] = str(fonts)
    if app == "lightcraft" and ai_masks and sam3_dir and os.path.isdir(sam3_dir):
        out["CRAFT_AI_MASKS"] = "1"
        out["CRAFT_SAM3_DIR"] = str(sam3_dir)
    return out


class Session:
    def __init__(self, app, work, fonts=None, timeout=600, ai_masks=False, sam3_dir=None):
        env = sandbox_env(app, work, fonts, ai_masks, sam3_dir)
        self.app, self.timeout, self.log, self.n = app, timeout, [], 0
        self.p = subprocess.Popen([WRAPPER, app], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env)
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._stderr: list[str] = []
        threading.Thread(target=lambda: self._stderr.extend(self.p.stderr), daemon=True).start()
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "media-lab-craftd", "version": "1"}}, timeout=30)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _pump(self):
        for line in self.p.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _send(self, msg):
        try:
            self.p.stdin.write(json.dumps(msg) + "\n")
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            raise CraftError(f"{self.app} exited: {''.join(self._stderr)[-400:]}") from None

    def _rpc(self, method, params, timeout=None):
        self.n += 1
        self._send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self.close(kill=True)
                raise CraftError(f"{self.app}: {method} timed out")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                raise CraftError(f"{self.app} exited: {''.join(self._stderr)[-400:]}")
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == self.n:
                return m

    def call(self, tool, args=None, note="", check=True, timeout=None):
        """Run one tool. Returns (parsed JSON or text, [png bytes]). Raises CraftError when check and it failed."""
        t = time.monotonic()
        r = self._rpc("tools/call", {"name": tool, "arguments": args or {}}, timeout)
        res = r.get("result") or {}
        err = bool(r.get("error") or res.get("isError"))
        texts = [c["text"] for c in res.get("content") or [] if c.get("type") == "text"]
        imgs = [base64.b64decode(c["data"]) for c in res.get("content") or [] if c.get("type") == "image"]
        text = r["error"].get("message", str(r["error"])) if r.get("error") else " ".join(texts)
        self.log.append({"app": self.app, "tool": tool, "note": note, "ok": not err,
                         "seconds": round(time.monotonic() - t, 2), "result": " ".join(text.split())[:300]})
        if err and check:
            raise CraftError(f"{self.app} {tool} {note}: {' '.join(text.split())[:300]}")
        try:
            return json.loads(text), imgs
        except ValueError:
            return text, imgs

    def close(self, kill=False):
        try:
            if kill:
                self.p.kill()
            else:
                self.p.stdin.close()
            self.p.wait(10)
        except Exception:  # noqa: BLE001
            self.p.kill()
            self.p.wait(5)
        for f in (self.p.stdin, self.p.stdout, self.p.stderr):
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close(kill=bool(exc[0]))
