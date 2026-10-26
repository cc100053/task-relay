"""Minimal JSON-RPC client for `codex app-server` over stdio.

Only read-only methods are used: `initialize` and `account/rateLimits/read`.
The app-server is spawned per probe and torn down afterwards, so the watchdog
never depends on a long-lived Codex session.
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


class AppServerClient:
    def __init__(self, codex: str, args: list[str], timeout: float = 30.0):
        self.codex = codex
        self.args = args
        self.timeout = timeout

    def read_rate_limits(self) -> dict:
        """Return the raw `account/rateLimits/read` result, or raise ProbeError."""
        try:
            proc = popen_hidden(
                [*command_prefix(self.codex), *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            raise ProbeError(f"could not start codex app-server: {exc}") from exc

        lines: queue.Queue[str | None] = queue.Queue()
        stderr_tail: list[str] = []

        def pump_stdout() -> None:
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        def pump_stderr() -> None:
            for line in proc.stderr:
                stderr_tail.append(line.rstrip())
                del stderr_tail[:-20]

        threading.Thread(target=pump_stdout, daemon=True).start()
        threading.Thread(target=pump_stderr, daemon=True).start()
        deadline = time.monotonic() + self.timeout

        def send(message: dict) -> None:
            try:
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()
            except OSError as exc:
                raise ProbeError(f"app-server stdin closed: {exc}") from exc

        def wait_for(request_id: int) -> dict:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProbeError("app-server did not answer in time")
                try:
                    line = lines.get(timeout=remaining)
                except queue.Empty:
                    continue
                if line is None:
                    detail = " | ".join(stderr_tail[-3:])
                    raise ProbeError(f"app-server exited early{': ' + detail if detail else ''}")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue  # notifications and unrelated responses
                if "error" in message:
                    err = message["error"] or {}
                    raise ProbeError(f"app-server error {err.get('code')}: {err.get('message')}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ProbeError("app-server returned a non-object result")
                return result

        try:
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "codex_quota_watchdog", "version": __version__},
            }})
            wait_for(1)
            send({"method": "initialized", "params": {}})
            # Background polls skip the reset-credit detail lookup; the count is still returned.
            send({"id": 2, "method": "account/rateLimits/read",
                  "params": {"excludeResetCreditDetails": True}})
            return wait_for(2)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            kill_tree(proc)
