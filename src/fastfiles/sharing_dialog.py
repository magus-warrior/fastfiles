"""Owner controls for computer keys and folder permissions."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .access import new_computer_permission
from .locker import LockerConfig


class SharingDialog(QDialog):
    def __init__(self, config: LockerConfig, config_path: Path | None = None, parent=None):
        super().__init__(parent)
        self.config = config
        self.config_path = config_path
        self.changed = False
        self._selected_name: str | None = None
        self.setWindowTitle("Who can use my locker")
        self.resize(680, 650)
        layout = QVBoxLayout(self)

        self.mode_label = QLabel()
        self.mode_label.setWordWrap(True)
        layout.addWidget(self.mode_label)
        self.computers = QTreeWidget()
        self.computers.setHeaderLabels(["Computer", "View and download", "Upload"])
        self.computers.setRootIsDecorated(False)
        self.computers.setMinimumHeight(110)
        self.computers.itemSelectionChanged.connect(self._load_selected)
        layout.addWidget(self.computers, 1)

        selection_actions = QHBoxLayout()
        self.new_button = QPushButton("Add computer")
        self.new_button.clicked.connect(self._new_computer)
        self.revoke_button = QPushButton("Revoke access")
        self.revoke_button.clicked.connect(self._revoke)
        selection_actions.addWidget(self.new_button)
        selection_actions.addStretch()
        selection_actions.addWidget(self.revoke_button)
        layout.addLayout(selection_actions)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("For example: My laptop")
        form.addRow("Computer name", self.name_edit)
        self.download_edit = QPlainTextEdit()
        self.upload_edit = QPlainTextEdit()
        for edit in (self.download_edit, self.upload_edit):
            edit.setMaximumHeight(75)
            edit.setPlaceholderText("Leave empty to allow none")
        form.addRow("View and download", self.download_edit)
        form.addRow("Upload", self.upload_edit)
        layout.addLayout(form)

        instructions = QLabel(
            "One folder rule per line. Photos/** includes Photos and its contents; ** includes everything. "
            "Leave a box empty to deny that action. Your locker's sharing exclusions and read-only "
            "setting still apply."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.replace_label = QLabel(
            "Replacing this computer key immediately invalidates its previous key. "
            "Enter the new key on that computer to reconnect."
        )
        self.replace_label.setWordWrap(True)
        layout.addWidget(self.replace_label)
        self.save_button = QPushButton("Create computer key")
        self.save_button.clicked.connect(self._save)
        layout.addWidget(self.save_button)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b42318;")
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.error_label)

        self.key_label = QLabel(
            "Copy this key now and enter it when connecting from that computer. "
            "It is shown only here and cannot be recovered later."
        )
        self.key_label.setWordWrap(True)
        layout.addWidget(self.key_label)
        key_row = QHBoxLayout()
        self.key_field = QLineEdit()
        self.key_field.setReadOnly(True)
        self.key_field.setAccessibleName("New computer key")
        self.copy_button = QPushButton("Copy key")
        self.copy_button.clicked.connect(self._copy_key)
        key_row.addWidget(self.key_field, 1)
        key_row.addWidget(self.copy_button)
        layout.addLayout(key_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(self._clear_key)
        self._refresh()
        self._new_computer()

    def _refresh(self, selected_name: str | None = None) -> None:
        self.computers.blockSignals(True)
        self.computers.clear()
        selected_item = None
        for entry in sorted(self.config.computer_permissions, key=lambda item: item["name"].casefold()):
            item = QTreeWidgetItem([
                entry["name"],
                ", ".join(entry["download_patterns"]) or "None",
                ", ".join(entry["upload_patterns"]) or "None",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, entry["name"])
            for column in range(3):
                item.setToolTip(column, item.text(column))
            self.computers.addTopLevelItem(item)
            if entry["name"] == selected_name:
                selected_item = item
        if selected_item is not None:
            self.computers.setCurrentItem(selected_item)
        self.computers.blockSignals(False)
        if self.config.uses_computer_keys:
            self.mode_label.setText(
                "Computer keys are enabled. Each computer can use only its permitted folders. "
                "The shared pairing code no longer grants access."
            )
        else:
            self.mode_label.setText(
                "Your locker currently uses a shared pairing code. Creating the first computer key "
                "replaces that code for all connections. Create a separate key for each computer you allow."
            )
        self._load_selected()

    def _clear_key(self, *_args) -> None:
        self.key_field.clear()
        self.key_label.hide()
        self.key_field.hide()
        self.copy_button.hide()

    def _new_computer(self) -> None:
        self.computers.clearSelection()
        self._selected_name = None
        self.name_edit.clear()
        self.download_edit.setPlainText("**")
        self.upload_edit.setPlainText("Inbox/**")
        self.save_button.setText("Create computer key")
        self.revoke_button.setEnabled(False)
        self.replace_label.hide()
        self.error_label.clear()
        self._clear_key()
        self.name_edit.setFocus()

    def _load_selected(self) -> None:
        self._clear_key()
        self.error_label.clear()
        items = self.computers.selectedItems()
        if not items:
            self._selected_name = None
            self.revoke_button.setEnabled(False)
            self.replace_label.hide()
            self.save_button.setText("Create computer key")
            return
        name = items[0].data(0, Qt.ItemDataRole.UserRole)
        entry = next(item for item in self.config.computer_permissions if item["name"] == name)
        self._selected_name = name
        self.name_edit.setText(name)
        self.download_edit.setPlainText("\n".join(entry["download_patterns"]))
        self.upload_edit.setPlainText("\n".join(entry["upload_patterns"]))
        self.save_button.setText("Replace key and permissions")
        self.revoke_button.setEnabled(True)
        self.replace_label.show()

    @staticmethod
    def _patterns(edit: QPlainTextEdit) -> list[str]:
        return [line.strip() for line in edit.toPlainText().splitlines() if line.strip()]

    def _persist(self, entries: list[dict]) -> None:
        # The running service captures grants under the same config lock;
        # failed writes roll back before requests can see changed permissions.
        self.config.save_computer_permissions(entries, self.config_path)
        self.changed = True

    def _save(self) -> None:
        self.error_label.clear()
        try:
            entry, token = new_computer_permission(
                self.name_edit.text(), self._patterns(self.download_edit), self._patterns(self.upload_edit)
            )
            entries = [item for item in self.config.computer_permissions if item["name"] != self._selected_name]
            entries.append(entry)
            self._persist(entries)
        except (ValueError, OSError) as error:
            self._show_error(error)
            return
        self._refresh(entry["name"])
        self.key_field.setText(token)
        self.key_label.show()
        self.key_field.show()
        self.copy_button.show()
        self.key_field.selectAll()

    def _revoke(self) -> None:
        if self._selected_name is None:
            return
        try:
            self._persist([
                item for item in self.config.computer_permissions if item["name"] != self._selected_name
            ])
        except (ValueError, OSError) as error:
            self._show_error(error)
            return
        self._refresh()
        self._new_computer()

    def _show_error(self, error: Exception) -> None:
        if isinstance(error, (ValueError, PermissionError)):
            self.error_label.setText(f"Permissions were not saved: {error}")
        else:
            self.error_label.setText("Permissions could not be saved. Check that settings are writable and try again.")

    def _copy_key(self) -> None:
        if self.key_field.text():
            QApplication.clipboard().setText(self.key_field.text())
