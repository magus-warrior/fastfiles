import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent
    from PySide6.QtWidgets import QApplication, QListWidgetItem, QTreeWidgetItem

    from fastfiles.locker_widgets import ITEMS_MIME_TYPE, ComputerList, LockerTree, decode_items

    HAS_UI = True
except ImportError:
    HAS_UI = False


@unittest.skipUnless(HAS_UI, "Install .[desktop] to run drag and drop checks")
class LockerWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.photo = Path(self.temp.name) / "a photo.jpg"
        self.photo.write_bytes(b"photo")
        self.folder = Path(self.temp.name) / "Vacation"
        self.folder.mkdir()

    def show(self, widget):
        widget.resize(400, 300)
        widget.show()
        self.app.processEvents()
        self.addCleanup(self.dispose_widget, widget)
        return widget

    def dispose_widget(self, widget):
        # Close only hides the widget. Delete its native timers on the GUI
        # thread before later HTTP tests trigger Python GC on worker threads.
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def tree(self, side="local"):
        tree = LockerTree(side)
        tree.drag_scope = "window-one"
        tree.directory = "Inbox"
        tree.connection_id = "server-one" if side == "remote" else ""
        folder = QTreeWidgetItem(["Photos"])
        folder.setData(0, Qt.UserRole, "Inbox/Photos")
        folder.setData(0, Qt.UserRole + 1, True)
        tree.addTopLevelItem(folder)
        photo = QTreeWidgetItem(["image.jpg"])
        photo.setData(0, Qt.UserRole, "Inbox/image.jpg")
        photo.setData(0, Qt.UserRole + 1, False)
        tree.addTopLevelItem(photo)
        return self.show(tree)

    def computers(self):
        computers = ComputerList()
        computers.drag_scope = "window-one"
        computers.addItem(QListWidgetItem("Computers"))
        server = QListWidgetItem("Home server")
        server.setData(Qt.UserRole, "home:47832")
        computers.addItem(server)
        return self.show(computers)

    def external(self, paths=None):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in (paths or [self.photo])])
        return mime

    def internal(self, side="local", **overrides):
        payload = {"side": side, "connection_id": "server-one" if side == "remote" else "",
                   "scope": "window-one", "items": [{"path": "Trip/photo.jpg", "is_dir": False}]}
        payload.update(overrides)
        mime = QMimeData()
        mime.setData(ITEMS_MIME_TYPE, json.dumps(payload).encode())
        return mime

    def enter(self, widget, mime, position=None, actions=None):
        event = QDragEnterEvent(
            position or QPoint(10, 10), actions or (Qt.CopyAction | Qt.MoveAction), mime,
            Qt.LeftButton, Qt.NoModifier,
        )
        self.app.sendEvent(widget.viewport(), event)
        return event

    def drop(self, widget, mime, position=None, actions=None):
        position = position or QPoint(10, 10)
        self.enter(widget, mime, position, actions)
        event = QDropEvent(QPointF(position), actions or (Qt.CopyAction | Qt.MoveAction), mime,
                           Qt.LeftButton, Qt.NoModifier)
        self.app.sendEvent(widget.viewport(), event)
        return event

    def test_external_files_and_folders_copy_to_hovered_folder(self):
        tree = self.tree()
        received = []
        tree.paths_dropped.connect(lambda *args: received.append(args))
        mime = self.external([self.photo, self.folder, self.photo])
        point = tree.visualItemRect(tree.topLevelItem(0)).center()
        event = self.drop(tree, mime, point)
        self.assertTrue(event.isAccepted())
        self.assertEqual(event.dropAction(), Qt.CopyAction)
        self.assertEqual(received, [([str(self.photo), str(self.folder)], "Inbox/Photos")])
        self.assertTrue(self.photo.exists())
        self.assertTrue(self.folder.is_dir())
        self.assertIsNone(tree._drop_highlight)

    def test_file_and_blank_space_use_current_directory(self):
        tree = self.tree("remote")
        received = []
        tree.paths_dropped.connect(lambda *args: received.append(args))
        for point in (tree.visualItemRect(tree.topLevelItem(1)).center(), QPoint(50, 200)):
            self.assertTrue(self.drop(tree, self.external(), point).isAccepted())
        self.assertEqual(received, [([str(self.photo)], "Inbox"), ([str(self.photo)], "Inbox")])

    def test_internal_drag_copies_between_panes_with_source_connection(self):
        for source, target in (("local", "remote"), ("remote", "local")):
            tree = self.tree(target)
            received = []
            tree.items_dropped.connect(lambda *args: received.append(args))
            event = self.drop(tree, self.internal(source), QPoint(50, 200))
            self.assertTrue(event.isAccepted())
            self.assertEqual(event.dropAction(), Qt.CopyAction)
            self.assertEqual(received, [(source, [{"path": "Trip/photo.jpg", "is_dir": False}],
                                        "Inbox", "server-one" if source == "remote" else "")])
            self.assertEqual(tree.topLevelItemCount(), 2)

    def test_internal_drag_rejects_same_side_and_other_window(self):
        for side in ("local", "remote"):
            tree = self.tree(side)
            self.assertFalse(self.drop(tree, self.internal(side)).isAccepted())
            other = "local" if side == "remote" else "remote"
            self.assertFalse(self.drop(tree, self.internal(other, scope="other-window")).isAccepted())
        tree.drag_scope = ""
        self.assertFalse(self.drop(tree, self.internal("local", scope="")).isAccepted())

    def test_malformed_internal_payloads_cannot_fall_back_to_urls(self):
        tree = self.tree("remote")
        payloads = [
            None, [], {}, {"side": "local"},
            {"side": "unknown", "connection_id": "", "scope": "window-one", "items": []},
        ]
        for payload in payloads:
            mime = self.external()
            mime.setData(ITEMS_MIME_TYPE, json.dumps(payload).encode())
            self.assertFalse(self.drop(tree, mime).isAccepted())
        for items in ([], "a", [None], [{"path": "../secret", "is_dir": False}],
                      [{"path": "/secret", "is_dir": False}], [{"path": "a", "is_dir": "false"}],
                      [{"path": "a\\b", "is_dir": False}], [{"path": "", "is_dir": True}]):
            self.assertFalse(self.drop(tree, self.internal(items=items)).isAccepted())
        mime = self.external()
        mime.setData(ITEMS_MIME_TYPE, b"not json")
        self.assertFalse(self.drop(tree, mime).isAccepted())
        self.assertIsNone(decode_items(self.internal("remote", connection_id=""), "window-one"))

    def test_external_remote_missing_and_mixed_urls_are_rejected(self):
        tree = self.tree()
        for urls in ([QUrl("https://example.com/photo.jpg")],
                     [QUrl("file://other-computer/shared/photo.jpg")],
                     [QUrl.fromLocalFile(str(self.photo)), QUrl("https://example.com/photo.jpg")],
                     [QUrl.fromLocalFile(str(self.photo.parent / "missing"))]):
            mime = QMimeData()
            mime.setUrls(urls)
            self.assertFalse(self.drop(tree, mime).isAccepted())
        self.assertFalse(self.drop(tree, QMimeData()).isAccepted())

    def test_disabled_destination_and_move_only_drop_are_rejected(self):
        tree = self.tree()
        tree.drop_enabled = False
        self.assertFalse(self.drop(tree, self.external()).isAccepted())
        tree.drop_enabled = True
        tree.setEnabled(False)
        self.assertFalse(self.drop(tree, self.external()).isAccepted())
        tree.setEnabled(True)
        self.assertFalse(self.drop(tree, self.external(), actions=Qt.MoveAction).isAccepted())

    def test_permission_is_rechecked_at_drop(self):
        tree = self.tree()
        received = []
        tree.paths_dropped.connect(lambda *args: received.append(args))
        mime = self.external()
        self.assertTrue(self.enter(tree, mime).isAccepted())
        tree.drop_enabled = False
        event = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        self.app.sendEvent(tree.viewport(), event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(received, [])
        self.assertIsNone(tree._drop_highlight)

    def test_hover_highlights_destination_without_changing_selection(self):
        tree = self.tree()
        photo = tree.topLevelItem(1)
        photo.setSelected(True)
        folder = tree.topLevelItem(0)
        point = tree.visualItemRect(folder).center()
        mime = self.external()
        self.assertTrue(self.enter(tree, mime, point).isAccepted())
        self.assertEqual(tree._drop_highlight, tree.visualItemRect(folder).adjusted(1, 1, -1, -1))
        self.assertEqual(tree.selectedItems(), [photo])
        self.app.processEvents()
        self.app.sendEvent(tree.viewport(), QDragLeaveEvent())
        self.assertIsNone(tree._drop_highlight)

    def test_start_drag_serializes_multiple_selections_and_only_offers_copy(self):
        tree = self.tree()
        for index in range(tree.topLevelItemCount()):
            tree.topLevelItem(index).setSelected(True)
        with patch("fastfiles.locker_widgets.QDrag") as drag:
            tree.startDrag(Qt.CopyAction | Qt.MoveAction)
            drag.return_value.exec.assert_called_once_with(Qt.CopyAction, Qt.CopyAction)
            mime = drag.return_value.setMimeData.call_args.args[0]
            payload = decode_items(mime, "window-one")
            self.assertEqual(payload["side"], "local")
            self.assertEqual(payload["items"], [{"path": "Inbox/Photos", "is_dir": True},
                                                 {"path": "Inbox/image.jpg", "is_dir": False}])
            self.assertFalse(mime.hasUrls())
        self.assertEqual(tree.topLevelItemCount(), 2)
        self.assertEqual(len(tree.selectedItems()), 2)

    def test_remote_drag_rejects_upload_only_folder_and_mixed_selection(self):
        tree = self.tree("remote")
        folder, photo = tree.topLevelItem(0), tree.topLevelItem(1)
        folder.setData(0, Qt.UserRole + 2, {"can_download": False, "can_upload": True})
        photo.setData(0, Qt.UserRole + 2, {"can_download": True})
        folder.setSelected(True)
        with patch("fastfiles.locker_widgets.QDrag") as drag:
            tree.startDrag(Qt.CopyAction)
            drag.assert_not_called()
            photo.setSelected(True)
            tree.startDrag(Qt.CopyAction)
            drag.assert_not_called()
        self.assertEqual(len(tree.selectedItems()), 2)
        folder.setSelected(False)
        with patch("fastfiles.locker_widgets.QDrag") as drag:
            tree.startDrag(Qt.CopyAction)
            drag.return_value.exec.assert_called_once_with(Qt.CopyAction, Qt.CopyAction)

    def test_remote_drag_accepts_legacy_entries_without_permission_metadata(self):
        tree = self.tree("remote")
        tree.topLevelItem(0).setSelected(True)
        with patch("fastfiles.locker_widgets.QDrag") as drag:
            tree.startDrag(Qt.CopyAction)
            drag.return_value.exec.assert_called_once_with(Qt.CopyAction, Qt.CopyAction)
            mime = drag.return_value.setMimeData.call_args.args[0]
            payload = decode_items(mime, "window-one")
            self.assertEqual(payload["connection_id"], "server-one")
            self.assertEqual(payload["items"], [{"path": "Inbox/Photos", "is_dir": True}])

    def test_computer_accepts_local_items_or_external_paths(self):
        computers = self.computers()
        received = []
        computers.transfer_dropped.connect(lambda *args: received.append(args))
        point = computers.visualItemRect(computers.item(1)).center()
        self.assertTrue(self.drop(computers, self.external(), point).isAccepted())
        self.assertEqual(received[0], ("home:47832", {"paths": [str(self.photo)]}))
        self.assertTrue(self.drop(computers, self.internal(), point).isAccepted())
        self.assertEqual(received[1][0], "home:47832")
        self.assertEqual(received[1][1]["side"], "local")
        self.assertFalse(self.drop(computers, self.internal("remote"), point).isAccepted())
        self.assertFalse(self.drop(computers, self.internal(scope="other-window"), point).isAccepted())
        self.assertEqual(computers.count(), 2)

    def test_computer_rejects_headers_empty_space_and_disabled_rows(self):
        computers = self.computers()
        header = computers.visualItemRect(computers.item(0)).center()
        server = computers.visualItemRect(computers.item(1)).center()
        self.assertFalse(self.drop(computers, self.external(), header).isAccepted())
        self.assertFalse(self.drop(computers, self.external(), QPoint(50, 200)).isAccepted())
        computers.item(1).setFlags(Qt.ItemFlag.NoItemFlags)
        self.assertFalse(self.drop(computers, self.external(), server).isAccepted())
        computers.item(1).setFlags(Qt.ItemFlag.ItemIsEnabled)
        computers.drop_enabled = False
        self.assertFalse(self.drop(computers, self.external(), server).isAccepted())

    def test_computer_drag_can_enter_whitespace_then_highlight_a_computer(self):
        computers = self.computers()
        mime = self.external()
        self.assertTrue(self.enter(computers, mime, QPoint(50, 200)).isAccepted())
        point = computers.visualItemRect(computers.item(1)).center()
        event = QDragMoveEvent(point, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        self.app.sendEvent(computers.viewport(), event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(computers._drop_highlight,
                         computers.visualItemRect(computers.item(1)).adjusted(1, 1, -1, -1))
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
