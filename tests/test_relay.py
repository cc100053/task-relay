import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_quota_watchdog import desktop, relay
from codex_quota_watchdog.cli import build_parser, cmd_clear, cmd_register_relay
from codex_quota_watchdog.config import ConfigError, load_config, register_relay
from codex_quota_watchdog.state import MONITORING, NEEDS_USER, WAITING_RESET
from codex_quota_watchdog.appserver import ProbeError
from .helpers import rate_limits
from .test_watchdog import WatchdogTestCase, TID

SUCCESSOR = "0199a213-81c0-7800-8aa1-bbab2a035a55"
WORKER = "0199a213-81c0-7800-8aa1-bbab2a035a56"
TURN = "0199a213-81c0-7800-8aa1-bbab2a035a57"
REQUEST = "0199a213-81c0-7800-8aa1-bbab2a035a58"


class RelayTests(WatchdogTestCase):
    def setUp(self):
        super().setUp()
        self.path = Path(self.tmp.name, "relay.json")
        self.cfg.restart_mode = "relay-queue"
        self.cfg.relay_state_path = str(self.path)
        self.raw = {"status": "RUNNING", "coordinator": {"threadId": TID, "hostId": "local"},
                    "workers": [{"threadId": WORKER, "hostId": "local"}]}
        self.save()
        self.turns = {
            TID: desktop.DesktopTurn(TID, TID, "completed", False, "/work"),
            SUCCESSOR: desktop.DesktopTurn(SUCCESSOR, SUCCESSOR, "completed", False, "/work"),
            WORKER: desktop.DesktopTurn(WORKER, TURN, "failed", True, "/work"),
        }
        patch = mock.patch.object(desktop, "latest_turn", side_effect=lambda tid: self.turns[tid])
        self.latest = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(desktop, "queue_resume")
        self.queue = patch.start()
        self.addCleanup(patch.stop)

    def save(self):
        self.path.write_text(json.dumps(self.raw), encoding="utf-8")

    def ready(self, dog, used=0):
        self.clock.now = dog.state.resume_after + 1
        self.set_quota(five_used=used)
        dog.tick()
        self.clock.now += self.cfg.confirm_interval_seconds
        dog.tick()

    def test_worker_failure_wakes_current_successor_once_after_restart(self):
        dog = self.dog()
        self.set_quota(five_used=100)
        dog.tick()
        self.assertEqual(dog.state.phase, WAITING_RESET)
        self.assertEqual(dog.state.thread_id, TID)
        self.assertEqual(self.procs.stopped, [])
        self.raw["status"] = "HANDOFF_PENDING"
        self.raw["coordinator"]["threadId"] = SUCCESSOR
        self.save()
        dog.tick()
        self.assertEqual(dog.state.phase, MONITORING)
        self.queue.assert_not_called()
        self.raw["status"] = "RUNNING"
        self.save()
        dog.tick()
        self.ready(dog)
        args = self.queue.call_args.args
        self.assertEqual(args[1], SUCCESSOR)
        self.assertIn(str(self.path), args[2])
        self.assertIn("verify this chat is its active coordinator", args[2])
        self.assertEqual(self.procs.stopped, [])
        self.assertEqual(self.launched, [])
        self.dog().tick()
        self.queue.assert_called_once()

    def test_successor_change_during_wait_targets_new_uuid(self):
        dog = self.dog()
        self.set_quota(five_used=100)
        dog.tick()
        self.raw["coordinator"]["threadId"] = SUCCESSOR
        self.save()
        self.ready(dog)
        self.assertEqual(self.queue.call_args.args[1], SUCCESSOR)

    def test_explicit_wait_wakes_completed_coordinator_without_worker_failure(self):
        self.raw.update(status="WAITING_QUOTA", quotaRecoveryId=REQUEST, workers=[],
                        resumeAtUTC="2027-01-15T09:00:00Z")
        self.save()
        dog = self.dog()
        self.set_quota(five_used=0)
        dog.tick()
        self.assertGreaterEqual(dog.state.resume_after, relay.read(self.path).resume_at)
        self.ready(dog)
        self.queue.assert_called_once()
        self.dog().tick()
        self.queue.assert_called_once()

    def test_new_wait_id_allows_a_later_episode(self):
        self.raw.update(status="WAITING_QUOTA", quotaRecoveryId=REQUEST, workers=[])
        self.save()
        dog = self.dog()
        self.set_quota(five_used=0)
        dog.tick()
        self.ready(dog)
        self.raw["quotaRecoveryId"] = TURN
        self.save()
        dog.tick()
        self.ready(dog)
        self.assertEqual(self.queue.call_count, 2)

    def test_exactly_fifteen_percent_resumes_but_below_does_not(self):
        for used, expected in ((85, 1), (85.1, 0)):
            with self.subTest(used=used):
                self.store.path.unlink(missing_ok=True)
                self.queue.reset_mock()
                dog = self.dog()
                self.set_quota(five_used=100)
                dog.tick()
                self.ready(dog, used)
                self.assertEqual(self.queue.call_count, expected)

    def test_unknown_quota_weekly_block_and_account_change_do_not_send(self):
        for case in ("unknown", "weekly", "account"):
            with self.subTest(case=case):
                self.store.path.unlink(missing_ok=True)
                dog = self.dog()
                self.set_quota(five_used=100)
                dog.tick()
                self.clock.now = dog.state.resume_after + 1
                self.client.reply = (ProbeError("offline") if case == "unknown" else
                                     rate_limits(self.clock.now, five_used=0,
                                                 week_used=90 if case == "weekly" else 0,
                                                 account="other" if case == "account" else "acct-1"))
                dog.tick()
                dog.tick()
                self.queue.assert_not_called()
                self.assertEqual(dog.state.phase, NEEDS_USER if case == "account" else WAITING_RESET)

    def test_paused_done_needs_input_and_handoff_cancel_wait(self):
        for status in ("PAUSED", "DONE", "NEEDS_INPUT", "HANDOFF_PENDING"):
            with self.subTest(status=status):
                self.raw["status"] = "RUNNING"
                self.save()
                dog = self.dog()
                self.set_quota(five_used=100)
                dog.tick()
                self.raw["status"] = status
                self.save()
                dog.tick()
                self.assertEqual(dog.state.phase, MONITORING)
                self.assertIsNone(dog.state.resume_after)
        self.queue.assert_not_called()

    def test_active_or_interrupted_coordinator_defers_delivery(self):
        for status in ("inProgress", "interrupted", "failed", "unknown"):
            with self.subTest(status=status):
                dog = self.dog()
                self.set_quota(five_used=100)
                dog.tick()
                self.turns[TID] = desktop.DesktopTurn(TID, TID, status, False, "/work")
                self.ready(dog)
                self.queue.assert_not_called()
                self.turns[TID] = desktop.DesktopTurn(TID, TID, "completed", False, "/work")

    def test_resolved_worker_failure_cancels_delivery(self):
        dog = self.dog()
        self.set_quota(five_used=100)
        dog.tick()
        self.turns[WORKER] = desktop.DesktopTurn(WORKER, TURN, "completed", False, "/work")
        self.ready(dog)
        self.queue.assert_not_called()
        self.assertEqual(dog.state.phase, MONITORING)

    def test_ambiguous_send_persists_attempt_and_clear_keeps_it(self):
        dog = self.dog()
        self.set_quota(five_used=100)
        dog.tick()
        self.queue.side_effect = desktop.DesktopError("uncertain delivery")
        self.ready(dog)
        self.assertEqual(dog.state.phase, NEEDS_USER)
        self.assertTrue(self.store.load().relay_attempted_recoveries)
        with mock.patch("codex_quota_watchdog.cli.paths", return_value={"state": self.store.path}):
            cmd_clear(None)
        self.dog().tick()
        self.queue.assert_called_once()

    def test_no_failure_or_explicit_wait_does_not_wake(self):
        self.turns[WORKER] = desktop.DesktopTurn(WORKER, TURN, "completed", False, "/work")
        dog = self.dog()
        self.set_quota(five_used=100)
        dog.tick()
        self.assertEqual(dog.state.phase, MONITORING)
        self.queue.assert_not_called()

    def test_invalid_or_remote_state_fails_closed_without_killing(self):
        for changes in ({"coordinator": {"threadId": TID, "hostId": "remote"}},
                        {"coordinator": {"threadId": "--last", "hostId": "local"}},
                        {"status": "WAITING_QUOTA"}, {"workers": "all"},
                        {"resumeAtUTC": "2027-01-15T09:00:00"}):
            with self.subTest(changes=changes):
                original = self.raw.copy()
                self.raw.update(changes)
                self.save()
                self.store.path.unlink(missing_ok=True)
                dog = self.dog()
                self.set_quota(five_used=100)
                dog.tick()
                self.assertEqual(dog.state.phase, NEEDS_USER)
                self.assertEqual(self.procs.stopped, [])
                self.raw = original
        self.queue.assert_not_called()

    def test_missing_state_during_wait_parks(self):
        dog = self.dog()
        self.set_quota(five_used=100)
        dog.tick()
        self.path.unlink()
        dog.tick()
        self.assertEqual(dog.state.phase, NEEDS_USER)
        self.queue.assert_not_called()

    def test_ownership_changes_during_final_check_defer_delivery(self):
        dog = self.dog()
        self.set_quota(five_used=100)
        dog.tick()
        original = self.latest.side_effect

        def change_owner(thread_id):
            turn = original(thread_id)
            if thread_id == WORKER:
                self.raw["coordinator"]["threadId"] = SUCCESSOR
                self.save()
            return turn

        self.latest.side_effect = change_owner
        dog._restart_relay(self.clock.now)
        self.queue.assert_not_called()
        self.assertEqual(self.store.load().relay_attempted_recoveries, [])

    def test_final_check_honors_a_new_resume_deadline(self):
        dog = self.dog()
        self.set_quota(five_used=100)
        dog.tick()
        self.raw["resumeAtUTC"] = "2027-01-15T09:00:00Z"
        self.save()
        dog._restart_relay(self.clock.now)
        self.queue.assert_not_called()
        self.assertEqual(dog.state.resume_after, relay.read(self.path).resume_at)

    def test_coordinator_quota_failure_without_workers_is_recoverable(self):
        self.raw["workers"] = []
        self.save()
        self.turns[TID] = desktop.DesktopTurn(TID, TURN, "failed", True, "/work")
        dog = self.dog()
        self.set_quota(five_used=100)
        dog.tick()
        self.ready(dog)
        self.assertEqual(self.queue.call_args.args[1], TID)

    def test_dry_run_never_queues_or_stops(self):
        dog = self.dog(dry_run=True)
        self.set_quota(five_used=100)
        dog.tick()
        self.ready(dog)
        self.queue.assert_not_called()
        self.assertEqual(self.procs.stopped, [])


class RegistrationTests(unittest.TestCase):
    def test_preserves_flat_and_table_configs_and_comments(self):
        for source in ('codex_path = "codex"\narmed = false\n',
                       '# retained\n[watchdog]\ncodex_path = "codex"\narmed = false\n',
                       '[watchdog] # settings\ncodex_path = "codex"\narmed = false\n'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "config.toml")
                state = Path(tmp, "relay.json")
                path.write_text(source)
                register_relay(path, state, arm=True)
                cfg = load_config(path)
                self.assertEqual(cfg.restart_mode, "relay-queue")
                self.assertEqual(cfg.relay_state_path, str(state))
                self.assertEqual(cfg.codex_path, "codex")
                self.assertTrue(cfg.armed)
                once = path.read_text()
                register_relay(path, state)
                self.assertEqual(path.read_text(), once)
                if "# retained" in source:
                    self.assertIn("# retained", once)

    def test_new_config_and_absolute_path_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.toml")
            register_relay(path, Path(tmp, "relay.json"))
            self.assertFalse(load_config(path).armed)
            with self.assertRaises(ConfigError):
                load_config(path, {"relay_state_path": "relative.json"})

    def test_registration_preserves_other_tables_and_rejects_bad_toml_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.toml")
            state = Path(tmp, "relay.json")
            path.write_text('[watchdog]\narmed = false\n[other]\nkeep = "value"\n')
            register_relay(path, state)
            self.assertIn('[other]\nkeep = "value"', path.read_text())
            self.assertEqual(load_config(path).relay_state_path, str(state))
            path.write_text('invalid = [')
            with self.assertRaises(ValueError):
                register_relay(path, state)
            self.assertEqual(path.read_text(), 'invalid = [')

    def test_registration_refuses_to_replace_another_active_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.toml")
            first, second = Path(tmp, "first.json"), Path(tmp, "second.json")
            raw = {"status": "RUNNING", "coordinator": {"threadId": TID, "hostId": "local"}}
            first.write_text(json.dumps(raw))
            second.write_text(json.dumps(raw))
            register_relay(path, first)
            before = path.read_text()
            args = build_parser().parse_args(["--config", str(path), "register-relay", "--state", str(second)])
            self.assertEqual(cmd_register_relay(args), 2)
            self.assertEqual(path.read_text(), before)

    def test_cli_registration_checks_queue_before_writing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.toml")
            state = Path(tmp, "relay.json")
            state.write_text(json.dumps({"status": "RUNNING", "coordinator": {
                "threadId": TID, "hostId": "local"}, "workers": []}))
            args = build_parser().parse_args(["--config", str(path), "register-relay", "--state", str(state), "--arm"])
            turn = desktop.DesktopTurn(TID, TID, "completed", False, "/work")
            with mock.patch("codex_quota_watchdog.cli.find_codex", return_value="codex"), \
                 mock.patch.object(desktop, "latest_turn", return_value=turn), \
                 mock.patch.object(desktop, "check_queue", side_effect=desktop.DesktopError("unavailable")):
                self.assertEqual(cmd_register_relay(args), 2)
                self.assertFalse(path.exists())
            with mock.patch("codex_quota_watchdog.cli.find_codex", return_value="codex"), \
                 mock.patch.object(desktop, "latest_turn", return_value=turn), \
                 mock.patch.object(desktop, "check_queue"):
                self.assertEqual(cmd_register_relay(args), 0)
            self.assertEqual(load_config(path).relay_state_path, str(state.resolve()))

    def test_watch_target_flags_are_exclusive(self):
        args = build_parser().parse_args(["watch", "--relay-state", "/work/state.json"])
        self.assertEqual(args.relay_state, "/work/state.json")
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["watch", "--relay-state", "/work/state.json", "--desktop-thread", TID])
