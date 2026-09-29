"""Windows behavior checks that also run on POSIX development machines."""
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastfiles.core import rsync_local_path
from fastfiles.logging_config import PrivateRotatingFileHandler, log_path
from fastfiles.policy import allowed, relative_path, validate_patterns
from fastfiles.storage import config_dir


class WindowsTests(unittest.TestCase):
    def test_native_data_locations(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('sys.platform', 'win32'), patch.dict(os.environ, {
                'APPDATA': folder, 'LOCALAPPDATA': folder,
                'XDG_CONFIG_HOME': '/ignored', 'XDG_STATE_HOME': '/ignored',
            }):
                self.assertEqual(config_dir(), Path(folder) / 'FastFiles')
                self.assertEqual(log_path(), Path(folder) / 'FastFiles' / 'fastfiles.log')

    def test_log_rotation_without_posix_permissions(self):
        with tempfile.TemporaryDirectory() as folder, patch('sys.platform', 'win32'):
            with patch('os.fchmod', create=True, side_effect=AssertionError('POSIX-only call')):
                path = Path(folder) / 'test.log'
                handler = PrivateRotatingFileHandler(path, maxBytes=10, backupCount=1, encoding='utf-8')
                try:
                    for _ in range(2):
                        handler.emit(logging.LogRecord('test', logging.INFO, '', 0, 'rollover test', (), None))
                    self.assertIn('rollover test', path.read_text())
                    self.assertTrue(Path(str(path) + '.1').exists())
                finally:
                    handler.close()

    def test_windows_unsafe_names_and_streams_are_rejected(self):
        with patch('sys.platform', 'win32'):
            for path in ('C:escape', 'folder/C:/escape', 'file:secret', 'NUL', 'con.txt',
                         'COM1', 'LPT².txt', 'file.', 'file ', 'bad|name', 'bad\x01name'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    relative_path(path)
            self.assertEqual(relative_path('folder/hello.txt'), 'folder/hello.txt')
            validate_patterns(['**', '*.txt', 'folder/?.txt'], 'allow_patterns')

    def test_windows_policy_cannot_be_bypassed_with_case(self):
        with patch('sys.platform', 'win32'):
            self.assertFalse(allowed('PRIVATE/data', ['**'], ['private']))
            self.assertTrue(allowed('PUBLIC/readme.TXT', ['public/*.txt'], []))
            with self.assertRaises(PermissionError):
                relative_path('.FASTFILES-secret')

    def test_cygwin_path_conversion_is_one_argument_and_keeps_trailing_slash(self):
        with patch('sys.platform', 'win32'), patch('fastfiles.core.shutil.which', return_value='cygpath'), \
             patch('subprocess.CREATE_NO_WINDOW', 0, create=True), \
             patch('fastfiles.core.subprocess.check_output', return_value='/cygdrive/c/My Files\n') as run:
            self.assertEqual(rsync_local_path('My Files/'), '/cygdrive/c/My Files/')
            self.assertEqual(run.call_args.args[0], ['cygpath', '-u', '--', str(Path('My Files').absolute())])

    def test_missing_cygwin_has_actionable_error(self):
        with patch('sys.platform', 'win32'), patch('fastfiles.core.shutil.which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Cygwin'):
                rsync_local_path('file')
