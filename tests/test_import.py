import tempfile
import threading
import unittest
from pathlib import Path

from fastfiles.locker import Locker, TransferCancelled
from fastfiles.transfers import import_to_locker


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.locker = Locker(self.root / 'locker')
        self.source = self.root / 'file.txt'
        self.source.write_text('new')
        self.cancel = threading.Event()

    def copy(self, sources=None, **options):
        return import_to_locker(self.locker, sources or [str(self.source)], '',
                                lambda *_: None, self.cancel, **options)

    def test_existing_files_require_overwrite(self):
        target = self.locker.root / self.source.name
        target.write_text('old')
        with self.assertRaises(FileExistsError):
            self.copy()
        self.assertEqual(target.read_text(), 'old')
        self.copy(overwrite=True)
        self.assertEqual(target.read_text(), 'new')

    def test_policy_is_checked_before_any_copy(self):
        self.locker.deny_patterns = ['blocked']
        blocked = self.root / 'blocked'
        blocked.write_text('private')
        with self.assertRaises(PermissionError):
            self.copy([str(self.source), str(blocked)])
        self.assertEqual(list(self.locker.root.iterdir()), [])

    def test_cancelled_copy_leaves_no_temporary_file(self):
        def progress(*_):
            self.cancel.set()
        with self.assertRaises(TransferCancelled):
            import_to_locker(self.locker, [str(self.source)], '', progress, self.cancel)
        self.assertEqual(list(self.locker.root.iterdir()), [])
        self.assertEqual(self.source.read_text(), 'new')

    def test_cannot_copy_locker_into_itself(self):
        with self.assertRaises(ValueError):
            self.copy([str(self.locker.root)])

    def test_same_destination_names_are_rejected_before_copy(self):
        folder = self.root / 'other'
        folder.mkdir()
        other = folder / self.source.name
        other.write_text('other')
        with self.assertRaises(ValueError):
            self.copy([str(self.source), str(other)])
        self.assertEqual(list(self.locker.root.iterdir()), [])
