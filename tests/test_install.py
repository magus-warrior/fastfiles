"""Exercise the installer's system-package step without modifying the host."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipIf(os.name == "nt", "Linux package-manager installer")
class InstallerTests(unittest.TestCase):
    def run_packages(self, manager, *, headless=False, missing=True, failure=False):
        installer = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
        start = installer.index('if [[ "${INSTALL_TARGET}" ==')
        end = installer.index('info "Installing FastFiles and its dependencies"')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "calls"
            commands = {
                "uname": "echo Linux",
                "python3": 'input=$(cat); if [[ "$input" == *"except OSError"* ]]; then '
                + ('echo libxcb-cursor.so.0; ' if missing else ':; ')
                + "fi",
                "sudo": '"$@"',
                "cat": '/bin/cat "$@"',
            }
            if manager:
                commands[manager] = 'echo "$*" >> "$CALL_LOG"\n' + ("exit 1" if failure else "exit 0")
            for name, body in commands.items():
                path = root / name
                path.write_text("#!/bin/bash\n" + body + "\n")
                path.chmod(0o755)
            script = (
                'set -Eeuo pipefail\ninfo() { :; }\nfail() { echo "$1" >&2; exit 1; }\n'
                + ('INSTALL_TARGET=fastfiles\n' if headless else 'INSTALL_TARGET="fastfiles[desktop]"\n')
                + installer[start:end]
            )
            result = subprocess.run(
                ["/bin/bash", "-c", script],
                env={**os.environ, "PATH": directory, "CALL_LOG": str(log)},
                capture_output=True, text=True, timeout=10,
            )
            return result, log.read_text() if log.exists() else ""

    def test_supported_package_managers_install_dependencies(self):
        for manager, package in (
            ("apt-get", "libxcb-cursor0"), ("dnf", "xcb-util-cursor"), ("pacman", "xcb-util-cursor")
        ):
            with self.subTest(manager=manager):
                result, calls = self.run_packages(manager)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(package, calls)
                if manager == "apt-get":
                    self.assertTrue(calls.startswith("update\n"))

    def test_headless_and_satisfied_dependencies_skip_packages(self):
        for options in ({"headless": True}, {"missing": False}):
            result, calls = self.run_packages("apt-get", **options)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls, "")

    def test_failed_package_install_stops_setup(self):
        result, _ = self.run_packages("dnf", failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not install", result.stderr)

    def test_unsupported_distribution_gets_actionable_error(self):
        result, _ = self.run_packages(None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("distribution's package manager", result.stderr)
