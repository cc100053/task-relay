"""Finding the threads that stopped on the usage limit, via `codex app-server`.

Used when Codex runs inside the desktop app (or several sessions run at once): no command line
carries a thread UUID, but Codex records why each turn ended. A thread is a resume candidate only
when its *latest* turn failed with `codexErrorInfo == "usageLimitExceeded"` - the exact signal
Codex writes when it hits the limit itself. Anything else (a finished turn, an interrupted turn
with no error, a later turn the user already started) is left alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .appserver import ProbeError
from .config import Config
from .restart import valid_thread_id

log = logging.getLogger(__name__)

LIMIT_ERROR = "usageLimitExceeded"
TOP_LEVEL_SOURCES = ["cli", "vscode", "exec", "appServer", "unknown"]
SUBAGENT_SOURCES = ["subAgent", "subAgentThreadSpawn", "subAgentOther"]
PAGE_SIZE = 50
MAX_PAGES = 4


@dataclass
class LimitedThread:
    thread_id: str
    turn_id: str
    cwd: str | None
    title: str
    completed_at: float | None
    subagent: bool


@dataclass
class TurnInfo:
    turn_id: str
    status: str
    error_kind: str | None
    started_at: float | None
    completed_at: float | None


def source_kinds(cfg: Config) -> list[str]:
    return TOP_LEVEL_SOURCES + (SUBAGENT_SOURCES if cfg.resume_subagents else [])


def _error_kind(turn: dict) -> str | None:
    error = turn.get("error")
    if not isinstance(error, dict):
        return None
    info = error.get("codexErrorInfo")
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and len(info) == 1:
        return next(iter(info))
    return None


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def latest_turn(session, thread_id: str) -> TurnInfo | None:
    result = session.call("thread/turns/list", {"threadId": thread_id, "limit": 1, "sortDirection": "desc"})
    data = result.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    turn = data[0]
    if not isinstance(turn.get("id"), str):
        return None
    return TurnInfo(turn["id"], str(turn.get("status")), _error_kind(turn),
                    _num(turn.get("startedAt")), _num(turn.get("completedAt")))


def _is_subagent(thread: dict) -> bool:
    source = thread.get("source")
    return isinstance(source, dict) and "subAgent" in source


def recent_threads(session, cfg: Config, since: float) -> list[dict]:
    """Non-archived threads updated at or after `since`, newest first."""
    found: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        params = {"limit": PAGE_SIZE, "sortKey": "updated_at", "sortDirection": "desc",
                  "archived": False, "sourceKinds": source_kinds(cfg)}
        if cursor:
            params["cursor"] = cursor
        result = session.call("thread/list", params)
        data = result.get("data")
        if not isinstance(data, list):
            raise ProbeError("thread/list returned no data list")
        for thread in data:
            if not isinstance(thread, dict) or not valid_thread_id(thread.get("id")):
                continue
            updated = _num(thread.get("updatedAt"))
            if updated is None or updated < since:
                return found
            if thread.get("ephemeral"):
                continue
            if _is_subagent(thread) and not cfg.resume_subagents:
                continue
            found.append(thread)
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return found


def find_limited(client, cfg: Config, now: float) -> list[LimitedThread]:
    """Threads whose latest turn ended on the usage limit within the lookback window.

    Raises ProbeError when the list itself can't be read; a single unreadable thread is skipped.
    """
    since = now - cfg.thread_lookback_hours * 3600
    limited: list[LimitedThread] = []
    with client.session() as session:
        for thread in recent_threads(session, cfg, since):
            try:
                turn = latest_turn(session, thread["id"])
            except ProbeError as exc:
                log.warning("could not read turns of thread %s: %s", thread["id"], exc)
                continue
            if turn is None or turn.error_kind != LIMIT_ERROR:
                continue
            if turn.completed_at is not None and turn.completed_at < since:
                continue
            title = thread.get("name") or thread.get("preview") or ""
            limited.append(LimitedThread(
                thread["id"], turn.turn_id, thread.get("cwd") if isinstance(thread.get("cwd"), str) else None,
                " ".join(str(title).split())[:60], turn.completed_at, _is_subagent(thread)))
    return limited


def started_since(client, thread_id: str, since: float) -> bool | None:
    """Has a turn started on this thread at or after `since`? None when it can't be read."""
    try:
        with client.session() as session:
            turn = latest_turn(session, thread_id)
    except ProbeError as exc:
        log.warning("could not check thread %s: %s", thread_id, exc)
        return None
    return bool(turn and turn.started_at is not None and turn.started_at >= since - 5)
