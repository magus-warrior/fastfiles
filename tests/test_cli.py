import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastfiles.activity import ActivityStore
from fastfiles.locker import LockerConfig, Peer, PeerClient, hash_access_code


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {
            **os.environ,
            "APPDATA": str(self.root / "config"),
            "LOCALAPPDATA": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        }

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "fastfiles", *args],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_missing_config_validation_does_not_create_one(self):
        result = self.run_cli("--check-config")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "config").exists())

    def test_init_and_validate_config_with_custom_path(self):
        path = self.root / "custom.json"
        self.assertEqual(self.run_cli("--init-config", "--config", str(path)).returncode, 0)
        original = path.read_bytes()
        result = self.run_cli("--check-config", "--config", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), original)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_config_stops_serve_without_replacing_file(self):
        path = self.root / "invalid.json"
        path.write_text('{"deny_patterns":')
        result = self.run_cli("--serve", "--config", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid JSON", result.stderr)
        self.assertEqual(path.read_text(), '{"deny_patterns":')

    @unittest.skipIf(os.name == "nt", "POSIX signal smoke test")
    def test_headless_service_cli_starts_responds_and_stops(self):
        path = self.root / "server.json"
        config = LockerConfig(
            "cli-test",
            "CLI test",
            str(self.root / "locker"),
            hash_access_code("123456"),
            "123456",
            bind_address="127.0.0.1",
            port=0,
        )
        config.save(path)
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "fastfiles", "--serve", "--no-discovery", "--config", str(path)],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            output = b""
            deadline = time.monotonic() + 5
            while b"Log:" not in output and time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], 0.1)
                if ready:
                    output += os.read(process.stdout.fileno(), 4096)
                if process.poll() is not None:
                    break
            import re

            match = re.search(rb"Listening on port (\d+)", output)
            self.assertIsNotNone(match, "Headless service did not become ready")
            peer = Peer("cli-test", "CLI", "127.0.0.1", int(match[1]))
            self.assertEqual([entry["name"] for entry in PeerClient(peer, "123456").list()], ["Inbox"])
            self.assertIn(b"Receiving:", output)
            self.assertIn(b"Inbox:", output)
            process.terminate()
            _, errors = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, errors.decode())
            log = self.root / "state" / "fastfiles" / "fastfiles.log"
            self.assertNotIn("123456", log.read_text())
            if os.name != "nt":
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_grant_list_revoke_keeps_keys_private_and_disables_legacy_access(self):
        path = self.root / "server.json"
        result = self.run_cli("--config", str(path), "--grant-computer", "Laptop",
                              "--download", "Photos/**", "--upload", "Inbox/**")
        self.assertEqual(result.returncode, 0, result.stderr)
        token = result.stdout.split("shown once): ")[1].splitlines()[0]
        self.assertTrue(token.startswith("ff_"))
        self.assertNotIn(token, path.read_text())
        config = LockerConfig.load(path)
        self.assertTrue(config.uses_computer_keys)
        self.assertEqual(config.computer_permissions[0]["download_patterns"], ["Photos/**"])
        listing = self.run_cli("--config", str(path), "--list-computers")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("Laptop", listing.stdout)
        self.assertNotIn(token, listing.stdout)
        self.assertNotIn(config.computer_permissions[0]["token_hash"], listing.stdout)
        duplicate = self.run_cli("--config", str(path), "--grant-computer", "laptop", "--upload", "**")
        self.assertEqual(duplicate.returncode, 2)
        revoke = self.run_cli("--config", str(path), "--revoke-computer", "laptop")
        self.assertEqual(revoke.returncode, 0, revoke.stderr)
        config = LockerConfig.load(path)
        self.assertTrue(config.uses_computer_keys)
        self.assertEqual(config.computer_permissions, [])

    def test_invalid_grant_never_replaces_existing_config(self):
        path = self.root / "server.json"
        self.run_cli("--config", str(path), "--init-config")
        before = path.read_bytes()
        result = self.run_cli("--config", str(path), "--grant-computer", "Laptop", "--upload", "../outside/**")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads(path.read_text())["computer_permissions"], [])

    def test_activity_cli_reads_receipts_without_starting_server(self):
        path = self.root / "server.json"
        config = LockerConfig("activity-test", "Test", str(self.root / "locker"), hash_access_code("123456"))
        config.save(path)
        empty = self.run_cli("--config", str(path), "--activity")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertIn("No transfer activity", empty.stdout)
        ActivityStore(config.locker_path).record("received", "Laptop", "Inbox/photo.jpg", 1, 42)
        result = self.run_cli("--config", str(path), "--activity")
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ("received", "Laptop", "Inbox/photo.jpg", "1 files, 42 bytes"):
            self.assertIn(expected, result.stdout)
        self.assertNotIn("123456", result.stdout)

    def test_activity_missing_config_does_not_initialize_one(self):
        result = self.run_cli("--activity")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "config").exists())
