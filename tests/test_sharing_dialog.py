import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from fastfiles.sharing_dialog import SharingDialog

    HAS_UI = True
except ImportError:
    HAS_UI = False

from fastfiles.access import authenticate_computer
from fastfiles.locker import LockerConfig, hash_access_code


@unittest.skipUnless(HAS_UI, "Install .[desktop] to run UI checks")
class SharingDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.json"
        self.config = LockerConfig(
            "local", "Home", str(self.root / "locker"), hash_access_code("123456"), "123456"
        )
        self.config.save(self.path)
        self.dialog = SharingDialog(self.config, self.path)
        self.dialog.show()
        self.app.processEvents()
        self.addCleanup(self.close_dialog)

    def close_dialog(self):
        self.dialog.close()
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def click(self, button):
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.app.processEvents()

    def create(self, name="Laptop"):
        self.dialog.name_edit.setText(name)
        self.click(self.dialog.save_button)
        self.assertEqual(self.dialog.error_label.text(), "")
        return self.dialog.key_field.text()

    def test_create_persists_only_hash_and_revoke_keeps_computer_access_enabled(self):
        token = self.create()
        self.assertTrue(token.startswith("ff_"))
        self.assertTrue(self.dialog.changed)
        entry = authenticate_computer(token, self.config.computer_permissions)
        self.assertEqual(entry["name"], "Laptop")
        self.assertEqual(entry["download_patterns"], ["**"])
        self.assertEqual(entry["upload_patterns"], ["Inbox/**"])
        saved = self.path.read_text()
        self.assertNotIn(token, saved)
        self.assertEqual(json.loads(saved)["computer_permissions"], self.config.computer_permissions)
        self.click(self.dialog.copy_button)
        self.assertEqual(self.app.clipboard().text(), token)
        self.click(self.dialog.revoke_button)
        self.assertEqual(self.config.computer_permissions, [])
        self.assertTrue(self.config.computer_access_enabled)
        self.assertTrue(LockerConfig.load(self.path).uses_computer_keys)
        self.assertEqual(self.dialog.key_field.text(), "")

    def test_edit_permissions_preserves_key_and_rejects_invalid_rules(self):
        token = self.create()
        self.dialog.name_edit.setText("Studio laptop")
        self.dialog.download_edit.setPlainText("Photos/**")
        self.click(self.dialog.update_button)
        entry = authenticate_computer(token, self.config.computer_permissions)
        self.assertEqual(entry["name"], "Studio laptop")
        self.assertEqual(entry["download_patterns"], ["Photos/**"])
        self.assertEqual(self.dialog.key_field.text(), "")
        before = self.path.read_bytes()
        self.dialog.download_edit.setPlainText("../outside")
        self.click(self.dialog.update_button)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(authenticate_computer(token, self.config.computer_permissions), entry)

    def test_replacing_selected_computer_rotates_key_and_changes_folder_rules(self):
        token = self.create()
        self.assertTrue(self.dialog.replace_label.isVisible())
        self.assertIn("Replace", self.dialog.save_button.text())
        self.dialog.download_edit.setPlainText("Photos/**\nPublic/**")
        self.dialog.upload_edit.clear()
        replacement = self.create()
        self.assertNotEqual(token, replacement)
        self.assertIsNone(authenticate_computer(token, self.config.computer_permissions))
        entry = authenticate_computer(replacement, self.config.computer_permissions)
        self.assertEqual(entry["download_patterns"], ["Photos/**", "Public/**"])
        self.assertEqual(entry["upload_patterns"], [])
        self.assertEqual(len(self.config.computer_permissions), 1)

    def test_invalid_rules_leave_disk_and_running_configuration_unchanged(self):
        before = self.path.read_text()
        self.dialog.name_edit.setText("Laptop")
        self.dialog.download_edit.setPlainText("../outside")
        self.click(self.dialog.save_button)
        self.assertIn("not saved", self.dialog.error_label.text())
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.config.computer_permissions, [])
        self.assertFalse(self.config.computer_access_enabled)
        self.assertFalse(self.dialog.changed)
        self.assertEqual(self.dialog.key_field.text(), "")

    def test_failed_disk_write_does_not_mutate_shared_config_or_reveal_key(self):
        before = self.path.read_text()
        self.dialog.name_edit.setText("Laptop")
        with patch.object(LockerConfig, "save", side_effect=OSError("write failed")):
            self.click(self.dialog.save_button)
        self.assertIn("could not be saved", self.dialog.error_label.text())
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.config.computer_permissions, [])
        self.assertFalse(self.config.computer_access_enabled)
        self.assertFalse(self.dialog.changed)
        self.assertEqual(self.dialog.key_field.text(), "")

    def test_duplicate_computer_names_preserve_original_grant(self):
        original = self.create()
        before = self.path.read_text()
        self.click(self.dialog.new_button)
        self.dialog.name_edit.setText("laptop")
        self.click(self.dialog.save_button)
        self.assertIn("unique", self.dialog.error_label.text())
        self.assertEqual(self.path.read_text(), before)
        self.assertIsNotNone(authenticate_computer(original, self.config.computer_permissions))

    def test_key_is_cleared_on_new_selection_and_close(self):
        self.create()
        self.click(self.dialog.new_button)
        self.assertEqual(self.dialog.key_field.text(), "")
        self.create("Desktop")
        self.dialog.reject()
        self.assertEqual(self.dialog.key_field.text(), "")
