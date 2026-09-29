import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QMimeData, QPoint, QPointF, QProcess, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from qt_material import apply_stylesheet

    from fastfiles.ui import MainWindow

    HAS_UI = True
except ImportError:
    HAS_UI = False

from fastfiles.locker import Locker, LockerConfig, LockerHTTPServer, Peer, hash_access_code
from fastfiles.profiles import PeerProfile


@unittest.skipUnless(HAS_UI, "Install .[desktop] to run UI checks")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_stylesheet(
            cls.app,
            theme="light_blue.xml",
            invert_secondary=True,
            extra={"density_scale": "0", "font_family": "Noto Sans"},
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environ = patch.dict(os.environ, {
            "APPDATA": str(self.root / "config"),
            "LOCALAPPDATA": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        })
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.keyring = patch("fastfiles.ui.SecretStore")
        self.keyring.start().return_value.get.return_value = ""
        self.addCleanup(self.keyring.stop)
        self.warnings = patch.object(QMessageBox, "warning")
        self.warning = self.warnings.start()
        self.addCleanup(self.warnings.stop)
        config = LockerConfig(
            "local-test", "Local test", str(self.root / "local"), hash_access_code("123456"), "123456"
        )
        self.window = MainWindow(config, start_services=False)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.close_window)

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(10)
        self.assertTrue(predicate(), "Timed out waiting for GUI state")

    def close_window(self):
        if self.window._locker_busy:
            self.window._locker_cancel.set()
            self.wait_for(lambda: not self.window._locker_busy)
        self.wait_for(lambda: not self.window._checking_peers)
        self.window.close()
        self.app.processEvents()

    def server(self):
        config = LockerConfig(
            "remote-test", "Remote test", str(self.root / "remote"), hash_access_code("654321"), "654321"
        )
        server = LockerHTTPServer(("127.0.0.1", 0), Locker(config.locker_path), config)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()

        def stop():
            if thread.is_alive():
                server.shutdown()
                server.server_close()
                thread.join(2)

        self.addCleanup(stop)
        return server, stop

    def test_actions_visible_and_inputs_do_not_overlap_at_small_size(self):
        for width, height in ((1120, 840), (800, 600)):
            self.window.resize(width, height)
            for tab, button in ((0, self.window.upload_peer), (1, self.window.start)):
                self.window.tabs.setCurrentIndex(tab)
                self.app.processEvents()
                point = button.mapTo(self.window, QPoint(0, 0))
                self.assertGreaterEqual(point.y(), 0)
                self.assertLessEqual(point.y() + button.height(), self.window.height())
                self.assertLessEqual(point.x() + button.width(), self.window.width())
            self.assertGreaterEqual(self.window.paths.height(), 100)
            self.assertGreater(self.window.compress.y(), self.window.paths.y() + self.window.paths.height())

    def drop_paths(self, area, paths):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(area, enter)
        drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(area, drop)
        return drop.isAccepted()

    def test_direct_drop_queues_files_folders_and_receive_destination(self):
        w = self.window
        w.tabs.setCurrentIndex(1)
        source = self.root / "file with spaces.txt"
        source.write_text("hello")
        folder = self.root / "folder"
        folder.mkdir()
        self.assertTrue(self.drop_paths(w.direct_drop, [source, folder]))
        self.drop_paths(w.direct_drop, [source])
        self.assertEqual(w._local_paths, [str(source), str(folder)])
        self.assertIsNone(w._process)
        w.receive_radio.setChecked(True)
        self.assertFalse(self.drop_paths(w.direct_drop, [source]))
        self.assertEqual(w._local_paths, [])
        self.assertTrue(self.drop_paths(w.direct_drop, [folder]))
        self.assertEqual(w._local_paths, [str(folder)])
        w._set_running(True)
        self.assertFalse(w.direct_drop.isEnabled())
        w._set_running(False)

    def test_locker_drop_copies_into_current_folder_and_refreshes(self):
        w = self.window
        source = self.root / "dropped"
        (source / "empty").mkdir(parents=True)
        (source / "file.txt").write_text("hello")
        (w.locker.root / "destination").mkdir()
        w.local_relative = "destination"
        self.assertTrue(self.drop_paths(w.locker_drop, [source]))
        self.assertFalse(w.locker_drop.isEnabled())
        self.wait_for(lambda: not w._locker_busy)
        target = w.locker.root / "destination" / "dropped"
        self.assertEqual((target / "file.txt").read_text(), "hello")
        self.assertTrue((target / "empty").is_dir())
        self.assertTrue((source / "file.txt").exists())
        self.assertIn("Added 1 file", w.locker_status.text())
        self.assertEqual(w.local_tree.topLevelItemCount(), 1)
        w.locker_config.read_only = True
        w._update_locker_controls()
        self.assertFalse(w.locker_drop.isEnabled())

    def test_saved_ip_stays_selectable_and_status_tracks_service(self):
        server, stop = self.server()
        w = self.window
        w.profile_store.save_peer(PeerProfile("My server", "127.0.0.1", server.server_port))
        w._update_peers([])
        w.peer_combo.setCurrentIndex(0)
        w._check_availability()
        self.wait_for(lambda: not w._checking_peers)
        self.assertIn("Online", w.peer_combo.currentText())
        self.assertEqual(w._selected_peer().port, server.server_port)
        stop()
        w._check_availability()
        self.wait_for(lambda: not w._checking_peers)
        self.assertIn("Offline", w.peer_combo.currentText())
        self.assertTrue(w.peer_combo.isEnabled())
        self.assertEqual(w.peer_combo.count(), 1)

    def test_manual_input_survives_discovery_refresh_without_switching_peer(self):
        w = self.window
        w.peer_combo.setEditText("10.0.0.8:5000")
        w.peer_code.setText("123456")
        w._update_peers([Peer("other", "Other", "10.0.0.9", 47832)])
        self.assertEqual(w._selected_peer().address, "10.0.0.8")
        self.assertEqual(w.peer_code.text(), "123456")
        w.peer_combo.setCurrentIndex(0)
        self.assertEqual(w.peer_code.text(), "")
        self.assertIsNone(w._connected_client)

    def test_locker_copy_ui_locks_destination_and_keeps_completion_receipt(self):
        server, _ = self.server()
        w = self.window
        source = w.locker.root / "example.txt"
        source.write_text("hello")
        w._refresh_local_locker()
        w.peer_combo.setEditText(f"127.0.0.1:{server.server_port}")
        w.peer_code.setText("654321")
        w._connect_peer()
        self.wait_for(lambda: not w._locker_busy)
        self.assertIsNotNone(w._connected_client, w.locker_status.text())
        w.local_tree.setCurrentItem(w.local_tree.topLevelItem(0))
        w._upload_selected()
        self.assertFalse(w.peer_combo.isEnabled())
        self.assertFalse(w.local_tree.isEnabled())
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual((server.locker.root / "example.txt").read_text(), "hello")
        self.assertIn("Sent 1 file", w.locker_status.text())
        self.assertIn("example.txt", w.locker_status.text())
        self.assertEqual(w.remote_tree.topLevelItemCount(), 1)
        self.assertTrue(w.peer_combo.isEnabled())

    def setup_direct(self):
        w = self.window
        w.tabs.setCurrentIndex(1)
        source = self.root / "file.txt"
        source.write_text("hello")
        w.host.setCurrentText("example")
        w.remote_path.setCurrentText("~/")
        w._add_paths([str(source)])

    def test_failed_start_restores_controls(self):
        self.setup_direct()
        with (
            patch("fastfiles.ui.build_rsync_command", return_value=["/no-such-fastfiles-executable"]),
            patch("fastfiles.ui.shutil.which", return_value="/usr/bin/tool"),
        ):
            self.window._start_transfer()
            self.wait_for(lambda: self.window._process is None)
        self.assertTrue(self.window.start.isEnabled())
        self.assertTrue(self.window.direct_profile.isEnabled())
        self.assertEqual(self.window.status.text(), "Transfer failed")

    def test_dry_run_and_unterminated_output_are_reported(self):
        self.setup_direct()
        self.window.dry_run.setChecked(True)
        with (
            patch(
                "fastfiles.ui.build_rsync_command",
                return_value=[sys.executable, "-c", "print('last detail', end='')"],
            ),
            patch("fastfiles.ui.shutil.which", return_value="/usr/bin/tool"),
        ):
            self.window._start_transfer()
            self.wait_for(lambda: self.window._process is None)
        self.assertIn("no files copied", self.window.status.text())
        self.assertIn("last detail", self.window.log.toPlainText())

    def test_cancellation_restores_controls(self):
        self.setup_direct()
        with (
            patch(
                "fastfiles.ui.build_rsync_command",
                return_value=[sys.executable, "-c", "import time; time.sleep(20)"],
            ),
            patch("fastfiles.ui.shutil.which", return_value="/usr/bin/tool"),
        ):
            self.window._start_transfer()
            self.wait_for(
                lambda: (
                    self.window._process is not None
                    and self.window._process.state() == QProcess.ProcessState.Running
                )
            )
            self.window._cancel_transfer()
            self.wait_for(lambda: self.window._process is None)
        self.assertEqual(self.window.status.text(), "Transfer cancelled")
        self.assertTrue(self.window.start.isEnabled())

    def test_delete_key_in_host_does_not_remove_selected_files(self):
        self.setup_direct()
        self.window.paths.setCurrentRow(0)
        self.window.host.setFocus()
        QTest.keyClick(self.window.host.lineEdit(), Qt.Key.Key_Delete)
        self.assertEqual(self.window.paths.count(), 1)
