import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest import mock

from codex_quota_watchdog import desktop
from codex_quota_watchdog.cli import build_parser, cmd_clear
from codex_quota_watchdog.config import ConfigError, load_config
from codex_quota_watchdog.state import State, StateStore

TID = "0199a213-81c0-7800-8aa1-bbab2a035a53"
TURN = "0199a213-81c0-7800-8aa1-bbab2a035a54"


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id TEXT, cwd TEXT, originator TEXT, archived INTEGER)")
            db.execute("INSERT INTO threads VALUES (?, '/work', 'Codex Desktop', 0)", (TID,))
        with closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as db, db:
            db.execute("CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, status TEXT, "
                       "error_json TEXT, rollout_ordinal INTEGER)")

    def add_turn(self, status="failed", error=None, ordinal=1):
        with closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as db, db:
            db.execute("INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?)",
                       (TID, TURN, status, json.dumps(error), ordinal))

    def test_only_structured_quota_failure_is_recoverable(self):
        for status, error, expected in [
            ("failed", {"codexErrorInfo": "usageLimitExceeded"}, True),
            ("failed", {"message": "usage limit exceeded"}, False),
            ("failed", {"codexErrorInfo": "serverOverloaded"}, False),
            ("failed", {"codexErrorInfo": "unauthorized"}, False),
            ("failed", {"codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 429}}}, False),
            ("inProgress", {"codexErrorInfo": "usageLimitExceeded"}, False),
            ("interrupted", None, False),
            ("completed", None, False),
        ]:
            with self.subTest(status=status, error=error):
                with closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as db, db:
                    db.execute("DELETE FROM thread_turns")
                self.add_turn(status, error)
                turn = desktop.latest_turn(TID)
                self.assertEqual(turn.quota_limited, expected)
                self.assertEqual(turn.thread_id, TID)
                self.assertEqual(turn.cwd, "/work")

    def test_new_turn_hides_old_failure(self):
        self.add_turn(error={"codexErrorInfo": "usageLimitExceeded"})
        self.add_turn("inProgress", ordinal=2)
        self.assertFalse(desktop.latest_turn(TID).quota_limited)

    def test_missing_other_archived_and_non_desktop_threads_refused(self):
        with self.assertRaises(desktop.DesktopError):
            desktop.latest_turn(TURN)
        for column, value in [("archived", 1), ("originator", "codex_cli")]:
            with self.subTest(column=column):
                with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db, db:
                    db.execute("UPDATE threads SET archived=0, originator='Codex Desktop'")
                    db.execute(f"UPDATE threads SET {column}=?", (value,))
                with self.assertRaises(desktop.DesktopError):
                    desktop.latest_turn(TID)

    def test_invalid_uuid_and_missing_schema_fail_closed(self):
        with self.assertRaises(desktop.DesktopError):
            desktop.latest_turn("latest")
        (self.home / "state_5.sqlite").unlink()
        with self.assertRaises(desktop.DesktopError):
            desktop.latest_turn(TID)
        self.assertFalse((self.home / "state_5.sqlite").exists())

    def test_empty_chat_and_malformed_error(self):
        self.assertIsNone(desktop.latest_turn(TID).turn_id)
        self.add_turn(error=["usageLimitExceeded"])
        with self.assertRaises(desktop.DesktopError):
            desktop.latest_turn(TID)

    def test_queue_uses_exact_chat_without_exec_or_last(self):
        with mock.patch.object(desktop, "popen_hidden") as spawn:
            proc = spawn.return_value.__enter__.return_value
            proc.communicate.return_value = ("queued", "")
            proc.returncode = 0
            desktop.queue_resume("codex", TID, "Continue", 10)
        self.assertEqual(spawn.call_args.args[0], ["codex", "queue", "--thread", TID, "--message", "Continue"])
        with self.assertRaises(desktop.DesktopError):
            desktop.queue_resume("codex", "--last", "Continue", 10)

    def test_queue_failure_and_timeout_report_uncertain_delivery(self):
        with mock.patch.object(desktop, "popen_hidden") as spawn:
            proc = spawn.return_value.__enter__.return_value
            proc.communicate.return_value = ("", "queue unavailable")
            proc.returncode = 1
            with self.assertRaisesRegex(desktop.DesktopError, "queue unavailable"):
                desktop.queue_resume("codex", TID, "Continue", 10)
            proc.communicate.side_effect = [subprocess.TimeoutExpired("codex", 10), ("", "")]
            with mock.patch.object(desktop, "kill_tree") as kill:
                with self.assertRaisesRegex(desktop.DesktopError, "uncertain"):
                    desktop.queue_resume("codex", TID, "Continue", 10)
                kill.assert_called_once_with(proc)

    def test_mode_requires_uuid_and_can_capture_executor_thread(self):
        missing = self.home / "missing.toml"
        with self.assertRaises(ConfigError):
            load_config(missing, {"restart_mode": "desktop-queue"})
        cfg = load_config(missing, {"restart_mode": "desktop-queue", "thread_id": TID})
        self.assertEqual(cfg.thread_id, TID)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": TID}):
            args = build_parser().parse_args(["watch", "--desktop-thread"])
        self.assertEqual(args.desktop_thread, TID)

    def test_clear_preserves_delivery_marker(self):
        store = StateStore(self.home / "state.json")
        store.save(State(desktop_attempted_turn_id=TURN))
        with mock.patch("codex_quota_watchdog.cli.paths", return_value={"state": store.path}):
            cmd_clear(None)
        self.assertEqual(store.load().desktop_attempted_turn_id, TURN)
