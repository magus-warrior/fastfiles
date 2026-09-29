"""Exercise update entry points against local Git remotes, without installing packages."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.author = self.root / 'author'
        self.checkout = self.root / 'checkout with spaces'
        self.env = {
            **os.environ,
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
            'GIT_TERMINAL_PROMPT': '0',
        }
        self.git(self.root, 'init', '--bare', str(self.remote))
        self.git(self.root, 'clone', str(self.remote), str(self.author))
        source = Path(__file__).resolve().parents[1]
        for name in ('update.sh', 'update.ps1'):
            shutil.copyfile(source / name, self.author / name)
        (self.author / 'install.sh').write_text('#!/usr/bin/env bash\nprintf "%s" "$*" > installed\n')
        (self.author / 'install.ps1').write_text(
            "param([switch]$Headless)\nSet-Content -Path installed -Value $Headless\n"
        )
        self.git(self.author, 'add', '.')
        self.git(self.author, 'commit', '-m', 'Initial')
        self.git(self.author, 'push', 'origin', 'HEAD')
        self.git(self.root, 'clone', str(self.remote), str(self.checkout))

    def git(self, directory, *args):
        return subprocess.run(['git', '-C', str(directory), *args], env=self.env,
                              capture_output=True, text=True, check=True, timeout=20)

    def update(self):
        if os.name == 'nt':
            command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                       '-File', str(self.checkout / 'update.ps1'), '-Headless']
        else:
            command = ['bash', str(self.checkout / 'update.sh'), '--headless']
        return subprocess.run(command, cwd=self.root, env=self.env, capture_output=True,
                              text=True, timeout=30)

    def publish(self):
        (self.author / 'version').write_text('new release')
        self.git(self.author, 'add', '.')
        self.git(self.author, 'commit', '-m', 'Release')
        self.git(self.author, 'push')

    def test_pulls_then_installs_from_checkout_with_spaces(self):
        self.publish()
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.checkout / 'version').read_text(), 'new release')
        self.assertEqual((self.checkout / 'installed').read_text().strip(),
                         'True' if os.name == 'nt' else '--headless')

    def test_local_changes_stop_update(self):
        self.publish()
        (self.checkout / 'install.sh').write_text('local changes')
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.checkout / 'version').exists())
        self.assertFalse((self.checkout / 'installed').exists())
        self.assertEqual((self.checkout / 'install.sh').read_text(), 'local changes')

    def test_diverged_history_stops_before_installation(self):
        self.publish()
        (self.checkout / 'local').write_text('local commit')
        self.git(self.checkout, 'add', '.')
        self.git(self.checkout, 'commit', '-m', 'Local commit')
        before = self.git(self.checkout, 'rev-parse', 'HEAD').stdout
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD').stdout, before)
        self.assertFalse((self.checkout / 'installed').exists())

    def test_failed_installer_reports_failure_after_pull(self):
        (self.author / 'install.sh').write_text('exit 7\n')
        (self.author / 'install.ps1').write_text('exit 7\n')
        self.publish()
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.checkout / 'version').exists())
        self.assertIn('installation failed', result.stdout + result.stderr)
