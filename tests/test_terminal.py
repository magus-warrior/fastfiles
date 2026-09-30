import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastfiles.app import main
from fastfiles.background import BackgroundService, unit_quote
from fastfiles.desktop import desktop_error
from fastfiles.locker import LockerConfig, LockerService, Peer, PeerClient, hash_access_code
from fastfiles.terminal import TerminalMenu, browse


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = LockerConfig("test", "Test", str(self.root / "locker"), hash_access_code("123456"),
                                   "123456", bind_address="127.0.0.1", port=0)
        self.path = self.root / "config.json"
        self.config.save(self.path)
        self.menu = TerminalMenu(self.config, self.path, advertise=False)
        self.addCleanup(self.menu.stop)
        self.output = patch("sys.stdout", new_callable=io.StringIO).start()
        self.addCleanup(patch.stopall)
        patch.object(self.menu.background, "active", return_value=False).start()

    def test_import_and_receive_tree_through_real_server(self):
        source = self.root / "Vacation photos"
        source.mkdir()
        (source / "empty").mkdir()
        (source / "pic.txt").write_text("hello")
        with patch("builtins.input", side_effect=[str(source), "", "", "n"]):
            self.menu.add()
        self.menu.start()
        peer = Peer("test", "Test", "127.0.0.1", self.menu.service.port)
        self.menu.client = PeerClient(peer, "123456")
        target = self.root / "download"
        with patch("fastfiles.terminal.browse", return_value=[{"path": source.name, "is_dir": True}]), \
             patch("builtins.input", side_effect=[str(target), "n"]):
            self.menu.receive()
        self.assertEqual((target / source.name / "pic.txt").read_text(), "hello")
        self.assertTrue((target / source.name / "empty").is_dir())
        with patch("builtins.input", side_effect=[str(source), "", "", "n"]):
            with self.assertRaises(FileExistsError):
                self.menu.add()

    def test_send_tree_to_real_peer(self):
        remote = LockerService(self.config)
        remote.start(advertise=False)
        self.addCleanup(remote.stop)
        self.menu.client = PeerClient(Peer("test", "Test", "127.0.0.1", remote.port), "123456")
        source = self.root / "a file.txt"
        source.write_text("sent")
        with patch("builtins.input", side_effect=[str(source), "", "Inbox", "n"]):
            self.menu.send()
        self.assertEqual((Path(self.config.locker_path) / "Inbox" / source.name).read_text(), "sent")

    def test_terminal_saves_computer_and_code_then_reconnects_without_prompt_or_print(self):
        from fastfiles.credentials import CredentialVault

        remote = LockerService(self.config)
        remote.start(advertise=False)
        self.addCleanup(remote.stop)
        self.menu.secrets.vault = CredentialVault(self.root / "private")
        with patch.dict("sys.modules", {"keyring": None}), \
             patch("builtins.input", return_value=f"127.0.0.1:{remote.port}"), \
             patch("fastfiles.terminal.getpass.getpass", return_value="123456"):
            self.menu.connect()
        self.assertEqual(len(self.menu.profiles.peers()), 1)
        second = TerminalMenu(self.config, self.path, advertise=False)
        second.secrets.vault = CredentialVault(self.root / "private")
        with patch.dict("sys.modules", {"keyring": None}), \
             patch("builtins.input", return_value="1"), \
             patch("fastfiles.terminal.getpass.getpass") as prompt:
            second.connect()
        prompt.assert_not_called()
        self.assertEqual(second.client.code, "123456")
        self.assertNotIn("123456", self.output.getvalue())
        self.menu.details()
        self.assertIn("Pairing code: 123456", self.output.getvalue())

    def test_eof_stops_foreground_service(self):
        with patch("builtins.input", side_effect=["1", EOFError]):
            self.assertEqual(self.menu.run(), 0)
        self.assertIsNone(self.menu.service)
        self.assertIn("Sharing stopped", self.output.getvalue())

    def test_bad_settings_leave_config_and_server_unchanged(self):
        self.menu.start()
        original = self.path.read_bytes()
        with patch("builtins.input", side_effect=["", "", "", "99999", "on", "**", "-"]):
            with self.assertRaises(ValueError):
                self.menu.settings()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIsNotNone(self.menu.service)

    def test_grants_apply_to_running_server_and_revoke(self):
        self.menu.start()
        with patch("builtins.input", side_effect=["g", "Laptop", "**", ""]):
            self.menu.access()
        token = self.output.getvalue().split("Computer key (shown once): ")[1].splitlines()[0]
        peer = Peer("test", "Test", "127.0.0.1", self.menu.service.port)
        client = PeerClient(peer, token)
        client.list()
        self.assertNotIn(token, self.path.read_text())
        with patch("builtins.input", side_effect=["r", "Laptop"]):
            self.menu.access()
        with self.assertRaises(OSError):
            client.list()
        with self.assertRaises(OSError):
            PeerClient(peer, "123456").list()

    def test_browse_rejects_negative_selection_then_selects_all(self):
        entries = [{"name": "a", "path": "a", "size": 1, "is_dir": False}]
        with patch("builtins.input", side_effect=["s 0", "a"]):
            self.assertEqual(browse(lambda _: entries, select=True), entries)
        self.assertIn("Choose one", self.output.getvalue())

    def test_default_terminal_start_never_probes_qt(self):
        with patch("sys.stdin.isatty", return_value=True), \
             patch("fastfiles.terminal.run_menu", return_value=0) as menu, \
             patch("fastfiles.desktop.desktop_error") as probe:
            self.assertEqual(main(["--config", str(self.path)]), 0)
        menu.assert_called_once()
        probe.assert_not_called()

    def test_explicit_menu_works_without_display_and_exits_on_eof(self):
        result = subprocess.run([sys.executable, "-m", "fastfiles", "--menu", "--config", str(self.path)],
                                input="bad\n0\n", capture_output=True, text=True, timeout=10,
                                env={**os.environ, "DISPLAY": "", "WAYLAND_DISPLAY": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Always-on sharing", result.stdout)
        self.assertIn("Goodbye", result.stdout)
        self.assertNotIn("qt.qpa", result.stderr)

    def test_desktop_launcher_without_stdin_does_not_crash(self):
        with patch("sys.stdin", None), \
             patch("fastfiles.desktop.desktop_error", return_value="No display"), \
             patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["--config", str(self.path)]), 2)

    def test_gui_probe_failure_returns_error_without_importing_ui(self):
        with patch("fastfiles.desktop.desktop_error", return_value="Missing Qt library"), \
             patch("sys.stderr", new_callable=io.StringIO) as errors:
            self.assertEqual(main(["--gui", "--config", str(self.path)]), 2)
        self.assertIn("--menu", errors.getvalue())


class DesktopProbeTests(unittest.TestCase):
    def test_native_abort_is_contained(self):
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "xcb"}), \
             patch("fastfiles.desktop.subprocess.run", return_value=subprocess.CompletedProcess([], -6, "", "plugin missing")):
            self.assertIn("plugin missing", desktop_error())

    def test_timeout_is_actionable(self):
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "xcb"}), \
             patch("fastfiles.desktop.subprocess.run", side_effect=subprocess.TimeoutExpired("Qt", 20)):
            self.assertIn("Desktop check failed", desktop_error())


class BackgroundTests(unittest.TestCase):
    def test_service_uses_private_config_and_survives_terminal(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_CONFIG_HOME": root}):
            service = BackgroundService(Path(root) / "my config.json")
            with patch.object(service, "command") as command:
                service.enable(advertise=False)
                content = service.path.read_text()
                self.assertIn('--serve', content)
                self.assertIn('--no-discovery', content)
                self.assertIn('Restart=on-failure', content)
                self.assertIn(unit_quote(str(service.config_path)), content)
                self.assertIn(unit_quote(sys.executable), content)
                command.assert_any_call("enable", "--now", service.name)
                service.disable()
                command.assert_called_with("disable", "--now", service.name)
            if os.name != "nt":
                self.assertEqual(service.path.stat().st_mode & 0o777, 0o600)

    def test_unit_escapes_systemd_expansion_and_rejects_newline(self):
        self.assertEqual(unit_quote('/path/100%/$HOME'), '"/path/100%%/$$HOME"')
        with self.assertRaises(ValueError):
            unit_quote("/path\nExecStart=bad")
