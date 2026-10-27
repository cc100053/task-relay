import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_quota_watchdog import procs
from codex_quota_watchdog.config import Config

PS = """\
    1     0 Mon Oct 26 10:00:00 2026 /sbin/init
  100     1 Mon Oct 26 10:00:01 2026 bash
  200   100 Mon Oct 26 10:00:02 2026 node /usr/lib/node_modules/@openai/codex/bin/codex.js
  201   200 Mon Oct 26 10:00:02 2026 /usr/lib/node_modules/@openai/codex/vendor/codex-x86_64-unknown-linux-musl/codex/codex
  202   201 Mon Oct 26 10:00:03 2026 python build.py
  300   100 Mon Oct 26 10:00:04 2026 codex exec resume 0199a213-81c0-7800-8aa1-bbab2a035a53 continue
  400     1 Mon Oct 26 10:00:05 2026 /Applications/Codex.app/Contents/MacOS/Codex
  401   400 Mon Oct 26 10:00:05 2026 /Applications/Codex.app/Contents/Resources/codex app-server
  410     1 Mon Oct 26 10:00:05 2026 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT
  411   410 Mon Oct 26 10:00:05 2026 /Applications/ChatGPT.app/Contents/Frameworks/Codex Framework.framework/Helpers/Codex (Renderer).app/Contents/MacOS/Codex (Renderer)
  412   410 Mon Oct 26 10:00:05 2026 /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex exec-server
  413   410 Mon Oct 26 10:00:05 2026 /Users/me/.codex/computer-use/Codex Computer Use.app/Contents/MacOS/SkyComputerUseService
  414     1 Mon Oct 26 10:00:05 2026 /Applications/ChatGPT.app/Contents/Frameworks/Codex Framework.framework/Helpers/browser_crashpad_handler
  500   100 Mon Oct 26 10:00:06 2026 python -m codex_quota_watchdog watch
  501   500 Mon Oct 26 10:00:07 2026 codex app-server
  600     1 Mon Oct 26 10:00:08 2026 vim codex-notes.txt
"""


class ProcTests(unittest.TestCase):
    def setUp(self):
        self.procs = procs.parse_ps(PS)

    def test_parse(self):
        p = next(p for p in self.procs if p.pid == 300)
        self.assertEqual(p.name, "codex")
        self.assertEqual(p.started, "Mon Oct 26 10:00:04 2026")

    def test_targets_cli_roots_only(self):
        targets = procs.find_targets(Config(), procs=self.procs, own_pid=500)
        self.assertEqual(sorted(t.pid for t in targets), [200, 300])

    def test_desktop_opt_in(self):
        cfg = Config(stop_desktop=True)
        targets = procs.find_targets(cfg, procs=self.procs, own_pid=500)
        self.assertEqual(sorted(t.pid for t in targets), [200, 300, 400, 410, 414])

    def test_ancestor_protected(self):
        # Watchdog launched from a shell inside the Codex CLI: never kill our own host.
        targets = procs.find_targets(Config(), procs=self.procs, own_pid=202)
        self.assertEqual(sorted(t.pid for t in targets), [300, 501])

    def test_exclude(self):
        targets = procs.find_targets(Config(), procs=self.procs, own_pid=500, exclude={300})
        self.assertEqual([t.pid for t in targets], [200])

    def test_thread_ids(self):
        ids = procs.thread_ids_from(self.procs)
        self.assertEqual(ids, ["0199a213-81c0-7800-8aa1-bbab2a035a53"])

    def test_windows_desktop_path(self):
        p = procs.Proc(10, 1, "Codex.exe", r"C:\Users\me\AppData\Local\Programs\OpenAI\Codex\Codex.exe", "1")
        self.assertEqual(procs.classify(p, Config()), "desktop")
        p = procs.Proc(11, 1, "codex.exe", r"C:\Users\me\AppData\Roaming\npm\node_modules\@openai\codex\vendor\codex.exe", "1")
        self.assertEqual(procs.classify(p, Config()), "cli")

    def test_desktop_helpers_classified_before_cli(self):
        for p in self.procs:
            if 410 <= p.pid <= 414:
                with self.subTest(pid=p.pid):
                    self.assertEqual(procs.classify(p, Config()), "desktop")

    def test_desktop_ancestor_protected(self):
        targets = procs.find_targets(Config(stop_desktop=True), procs=self.procs, own_pid=411)
        self.assertNotIn(410, [p.pid for p in targets])

    def test_posix_executable_names_with_spaces(self):
        listing = (
            "700 1 Mon Oct 26 10:00:05 2026 /tmp/Codex Tools/python worker.py\n"
            "701 1 Mon Oct 26 10:00:05 2026 /tmp/CLI Tools/codex exec task\n"
            "702 1 Mon Oct 26 10:00:05 2026 codex exec finished\n"
        )
        names = "700 /tmp/Codex Tools/python\n701 /tmp/CLI Tools/codex\n"
        replies = [SimpleNamespace(returncode=0, stdout=listing),
                   SimpleNamespace(returncode=0, stdout=names)]
        with patch.object(procs.subprocess, "run", side_effect=replies):
            processes = procs._list_posix()
        self.assertEqual([p.name for p in processes], ["python", "codex"])
        targets = procs.find_targets(Config(), procs=processes, own_pid=500)
        self.assertEqual([p.pid for p in targets], [701])


if __name__ == "__main__":
    unittest.main()
