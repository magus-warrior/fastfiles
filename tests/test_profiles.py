import tempfile
import unittest
from pathlib import Path

from fastfiles.logging_config import PrivateRotatingFileHandler
from fastfiles.profiles import DirectProfile, PeerProfile, ProfileStore


class PersistenceTests(unittest.TestCase):
    def test_corrupt_profiles_cannot_be_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "connections.json"
            for contents in ("broken", '{"peers": {}}', '{"direct": [{"name": 1}]}'):
                path.write_text(contents)
                with self.assertRaises(ValueError):
                    ProfileStore(path).save_peer(PeerProfile("Server", "10.0.0.2", 47832))
                self.assertEqual(path.read_text(), contents)

    def test_editing_and_deleting_profiles_preserves_other_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ProfileStore(Path(temporary) / "connections.json")
            store.save_direct(DirectProfile("SSH", "user@host", "~/"))
            store.save_peer(PeerProfile("Home", "10.0.0.2", 47832))
            store.save_peer(PeerProfile("Home", "10.0.0.3", 47832))
            self.assertEqual(len(store.peers()), 1)
            self.assertEqual(store.peers()[0].address, "10.0.0.3")
            store.delete_peer("Home")
            self.assertEqual(len(store.direct()), 1)
            self.assertEqual(store.peers(), [])
            text = store.path.read_text()
            self.assertNotIn("code", text)
            self.assertNotIn("password", text)

    def test_rotating_logs_stay_private(self):
        import logging

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fastfiles.log"
            handler = PrivateRotatingFileHandler(path, maxBytes=10, backupCount=1, encoding="utf-8")
            try:
                record = logging.LogRecord("test", logging.INFO, "", 0, "testing rollover", (), None)
                handler.emit(record)
                handler.emit(record)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(Path(str(path) + ".1").stat().st_mode & 0o777, 0o600)
            finally:
                handler.close()
