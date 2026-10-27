"""Recover an explicitly registered desktop chat through `codex queue`."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .appserver import check_cmd_safe, command_prefix, kill_tree, popen_hidden
from .restart import valid_thread_id


class DesktopError(RuntimeError):
    pass


@dataclass(frozen=True)
class DesktopTurn:
    thread_id: str
    turn_id: str | None
    status: str
    quota_limited: bool
    cwd: str


def latest_turn(thread_id: str) -> DesktopTurn:
    """Read only the registered chat's metadata and latest turn; never scan other chats."""
    if not valid_thread_id(thread_id):
        raise DesktopError("desktop recovery requires an exact thread UUID")
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    # shortcut: local desktop schema versions are fixed; revalidate when Codex changes them.
    try:
        with closing(sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute(
                "SELECT cwd, originator, archived FROM threads WHERE id = ?", (thread_id,),
            ).fetchone()
        if row is None or row[1] != "Codex Desktop" or row[2]:
            raise DesktopError("registered thread is missing, archived, or not a local desktop chat")
        with closing(sqlite3.connect((home / "thread_history_1.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
            turn = db.execute(
                "SELECT turn_id, status, error_json FROM thread_turns "
                "WHERE thread_id = ? ORDER BY rollout_ordinal DESC LIMIT 1", (thread_id,),
            ).fetchone()
        if turn is None:
            return DesktopTurn(thread_id, None, "empty", False, row[0])
        if not valid_thread_id(turn[0]):
            raise DesktopError("invalid desktop turn UUID")
        error = json.loads(turn[2]) if turn[2] else None
        if error is not None and not isinstance(error, dict):
            raise DesktopError("invalid desktop turn error")
        limited = turn[1] == "failed" and isinstance(error, dict) and _is_usage_limit(error.get("codexErrorInfo"))
        return DesktopTurn(thread_id, turn[0], turn[1], limited, row[0])
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise DesktopError(f"cannot read desktop chat state: {exc}") from exc


def _is_usage_limit(info) -> bool:
    # Unit variants serialize as a string, data-carrying ones as a single-key object.
    if isinstance(info, dict):
        return list(info) == ["usageLimitExceeded"]
    return info == "usageLimitExceeded"


def check_queue(codex: str, timeout: float) -> None:
    # A CLI without `queue` may treat it as a prompt and print top-level help, so check the flags.
    out = _queue_command([*command_prefix(codex), "queue", "--help"], timeout)
    if "--thread" not in out or "--message" not in out:
        raise DesktopError("this codex CLI has no `queue --thread --message` command")


def queue_resume(codex: str, thread_id: str, prompt: str, timeout: float) -> None:
    if not valid_thread_id(thread_id):
        raise DesktopError("refusing to queue without an exact thread UUID")
    _queue_command([*command_prefix(codex), "queue", "--thread", thread_id, "--message", prompt], timeout)


def _queue_command(args: list[str], timeout: float) -> str:
    check_cmd_safe(args)
    try:
        with popen_hidden(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace") as proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                proc.communicate()
                raise DesktopError("desktop queue timed out; delivery is uncertain, not retrying")
            if proc.returncode:
                raise DesktopError(f"desktop queue failed: {stderr.strip()[:300]}")
            return stdout + stderr
    except OSError as exc:
        raise DesktopError(f"cannot run desktop queue: {exc}") from exc
