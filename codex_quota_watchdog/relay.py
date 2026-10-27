"""Read TaskRelay ownership and recovery requests; only the coordinator writes them."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import desktop
from .restart import valid_thread_id

STATUSES = {"RUNNING", "REVIEW_READY", "HANDOFF_PENDING", "WAITING_QUOTA", "NEEDS_INPUT", "DONE", "PAUSED"}


def can_wake(turn: desktop.DesktopTurn) -> bool:
    return turn.status in {"completed", "empty"} or turn.quota_limited


@dataclass(frozen=True)
class Relay:
    path: Path
    coordinator: str
    workers: tuple[str, ...]
    status: str
    recovery_id: str | None
    resume_at: float | None

    def pending(self, attempted: list[str]) -> list[str]:
        if not isinstance(attempted, list) or not all(isinstance(key, str) for key in attempted):
            raise desktop.DesktopError("invalid persisted TaskRelay delivery markers")
        if self.status not in {"RUNNING", "REVIEW_READY", "WAITING_QUOTA"}:
            return []
        events = []
        if self.status == "WAITING_QUOTA":
            events.append(f"request:{self.recovery_id}")
        for thread_id in (self.coordinator, *self.workers):
            turn = desktop.latest_turn(thread_id)
            if turn.quota_limited:
                events.append(f"failure:{thread_id}:{turn.turn_id}")
        return [key for event in events if (key := f"{self.path}#{event}") not in attempted]

    def prompt(self) -> str:
        return (
            f"Use $task-relay to recover the relay at {json.dumps(str(self.path))}. "
            "Re-read that state and verify this chat is its active coordinator before acting. "
            "Recheck quota, reconcile worker status, checkpoints, pending results and ownership, "
            "then continue only the authorized next action. Preserve pending human decisions. "
            "If ownership changed or the relay is paused or done, exit without dispatching."
        )


def read(path: str | Path) -> Relay:
    path = Path(path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("status") not in STATUSES:
            raise ValueError("invalid relay status")
        coordinator = _thread(raw.get("coordinator"))
        workers = raw.get("workers", [])
        if not isinstance(workers, list):
            raise ValueError("workers must be a list")
        worker_ids = tuple(dict.fromkeys(_thread(worker) for worker in workers))
        if coordinator in worker_ids:
            raise ValueError("coordinator must not also be a worker")
        recovery_id = raw.get("quotaRecoveryId")
        if raw["status"] == "WAITING_QUOTA" and not valid_thread_id(recovery_id):
            raise ValueError("WAITING_QUOTA needs a quotaRecoveryId UUID")
        resume_at = raw.get("resumeAtUTC")
        if resume_at is not None:
            stamp = datetime.fromisoformat(resume_at.replace("Z", "+00:00"))
            if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
                raise ValueError("resumeAtUTC must have a UTC timezone")
            resume_at = stamp.timestamp()
            if not math.isfinite(resume_at):
                raise ValueError("invalid resumeAtUTC")
        return Relay(path, coordinator, worker_ids, raw["status"], recovery_id, resume_at)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise desktop.DesktopError(f"cannot read TaskRelay state: {exc}") from exc


def _thread(value) -> str:
    if not isinstance(value, dict) or value.get("hostId") != "local":
        raise ValueError("relay participants must specify hostId=local")
    thread_id = value.get("threadId")
    if not isinstance(thread_id, str) or not valid_thread_id(thread_id):
        raise ValueError("relay participant needs an exact threadId UUID")
    return thread_id.lower()
