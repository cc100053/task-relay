import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_quota_watchdog import procs as real_procs
from codex_quota_watchdog import restart, threads
from codex_quota_watchdog.appserver import ProbeError
from codex_quota_watchdog.config import Config
from codex_quota_watchdog.state import MONITORING, NEEDS_USER, WAITING_RESET, StateStore
from codex_quota_watchdog.watchdog import Watchdog

from .helpers import rate_limits

TID = "0199a213-81c0-7800-8aa1-bbab2a035a53"
TID2 = "0199a213-81c0-7800-8aa1-bbab2a035a54"
TID3 = "0199a213-81c0-7800-8aa1-bbab2a035a55"
T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class FakeClient:
    """Returns whatever the test puts in .reply (dict, or an exception to raise)."""

    def __init__(self):
        self.reply = None
        self.calls = 0

    def read_rate_limits(self):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeProcs:
    def __init__(self):
        self.running = [real_procs.Proc(300, 1, "codex", f"codex exec resume {TID} go", "t1")]
        self.stopped = []
        self.stop_ok = True

    def list_processes(self):
        return list(self.running)

    def find_targets(self, cfg, procs=None, exclude=None):
        return [p for p in self.running if p.pid not in (exclude or set())]

    def stop_processes(self, targets, cfg, dry_run):
        self.stopped.append([t.pid for t in targets])
        if self.stop_ok and not dry_run:
            self.running = [p for p in self.running if p not in targets]
        return self.stop_ok

    thread_ids_from = staticmethod(real_procs.thread_ids_from)


class FakeJob:
    def __init__(self, code=None, thread_id=None):
        self.code = code
        self.pid = 999
        self.thread_id = thread_id
        self.stopped_by_watchdog = False
        self.log_path = Path("job.log")

    def poll(self):
        return self.code


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name, "state.json"))
        self.clock = Clock()
        self.client = FakeClient()
        self.procs = FakeProcs()
        self.cfg = Config(armed=True)
        self.launched = []
        patcher = mock.patch.object(restart, "restart", side_effect=self._fake_restart)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _fake_restart(self, args, cwd, jobs_dir, thread_id):
        self.launched.append(args)
        return FakeJob(thread_id=thread_id)

    def dog(self, dry_run=False):
        return Watchdog(self.cfg, self.store, self.client, "codex", Path(self.tmp.name), dry_run,
                        clock=self.clock, proc_api=self.procs)

    def set_quota(self, **kw):
        self.client.reply = rate_limits(self.clock.now, **kw)

    def test_healthy_quota_does_nothing(self):
        d = self.dog()
        self.set_quota(five_used=50)
        self.assertEqual(d.tick(), self.cfg.poll_seconds)
        self.assertEqual(self.procs.stopped, [])

    def test_polls_faster_near_limit(self):
        d = self.dog()
        self.set_quota(five_used=90)
        self.assertEqual(d.tick(), self.cfg.near_limit_poll_seconds)

    def test_full_cycle_stop_wait_confirm_restart(self):
        d = self.dog()
        self.set_quota(five_used=98, five_reset=3600)
        d.tick()
        self.assertEqual(self.procs.stopped, [[300]])
        self.assertEqual(d.state.phase, WAITING_RESET)
        self.assertEqual(d.state.thread_id, TID)
        reset_at = T0 + 3600
        self.assertEqual(d.state.resume_after, int(reset_at) + self.cfg.grace_seconds)

        # Before the reset: no probing at all.
        calls = self.client.calls
        self.clock.now = reset_at - 100
        d.tick()
        self.assertEqual(self.client.calls, calls)

        # After reset + grace: needs confirm_reads healthy reads.
        self.clock.now = reset_at + self.cfg.grace_seconds + 1
        self.set_quota(five_used=0, five_reset=5 * 3600)
        d.tick()
        self.assertEqual(self.launched, [])
        self.clock.now += 20
        d.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertIn(TID, self.launched[0])
        self.assertEqual(d.state.phase, MONITORING)
        self.assertEqual(self.store.load().restart_history, [self.clock.now])

    def test_unknown_quota_never_stops_or_restarts(self):
        d = self.dog()
        self.client.reply = ProbeError("down")
        d.tick()
        self.assertEqual(self.procs.stopped, [])
        # And in waiting phase, unknown never restarts.
        d.state.phase = WAITING_RESET
        d.state.thread_id = TID
        d.state.resume_after = self.clock.now - 1
        for _ in range(5):
            delay = d.tick()
        self.assertEqual(self.launched, [])
        # Backoff grows to the cap; each sleep is still chunked.
        self.assertEqual(d._unknown_delay(), self.cfg.unknown_backoff_max_seconds)
        self.assertEqual(delay, self.cfg.max_sleep_chunk_seconds)

    def test_null_ordinary_usage_blocks_restart(self):
        d = self.dog()
        d.state.phase = WAITING_RESET
        d.state.thread_id = TID
        d.state.resume_after = self.clock.now - 1
        self.set_quota(five_used=0, allowed=None)
        for _ in range(4):
            d.tick()
            self.clock.now += 30
        self.assertEqual(self.launched, [])
        self.assertEqual(d.state.phase, WAITING_RESET)

    def test_reset_not_yet_effective_pushes_wait(self):
        d = self.dog()
        d.state.phase = WAITING_RESET
        d.state.thread_id = TID
        d.state.resume_after = self.clock.now - 1
        self.set_quota(five_used=100, five_reset=600)
        delay = d.tick()
        self.assertEqual(d.state.resume_after, int(self.clock.now + 600) + self.cfg.grace_seconds)
        self.assertGreater(delay, 0)
        self.assertEqual(self.launched, [])

    def test_no_thread_id_needs_user(self):
        self.procs.running = [real_procs.Proc(300, 1, "codex", "codex", "t1")]
        d = self.dog()
        self.set_quota(five_used=99)
        d.tick()
        self.clock.now = d.state.resume_after + 1
        self.set_quota(five_used=0)
        d.tick(); d.tick()
        self.assertEqual(self.launched, [])
        self.assertEqual(d.state.phase, NEEDS_USER)
        self.assertIn("refusing to guess", d.state.note)

    def test_account_change_needs_user(self):
        d = self.dog()
        self.set_quota(five_used=99, account="a")
        d.tick()
        self.clock.now = d.state.resume_after + 1
        self.set_quota(five_used=0, account="b")
        d.tick()
        self.assertEqual(d.state.phase, NEEDS_USER)

    def test_restart_cap(self):
        self.cfg.max_restarts_per_day = 1
        d = self.dog()
        d.state.restart_history = [self.clock.now - 100]
        d.state.phase = WAITING_RESET
        d.state.thread_id = TID
        d.state.resume_after = self.clock.now - 1
        self.set_quota(five_used=0)
        d.tick(); d.tick()
        self.assertEqual(self.launched, [])
        self.assertEqual(d.state.phase, NEEDS_USER)

    def test_restart_mode_none(self):
        self.cfg.restart_mode = "none"
        d = self.dog()
        d.state.phase = WAITING_RESET
        d.state.resume_after = self.clock.now - 1
        self.set_quota(five_used=0)
        d.tick(); d.tick()
        self.assertEqual(self.launched, [])
        self.assertEqual(d.state.phase, MONITORING)

    def test_dry_run_never_launches(self):
        d = self.dog(dry_run=True)
        self.set_quota(five_used=99)
        d.tick()
        self.assertEqual(self.procs.running[0].pid, 300)  # still running
        self.clock.now = d.state.resume_after + 1
        self.set_quota(five_used=0)
        d.tick(); d.tick()
        self.assertEqual(self.launched, [])

    def test_stop_failure_needs_user(self):
        self.procs.stop_ok = False
        d = self.dog()
        self.set_quota(five_used=99)
        d.tick()
        self.assertEqual(d.state.phase, NEEDS_USER)

    def test_managed_job_success_keeps_monitoring(self):
        d = self.dog()
        d.job = FakeJob(code=0, thread_id=TID)
        self.set_quota(five_used=10)
        d.tick()
        self.assertEqual(d.state.phase, MONITORING)
        self.assertIsNone(d.job)
        self.assertIsNone(d.state.thread_id)

    def test_parked_still_stops_but_never_restarts(self):
        d = self.dog()
        d.state.phase = NEEDS_USER
        d.state.thread_id = TID
        self.set_quota(five_used=99)
        d.tick()
        self.assertEqual(self.procs.stopped, [[300]])
        self.set_quota(five_used=0)
        d.tick()
        self.assertEqual(self.launched, [])
        self.assertEqual(d.state.phase, NEEDS_USER)

    def test_managed_job_failure_with_quota_left_needs_user(self):
        d = self.dog()
        d.job = FakeJob(code=1, thread_id=TID)
        self.set_quota(five_used=10)
        d.tick()
        self.assertEqual(d.state.phase, NEEDS_USER)

    def test_managed_job_hit_limit_waits_then_resumes_same_thread(self):
        d = self.dog()
        d.job = FakeJob(code=1, thread_id=TID)
        self.set_quota(five_used=100)
        d.tick()
        self.assertEqual(d.state.phase, WAITING_RESET)
        self.assertEqual(d.state.thread_id, TID)

    def test_unreadable_state_needs_user(self):
        self.store.path.write_text("{not json", encoding="utf-8")
        d = self.dog()
        self.assertEqual(d.state.phase, NEEDS_USER)
        self.set_quota(five_used=0)
        d.tick()
        self.assertEqual(self.launched, [])


class DiscoveryTests(unittest.TestCase):
    """Desktop app / several sessions: resume every thread that stopped on the usage limit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name, "state.json"))
        self.clock = Clock()
        self.client = FakeClient()
        self.procs = FakeProcs()
        self.procs.running = []  # the desktop app is not stopped
        self.cfg = Config(armed=True, discover_threads=True, restart_mode="queue", stop_cli=False,
                          resume_stagger_seconds=0)
        self.limited = []
        self.queued = []
        self.queue_ok = True
        self.started = {}
        for name, fake in (("find_limited", lambda client, cfg, now: list(self.limited)),
                           ("started_since", lambda client, tid, since: self.started.get(tid))):
            patcher = mock.patch.object(threads, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(restart, "queue", side_effect=self._fake_queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.launched = []
        patcher = mock.patch.object(restart, "restart", side_effect=lambda args, cwd, jobs, tid:
                                    self.launched.append((args, cwd)) or FakeJob(thread_id=tid))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_queue(self, args):
        self.queued.append(args[args.index("--thread") + 1])
        return self.queue_ok, "" if self.queue_ok else "app not running"

    def dog(self, dry_run=False):
        return Watchdog(self.cfg, self.store, self.client, "codex", Path(self.tmp.name), dry_run,
                        clock=self.clock, proc_api=self.procs, sleep=lambda s: None)

    def set_quota(self, **kw):
        self.client.reply = rate_limits(self.clock.now, **kw)

    def limit_hit(self, *tids):
        self.limited = [threads.LimitedThread(t, "turn-" + t[-2:], "/work/" + t[-2:], "task", T0, False)
                        for t in tids]

    def through_reset(self, d):
        self.set_quota(five_used=99)
        d.tick()
        self.assertEqual(d.state.phase, WAITING_RESET)
        self.clock.now = d.state.resume_after + 1
        self.set_quota(five_used=0, five_reset=5 * 3600)
        d.tick(); self.clock.now += 20; d.tick()

    def test_waits_without_stopping_then_queues_every_limited_thread(self):
        d = self.dog()
        self.limit_hit(TID, TID2, TID3)
        self.through_reset(d)
        self.assertEqual(self.procs.stopped, [])
        self.assertEqual(self.queued, [TID, TID2, TID3])
        self.assertEqual(d.state.phase, MONITORING)
        self.assertEqual(sorted(e["thread_id"] for e in d.state.pending_queue), sorted([TID, TID2, TID3]))
        self.assertEqual(sorted(d.state.handled_turns), ["turn-53", "turn-54", "turn-55"])

    def test_same_limited_turn_is_never_resumed_twice(self):
        d = self.dog()
        self.limit_hit(TID)
        self.through_reset(d)
        self.through_reset(d)
        self.assertEqual(self.queued, [TID])

    def test_no_limited_thread_resumes_nothing(self):
        d = self.dog()
        self.through_reset(d)
        self.assertEqual(self.queued, [])
        self.assertEqual(d.state.phase, MONITORING)

    def test_queue_failure_needs_user(self):
        self.queue_ok = False
        d = self.dog()
        self.limit_hit(TID, TID2)
        self.through_reset(d)
        self.assertEqual(d.state.phase, NEEDS_USER)
        self.assertIn("app not running", d.state.note)

    def test_thread_cap(self):
        self.cfg.max_threads_per_resume = 2
        d = self.dog()
        self.limit_hit(TID, TID2, TID3)
        self.through_reset(d)
        self.assertEqual(self.queued, [TID, TID2])
        self.assertEqual(d.state.phase, NEEDS_USER)
        self.assertIn(TID3, d.state.note)

    def test_per_thread_restart_cap(self):
        self.cfg.max_restarts_per_day = 1
        d = self.dog()
        d.state.thread_restarts = {TID: [self.clock.now - 100]}
        self.limit_hit(TID, TID2)
        self.through_reset(d)
        self.assertEqual(self.queued, [TID2])
        self.assertEqual(d.state.phase, NEEDS_USER)

    def test_queued_message_not_started_needs_user(self):
        d = self.dog()
        self.limit_hit(TID, TID2)
        self.through_reset(d)
        self.started = {TID: True, TID2: False}
        self.clock.now += self.cfg.queue_start_timeout_seconds + 1
        d.tick()
        self.assertEqual(d.state.phase, NEEDS_USER)
        self.assertIn(TID2, d.state.note)
        self.assertNotIn(TID, d.state.note)
        self.assertEqual(d.state.pending_queue, [])

    def test_queued_messages_started_stay_monitoring(self):
        d = self.dog()
        self.limit_hit(TID)
        self.through_reset(d)
        self.started = {TID: True}
        self.clock.now += self.cfg.queue_start_timeout_seconds + 1
        d.tick()
        self.assertEqual(d.state.phase, MONITORING)
        self.assertEqual(d.state.pending_queue, [])

    def test_stopped_cli_sessions_are_all_resumed_with_exec(self):
        self.cfg.restart_mode = "exec-resume"
        self.cfg.stop_cli = True
        self.procs.running = [real_procs.Proc(300, 1, "codex", f"codex resume {TID}", "t1"),
                              real_procs.Proc(301, 1, "codex", f"codex exec resume {TID2} go", "t2")]
        d = self.dog()
        self.limit_hit(TID3)
        self.through_reset(d)
        self.assertEqual(self.procs.stopped, [[300, 301]])
        resumed = [args[args.index("resume") + 1] for args, _ in self.launched]
        self.assertEqual(sorted(resumed), sorted([TID, TID2, TID3]))
        self.assertIn(("/work/55"), [cwd for _, cwd in self.launched])
        self.assertEqual(d.state.phase, MONITORING)

    def test_dry_run_queues_nothing(self):
        d = self.dog(dry_run=True)
        self.limit_hit(TID)
        self.through_reset(d)
        self.assertEqual(self.queued, [])
        self.assertEqual(d.state.handled_turns, [])

    def test_thread_list_failure_keeps_waiting(self):
        d = self.dog()
        threads.find_limited.side_effect = ProbeError("down")
        self.through_reset(d)
        self.assertEqual(d.state.phase, WAITING_RESET)
        self.assertEqual(self.queued, [])


if __name__ == "__main__":
    unittest.main()
