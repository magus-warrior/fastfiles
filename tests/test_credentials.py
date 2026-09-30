import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastfiles.credentials import CredentialVault
from fastfiles.profiles import SecretStore


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = CredentialVault(self.root / "private")

    def test_saved_access_survives_restart_without_plaintext_files(self):
        self.vault.set("peer:example:47832", "654321-private-code")
        reopened = CredentialVault(self.vault.directory)
        self.assertEqual(reopened.get("peer:example:47832"), "654321-private-code")
        for path in self.vault.directory.iterdir():
            self.assertNotIn(b"654321-private-code", path.read_bytes())
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        if os.name != "nt":
            self.assertEqual(self.vault.directory.stat().st_mode & 0o777, 0o700)
        reopened.delete("peer:example:47832")
        self.assertEqual(reopened.get("peer:example:47832"), "")

    def test_missing_key_and_malformed_vault_preserve_existing_data(self):
        self.vault.set("peer", "code")
        original = self.vault.path.read_bytes()
        self.vault.key_path.unlink()
        with self.assertRaises(OSError):
            self.vault.set("new peer", "new code")
        self.assertEqual(self.vault.path.read_bytes(), original)
        self.vault.path.write_text("broken JSON")
        with self.assertRaises(ValueError):
            self.vault.set("peer", "code")
        self.assertEqual(self.vault.path.read_text(), "broken JSON")

    @unittest.skipIf(os.name == "nt", "Symlink creation may require administrator privileges")
    def test_symlinked_vault_is_not_followed(self):
        self.vault.directory.mkdir()
        target = self.root / "unrelated.json"
        target.write_text("{}")
        self.vault.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.vault.set("peer", "code")
        self.assertEqual(target.read_text(), "{}")

    def test_parallel_processes_preserve_all_saved_connections(self):
        script = (
            "from pathlib import Path; import sys; "
            "from fastfiles.credentials import CredentialVault; "
            "v=CredentialVault(Path(sys.argv[1])); "
            "[v.set(sys.argv[2]+str(i), 'test-code') for i in range(5)]"
        )
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.vault.directory), str(index)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for index in range(3)]
        try:
            for process in processes:
                _, errors = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, errors.decode())
            for index in range(3):
                for item in range(5):
                    self.assertEqual(self.vault.get(f"{index}{item}"), "test-code")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_headless_fallback_and_keyring_migration(self):
        fake = Mock()
        fake.get_keyring.return_value.priority = 0
        with patch.dict(sys.modules, {"keyring": fake}):
            store = SecretStore()
            store.vault = self.vault
            store.set("peer", "first code")
            self.assertEqual(store.get("peer"), "first code")
            fake.get_keyring.return_value.priority = 1
            fake.get_password.return_value = "second code"
            store.set("peer", "second code")
            fake.set_password.assert_called_with("FastFiles", "peer", "second code")
            self.assertFalse(self.vault.contains("peer"))
            self.assertEqual(store.get("peer"), "second code")

    def test_forgetting_while_keyring_locked_does_not_restore_stale_code(self):
        fake = Mock()
        fake.delete_password.side_effect = RuntimeError("locked")
        fake.get_password.return_value = "stale code"
        with patch.dict(sys.modules, {"keyring": fake}):
            store = SecretStore()
            store.vault = self.vault
            store.delete("peer")
            self.assertEqual(store.get("peer"), "")
            fake.get_password.assert_not_called()
