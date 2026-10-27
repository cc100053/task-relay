"""Find and stop Codex processes, conservatively.

Rules:
- Only processes that match the configured categories are touched.
- The watchdog never touches itself, its ancestors (e.g. a terminal hosted by Codex)
  or its own children (the app-server probe).
- A polite stop comes first; force-kill happens only after a timeout, and only for
  processes whose PID *and* start time still match what was recorded (no PID reuse).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace

from .appserver import CREATE_NO_WINDOW
from .config import Config

log = logging.getLogger(__name__)

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
NATIVE_NAME_RE = re.compile(r"^codex(-(x86_64|aarch64)-.+)?$", re.I)
DESKTOP_MARKERS = (
    "codex.app/", "chatgpt.app/", "codex computer use.app/",
    "/programs/openai/codex/", "/windowsapps/openai.codex",
)


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    name: str
    cmdline: str
    started: str  # opaque start-time token, used to detect PID reuse

    @property
    def key(self) -> tuple[int, str]:
        return (self.pid, self.started)


def list_processes() -> list[Proc]:
    if sys.platform == "win32":
        return _list_windows()
    return _list_posix()


def _list_windows() -> list[Proc]:
    script = (
        "Get-CimInstance Win32_Process | ForEach-Object { [pscustomobject]@{"
        "p=$_.ProcessId; pp=$_.ParentProcessId; n=$_.Name; c=$_.CommandLine; "
        "e=$_.ExecutablePath; t=if($_.CreationDate){$_.CreationDate.ToFileTimeUtc()}else{0} } }"
        " | ConvertTo-Json -Compress"
    )
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW, timeout=60,
    )
    if out.returncode != 0:
        raise OSError(f"process listing failed: {out.stderr.strip()[:200]}")
    data = json.loads(out.stdout or "[]")
    if isinstance(data, dict):
        data = [data]
    procs = []
    for item in data:
        cmd = item.get("c") or item.get("e") or item.get("n") or ""
        procs.append(Proc(int(item["p"]), int(item["pp"] or 0), item.get("n") or "", cmd, str(item.get("t"))))
    return procs


def _list_posix() -> list[Proc]:
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,lstart=,args="],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    if out.returncode != 0:
        raise OSError(f"ps failed: {out.stderr.strip()[:200]}")
    # args splits executable paths at spaces; comm preserves the executable name.
    names = subprocess.run(
        ["ps", "-A", "-o", "pid=,comm="],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    if names.returncode != 0:
        raise OSError(f"ps failed: {names.stderr.strip()[:200]}")
    by_pid = {}
    for line in names.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            by_pid[int(parts[0])] = os.path.basename(parts[1])
    return [replace(p, name=by_pid[p.pid]) for p in parse_ps(out.stdout) if p.pid in by_pid]


def parse_ps(text: str) -> list[Proc]:
    procs = []
    for line in text.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        pid, ppid = parts[0], parts[1]
        started = " ".join(parts[2:7])  # lstart: "Mon Oct 26 14:20:00 2026"
        args = parts[7]
        argv0 = args.split(None, 1)[0]
        procs.append(Proc(int(pid), int(ppid), os.path.basename(argv0), args, started))
    return procs


def _norm(text: str) -> str:
    return text.replace("\\", "/").lower()


def classify(proc: Proc, cfg: Config) -> str | None:
    """Return 'desktop', 'cli', 'extra' or None."""
    cmd = _norm(proc.cmdline)
    name = proc.name.lower().removesuffix(".exe")
    if any(m in cmd for m in DESKTOP_MARKERS):
        return "desktop"
    if NATIVE_NAME_RE.match(name):
        return "cli"
    if name in ("node", "bun") and "@openai/codex" in cmd:
        return "cli"
    for pattern in cfg.extra_process_patterns:
        if re.search(pattern, proc.cmdline, re.I):
            return "extra"
    return None


def _related(procs: list[Proc], pid: int) -> tuple[set[int], set[int]]:
    by_pid = {p.pid: p for p in procs}
    ancestors, cur = set(), by_pid.get(pid)
    while cur and cur.ppid and cur.ppid not in ancestors and cur.ppid != cur.pid:
        ancestors.add(cur.ppid)
        cur = by_pid.get(cur.ppid)
    descendants = _descendants(procs, {pid})
    return ancestors, descendants


def _descendants(procs: list[Proc], roots: set[int]) -> set[int]:
    found, changed = set(), True
    while changed:
        changed = False
        for p in procs:
            if (p.ppid in roots or p.ppid in found) and p.pid not in found and p.pid not in roots:
                found.add(p.pid)
                changed = True
    return found


def find_targets(cfg: Config, procs: list[Proc] | None = None, own_pid: int | None = None,
                 exclude: set[int] | None = None) -> list[Proc]:
    """Root Codex processes to stop (children are handled as part of their tree)."""
    procs = list_processes() if procs is None else procs
    own_pid = os.getpid() if own_pid is None else own_pid
    ancestors, descendants = _related(procs, own_pid)
    protected = ancestors | descendants | {own_pid} | (exclude or set())
    wanted = {"cli": cfg.stop_cli, "desktop": cfg.stop_desktop, "extra": True}
    matched = {}
    for p in procs:
        if p.pid in protected or p.pid <= 1:
            continue
        kind = classify(p, cfg)
        if kind and wanted[kind]:
            matched[p.pid] = p
    # Keep only roots: a matched process whose parent is also matched is part of that tree.
    return [p for p in matched.values() if p.ppid not in matched]


def thread_ids_from(procs: list[Proc]) -> list[str]:
    """UUIDs that appear after `resume` in command lines (exact thread ids, never guessed)."""
    ids = []
    for p in procs:
        match = re.search(r"\bresume\s+(" + UUID_RE.pattern + ")", p.cmdline, re.I)
        if match and match.group(1).lower() not in ids:
            ids.append(match.group(1).lower())
    return ids


def stop_processes(targets: list[Proc], cfg: Config, dry_run: bool) -> bool:
    """Politely stop each target tree, then force-kill survivors. Returns True if all gone."""
    if not targets:
        return True
    before = list_processes()
    tree: dict[tuple[int, str], Proc] = {}
    for t in targets:
        tree[t.key] = t
        for pid in _descendants(before, {t.pid}):
            p = next(x for x in before if x.pid == pid)
            tree[p.key] = p
    for t in targets:
        log.warning("%s stop pid=%s %s", "[dry-run] would" if dry_run else "will", t.pid, t.cmdline[:160])
    if dry_run:
        return True

    for t in targets:
        _polite(t)
    deadline = time.monotonic() + cfg.terminate_timeout_seconds
    while time.monotonic() < deadline:
        if not _alive(tree):
            log.info("all Codex processes exited after polite stop")
            return True
        time.sleep(1)

    survivors = _alive(tree)
    for p in survivors:
        log.warning("force-killing pid=%s (%s)", p.pid, p.name)
        _force(p)
    time.sleep(2)
    left = _alive(tree)
    if left:
        log.error("could not stop: %s", ", ".join(str(p.pid) for p in left))
    return not left


def _alive(tree: dict[tuple[int, str], Proc]) -> list[Proc]:
    """Recorded processes that still exist with the same start time."""
    current = {p.key for p in list_processes()}
    return [p for key, p in tree.items() if key in current]


def _polite(p: Proc) -> None:
    try:
        if sys.platform == "win32":
            # Without /F Windows asks the process to close (WM_CLOSE); consoles may ignore it.
            subprocess.run(["taskkill", "/T", "/PID", str(p.pid)], capture_output=True,
                           creationflags=CREATE_NO_WINDOW, timeout=30)
        else:
            os.kill(p.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("polite stop of %s failed: %s", p.pid, exc)


def _force(p: Proc) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(p.pid)], capture_output=True,
                           creationflags=CREATE_NO_WINDOW, timeout=30)
        else:
            os.kill(p.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("force kill of %s failed: %s", p.pid, exc)
