"""Persistent state (survives watchdog restarts) and the single-instance lock."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MONITORING = "monitoring"
WAITING_RESET = "waiting_reset"
NEEDS_USER = "needs_user"
DONE = "done"


@dataclass
class State:
    phase: str = MONITORING
    stopped_at: float | None = None
    stop_reasons: list[str] = field(default_factory=list)
    resume_after: float | None = None
    account_id: str | None = None
    thread_id: str | None = None
    cwd: str | None = None
    note: str = ""
    restart_history: list[float] = field(default_factory=list)
    last_check_at: float | None = None
    last_summary: list[str] = field(default_factory=list)
    # Job started by `cqw run`; restarted only through its exact thread id.
    managed_pid: int | None = None
    # Desktop failure selected for recovery, and the last delivery attempted (never auto-retry).
    desktop_turn_id: str | None = None
    desktop_attempted_turn_id: str | None = None
    relay_attempted_recoveries: list[str] = field(default_factory=list)
    relay_state_path: str | None = None

    def restarts_last_day(self, now: float) -> int:
        return sum(1 for t in self.restart_history if now - t < 86400)


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            known = State.__dataclass_fields__
            return State(**{k: v for k, v in data.items() if k in known})
        except (OSError, ValueError, TypeError) as exc:
            # Unreadable state must never lead to a restart: park in needs_user.
            return State(phase=NEEDS_USER, note=f"state file unreadable: {exc}")

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        state.restart_history = [t for t in state.restart_history if time.time() - t < 7 * 86400]
        tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """OS-level exclusive lock; released automatically if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise AlreadyRunning(f"another watchdog holds {self.path}") from exc
        self._fh = fh
        return self

    def __exit__(self, *exc) -> None:
        if self._fh:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
            self._fh = None
