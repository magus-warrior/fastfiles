import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from fastfiles.activity import ActivityStore
from fastfiles.locker import Locker


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ActivityStore(self.root)

    def test_receipts_persist_newest_first_with_private_permissions(self):
        first = self.store.record("received", "Laptop", str(self.root / "a.jpg"), 1, 40)
        second = self.store.record("received", "Phone", str(self.root / "b.jpg"), 1, 30)
        self.assertEqual(ActivityStore(self.root).recent(), [second, first])
        self.assertNotEqual(first["id"], second["id"])
        self.assertIn("+00:00", first["time"])
        self.assertEqual(self.store.recent(0), [])
        self.assertEqual(Locker(self.root).list(), [])
        if os.name != "nt":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_threads_using_separate_store_instances_do_not_lose_receipts(self):
        threads = [threading.Thread(
            target=ActivityStore(self.root).record,
            args=("received", f"Computer {index}", "photo.jpg", 1, index),
        ) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.store.recent()), 20)
        self.assertEqual(len({entry["id"] for entry in self.store.recent()}), 20)

    def test_malformed_activity_is_not_overwritten(self):
        self.store.path.write_text(json.dumps({"receipts": [{"bytes": "wrong"}]}))
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.record("received", "Laptop", "photo.jpg", 1, 10)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_separate_process_writers_preserve_every_receipt(self):
        script = """
import sys
import time
from pathlib import Path
from fastfiles.activity import ActivityStore
root = Path(sys.argv[1])
while not (root / 'start').exists():
    time.sleep(0.01)
for number in range(20):
    ActivityStore(root).record('received', sys.argv[2], str(number), 1, number)
"""
        processes = [subprocess.Popen(
            [sys.executable, "-c", script, str(self.root), str(index)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) for index in range(4)]
        try:
            (self.root / "start").touch()
            for process in processes:
                _, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error.decode())
            receipts = self.store.recent()
            self.assertEqual(len(receipts), 80)
            self.assertEqual(len({entry["id"] for entry in receipts}), 80)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()
