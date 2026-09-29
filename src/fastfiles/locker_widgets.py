"""Copy-only drag and drop for locker folders and saved computers."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QMimeData, QRect, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QTreeWidget

from .policy import relative_path

ITEMS_MIME_TYPE = "application/x-fastfiles-items"


def _valid_items(items: object) -> list[dict] | None:
    if not isinstance(items, list) or not items:
        return None
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return None
        if type(item.get("is_dir")) is not bool:
            return None
        try:
            path = relative_path(item["path"])
        except (ValueError, PermissionError):
            return None
        if not path or path != item["path"]:
            return None
        if path not in seen:
            result.append({"path": path, "is_dir": item["is_dir"]})
            seen.add(path)
    return result


def decode_items(mime: QMimeData, scope: str) -> dict | None:
    """Read a well-formed selection belonging to this window's drag scope."""
    if not scope or not mime.hasFormat(ITEMS_MIME_TYPE):
        return None
    encoded = mime.data(ITEMS_MIME_TYPE)
    if encoded.size() > 1024 * 1024:
        return None
    try:
        payload = json.loads(bytes(encoded))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("scope") != scope:
        return None
    if payload.get("side") not in ("local", "remote"):
        return None
    connection_id = payload.get("connection_id")
    if not isinstance(connection_id, str) or (payload["side"] == "remote" and not connection_id):
        return None
    items = _valid_items(payload.get("items"))
    if items is None:
        return None
    return {"side": payload["side"], "connection_id": connection_id, "scope": scope, "items": items}


def _local_paths(mime: QMimeData) -> list[str]:
    if not mime.hasUrls():
        return []
    urls = mime.urls()
    if not urls or any(not url.isLocalFile() or url.host() not in ("", "localhost") for url in urls):
        return []
    paths = list(dict.fromkeys(url.toLocalFile() for url in urls))
    try:
        if any(not path or not (Path(path).is_file() or Path(path).is_dir()) for path in paths):
            return []
    except (OSError, ValueError):
        return []
    return paths


class _CopyDropHighlight:
    """Draw the actual destination without changing the user's selection."""

    _drop_highlight: QRect | None = None

    def _show_destination(self, item=None) -> None:
        self._drop_highlight = (
            self.visualItemRect(item).adjusted(1, 1, -1, -1)
            if item is not None else self.viewport().rect().adjusted(2, 2, -2, -2)
        )
        self.viewport().update()

    def _clear_destination(self) -> None:
        self._drop_highlight = None
        self.viewport().update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._drop_highlight is not None:
            painter = QPainter(self.viewport())
            painter.setPen(QPen(QColor("#2563eb"), 2))
            painter.setBrush(QColor(37, 99, 235, 22))
            painter.drawRoundedRect(self._drop_highlight, 3, 3)
            painter.end()

    def dragLeaveEvent(self, event) -> None:
        self._clear_destination()
        event.accept()

    def _can_drop(self, event) -> bool:
        return bool(self.isEnabled() and self.drop_enabled and event.possibleActions() & Qt.CopyAction)


class LockerTree(_CopyDropHighlight, QTreeWidget):
    paths_dropped = Signal(list, str)
    items_dropped = Signal(str, list, str, str)

    def __init__(self, side: str = "local", parent=None):
        super().__init__(parent)
        self.side = side
        self.directory = ""
        self.connection_id = ""
        self.drag_scope = ""
        self.drop_enabled = True
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDropIndicatorShown(True)
        self.setDragDropOverwriteMode(False)

    def startDrag(self, supported_actions) -> None:
        if not self.isEnabled() or not self.drag_scope or self.side not in ("local", "remote"):
            return
        if self.side == "remote" and not self.connection_id:
            return
        selected = self.selectedItems()
        if self.side == "remote":
            for item in selected:
                entry = item.data(0, Qt.ItemDataRole.UserRole + 2)
                if isinstance(entry, dict) and entry.get("can_download") is False:
                    return
        items = _valid_items([
            {"path": item.data(0, Qt.ItemDataRole.UserRole),
             "is_dir": item.data(0, Qt.ItemDataRole.UserRole + 1)}
            for item in selected
        ])
        if items is None:
            return
        payload = {"side": self.side, "connection_id": self.connection_id,
                   "scope": self.drag_scope, "items": items}
        mime = QMimeData()
        mime.setData(ITEMS_MIME_TYPE, json.dumps(payload).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)

    def _drop_payload(self, event) -> dict | None:
        if not self._can_drop(event):
            return None
        mime = event.mimeData()
        if mime.hasFormat(ITEMS_MIME_TYPE):
            payload = decode_items(mime, self.drag_scope)
            if payload is None or (payload["side"], self.side) not in {
                ("local", "remote"), ("remote", "local")
            }:
                return None
            return payload
        paths = _local_paths(mime)
        return {"paths": paths} if paths else None

    def _destination(self, event) -> tuple[str, object]:
        item = self.itemAt(event.position().toPoint())
        if item is not None and item.data(0, Qt.ItemDataRole.UserRole + 1) is True:
            return item.data(0, Qt.ItemDataRole.UserRole), item
        return self.directory, None

    def dragEnterEvent(self, event) -> None:
        self.dragMoveEvent(event)

    def dragMoveEvent(self, event) -> None:
        if self._drop_payload(event) is None:
            self._clear_destination()
            event.ignore()
            return
        _, item = self._destination(event)
        self._show_destination(item)
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dropEvent(self, event) -> None:
        self._clear_destination()
        payload = self._drop_payload(event)
        if payload is None:
            event.ignore()
            return
        directory, _ = self._destination(event)
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if "paths" in payload:
            self.paths_dropped.emit(payload["paths"], directory)
        else:
            self.items_dropped.emit(payload["side"], payload["items"], directory, payload["connection_id"])


class ComputerList(_CopyDropHighlight, QListWidget):
    transfer_dropped = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.drag_scope = ""
        self.drop_enabled = True
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDropIndicatorShown(True)

    def _target(self, event):
        item = self.itemAt(event.position().toPoint())
        if item is None or not item.flags() & Qt.ItemFlag.ItemIsEnabled:
            return None
        key = item.data(Qt.ItemDataRole.UserRole)
        return item if isinstance(key, str) and key else None

    def _drop_payload(self, event) -> dict | None:
        if not self._can_drop(event) or self._target(event) is None:
            return None
        mime = event.mimeData()
        if mime.hasFormat(ITEMS_MIME_TYPE):
            payload = decode_items(mime, self.drag_scope)
            return payload if payload is not None and payload["side"] == "local" else None
        paths = _local_paths(mime)
        return {"paths": paths} if paths else None

    def dragEnterEvent(self, event) -> None:
        # Entry may be through whitespace before the pointer reaches a computer.
        if not self._can_drop(event):
            event.ignore()
            return
        mime = event.mimeData()
        payload = decode_items(mime, self.drag_scope) if mime.hasFormat(ITEMS_MIME_TYPE) else None
        valid = payload is not None and payload["side"] == "local"
        if not mime.hasFormat(ITEMS_MIME_TYPE):
            valid = bool(_local_paths(mime))
        if valid:
            if self._target(event) is not None:
                self._show_destination(self._target(event))
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._drop_payload(event) is None:
            self._clear_destination()
            event.ignore()
            return
        self._show_destination(self._target(event))
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dropEvent(self, event) -> None:
        self._clear_destination()
        payload = self._drop_payload(event)
        if payload is None:
            event.ignore()
            return
        key = self._target(event).data(Qt.ItemDataRole.UserRole)
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.transfer_dropped.emit(key, payload)
