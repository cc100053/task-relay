"""Minimal JSON-RPC client for `codex app-server` over stdio.

Only read-only methods are used: `initialize`, `account/rateLimits/read`,
`thread/list` and `thread/turns/list`. The app-server is spawned per probe and
torn down afterwards, so the watchdog never depends on a long-lived Codex session.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


class ProbeError(RuntimeError):
    """The quota could not be read. Callers must treat quota state as unknown."""


def find_codex(configured: str = "") -> str:
    if configured:
        return configured
    env = os.environ.get("CODEX_PATH")
    if env:
        return env
    found = shutil.which("codex")
    if found:
        return found
    candidates = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        appdata = os.environ.get("APPDATA", "")
        candidates += [
            Path(local, "Programs", "OpenAI", "Codex", "bin", "codex.exe"),
            Path(appdata, "npm", "codex.cmd"),
        ]
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Codex.app/Contents/Resources/codex"))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise ProbeError("codex executable not found; set codex_path in config or CODEX_PATH")


def command_prefix(codex: str) -> list[str]:
    """argv prefix for running codex.

    On Windows npm installs `codex.cmd`; running it would route every argument through
    cmd.exe parsing. Prefer `node <pkg>/bin/codex.js` when the shim's package is found.
    """
    path = Path(codex)
    if path.suffix.lower() not in (".cmd", ".bat"):
        return [codex]
    script = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    node = path.parent / "node.exe"
    node_cmd = str(node) if node.exists() else shutil.which("node")
    if script.exists() and node_cmd:
        return [node_cmd, str(script)]
    return [codex]


CMD_UNSAFE = set('"&|<>^%!')


def check_cmd_safe(argv: list[str]) -> None:
    """Refuse to pass metacharacters through a .cmd/.bat shim (cmd.exe would interpret them)."""
    if Path(argv[0]).suffix.lower() in (".cmd", ".bat"):
        for arg in argv[1:]:
            if CMD_UNSAFE & set(arg):
                raise ValueError(f"argument {arg[:40]!r} is unsafe to pass through {argv[0]}; "
                                 "set codex_path to codex.exe or node + codex.js")


def popen_hidden(args: list[str], **kwargs) -> subprocess.Popen:
    """Start a process without a console window, in its own process group."""
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs.setdefault("start_new_session", True)
    return subprocess.Popen(args, **kwargs)


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


class AppServerSession:
    """One `codex app-server` process; send requests with `call`, then close it."""

    def __init__(self, codex: str, args: list[str], timeout: float):
        try:
            self.proc = popen_hidden(
                [*command_prefix(codex), *args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            raise ProbeError(f"could not start codex app-server: {exc}") from exc
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stderr_tail: list[str] = []
        self.deadline = time.monotonic() + timeout
        self.next_id = 0
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        try:
            self.call("initialize", {
                "clientInfo": {"name": "codex_quota_watchdog", "version": __version__},
            })
            self._send({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "AppServerSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _pump_stdout(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _pump_stderr(self) -> None:
        for line in self.proc.stderr:
            self.stderr_tail.append(line.rstrip())
            del self.stderr_tail[:-20]

    def _send(self, message: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except OSError as exc:
            raise ProbeError(f"app-server stdin closed: {exc}") from exc

    def call(self, method: str, params: dict) -> dict:
        """Send one request and return its object result, or raise ProbeError."""
        self.next_id += 1
        request_id = self.next_id
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError("app-server did not answer in time")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                detail = " | ".join(self.stderr_tail[-3:])
                raise ProbeError(f"app-server exited early{': ' + detail if detail else ''}")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue  # notifications and unrelated replies
            if "error" in message:
                err = message["error"] or {}
                raise ProbeError(f"app-server error {err.get('code')}: {err.get('message')}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise ProbeError("app-server returned a non-object result")
            return result

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        kill_tree(self.proc)


class AppServerClient:
    def __init__(self, codex: str, args: list[str], timeout: float = 30.0):
        self.codex = codex
        self.args = args
        self.timeout = timeout

    def session(self, timeout: float | None = None) -> AppServerSession:
        return AppServerSession(self.codex, self.args, timeout or self.timeout)

    def read_rate_limits(self) -> dict:
        """Return the raw `account/rateLimits/read` result, or raise ProbeError."""
        with self.session() as session:
            # Background polls skip the reset-credit detail lookup; the count is still returned.
            return session.call("account/rateLimits/read", {"excludeResetCreditDetails": True})
