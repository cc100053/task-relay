import json
import unittest

from codex_quota_watchdog import restart
from codex_quota_watchdog.config import Config, ConfigError, load_config

TID = "0199a213-81c0-7800-8aa1-bbab2a035a53"


class RestartArgTests(unittest.TestCase):
    def test_exec_resume_uses_exact_uuid(self):
        cfg = Config(exec_extra_args=["--sandbox", "workspace-write"])
        args = restart.build_restart_args("codex", cfg, TID, None)
        self.assertEqual(args[:5], ["codex", "exec", "--sandbox", "workspace-write", "resume"])
        self.assertEqual(args[5], TID)
        self.assertNotIn("--last", args)

    def test_refuses_without_uuid(self):
        for bad in (None, "", "latest", "--last", TID[:-1]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                restart.build_restart_args("codex", Config(), bad, None)

    def test_command_mode_placeholders(self):
        cfg = Config(restart_mode="command", restart_command=["my.cmd", "{thread_id}", "{cwd}"])
        self.assertEqual(restart.build_restart_args("codex", cfg, TID, "/w"), ["my.cmd", TID, "/w"])
        with self.assertRaises(ValueError):
            restart.build_restart_args("codex", cfg, None, "/w")

    def test_thread_id_from_events(self):
        self.assertEqual(restart.thread_id_from_event(json.dumps({"type": "thread.started", "thread_id": TID})), TID)
        legacy = {"id": "0", "msg": {"type": "session_configured", "session_id": TID.upper()}}
        self.assertEqual(restart.thread_id_from_event(json.dumps(legacy)), TID)
        self.assertIsNone(restart.thread_id_from_event("not json"))
        self.assertIsNone(restart.thread_id_from_event(json.dumps({"type": "thread.started", "thread_id": "x"})))


class ConfigTests(unittest.TestCase):
    def test_overrides_and_validation(self):
        cfg = load_config(path=_missing(), overrides={"armed": True, "poll_seconds": 60})
        self.assertTrue(cfg.armed)
        with self.assertRaises(ConfigError):
            load_config(path=_missing(), overrides={"restart_mode": "magic"})
        with self.assertRaises(ConfigError):
            load_config(path=_missing(), overrides={"nope": 1})
        with self.assertRaises(ConfigError):
            load_config(path=_missing(), overrides={"five_hour_min_remaining": 150})

    def test_sample_config_parses(self):
        import tempfile
        from pathlib import Path
        from codex_quota_watchdog.config import SAMPLE_CONFIG
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "c.toml")
            p.write_text(SAMPLE_CONFIG, encoding="utf-8")
            self.assertFalse(load_config(p).armed)


def _missing():
    from pathlib import Path
    return Path("/nonexistent/cqw-config.toml")


if __name__ == "__main__":
    unittest.main()


class WindowsShimTests(unittest.TestCase):
    def test_cmd_shim_resolves_to_node(self):
        import tempfile
        from pathlib import Path
        from codex_quota_watchdog.appserver import command_prefix
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d, "codex.cmd")
            shim.write_text("@echo off")
            self.assertEqual(command_prefix(str(shim)), [str(shim)])  # package missing
            script = Path(d, "node_modules", "@openai", "codex", "bin", "codex.js")
            script.parent.mkdir(parents=True)
            script.write_text("")
            Path(d, "node.exe").write_text("")
            self.assertEqual(command_prefix(str(shim)), [str(Path(d, "node.exe")), str(script)])

    def test_unsafe_args_through_cmd_refused(self):
        from codex_quota_watchdog.appserver import check_cmd_safe
        check_cmd_safe(["codex.exe", "exec", "a & b"])  # fine: no cmd.exe involved
        check_cmd_safe(["codex.cmd", "exec", "resume", TID, "continue please."])
        with self.assertRaises(ValueError):
            check_cmd_safe(["codex.cmd", "exec", "do this & del x"])
