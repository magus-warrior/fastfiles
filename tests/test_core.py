import unittest
import tempfile
import json
from pathlib import Path

from fastfiles.core import (
    Direction,
    TransferRequest,
    build_rsync_command,
    parse_progress,
    parse_ssh_aliases,
    remote_spec,
)
from fastfiles.locker import Locker, parse_peer_address
from fastfiles.profiles import DirectProfile, PeerProfile, ProfileStore


class CommandTests(unittest.TestCase):
    def test_send_command_is_a_safe_argument_list(self):
        request = TransferRequest(
            direction=Direction.SEND,
            host="studio",
            remote_path="~/My Files/",
            local_paths=("/tmp/a file.txt", "/tmp/photos"),
        )
        command = build_rsync_command(request)
        self.assertEqual(command[0], "rsync")
        self.assertIn("/tmp/a file.txt", command)
        self.assertEqual(command[-1], "studio:./My Files/")

    def test_receive_order(self):
        request = TransferRequest(
            direction=Direction.RECEIVE,
            host="me@example.com",
            remote_path="~/report.pdf",
            local_paths=("/tmp/downloads",),
            compress=False,
        )
        command = build_rsync_command(request)
        self.assertEqual(command[-2:], ["me@example.com:./report.pdf", "/tmp/downloads"])
        self.assertNotIn("--compress", command)

    def test_missing_host_is_rejected(self):
        request = TransferRequest(Direction.SEND, "", "~/", ("/tmp/a",))
        with self.assertRaisesRegex(ValueError, "host"):
            build_rsync_command(request)

    def test_remote_spec_supports_user_and_ipv6(self):
        self.assertEqual(remote_spec("user@[::1]", "/tmp"), "user@[::1]:/tmp")

    def test_remote_home_path_is_safe_with_protected_args(self):
        self.assertEqual(remote_spec("server", "~/"), "server:./")
        self.assertEqual(remote_spec("server", "~/Downloads/file.txt"), "server:./Downloads/file.txt")


class ParserTests(unittest.TestCase):
    def test_progress2_line(self):
        progress = parse_progress("  1.25GB  42%   18.40MB/s    0:01:12 (xfr#4, to-chk=8/20)")
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.percent, 42)
        self.assertEqual(progress.speed, "18.40MB/s")
        self.assertEqual(progress.eta, "0:01:12")

    def test_progress2_accepts_iec_and_lowercase_units(self):
        progress = parse_progress("32.77KiB 100% 1.25MiB/s 0:00:00")
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.transferred, "32.77KiB")

    def test_non_progress_line(self):
        self.assertIsNone(parse_progress("sending incremental file list"))

    def test_ssh_aliases_ignore_patterns_and_comments(self):
        text = """
        # personal systems
        Host studio nas
          HostName 10.0.0.4
        Host *.company.test
        Host *
        Host\tlaptop
        """
        self.assertEqual(parse_ssh_aliases(text), ["laptop", "nas", "studio"])


class LockerTests(unittest.TestCase):
    def test_lists_files_and_folders_with_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "folder").mkdir()
            (root / "hello.txt").write_text("hello", encoding="utf-8")
            items = Locker(root).list()
            self.assertEqual([item["name"] for item in items], ["folder", "hello.txt"])
            self.assertEqual(items[1]["size"], 5)

    def test_cannot_escape_approved_locker(self):
        with tempfile.TemporaryDirectory() as directory:
            locker = Locker(Path(directory) / "locker")
            with self.assertRaises(PermissionError):
                locker.resolve("../private.txt", must_exist=False)

    def test_manual_vpn_peer_addresses(self):
        self.assertEqual(parse_peer_address("desktop.tailnet").port, 47832)
        self.assertEqual(parse_peer_address("10.20.30.40:5000").port, 5000)
        ipv6 = parse_peer_address("[fd00::1234]:4999")
        self.assertEqual(ipv6.address, "fd00::1234")
        self.assertEqual(ipv6.port, 4999)

    def test_allow_and_deny_policy_filters_and_blocks_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public").mkdir()
            (root / "public" / "ok.txt").write_text("ok")
            (root / "public" / "secret.txt").write_text("no")
            (root / "private.txt").write_text("no")
            locker = Locker(root, ["public", "public/**"], ["**/secret.txt"])
            self.assertEqual([item["name"] for item in locker.list("")], ["public"])
            self.assertEqual([item["name"] for item in locker.list("public")], ["ok.txt"])
            with self.assertRaises(PermissionError):
                locker.resolve("private.txt")
            with self.assertRaises(PermissionError):
                locker.resolve("public/secret.txt")


class ProfileTests(unittest.TestCase):
    def test_profiles_round_trip_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connections.json"
            store = ProfileStore(path)
            store.save_direct(DirectProfile("studio", "alex@studio", "~/drop"))
            store.save_peer(PeerProfile("nas", "10.0.0.8", 47832))
            self.assertEqual(store.direct()[0].name, "studio")
            self.assertEqual(store.peers()[0].address, "10.0.0.8")
            self.assertNotIn("password", json.loads(path.read_text()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
