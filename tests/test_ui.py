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
    from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, QProcess, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from qt_material import apply_stylesheet

    from fastfiles.ui import MainWindow

    HAS_UI = True
except ImportError:
    HAS_UI = False

from fastfiles.access import new_computer_permission
from fastfiles.locker import Locker, LockerConfig, LockerHTTPServer, Peer, PeerClient, hash_access_code
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

    def select_local(self, name):
        self.window._refresh_local_locker()
        tree = self.window.local_tree
        tree.clearSelection()
        for index in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(index)
            if item.text(0) == name:
                tree.setCurrentItem(item)
                return item
        self.fail(f"Missing local item: {name}")

    def test_local_rename_preserves_contents_and_refuses_collisions(self):
        root = self.window.locker.root
        (root / "old").mkdir()
        (root / "old" / "file.txt").write_text("keep")
        self.select_local("old")
        with patch("fastfiles.ui.QInputDialog.getText", return_value=("new", True)):
            self.window._rename_local()
        self.assertEqual((root / "new" / "file.txt").read_text(), "keep")
        self.assertFalse((root / "old").exists())
        (root / "existing").write_text("original")
        self.select_local("new")
        with patch("fastfiles.ui.QInputDialog.getText", return_value=("existing", True)):
            self.window._rename_local()
        self.assertTrue((root / "new").is_dir())
        self.assertEqual((root / "existing").read_text(), "original")
        with patch("fastfiles.ui.QInputDialog.getText", return_value=("../escape", True)):
            self.window._rename_local()
        self.assertTrue((root / "new").exists())

    def test_saved_computer_editor_renames_without_losing_favorites(self):
        from PySide6.QtWidgets import QDialogButtonBox, QLineEdit

        profile = PeerProfile("Studio", "studio", 47832, "device", {"Photos": "photos"}, "photos")
        self.window.profile_store.save_peer(profile)
        self.window._update_peers([])
        self.window.peer_combo.setCurrentIndex(self.window.peer_combo.findData("saved:Studio"))

        def edit(dialog):
            fields = dialog.findChildren(QLineEdit)
            fields[0].setText("Office")
            fields[2].setText("Inbox")
            dialog.findChild(QDialogButtonBox).accepted.emit()

        with patch("fastfiles.ui.QDialog.exec", edit):
            self.window._edit_profile(True)
        saved = self.window.profile_store.peers()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].name, "Office")
        self.assertEqual(saved[0].folders, {"Photos": "photos"})
        self.assertEqual(saved[0].last_folder, "Inbox")
        self.assertEqual(self.window.peer_combo.currentData(), "saved:Office")

    def test_local_trash_cancel_failure_and_read_only_preserve_file(self):
        source = self.window.locker.root / "keep.txt"
        source.write_text("keep")
        self.select_local("keep.txt")
        with patch.object(self.window, "_confirm", return_value=False), patch("fastfiles.ui.QFile") as file:
            self.window._trash_local()
            file.assert_not_called()
        with patch.object(self.window, "_confirm", return_value=True), patch("fastfiles.ui.QFile") as file:
            file.return_value.moveToTrash.return_value = False
            file.return_value.errorString.return_value = "Trash unavailable"
            self.window._trash_local()
            file.return_value.moveToTrash.assert_called_once()
        self.assertEqual(source.read_text(), "keep")
        self.window.locker_config.read_only = True
        with patch("fastfiles.ui.QInputDialog.getText") as prompt, patch.object(self.window, "_confirm") as confirm:
            self.window._rename_local()
            self.window._trash_local()
            prompt.assert_not_called()
            confirm.assert_not_called()

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
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def server(self, config=None):
        config = config or LockerConfig(
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

    def connect_server(self, server, credential="654321"):
        w = self.window
        w.peer_combo.setEditText(f"127.0.0.1:{server.server_port}")
        w.peer_code.setText(credential)
        w._connect_peer()
        self.wait_for(lambda: not w._locker_busy)
        self.assertIsNotNone(w._connected_client, w.locker_status.text())

    def tree_items(self, tree):
        return {
            tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole): tree.topLevelItem(index)
            for index in range(tree.topLevelItemCount())
        }

    def select_paths(self, tree, paths):
        tree.clearSelection()
        items = self.tree_items(tree)
        for path in paths:
            items[path].setSelected(True)
        self.app.processEvents()

    def drop_mime_on_view(self, view, mime, item=None):
        point = view.visualItemRect(item).center() if item is not None else QPoint(10, view.viewport().height() - 5)
        enter = QDragEnterEvent(point, Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(view.viewport(), enter)
        drop = QDropEvent(QPointF(point), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(view.viewport(), drop)
        return drop.isAccepted()

    def drop_selection(self, source_tree, destination_tree, target_item=None):
        # Capture the real widget's MIME data while skipping the blocking OS
        # drag loop, then deliver ordinary Qt drag/drop events to the target.
        with patch("fastfiles.locker_widgets.QDrag") as drag:
            source_tree.startDrag(Qt.DropAction.CopyAction)
            mime = drag.return_value.setMimeData.call_args.args[0]
        return self.drop_mime_on_view(destination_tree, mime, target_item)

    def save_computer(self, server, *, last_folder="", folders=None, device_id="remote-test"):
        w = self.window
        profile = PeerProfile("Home server", "127.0.0.1", server.server_port,
                              device_id, folders or {}, last_folder)
        w.profile_store.save_peer(profile)
        w.secret_store.get.return_value = "654321"
        w._update_peers([])
        return profile

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

    def test_send_picker_copies_files_and_folders_without_locker_staging(self):
        server, _ = self.server()
        self.connect_server(server)
        w = self.window
        source = self.root / "From Downloads.txt"
        source.write_text("sent directly")
        folder = self.root / "Holiday"
        (folder / "empty").mkdir(parents=True)
        (folder / "photo.txt").write_text("photo")
        from PySide6.QtWidgets import QFileDialog

        for button, path, mode in ((w.send_files, source, QFileDialog.FileMode.ExistingFiles),
                                   (w.send_folder, folder, QFileDialog.FileMode.Directory)):
            with patch.object(w, "_path_dialog") as picker:
                picker.return_value.exec.return_value = QFileDialog.DialogCode.Accepted
                picker.return_value.selectedFiles.return_value = [str(path)]
                button.click()
                self.assertEqual(picker.call_args.args[1], mode)
            self.wait_for(lambda: not w._locker_busy)
            self.assertTrue((server.locker.root / path.name).exists())
            self.assertFalse((w.locker.root / path.name).exists())
        self.assertEqual((server.locker.root / source.name).read_text(), "sent directly")
        self.assertTrue((server.locker.root / folder.name / "empty").is_dir())
        self.assertTrue(source.exists())

    def test_quick_send_requires_connection_and_writable_destination(self):
        w = self.window
        self.assertFalse(w.send_files.isEnabled())
        self.assertFalse(w.send_folder.isEnabled())
        self.assertIn("Open FastFiles on both", w.connection_summary.text())
        server, _ = self.server()
        self.connect_server(server)
        self.assertTrue(w.send_files.isEnabled())
        self.assertIn("Connected to", w.connection_summary.text())
        w._remote_info["read_only"] = True
        w._update_locker_controls()
        self.assertFalse(w.send_files.isEnabled())
        with patch.object(w, "_path_dialog") as picker:
            w._pick_locker_items(remote=True, folder=False)
        picker.assert_not_called()

    def test_polished_layout_keeps_quick_actions_visible_at_both_sizes(self):
        w = self.window
        for width, height in ((1400, 960), (800, 600)):
            w.resize(width, height)
            self.app.processEvents()
            self.assertEqual(w.locker_scroll.horizontalScrollBar().maximum(), 0)
            for button in (w.send_files, w.send_folder, w.add_locker_files, w.connect_peer):
                point = button.mapTo(w.locker_scroll.viewport(), QPoint(0, 0))
                self.assertGreaterEqual(point.x(), 0)
                self.assertGreaterEqual(point.y(), 0)
                self.assertLessEqual(point.x() + button.width(), w.locker_scroll.viewport().width())
                self.assertLessEqual(point.y() + button.height(), w.locker_scroll.viewport().height())
        self.assertFalse(w.profile_options.isVisible())

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

    def test_external_remote_drop_copies_photos_without_local_staging(self):
        server, _ = self.server()
        self.connect_server(server)
        w = self.window
        source = self.root / "camera"
        source.mkdir()
        photo = source / "holiday photo.jpg"
        photo.write_bytes(b"photo\0" * 1000)
        album = source / "album"
        (album / "empty").mkdir(parents=True)
        (album / "second.png").write_bytes(b"second photo")
        self.assertTrue(self.drop_paths(w.remote_drop, [photo, album]))
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual((server.locker.root / photo.name).read_bytes(), photo.read_bytes())
        self.assertEqual((server.locker.root / "album" / "second.png").read_bytes(), b"second photo")
        self.assertTrue((server.locker.root / "album" / "empty").is_dir())
        self.assertFalse((w.locker.root / photo.name).exists())
        self.assertFalse((w.locker.root / "album").exists())
        self.assertEqual(set(self.tree_items(w.remote_tree)), {photo.name, "album"})
        self.assertTrue(photo.exists())
        self.assertIn("Sent 2 file", w.locker_status.text())

    def test_send_and_receive_multiple_selected_files(self):
        server, _ = self.server()
        w = self.window
        for name in ("first.jpg", "second.png"):
            (w.locker.root / name).write_bytes(name.encode())
        w._refresh_local_locker()
        self.connect_server(server)
        self.select_paths(w.local_tree, ["first.jpg", "second.png"])
        self.assertTrue(w.upload_peer.isEnabled())
        QTest.mouseClick(w.upload_peer, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual(set(self.tree_items(w.remote_tree)), {"first.jpg", "second.png"})
        w._go_inbox()
        self.select_paths(w.remote_tree, ["first.jpg", "second.png"])
        self.assertTrue(w.download_peer.isEnabled())
        QTest.mouseClick(w.download_peer, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: not w._locker_busy)
        for name in ("first.jpg", "second.png"):
            self.assertEqual((w.locker.root / "Inbox" / name).read_bytes(), name.encode())
            self.assertTrue((w.locker.root / name).exists())
            self.assertTrue((server.locker.root / name).exists())
        self.assertIn("Received 2 file", w.locker_status.text())

    def test_drag_between_lockers_uses_the_folder_under_the_pointer(self):
        server, _ = self.server()
        w = self.window
        (w.locker.root / "photo.jpg").write_bytes(b"photo")
        (w.locker.root / "received").mkdir()
        (server.locker.root / "album").mkdir()
        w._refresh_local_locker()
        self.connect_server(server)
        self.select_paths(w.local_tree, ["photo.jpg"])
        target = self.tree_items(w.remote_tree)["album"]
        self.assertTrue(self.drop_selection(w.local_tree, w.remote_tree, target))
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual((server.locker.root / "album" / "photo.jpg").read_bytes(), b"photo")
        self.assertFalse((server.locker.root / "photo.jpg").exists())
        self.select_paths(w.remote_tree, ["album"])
        target = self.tree_items(w.local_tree)["received"]
        self.assertTrue(self.drop_selection(w.remote_tree, w.local_tree, target))
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual((w.locker.root / "received" / "album" / "photo.jpg").read_bytes(), b"photo")
        self.assertTrue((w.locker.root / "photo.jpg").exists())

    def test_poll_updates_incoming_activity_and_remote_files_preserving_selection(self):
        server, _ = self.server()
        w = self.window
        (w.locker.root / "selected.txt").write_bytes(b"local")
        (server.locker.root / "selected.txt").write_bytes(b"remote")
        w._refresh_local_locker()
        self.connect_server(server)
        self.select_paths(w.local_tree, ["selected.txt"])
        self.select_paths(w.remote_tree, ["selected.txt"])
        local_server, _ = self.server(w.locker_config)
        local_peer = Peer("local-test", "Local test", "127.0.0.1", local_server.server_port)
        photo = self.root / "incoming.jpg"
        photo.write_bytes(b"new photo")
        PeerClient(local_peer, "123456").upload(photo, photo.name)
        (server.locker.root / "new-remote.png").write_bytes(b"remote photo")
        # Trigger the connected timer promptly; exercise the same polling
        # signal and asynchronous listing used during normal idle browsing.
        self.assertTrue(w.locker_timer.isActive())
        w.locker_timer.timeout.emit()
        self.wait_for(lambda: not w._refreshing)
        self.assertIn(photo.name, self.tree_items(w.local_tree))
        self.assertIn("new-remote.png", self.tree_items(w.remote_tree))
        for tree in (w.local_tree, w.remote_tree):
            self.assertEqual([item.data(0, Qt.ItemDataRole.UserRole) for item in tree.selectedItems()],
                             ["selected.txt"])
        self.assertIn(photo.name, w.incoming_notice.text())
        self.assertEqual(w.activity_list.count(), 1)
        self.assertIn(photo.name, w.activity_list.item(0).text())

    def test_computer_bookmarks_and_last_folder_survive_reconnect(self):
        server, _ = self.server()
        (server.locker.root / "Photos").mkdir()
        (server.locker.root / "Inbox").mkdir()
        w = self.window
        profile = self.save_computer(server, last_folder="Photos")
        w.peer_combo.setCurrentIndex(w.peer_combo.findData(f"saved:{profile.name}"))
        self.assertEqual(w.peer_code.text(), "654321")
        w._connect_peer()
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual(w.remote_relative, "Photos")
        with patch("fastfiles.ui.QInputDialog.getText") as prompt:
            w._save_remote_folder()
        prompt.assert_not_called()
        w._load_remote("Inbox")
        self.wait_for(lambda: not w._locker_busy)
        saved = w.profile_store.peers()[0]
        self.assertEqual(saved.folders, {"Photos": "Photos"})
        self.assertEqual(saved.last_folder, "Inbox")
        self.assertEqual(saved.device_id, "remote-test")
        w._disconnect_peer()
        w._connect_peer()
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual(w.remote_relative, "Inbox")
        index = w.remote_folders.findText("Photos")
        self.assertGreater(index, 0)
        w.remote_folders.activated.emit(index)
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual(w.remote_relative, "Photos")
        self.assertEqual(w.profile_store.peers()[0].last_folder, "Photos")

    def test_favorite_unsaved_computer_is_one_click_and_keeps_access_private(self):
        server, _ = self.server()
        (server.locker.root / "Photos").mkdir()
        self.connect_server(server)
        w = self.window
        w._load_remote("Photos")
        self.wait_for(lambda: not w._locker_busy)
        with patch("fastfiles.ui.QInputDialog.getText") as prompt:
            w.save_folder.click()
        prompt.assert_not_called()
        saved = w.profile_store.peers()[0]
        self.assertEqual(saved.name, "Remote test")
        self.assertEqual(saved.folders, {"Photos": "Photos"})
        self.assertEqual(saved.last_folder, "Photos")
        self.assertEqual(saved.device_id, "remote-test")
        self.assertIsNotNone(w._connected_client)
        self.assertEqual(w.remote_relative, "Photos")
        w.secret_store.set.assert_called_with(saved.secret_id, "654321")
        self.assertNotIn("654321", w.profile_store.path.read_text())
        self.assertIn("Saved", w.quick_save_peer.text())
        self.assertIn("Favorited", w.save_folder.text())
        w.save_folder.click()
        self.assertEqual(w.profile_store.peers()[0].folders, {})
        self.assertTrue((server.locker.root / "Photos").is_dir())
        self.assertEqual(len(w.profile_store.peers()), 1)

    def test_one_click_save_keeps_computer_and_code_together_without_name_collisions(self):
        server, _ = self.server()
        w = self.window
        w.profile_store.save_peer(PeerProfile("Remote test", "other.example", 47832))
        self.connect_server(server)
        w.quick_save_peer.click()
        profiles = w.profile_store.peers()
        self.assertEqual([profile.name for profile in profiles], ["Remote test", "Remote test (2)"])
        self.assertEqual(profiles[0].address, "other.example")
        w.secret_store.set.assert_called_with(profiles[1].secret_id, "654321")
        self.assertEqual(len(w.profile_store.peers()), 2)

    def test_favorites_with_same_basename_do_not_replace_each_other(self):
        server, _ = self.server()
        for path in ("Family/Photos", "Work/Photos"):
            (server.locker.root / path).mkdir(parents=True)
        self.connect_server(server)
        w = self.window
        for path in ("Family/Photos", "Work/Photos"):
            w._load_remote(path)
            self.wait_for(lambda: not w._locker_busy)
            w.save_folder.click()
        self.assertEqual(w.profile_store.peers()[0].folders,
                         {"Photos": "Family/Photos", "Photos (2)": "Work/Photos"})

    def test_sidebar_drop_connects_with_remembered_key_and_uses_saved_folder(self):
        server, _ = self.server()
        (server.locker.root / "Photos").mkdir()
        w = self.window
        self.save_computer(server, last_folder="Photos")
        source = self.root / "camera.jpg"
        source.write_bytes(b"photo")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        self.assertTrue(self.drop_mime_on_view(w.computers, mime, w.computers.item(0)))
        self.wait_for(lambda: not w._locker_busy)
        self.assertIsNotNone(w._connected_client, w.locker_status.text())
        self.assertEqual(w.remote_relative, "Photos")
        self.assertEqual((server.locker.root / "Photos" / source.name).read_bytes(), b"photo")
        self.assertFalse((server.locker.root / source.name).exists())
        self.assertFalse((w.locker.root / source.name).exists())

    def test_sidebar_drop_missing_saved_folder_never_redirects_to_root(self):
        server, _ = self.server()
        w = self.window
        self.save_computer(server, last_folder="missing")
        source = self.root / "camera.jpg"
        source.write_bytes(b"photo")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        self.assertTrue(self.drop_mime_on_view(w.computers, mime, w.computers.item(0)))
        self.wait_for(lambda: not w._locker_busy)
        self.assertIsNone(w._connected_client)
        self.assertIn("failed", w.locker_status.text().lower())
        self.assertFalse((server.locker.root / source.name).exists())
        self.assertFalse((server.locker.root / "missing").exists())
        self.assertEqual(w.profile_store.peers()[0].last_folder, "missing")

    def test_sidebar_drop_to_another_alias_for_same_endpoint_uses_its_saved_folder(self):
        server, _ = self.server()
        for folder in ("Photos", "Documents"):
            (server.locker.root / folder).mkdir()
        w = self.window
        first = self.save_computer(server, last_folder="Photos")
        second = PeerProfile("Work files", first.address, first.port, first.device_id,
                             {"Work": "Documents"}, "Documents")
        w.profile_store.save_peer(second)
        w._update_peers([])
        w.peer_combo.setCurrentIndex(w.peer_combo.findData(f"saved:{first.name}"))
        w._connect_peer()
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual(w.remote_relative, "Photos")
        source = self.root / "report.txt"
        source.write_text("work")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        item = next(w.computers.item(index) for index in range(w.computers.count())
                    if w.computers.item(index).data(Qt.ItemDataRole.UserRole) == f"saved:{second.name}")
        self.assertTrue(self.drop_mime_on_view(w.computers, mime, item))
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual((server.locker.root / "Documents" / source.name).read_text(), "work")
        self.assertFalse((server.locker.root / "Photos" / source.name).exists())
        self.assertEqual(w.remote_relative, "Documents")
        self.assertGreater(w.remote_folders.findText("Work"), 0)

    def test_cancelled_sidebar_connect_does_not_send_on_later_reconnect(self):
        server, _ = self.server()
        w = self.window
        self.save_computer(server)
        source = self.root / "cancelled.jpg"
        source.write_bytes(b"photo")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        entered = threading.Event()
        release = threading.Event()
        original_info = PeerClient.info

        def delayed_info(client):
            entered.set()
            release.wait(2)
            return original_info(client)

        with patch.object(PeerClient, "info", delayed_info):
            try:
                self.assertTrue(self.drop_mime_on_view(w.computers, mime, w.computers.item(0)))
                self.assertTrue(entered.wait(2))
                w._cancel_locker_transfer()
            finally:
                release.set()
            self.wait_for(lambda: not w._locker_busy)
        self.assertIsNone(w._pending_drop)
        w._connect_peer()
        self.wait_for(lambda: not w._locker_busy)
        self.assertIsNotNone(w._connected_client, w.locker_status.text())
        self.assertFalse((server.locker.root / source.name).exists())

    def test_upload_only_computer_can_browse_and_send_into_inbox(self):
        entry, token = new_computer_permission("My laptop", [], ["Inbox/**"])
        config = LockerConfig(
            "remote-test", "Remote test", str(self.root / "remote"), hash_access_code("654321"), "654321",
            computer_permissions=[entry], computer_access_enabled=True,
        )
        server, _ = self.server(config)
        (server.locker.root / "Inbox").mkdir()
        (server.locker.root / "Inbox" / "private.jpg").write_bytes(b"private")
        (server.locker.root / "private.txt").write_bytes(b"private")
        w = self.window
        (w.locker.root / "photo.jpg").write_bytes(b"photo")
        w._refresh_local_locker()
        self.connect_server(server, token)
        self.assertEqual(set(self.tree_items(w.remote_tree)), {"Inbox"})
        self.select_paths(w.remote_tree, ["Inbox"])
        self.assertFalse(w.download_peer.isEnabled())
        w._remote_open(self.tree_items(w.remote_tree)["Inbox"])
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual(w.remote_relative, "Inbox")
        self.assertEqual(w.remote_tree.topLevelItemCount(), 0)
        self.select_paths(w.local_tree, ["photo.jpg"])
        self.assertTrue(w.upload_peer.isEnabled())
        self.assertTrue(w.remote_drop.isEnabled())
        self.assertFalse(w.download_peer.isEnabled())
        QTest.mouseClick(w.upload_peer, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual((server.locker.root / "Inbox" / "photo.jpg").read_bytes(), b"photo")
        self.assertEqual(w.remote_tree.topLevelItemCount(), 0)

    def test_saved_computer_identity_change_prevents_connection(self):
        server, _ = self.server()
        w = self.window
        profile = self.save_computer(server, device_id="previous-computer")
        w.peer_combo.setCurrentIndex(w.peer_combo.findData(f"saved:{profile.name}"))
        w._connect_peer()
        self.wait_for(lambda: not w._locker_busy)
        self.assertIsNone(w._connected_client)
        self.assertIn("different computer", w.locker_status.text())
        self.assertEqual(w.profile_store.peers()[0].device_id, "previous-computer")
        self.assertFalse(w.remote_drop.isEnabled())

    def test_successful_connection_updates_saved_access_but_rejects_bad_codes(self):
        server, _ = self.server()
        server.config.access_code = "987654"
        server.config.access_code_hash = hash_access_code("987654")
        w = self.window
        profile = self.save_computer(server)
        w.peer_combo.setCurrentIndex(w.peer_combo.findData(f"saved:{profile.name}"))
        w.peer_code.setText("000000")
        w._connect_peer()
        self.wait_for(lambda: not w._locker_busy)
        self.assertIsNone(w._connected_client)
        w.secret_store.set.assert_not_called()
        w.peer_code.setText("987654")
        w._connect_peer()
        self.wait_for(lambda: not w._locker_busy)
        self.assertIsNotNone(w._connected_client, w.locker_status.text())
        w.secret_store.set.assert_called_once_with(profile.secret_id, "987654")

    def test_poll_rejects_changed_computer_identity_without_repinning_saved_alias(self):
        server, _ = self.server()
        w = self.window
        profile = self.save_computer(server)
        w.peer_combo.setCurrentIndex(w.peer_combo.findData(f"saved:{profile.name}"))
        w._connect_peer()
        self.wait_for(lambda: not w._locker_busy)
        self.assertEqual(w.profile_store.peers()[0].device_id, "remote-test")
        server.config.device_id = "replacement-computer"
        w._poll_lockers()
        self.wait_for(lambda: not w._refreshing)
        self.assertEqual(w.profile_store.peers()[0].device_id, "remote-test")
        self.assertIsNone(w._connected_client)
        self.assertFalse(w.remote_drop.isEnabled())

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
