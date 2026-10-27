"""Configuration: built-in defaults, overridden by a TOML file, overridden by CLI flags."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from uuid import UUID

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
    # desktop-queue: recover the registered desktop chat through `codex queue`; never kill processes.
    # relay-queue : recover the current coordinator from TaskRelay state, including worker failures.
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
    # relay-queue reads the active coordinator from this durable TaskRelay state.
    relay_state_path: str = ""
    relay_min_remaining: float = 15.0
    # Safety valve against restart loops.
    max_restarts_per_day: int = 6

    # --- Logging -------------------------------------------------------------
    log_level: str = "INFO"
    log_max_bytes: int = 2_000_000
    log_backups: int = 5

    def threshold_for(self, kind: str) -> float:
        if self.restart_mode == "relay-queue":
            return self.relay_min_remaining
        return {
            "5h": self.five_hour_min_remaining,
            "weekly": self.weekly_min_remaining,
        }.get(kind, self.other_min_remaining)


class ConfigError(ValueError):
    pass


RESTART_MODES = ("none", "exec-resume", "command", "desktop-queue", "relay-queue")


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
    for name in ("five_hour_min_remaining", "weekly_min_remaining", "other_min_remaining", "relay_min_remaining"):
        value = getattr(cfg, name)
        if not 0 <= value < 100:
            raise ConfigError(f"{name} must be between 0 and 100 (got {value})")
    if cfg.restart_mode not in RESTART_MODES:
        raise ConfigError(f"restart_mode must be one of {', '.join(RESTART_MODES)}")
    if cfg.restart_mode == "command" and not cfg.restart_command:
        raise ConfigError("restart_mode = 'command' needs restart_command")
    if cfg.restart_mode == "desktop-queue":
        try:
            valid = str(UUID(cfg.thread_id)) == cfg.thread_id.lower()
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError("desktop-queue needs an exact thread_id UUID")
    if cfg.restart_mode == "relay-queue" and not Path(cfg.relay_state_path).is_absolute():
        raise ConfigError("relay-queue needs an absolute relay_state_path")
    if cfg.poll_seconds < 15:
        raise ConfigError("poll_seconds must be at least 15")
    if cfg.confirm_reads < 1:
        raise ConfigError("confirm_reads must be at least 1")
    if not cfg.app_server_args:
        raise ConfigError("app_server_args must not be empty")


def register_relay(path: Path, state_path: Path, arm: bool = False) -> None:
    """Update only recovery settings, preserving other TOML settings and comments."""
    source = path.read_text(encoding="utf-8") if path.exists() else SAMPLE_CONFIG
    raw = tomllib.loads(source)
    updates = {"restart_mode": "relay-queue", "relay_state_path": str(state_path)}
    if arm:
        updates["armed"] = True
    expected = Config()
    _apply(expected, raw.get("watchdog", raw), source=str(path))
    _apply(expected, updates, source="TaskRelay registration")
    validate(expected)
    pending = updates.copy()
    section = "" if "watchdog" not in raw else "watchdog"
    current = ""
    lines = []
    inserted = False
    for line in source.splitlines(keepends=True):
        header = re.match(r"\s*\[([^\]]+)\]\s*(?:#.*)?$", line)
        if header:
            if current == section and not inserted:
                lines.extend(f"{key} = {json.dumps(value)}\n" for key, value in pending.items())
                inserted = True
            current = header[1].strip()
        match = re.match(r"\s*(\w+)\s*=", line)
        if current == section and match and match[1] in updates:
            key = match[1]
            lines.append(f"{key} = {json.dumps(updates[key])}\n")
            pending.pop(key, None)
            continue
        lines.append(line if line.endswith("\n") else line + "\n")
    if not inserted:
        lines.extend(f"{key} = {json.dumps(value)}\n" for key, value in pending.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as fh:
        temporary = Path(fh.name)
        fh.write("".join(lines))
    try:
        if load_config(temporary) != expected:
            raise ConfigError("cannot safely update this TOML layout; recovery settings were not changed")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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

# none | exec-resume | command | desktop-queue | relay-queue
restart_mode = "exec-resume"
# relay_state_path = "/absolute/project/.task-relay/state.json"
# relay_min_remaining = 15.0  # relay-queue pauses strictly below this percentage
# exec_extra_args = ["--sandbox", "workspace-write"]
# thread_id = "00000000-0000-0000-0000-000000000000"
# restart_cwd = "C:\\\\work\\\\repo"
# restart_command = ["C:\\\\tools\\\\my-resume.cmd", "{thread_id}"]
max_restarts_per_day = 6

# codex_path = "C:\\\\Users\\\\me\\\\AppData\\\\Roaming\\\\npm\\\\codex.cmd"
# app_server_args = ["app-server"]   # older CLIs: ["app-server", "--stdio"]
"""
