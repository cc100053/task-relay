"""Configuration: built-in defaults, overridden by a TOML file, overridden by CLI flags."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

APP_NAME = "codex-quota-watchdog"


def data_dir() -> Path:
    """Per-user directory for config, state and logs."""
    override = os.environ.get("CQW_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP_NAME


def default_config_path() -> Path:
    return data_dir() / "config.toml"


@dataclass
class Config:
    # --- Codex entry point -------------------------------------------------
    # Path to the codex executable. Empty = auto-detect (desktop bundle, then PATH).
    codex_path: str = ""
    # Arguments that start the app-server on stdio. Older CLIs need ["app-server", "--stdio"].
    app_server_args: list[str] = field(default_factory=lambda: ["app-server"])
    probe_timeout_seconds: float = 30.0

    # --- Thresholds ----------------------------------------------------------
    # Stop Codex when the remaining percentage of a window is <= this value.
    five_hour_min_remaining: float = 3.0
    weekly_min_remaining: float = 1.0
    # Any other window (unknown duration) uses this threshold.
    other_min_remaining: float = 1.0

    # --- Timing --------------------------------------------------------------
    poll_seconds: int = 120
    # Poll faster once remaining quota is within this many points of a threshold.
    near_limit_margin: float = 10.0
    near_limit_poll_seconds: int = 30
    # Extra wait after the official resetsAt before we re-check quota.
    grace_seconds: int = 120
    # While quota state is unknown, back off between these bounds.
    unknown_backoff_min_seconds: int = 60
    unknown_backoff_max_seconds: int = 900
    # Longest single sleep, so laptop sleep / clock jumps are noticed quickly.
    max_sleep_chunk_seconds: int = 300
    # Consecutive healthy reads required after the reset before restarting.
    confirm_reads: int = 2
    confirm_interval_seconds: int = 20

    # --- Process control -----------------------------------------------------
    # Without arming, the watchdog only logs what it would do (dry run).
    armed: bool = False
    # Which processes count as "Codex". cli = codex CLI / TUI / exec / node launcher.
    # desktop = the ChatGPT/Codex desktop app (off by default: killing it closes your UI).
    stop_cli: bool = True
    stop_desktop: bool = False
    # Extra regexes matched against the full command line.
    extra_process_patterns: list[str] = field(default_factory=list)
    # Seconds to wait after a polite stop before force-killing.
    terminate_timeout_seconds: int = 20

    # --- Restart -------------------------------------------------------------
    # none        : stop + wait only, never start anything.
    # exec-resume : `codex exec resume <thread UUID> <prompt>` (exact UUID only, never --last).
    # queue       : `codex queue --thread <UUID> --message <prompt>`; the Codex desktop app
    #               sends the queued message in the same thread, visible in its UI.
    # command     : run `restart_command` (a list of args, placeholders allowed).
    restart_mode: str = "exec-resume"
    resume_prompt: str = (
        "The ChatGPT usage window has reset. Continue the previous task from where it stopped. "
        "Re-check the current state of the workspace before making further changes."
    )
    # Extra args inserted right after `exec` (for example ["--sandbox", "workspace-write"]).
    exec_extra_args: list[str] = field(default_factory=list)
    restart_command: list[str] = field(default_factory=list)
    # Working directory for restarted work. Empty = the directory recorded at stop time.
    restart_cwd: str = ""
    # Thread to resume when none was captured automatically.
    thread_id: str = ""
    # Safety valve against restart loops (per thread when discover_threads is on).
    max_restarts_per_day: int = 6

    # --- Thread discovery (desktop app, several sessions at once) -------------
    # Find every thread whose latest turn stopped on the usage limit and resume each one.
    # Also enters the wait phase when no Codex process was stopped (Codex stops itself).
    discover_threads: bool = False
    # Only threads whose limited turn ended within this many hours.
    thread_lookback_hours: float = 12.0
    # Sub-agent threads are normally driven by their parent; resume them separately only if true.
    resume_subagents: bool = False
    # Resume at most this many threads per reset, and pause between them.
    max_threads_per_resume: int = 8
    resume_stagger_seconds: int = 5
    # restart_mode = "queue": warn (needs_user) when no turn has started this long after queueing.
    queue_start_timeout_seconds: int = 900

    # --- Logging -------------------------------------------------------------
    log_level: str = "INFO"
    log_max_bytes: int = 2_000_000
    log_backups: int = 5

    def threshold_for(self, kind: str) -> float:
        return {
            "5h": self.five_hour_min_remaining,
            "weekly": self.weekly_min_remaining,
        }.get(kind, self.other_min_remaining)


class ConfigError(ValueError):
    pass


RESTART_MODES = ("none", "exec-resume", "queue", "command")


def load_config(path: Path | None = None, overrides: dict | None = None) -> Config:
    cfg = Config()
    path = path or default_config_path()
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        # Allow either a flat file or a [watchdog] table.
        raw = raw.get("watchdog", raw)
        _apply(cfg, raw, source=str(path))
    if overrides:
        _apply(cfg, {k: v for k, v in overrides.items() if v is not None}, source="command line")
    validate(cfg)
    return cfg


def _apply(cfg: Config, raw: dict, source: str) -> None:
    known = {f.name: f for f in fields(cfg)}
    for key, value in raw.items():
        if key not in known:
            raise ConfigError(f"{source}: unknown setting '{key}'")
        current = getattr(cfg, key)
        if isinstance(current, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"{source}: '{key}' must be true/false")
        elif isinstance(current, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{source}: '{key}' must be a number")
            value = type(current)(value)
        elif isinstance(current, list):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{source}: '{key}' must be a list of strings")
        elif isinstance(current, str) and not isinstance(value, str):
            raise ConfigError(f"{source}: '{key}' must be a string")
        setattr(cfg, key, value)


def validate(cfg: Config) -> None:
    for name in ("five_hour_min_remaining", "weekly_min_remaining", "other_min_remaining"):
        value = getattr(cfg, name)
        if not 0 <= value < 100:
            raise ConfigError(f"{name} must be between 0 and 100 (got {value})")
    if cfg.restart_mode not in RESTART_MODES:
        raise ConfigError(f"restart_mode must be one of {', '.join(RESTART_MODES)}")
    if cfg.restart_mode == "command" and not cfg.restart_command:
        raise ConfigError("restart_mode = 'command' needs restart_command")
    if cfg.poll_seconds < 15:
        raise ConfigError("poll_seconds must be at least 15")
    if cfg.confirm_reads < 1:
        raise ConfigError("confirm_reads must be at least 1")
    if not cfg.app_server_args:
        raise ConfigError("app_server_args must not be empty")
    if cfg.thread_lookback_hours <= 0:
        raise ConfigError("thread_lookback_hours must be positive")
    if cfg.max_threads_per_resume < 1:
        raise ConfigError("max_threads_per_resume must be at least 1")


SAMPLE_CONFIG = """\
# Codex Quota Watchdog configuration. Every key is optional; defaults are shown.
[watchdog]
# Leave false until `cqw doctor` lists exactly the processes you expect.
armed = false

# Stop Codex when remaining % of a window is at or below these values.
five_hour_min_remaining = 3.0
weekly_min_remaining = 1.0

poll_seconds = 120
grace_seconds = 120          # wait this long after the official resetsAt
confirm_reads = 2            # healthy reads in a row before restarting

stop_cli = true              # codex CLI / TUI / exec processes
stop_desktop = false         # the desktop app (closes your UI if true)
terminate_timeout_seconds = 20

# none | exec-resume | queue | command
restart_mode = "exec-resume"
# exec_extra_args = ["--sandbox", "workspace-write"]
# thread_id = "00000000-0000-0000-0000-000000000000"
# restart_cwd = "C:\\\\work\\\\repo"
# restart_command = ["C:\\\\tools\\\\my-resume.cmd", "{thread_id}"]
max_restarts_per_day = 6

# Codex desktop app / several sessions at once: let Codex stop itself at the limit, then
# resume every thread whose last turn hit the limit, inside the app.
# discover_threads = true
# restart_mode = "queue"
# stop_cli = false
# thread_lookback_hours = 12
# resume_subagents = false
# max_threads_per_resume = 8

# codex_path = "C:\\\\Users\\\\me\\\\AppData\\\\Roaming\\\\npm\\\\codex.cmd"
# app_server_args = ["app-server"]   # older CLIs: ["app-server", "--stdio"]
"""
