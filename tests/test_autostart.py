import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_quota_watchdog import autostart, procs
from codex_quota_watchdog.cli import build_parser, cmd_autostart
from codex_quota_watchdog.state import AlreadyRunning


class AutostartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)

    def test_linux_install_restarts_service_after_enabling(self):
        service = self.home / "watchdog.service"
        with mock.patch.object(autostart.sys, "platform", "linux"), \
             mock.patch.object(autostart, "target_path", return_value=service), \
             mock.patch.object(autostart.subprocess, "run") as run:
            autostart.install(["--config", "/work/config.toml"])
        self.assertIn("KillMode=process\n", service.read_text())
        self.assertEqual(run.call_args_list, [
            mock.call(["systemctl", "--user", "daemon-reload"], check=True),
            mock.call(["systemctl", "--user", "enable", service.name], check=True),
            mock.call(["systemctl", "--user", "restart", service.name], check=True),
        ])

    def test_windows_install_replaces_only_the_recorded_watchdog(self):
        (self.home / "watchdog.pid").write_text("321")
        process = procs.Proc(321, 1, "pythonw.exe",
                             '"C:\\Python\\pythonw.exe" "-m" "codex_quota_watchdog" '
                             '"--config" "C:\\My Work\\config.toml" "watch" "--quiet"', "started")
        order = mock.Mock()
        with mock.patch.object(autostart.sys, "platform", "win32"), \
             mock.patch.object(autostart, "target_path", return_value=self.home / "startup.cmd"), \
             mock.patch.object(autostart, "data_dir", return_value=self.home), \
             mock.patch.object(autostart, "InstanceLock") as lock, \
             mock.patch.object(procs, "list_processes", return_value=[process]), \
             mock.patch.object(procs, "_alive", return_value=[process]), \
             mock.patch.object(procs, "_force") as stop, \
             mock.patch.object(autostart.subprocess, "Popen") as start:
            lock.return_value.__enter__.side_effect = [AlreadyRunning("busy"), AlreadyRunning("busy"), None]
            order.attach_mock(stop, "stop")
            order.attach_mock(start, "start")
            with mock.patch.object(autostart.time, "sleep"):
                autostart.install([])
            stop.assert_called_once_with(process)
            self.assertEqual([call[0] for call in order.mock_calls], ["stop", "start"])
            self.assertEqual(lock.return_value.__enter__.call_count, 3)

    def test_windows_install_refuses_an_unrelated_pid(self):
        (self.home / "watchdog.pid").write_text("321")
        process = procs.Proc(321, 1, "python.exe", "python.exe other.py", "started")
        with mock.patch.object(autostart.sys, "platform", "win32"), \
             mock.patch.object(autostart, "target_path", return_value=self.home / "startup.cmd"), \
             mock.patch.object(autostart, "data_dir", return_value=self.home), \
             mock.patch.object(autostart, "InstanceLock") as lock, \
             mock.patch.object(procs, "list_processes", return_value=[process]), \
             mock.patch.object(procs, "_force") as stop, \
             mock.patch.object(autostart.subprocess, "Popen") as start:
            lock.return_value.__enter__.side_effect = AlreadyRunning("busy")
            with self.assertRaisesRegex(RuntimeError, "watchdog"):
                autostart.install([])
            stop.assert_not_called()
            start.assert_not_called()

    def test_windows_fresh_install_does_not_stop_any_process(self):
        with mock.patch.object(autostart.sys, "platform", "win32"), \
             mock.patch.object(autostart, "target_path", return_value=self.home / "startup.cmd"), \
             mock.patch.object(autostart, "data_dir", return_value=self.home), \
             mock.patch.object(autostart, "InstanceLock"), \
             mock.patch.object(procs, "list_processes") as processes, \
             mock.patch.object(procs, "_force") as stop, \
             mock.patch.object(autostart.subprocess, "Popen") as start:
            autostart.install([])
            processes.assert_not_called()
            stop.assert_not_called()
            start.assert_called_once()

    def test_windows_install_timeout_never_starts_replacement(self):
        (self.home / "watchdog.pid").write_text("321")
        process = procs.Proc(321, 1, "pythonw.exe",
                             'pythonw.exe -m codex_quota_watchdog watch --quiet', "started")
        with mock.patch.object(autostart.sys, "platform", "win32"), \
             mock.patch.object(autostart, "target_path", return_value=self.home / "startup.cmd"), \
             mock.patch.object(autostart, "data_dir", return_value=self.home), \
             mock.patch.object(autostart, "InstanceLock") as lock, \
             mock.patch.object(procs, "list_processes", return_value=[process]), \
             mock.patch.object(procs, "_alive", return_value=[process]), \
             mock.patch.object(procs, "_force"), \
             mock.patch.object(autostart.time, "monotonic", side_effect=[0, 11]), \
             mock.patch.object(autostart.subprocess, "Popen") as start:
            lock.return_value.__enter__.side_effect = AlreadyRunning("busy")
            with self.assertRaisesRegex(RuntimeError, "did not stop"):
                autostart.install([])
            start.assert_not_called()

    def test_cli_reports_install_failure(self):
        args = build_parser().parse_args(["autostart", "install"])
        with mock.patch.object(autostart, "install", side_effect=RuntimeError("watchdog did not stop")), \
             mock.patch("sys.stderr"):
            self.assertEqual(cmd_autostart(args), 2)
