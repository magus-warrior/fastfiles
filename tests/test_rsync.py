import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastfiles.core import Direction, TransferRequest, build_rsync_command, read_ssh_aliases


@unittest.skipUnless(os.name != "nt" and shutil.which("rsync"), "requires POSIX and rsync")
class RsyncIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote home"
        self.remote.mkdir()

    def run_transfer(self, request):
        command = build_rsync_command(request)
        adapter = Path(__file__).parent / "fixtures" / "remote_shell.py"
        command = [
            "--rsh=" + shlex.join([sys.executable, str(adapter)]) if value.startswith("--rsh=") else value
            for value in command
        ]
        completed = subprocess.run(
            command,
            env={**os.environ, "FASTFILES_TEST_REMOTE": str(self.remote)},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        return completed.stdout

    def test_send_and_receive_use_remote_home_and_preserve_names(self):
        source = self.root / "some:file with spaces ü.txt"
        source.write_text("sample")
        self.run_transfer(TransferRequest(Direction.SEND, "local-test", "~/", (str(source),)))
        self.assertEqual((self.remote / source.name).read_text(), "sample")
        self.assertFalse((self.remote / "~").exists())
        destination = self.root / "received"
        destination.mkdir()
        self.run_transfer(
            TransferRequest(Direction.RECEIVE, "local-test", "~/" + source.name, (str(destination),))
        )
        self.assertEqual((destination / source.name).read_text(), "sample")

    def test_dry_run_copies_nothing_and_real_copy_keeps_empty_folders(self):
        source = self.root / "folder"
        (source / "empty").mkdir(parents=True)
        (source / "file").write_text("sample")
        self.run_transfer(TransferRequest(Direction.SEND, "local-test", "~/", (str(source),), dry_run=True))
        self.assertEqual(list(self.remote.iterdir()), [])
        self.run_transfer(TransferRequest(Direction.SEND, "local-test", "~/", (str(source),)))
        self.assertTrue((self.remote / "folder" / "empty").is_dir())
        self.assertEqual((self.remote / "folder" / "file").read_text(), "sample")


class HostParsingTests(unittest.TestCase):
    def test_invalid_host_cannot_select_daemon_or_shell_flags(self):
        for host in (
            "-oProxyCommand=foo",
            "host:873",
            "rsync://host",
            "user@host:module",
            "bad host",
            "host\n",
        ):
            if host == "host\n":
                continue  # Surrounding whitespace is intentionally trimmed.
            with self.subTest(host=host), self.assertRaises(ValueError):
                build_rsync_command(TransferRequest(Direction.SEND, host, "~/", ("/tmp/file",)))

    def test_ssh_include_comments_and_cycles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").write_text("Include hosts\nHost one # comment\nHost *.wild\n")
            (root / "hosts").write_text('Include config\nHost="two"\n')
            self.assertEqual(read_ssh_aliases(root / "config"), ["one", "two"])
