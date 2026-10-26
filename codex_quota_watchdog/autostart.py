"""Per-user autostart without admin rights: Startup folder / LaunchAgent / systemd --user."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.codex-quota-watchdog"
PKG_PARENT = str(Path(__file__).resolve().parent.parent)


def _python(windowless: bool) -> str:
    exe = Path(sys.executable)
    if windowless and sys.platform == "win32":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def _args(extra: list[str]) -> list[str]:
    return ["-m", "codex_quota_watchdog", *extra, "watch", "--quiet"]


def target_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs",
                    "Startup", "CodexQuotaWatchdog.cmd")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / "codex-quota-watchdog.service"


def install(extra: list[str]) -> str:
    path = target_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        args = " ".join(f'"{a}"' for a in _args(extra))
        path.write_text(
            "@echo off\r\n"
            f'set "PYTHONPATH={PKG_PARENT}"\r\n'
            f'start "" /B "{_python(True)}" {args}\r\n',
            encoding="utf-8",
        )
        subprocess.Popen(["cmd.exe", "/c", str(path)], creationflags=0x08000000)
        return f"installed {path} and started the watchdog in the background"
    if sys.platform == "darwin":
        plist = {
            "Label": LABEL,
            "ProgramArguments": [_python(False), *_args(extra)],
            "EnvironmentVariables": {"PYTHONPATH": PKG_PARENT, "PATH": os.environ.get("PATH", "")},
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Background",
        }
        with path.open("wb") as fh:
            plistlib.dump(plist, fh)
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        subprocess.run(["launchctl", "load", str(path)], check=True)
        return f"installed and loaded {path}"
    cmd = " ".join([_python(False), *_args(extra)])
    path.write_text(
        "[Unit]\nDescription=Codex Quota Watchdog\n\n"
        "[Service]\n"
        f"Environment=PYTHONPATH={PKG_PARENT}\n"
        f"Environment=\"PATH={os.environ.get('PATH', '')}\"\n"
        f"ExecStart={cmd}\nRestart=on-failure\nRestartSec=30\n\n"
        "[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", path.name], check=True)
    return f"installed and started {path} (systemd --user)"


def uninstall() -> str:
    path = target_path()
    if sys.platform == "darwin" and path.exists():
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    elif sys.platform not in ("win32", "darwin"):
        subprocess.run(["systemctl", "--user", "disable", "--now", path.name], capture_output=True)
    if path.exists():
        path.unlink()
        msg = f"removed {path}"
    else:
        msg = "autostart was not installed"
    if sys.platform == "win32":
        msg += " (a watchdog that is already running keeps running until you log off or end it)"
    return msg


def status() -> str:
    path = target_path()
    return f"installed ({path})" if path.exists() else "not installed"
