import json
import os
import sys
import unittest
from pathlib import Path

from codex_quota_watchdog import threads
from codex_quota_watchdog.appserver import AppServerClient, ProbeError
from codex_quota_watchdog.config import Config

FAKE = str(Path(__file__).with_name("fake_app_server.py"))
NOW = 1_800_000_000
A = "0199a213-81c0-7800-8aa1-bbab2a035a53"
B = "0199a213-81c0-7800-8aa1-bbab2a035a54"
C = "0199a213-81c0-7800-8aa1-bbab2a035a55"
D = "0199a213-81c0-7800-8aa1-bbab2a035a56"


def thread(tid, updated, **kw):
    return {"id": tid, "updatedAt": updated, "cwd": "/work/" + tid[-2:], "preview": "task " + tid[-2:],
            "ephemeral": False, "source": "vscode", **kw}


def turn(tid, status="failed", kind="usageLimitExceeded", started=NOW - 600, completed=NOW - 60):
    error = {"message": "limit", "codexErrorInfo": kind} if kind else None
    return {"id": "turn-" + tid[-2:], "status": status, "error": error, "items": [],
            "startedAt": started, "completedAt": completed}


class FakeSession:
    def __init__(self, listing, turns, fail=()):
        self.listing, self.turns, self.fail = listing, turns, fail
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/list":
            return {"data": self.listing, "nextCursor": None}
        if params["threadId"] in self.fail:
            raise ProbeError("unreadable")
        return {"data": self.turns.get(params["threadId"], [])}


class FakeClient:
    def __init__(self, session):
        self._session = session

    def session(self):
        return self._session


class FindLimitedTests(unittest.TestCase):
    def test_only_latest_turn_on_usage_limit(self):
        listing = [thread(A, NOW - 50), thread(B, NOW - 60), thread(C, NOW - 70), thread(D, NOW - 80)]
        turns = {
            A: [turn(A)],
            B: [turn(B, status="completed", kind=None)],            # finished normally
            C: [turn(C, status="interrupted", kind=None)],          # killed, no limit signal
            D: [turn(D, kind={"httpConnectionFailed": {"httpStatusCode": 500}})],
        }
        session = FakeSession(listing, turns)
        found = threads.find_limited(FakeClient(session), Config(), NOW)
        self.assertEqual([t.thread_id for t in found], [A])
        self.assertEqual(found[0].turn_id, "turn-53")
        self.assertEqual(found[0].cwd, "/work/53")
        params = session.calls[0][1]
        self.assertEqual(params["sortKey"], "updated_at")
        self.assertNotIn("subAgent", params["sourceKinds"])

    def test_lookback_stops_the_scan(self):
        cfg = Config(thread_lookback_hours=1)
        listing = [thread(A, NOW - 50), thread(B, NOW - 7200)]
        session = FakeSession(listing, {A: [turn(A)], B: [turn(B)]})
        found = threads.find_limited(FakeClient(session), cfg, NOW)
        self.assertEqual([t.thread_id for t in found], [A])
        self.assertEqual(len([c for c in session.calls if c[0] == "thread/turns/list"]), 1)

    def test_subagents_and_ephemeral_skipped_by_default(self):
        listing = [thread(A, NOW - 50, source={"subAgent": {"thread_spawn": {}}}),
                   thread(B, NOW - 60, ephemeral=True)]
        session = FakeSession(listing, {A: [turn(A)], B: [turn(B)]})
        self.assertEqual(threads.find_limited(FakeClient(session), Config(), NOW), [])
        found = threads.find_limited(FakeClient(FakeSession(listing, {A: [turn(A)]})),
                                     Config(resume_subagents=True), NOW)
        self.assertEqual([(t.thread_id, t.subagent) for t in found], [(A, True)])

    def test_unreadable_thread_is_skipped(self):
        listing = [thread(A, NOW - 50), thread(B, NOW - 60)]
        session = FakeSession(listing, {A: [turn(A)], B: [turn(B)]}, fail=(A,))
        found = threads.find_limited(FakeClient(session), Config(), NOW)
        self.assertEqual([t.thread_id for t in found], [B])

    def test_invalid_ids_ignored(self):
        session = FakeSession([thread("not-a-uuid", NOW)], {})
        self.assertEqual(threads.find_limited(FakeClient(session), Config(), NOW), [])

    def test_started_since(self):
        client = FakeClient(FakeSession([], {A: [turn(A, status="inProgress", kind=None, started=NOW + 30)]}))
        self.assertTrue(threads.started_since(client, A, NOW))
        client = FakeClient(FakeSession([], {A: [turn(A)]}))
        self.assertFalse(threads.started_since(client, A, NOW))
        client = FakeClient(FakeSession([], {}, fail=(A,)))
        self.assertIsNone(threads.started_since(client, A, NOW))


class OverAppServerTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FAKE_THREADS", None)

    def test_session_makes_several_calls(self):
        os.environ["FAKE_THREADS"] = json.dumps({"list": [thread(A, NOW - 10)], "turns": {A: [turn(A)]}})
        client = AppServerClient(sys.executable, [FAKE], timeout=10)
        found = threads.find_limited(client, Config(), NOW)
        self.assertEqual([t.thread_id for t in found], [A])


if __name__ == "__main__":
    unittest.main()
