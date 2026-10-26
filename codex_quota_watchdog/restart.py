"""Starting Codex work: the initial managed job (`cqw run`) and post-reset restarts."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from .appserver import check_cmd_safe, command_prefix, popen_hidden
from .config import Config

log = logging.getLogger(__name__)

UUID_FULL_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def valid_thread_id(value: str | None) -> bool:
    return bool(value and UUID_FULL_RE.match(value))


class Job:
    """A Codex process started by the watchdog, with output captured to a log file."""

    def __init__(self, proc: subprocess.Popen, log_path: Path, thread_id: str | None = None):
        self.proc = proc
        self.log_path = log_path
        self.thread_id = thread_id
        self.stopped_by_watchdog = False
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        with self.log_path.open("a", encoding="utf-8") as out:
            for line in self.proc.stdout:
                out.write(line)
                out.flush()
                if not self.thread_id:
                    self.thread_id = thread_id_from_event(line)
                    if self.thread_id:
                        log.info("job thread id: %s", self.thread_id)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def poll(self) -> int | None:
        return self.proc.poll()


def thread_id_from_event(line: str) -> str | None:
    """Exact thread id from `codex exec --json` output (thread.started / session_configured)."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    candidates = []
    if event.get("type") == "thread.started":
        candidates.append(event.get("thread_id"))
    msg = event.get("msg")
    if isinstance(msg, dict) and msg.get("type") == "session_configured":
        candidates.append(msg.get("session_id"))
    for value in candidates:
        if isinstance(value, str) and valid_thread_id(value):
            return value.lower()
    return None


def _spawn(args: list[str], cwd: str | None, jobs_dir: Path, label: str) -> Job:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    log_path = jobs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}.log"
    check_cmd_safe(args)
    log.info("starting %s in %s: %s", label, cwd or "(current dir)", _show(args))
    with log_path.open("a", encoding="utf-8") as out:
        out.write(f"# {time.ctime()} {_show(args)}\n")
    proc = popen_hidden(
        args, cwd=cwd or None, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    return Job(proc, log_path)


def _show(args: list[str]) -> str:
    return " ".join(a if len(a) < 80 else a[:77] + "..." for a in args)


def start_new_job(codex: str, cfg: Config, prompt: str, cwd: str | None, jobs_dir: Path) -> Job:
    args = [*command_prefix(codex), "exec", *cfg.exec_extra_args, "--json", prompt]
    return _spawn(args, cwd, jobs_dir, "run")


def build_restart_args(codex: str, cfg: Config, thread_id: str | None, cwd: str | None) -> list[str]:
    """Raise ValueError when restarting would require guessing."""
    if cfg.restart_mode == "exec-resume":
        if not valid_thread_id(thread_id):
            raise ValueError("no exact thread UUID recorded; refusing to guess (no --last)")
        return [*command_prefix(codex), "exec", *cfg.exec_extra_args, "resume", thread_id, cfg.resume_prompt]
    if cfg.restart_mode == "queue":
        if not valid_thread_id(thread_id):
            raise ValueError("no exact thread UUID recorded; refusing to guess")
        return [*command_prefix(codex), "queue", "--thread", thread_id, "--message", cfg.resume_prompt]
    if cfg.restart_mode == "command":
        values = {"codex": codex, "thread_id": thread_id or "", "prompt": cfg.resume_prompt,
                  "cwd": cwd or ""}
        if any("{thread_id}" in part for part in cfg.restart_command) and not valid_thread_id(thread_id):
            raise ValueError("restart_command needs {thread_id} but no exact thread UUID is recorded")
        return [part.format(**values) for part in cfg.restart_command]
    raise ValueError(f"restart_mode '{cfg.restart_mode}' does not start anything")


def restart(args: list[str], cwd: str | None, jobs_dir: Path, thread_id: str | None) -> Job:
    job = _spawn(args, cwd, jobs_dir, "resume")
    if thread_id:
        job.thread_id = thread_id
    return job


def queue(args: list[str], timeout: float = 120.0) -> tuple[bool, str]:
    """Run `codex queue ...` to completion; it only stores the message, so it returns quickly."""
    check_cmd_safe(args)
    log.info("queueing: %s", _show(args))
    try:
        done = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    lines = [ln for ln in (done.stdout + done.stderr).splitlines()
             if ln.strip() and not ln.lstrip().startswith(("WARNING", "Stack backtrace")) and ": <unknown>" not in ln]
    return done.returncode == 0, " | ".join(lines[-3:])
