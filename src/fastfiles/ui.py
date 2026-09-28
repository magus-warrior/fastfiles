from __future__ import annotations

import posixpath
import threading
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QLineEdit,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core import (
    Direction,
    TransferRequest,
    build_rsync_command,
    display_command,
    parse_progress,
    read_ssh_aliases,
)
from .locker import (
    Locker, LockerConfig, LockerService, Peer, PeerClient, PeerDiscovery, parse_peer_address,
)
from .profiles import DirectProfile, PeerProfile, ProfileStore, SecretStore

logger = logging.getLogger("fastfiles.ui")


STYLE = """
QMainWindow { background: #f5f7fb; }
QFrame#card {
  background: #ffffff;
  border: 1px solid #dfe3eb;
  border-radius: 10px;
}
QLabel#title { color: #172033; font-size: 30px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #667085; }
QLabel#section { color: #263247; font-weight: 600; }
QTabWidget::pane { border: 0; }
QTabBar::tab { min-width: 110px; padding: 14px 22px; }
QTreeWidget, QListWidget, QPlainTextEdit {
  background: #ffffff;
  border: 1px solid #dfe3eb;
  border-radius: 6px;
}
QTreeWidget::item, QListWidget::item { padding: 7px 5px; }
QHeaderView::section {
  background: #f5f7fb;
  color: #667085;
  border: 0;
  border-bottom: 1px solid #dfe3eb;
  padding: 8px;
  font-weight: 600;
}
QPushButton#primary { font-weight: 700; padding: 11px 24px; }
QPushButton#danger { color: #b42318; }
QProgressBar { height: 8px; border-radius: 4px; text-align: center; color: transparent; }
QProgressBar::chunk { border-radius: 4px; }
"""


class LockerSignals(QObject):
    peers = Signal(object)
    remote_items = Signal(object)
    operation_done = Signal(str)
    operation_error = Signal(str)
    progress = Signal(int)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FastFiles")
        self.resize(1040, 820)
        self.setMinimumSize(820, 700)
        self._process: QProcess | None = None
        self._output_buffer = ""
        self._local_paths: list[str] = []
        self.profile_store = ProfileStore()
        self.secret_store = SecretStore()
        self.locker_config = LockerConfig.load()
        self.locker = Locker(
            self.locker_config.locker_path,
            self.locker_config.allow_patterns,
            self.locker_config.deny_patterns,
        )
        self.locker_service: LockerService | None = None
        try:
            self.locker_service = LockerService(self.locker_config)
            self.locker_service.start()
        except OSError:
            # An always-on `fastfiles --serve` process may already own the port.
            self.locker_service = None
        self.locker_signals = LockerSignals()
        self.peers: dict[str, Peer] = {}
        self.local_relative = ""
        self.remote_relative = ""
        self._build_ui()
        self._set_direction(Direction.SEND)
        self.locker_signals.peers.connect(self._update_peers)
        self.locker_signals.remote_items.connect(self._show_remote_items)
        self.locker_signals.operation_done.connect(self._locker_done)
        self.locker_signals.operation_error.connect(self._locker_error)
        self.locker_signals.progress.connect(self.locker_progress.setValue)
        self.discovery = PeerDiscovery(self.locker_config.device_id, self.locker_signals.peers.emit)
        self._update_peers([])
        self._refresh_local_locker()

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        locker_page = self._build_locker_page()
        direct_page = QWidget()
        tabs.addTab(locker_page, "Locker")
        tabs.addTab(direct_page, "Direct SSH")
        root_layout.addWidget(tabs)

        outer = QVBoxLayout(direct_page)
        outer.setContentsMargins(40, 30, 40, 30)
        outer.setSpacing(16)

        title = QLabel("FastFiles")
        title.setObjectName("title")
        subtitle = QLabel("Secure, resumable transfers over your existing SSH setup")
        subtitle.setObjectName("subtitle")
        outer.addWidget(title)
        outer.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)

        profiles = QHBoxLayout()
        profiles.addWidget(self._label("Saved machine"))
        self.direct_profile = QComboBox()
        self.direct_profile.setPlaceholderText("Choose a saved machine")
        self.save_direct_profile = QPushButton("Save as…")
        self.delete_direct_profile = QPushButton("Delete")
        profiles.addWidget(self.direct_profile, 1)
        profiles.addWidget(self.save_direct_profile)
        profiles.addWidget(self.delete_direct_profile)
        layout.addLayout(profiles)

        modes = QHBoxLayout()
        self.send_radio = QRadioButton("Send to remote")
        self.receive_radio = QRadioButton("Receive from remote")
        self.send_radio.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.send_radio)
        group.addButton(self.receive_radio)
        modes.addWidget(self.send_radio)
        modes.addWidget(self.receive_radio)
        modes.addStretch()
        layout.addLayout(modes)

        layout.addWidget(self._label("Remote host"))
        self.host = QComboBox()
        self.host.setEditable(True)
        self.host.setPlaceholderText("server, user@server, or SSH alias")
        self.host.addItems(read_ssh_aliases())
        layout.addWidget(self.host)

        layout.addWidget(self._label("Remote path"))
        self.remote_path = QComboBox()
        self.remote_path.setEditable(True)
        self.remote_path.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.remote_path.setPlaceholderText("~/Downloads/")
        self.remote_path.addItems(["~/Downloads/", "~/Desktop/", "~/"])
        layout.addWidget(self.remote_path)

        path_header = QHBoxLayout()
        self.local_label = self._label("Local files")
        path_header.addWidget(self.local_label)
        path_header.addStretch()
        self.pick_files = QPushButton("Add files")
        self.pick_folder = QPushButton("Add folder")
        path_header.addWidget(self.pick_files)
        path_header.addWidget(self.pick_folder)
        layout.addLayout(path_header)

        self.paths = QListWidget()
        self.paths.setMinimumHeight(100)
        self.paths.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.paths.setToolTip("Select entries and press Delete to remove them")
        layout.addWidget(self.paths)

        options = QHBoxLayout()
        self.compress = QCheckBox("Compress")
        self.compress.setChecked(True)
        self.archive = QCheckBox("Preserve metadata")
        self.archive.setChecked(True)
        self.partial = QCheckBox("Resume partial")
        self.partial.setChecked(True)
        self.dry_run = QCheckBox("Dry run")
        for widget in (self.compress, self.archive, self.partial, self.dry_run):
            options.addWidget(widget)
        layout.addLayout(options)
        outer.addWidget(card)

        progress_card = QFrame()
        progress_card.setObjectName("card")
        progress_layout = QVBoxLayout(progress_card)
        progress_layout.setContentsMargins(22, 18, 22, 18)
        self.status = QLabel("Ready")
        self.status.setObjectName("section")
        self.stats = QLabel("Choose what you want to transfer")
        self.stats.setObjectName("muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        progress_layout.addWidget(self.status)
        progress_layout.addWidget(self.stats)
        progress_layout.addWidget(self.progress)
        outer.addWidget(progress_card)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setPlaceholderText("Transfer details will appear here")
        self.log.setMaximumHeight(125)
        outer.addWidget(self.log)

        actions = QHBoxLayout()
        self.cancel = QPushButton("Cancel")
        self.cancel.setObjectName("danger")
        self.cancel.setEnabled(False)
        self.start = QPushButton("Start transfer")
        self.start.setObjectName("primary")
        actions.addWidget(self.cancel)
        actions.addStretch()
        actions.addWidget(self.start)
        outer.addLayout(actions)

        self.setCentralWidget(root)
        self.setStyleSheet(STYLE)

        self.send_radio.toggled.connect(lambda checked: checked and self._set_direction(Direction.SEND))
        self.receive_radio.toggled.connect(lambda checked: checked and self._set_direction(Direction.RECEIVE))
        self.pick_files.clicked.connect(self._choose_files)
        self.pick_folder.clicked.connect(self._choose_folder)
        self.start.clicked.connect(self._start_transfer)
        self.cancel.clicked.connect(self._cancel_transfer)
        self.direct_profile.currentIndexChanged.connect(self._load_direct_profile)
        self.save_direct_profile.clicked.connect(self._save_direct_profile)
        self.delete_direct_profile.clicked.connect(self._delete_direct_profile)
        self._refresh_direct_profiles()

    def _build_locker_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(40, 30, 40, 30)
        outer.setSpacing(16)
        title = QLabel("Locker")
        title.setObjectName("title")
        subtitle = QLabel("Browse an approved folder on each computer and move files either way")
        subtitle.setObjectName("subtitle")
        outer.addWidget(title)
        outer.addWidget(subtitle)

        connection = QFrame()
        connection.setObjectName("card")
        connection_layout = QHBoxLayout(connection)
        connection_layout.setContentsMargins(18, 14, 18, 14)
        own = QLabel(f"This device: {self.locker_config.device_name}  •  Pairing code: {self.locker_config.access_code}")
        own.setObjectName("section")
        connection_layout.addWidget(own)
        connection_layout.addStretch()
        self.peer_combo = QComboBox()
        self.peer_combo.setEditable(True)
        self.peer_combo.setMinimumWidth(180)
        self.peer_combo.setPlaceholderText("Peer or VPN address")
        self.peer_code = QLineEdit()
        self.peer_code.setPlaceholderText("Peer code")
        self.peer_code.setMaxLength(6)
        self.peer_code.setMaximumWidth(110)
        self.connect_peer = QPushButton("Connect")
        self.save_peer = QPushButton("Save as…")
        connection_layout.addWidget(self.peer_combo)
        connection_layout.addWidget(self.peer_code)
        connection_layout.addWidget(self.connect_peer)
        connection_layout.addWidget(self.save_peer)
        outer.addWidget(connection)

        browsers = QHBoxLayout()
        browsers.setSpacing(16)
        local_card, self.local_tree, self.local_path_label, local_back = self._browser_card("My locker")
        remote_card, self.remote_tree, self.remote_path_label, remote_back = self._browser_card("Peer locker")
        browsers.addWidget(local_card)
        browsers.addWidget(remote_card)
        outer.addLayout(browsers, 1)

        actions = QHBoxLayout()
        self.download_peer = QPushButton("← Copy to my locker")
        self.upload_peer = QPushButton("Copy to peer →")
        self.download_peer.setEnabled(False)
        self.upload_peer.setEnabled(False)
        self.open_locker = QPushButton("Open my locker")
        actions.addWidget(self.open_locker)
        actions.addStretch()
        actions.addWidget(self.download_peer)
        actions.addWidget(self.upload_peer)
        outer.addLayout(actions)
        self.locker_status = QLabel(f"Sharing {self.locker.root}")
        self.locker_status.setObjectName("muted")
        self.locker_progress = QProgressBar()
        self.locker_progress.setRange(0, 100)
        self.locker_progress.setValue(0)
        outer.addWidget(self.locker_progress)
        outer.addWidget(self.locker_status)

        self.connect_peer.clicked.connect(self._connect_peer)
        self.save_peer.clicked.connect(self._save_peer_profile)
        self.peer_combo.currentIndexChanged.connect(self._peer_changed)
        local_back.clicked.connect(self._local_back)
        remote_back.clicked.connect(self._remote_back)
        self.local_tree.itemDoubleClicked.connect(self._local_open)
        self.remote_tree.itemDoubleClicked.connect(self._remote_open)
        self.upload_peer.clicked.connect(self._upload_selected)
        self.download_peer.clicked.connect(self._download_selected)
        self.open_locker.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.locker.root)))
        )
        return page

    def _refresh_direct_profiles(self, selected: str = "") -> None:
        self.direct_profile.blockSignals(True)
        self.direct_profile.clear()
        self.direct_profile.addItem("Choose a saved machine", "")
        for profile in self.profile_store.direct():
            self.direct_profile.addItem(profile.name, profile.name)
        index = self.direct_profile.findData(selected)
        self.direct_profile.setCurrentIndex(max(index, 0))
        self.direct_profile.blockSignals(False)

    def _load_direct_profile(self) -> None:
        name = self.direct_profile.currentData()
        profile = next((item for item in self.profile_store.direct() if item.name == name), None)
        if profile:
            self.host.setCurrentText(profile.host)
            self.remote_path.setCurrentText(profile.remote_path)

    def _save_direct_profile(self) -> None:
        name, accepted = QInputDialog.getText(self, "Save machine", "Alias")
        if not accepted or not name.strip():
            return
        profile = DirectProfile(name.strip(), self.host.currentText().strip(), self.remote_path.currentText().strip())
        if not profile.host:
            QMessageBox.warning(self, "Save machine", "Enter a host before saving this machine")
            return
        self.profile_store.save_direct(profile)
        self._refresh_direct_profiles(profile.name)

    def _delete_direct_profile(self) -> None:
        name = self.direct_profile.currentData()
        if name:
            self.profile_store.delete_direct(name)
            self._refresh_direct_profiles()

    def _save_peer_profile(self) -> None:
        peer = self._selected_peer()
        if not peer:
            self._locker_error("Enter or select a peer before saving it")
            return
        name, accepted = QInputDialog.getText(self, "Save peer", "Alias", text=peer.name)
        if not accepted or not name.strip():
            return
        profile = PeerProfile(name.strip(), peer.address, peer.port)
        self.profile_store.save_peer(profile)
        code = self.peer_code.text().strip()
        if code:
            try:
                self.secret_store.set(profile.secret_id, code)
            except Exception as error:
                QMessageBox.warning(self, "Save peer", f"Saved the peer, but the system keyring rejected its code: {error}")
        self._update_peers(list(self.peers.values()))

    def _browser_card(self, title: str) -> tuple[QFrame, QTreeWidget, QLabel, QPushButton]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        header = QHBoxLayout()
        label = self._label(title)
        back = QPushButton("Up")
        back.setMaximumWidth(55)
        header.addWidget(label)
        header.addStretch()
        header.addWidget(back)
        path = QLabel("/")
        path.setObjectName("muted")
        tree = QTreeWidget()
        tree.setHeaderLabels(["Name", "Size"])
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        layout.addLayout(header)
        layout.addWidget(path)
        layout.addWidget(tree)
        return card, tree, path, back

    @staticmethod
    def _human_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return ""

    def _fill_tree(self, tree: QTreeWidget, items: list[dict]) -> None:
        tree.clear()
        for entry in items:
            size = "Folder" if entry["is_dir"] else self._human_size(entry["size"])
            item = QTreeWidgetItem([entry["name"], size])
            item.setData(0, Qt.ItemDataRole.UserRole, entry["path"])
            item.setData(0, Qt.ItemDataRole.UserRole + 1, entry["is_dir"])
            tree.addTopLevelItem(item)

    def _refresh_local_locker(self) -> None:
        try:
            self._fill_tree(self.local_tree, self.locker.list(self.local_relative))
            self.local_path_label.setText("/" + self.local_relative)
        except OSError as error:
            self._locker_error(str(error))

    def _update_peers(self, peers: list[Peer]) -> None:
        current_id = self.peer_combo.currentData()
        current_text = self.peer_combo.currentText()
        discovered = {peer.device_id: peer for peer in peers if not peer.device_id.startswith("saved:")}
        for profile in self.profile_store.peers():
            saved = Peer(f"saved:{profile.name}", profile.name, profile.address, profile.port)
            discovered[saved.device_id] = saved
        self.peers = discovered
        self.peer_combo.blockSignals(True)
        self.peer_combo.clear()
        for peer in sorted(self.peers.values(), key=lambda value: value.name.casefold()):
            self.peer_combo.addItem(f"{peer.name} ({peer.address})", peer.device_id)
        if current_id:
            index = self.peer_combo.findData(current_id)
            if index >= 0:
                self.peer_combo.setCurrentIndex(index)
        elif current_text:
            self.peer_combo.setEditText(current_text)
        self.peer_combo.blockSignals(False)
        if not self.peers:
            self.remote_tree.clear()
            self.upload_peer.setEnabled(False)
            self.download_peer.setEnabled(False)

    def _peer_changed(self) -> None:
        self.remote_relative = ""
        self.remote_tree.clear()
        self.upload_peer.setEnabled(False)
        self.download_peer.setEnabled(False)
        self.locker_status.setText("Enter the pairing code shown on the other computer")
        peer = self._selected_peer()
        if peer:
            secret_id = f"peer:{peer.address}:{peer.port}"
            remembered = self.secret_store.get(secret_id)
            if remembered:
                self.peer_code.setText(remembered)

    def _selected_peer(self) -> Peer | None:
        peer = self.peers.get(self.peer_combo.currentData())
        selected_text = self.peer_combo.itemText(self.peer_combo.currentIndex())
        if peer and self.peer_combo.currentText() == selected_text:
            return peer
        text = self.peer_combo.currentText().strip()
        return parse_peer_address(text) if text else None

    def _client(self) -> PeerClient:
        peer = self._selected_peer()
        if not peer:
            raise ValueError("Select a discovered peer first")
        code = self.peer_code.text().strip()
        if len(code) != 6 or not code.isdigit():
            raise ValueError("Enter the peer's six-digit pairing code")
        return PeerClient(peer, code)

    def _run_locker_task(self, task) -> None:  # type: ignore[no-untyped-def]
        def run() -> None:
            try:
                task()
            except Exception as error:
                self.locker_signals.operation_error.emit(str(error))
        threading.Thread(target=run, name="fastfiles-operation", daemon=True).start()

    def _connect_peer(self) -> None:
        try:
            client = self._client()
        except ValueError as error:
            self._locker_error(str(error))
            return
        self.locker_status.setText(f"Connecting to {client.peer.name}…")
        logger.info("peer connection starting name=%s endpoint=%s", client.peer.name, client.peer.base_url)
        self.connect_peer.setEnabled(False)

        def task() -> None:
            info = client.info()
            if info.get("protocol") != 1:
                raise OSError("The peer uses an incompatible FastFiles protocol")
            items = client.list("")
            self.locker_signals.remote_items.emit(("", items))
        self._run_locker_task(task)

    def _load_remote(self, relative: str) -> None:
        try:
            client = self._client()
        except ValueError as error:
            self._locker_error(str(error))
            return
        self.locker_status.setText("Loading peer locker…")

        def task() -> None:
            self.locker_signals.remote_items.emit((relative, client.list(relative)))
        self._run_locker_task(task)

    def _show_remote_items(self, result: tuple[str, list[dict]]) -> None:
        relative, items = result
        self.remote_relative = relative
        self.remote_path_label.setText("/" + relative)
        self._fill_tree(self.remote_tree, items)
        self.connect_peer.setEnabled(True)
        self.upload_peer.setEnabled(True)
        self.download_peer.setEnabled(True)
        peer = self._selected_peer()
        self.locker_status.setText(f"Connected to {peer.name}" if peer else "Connected")

    def _local_open(self, item: QTreeWidgetItem) -> None:
        if item.data(0, Qt.ItemDataRole.UserRole + 1):
            self.local_relative = item.data(0, Qt.ItemDataRole.UserRole)
            self._refresh_local_locker()

    def _remote_open(self, item: QTreeWidgetItem) -> None:
        if item.data(0, Qt.ItemDataRole.UserRole + 1):
            self._load_remote(item.data(0, Qt.ItemDataRole.UserRole))

    def _local_back(self) -> None:
        self.local_relative = posixpath.dirname(self.local_relative)
        self._refresh_local_locker()

    def _remote_back(self) -> None:
        self._load_remote(posixpath.dirname(self.remote_relative))

    def _upload_selected(self) -> None:
        item = self.local_tree.currentItem()
        if item is None:
            self._locker_error("Select a file or folder in your locker")
            return
        try:
            client = self._client()
        except ValueError as error:
            self._locker_error(str(error))
            return
        relative = item.data(0, Qt.ItemDataRole.UserRole)
        source = self.locker.resolve(relative)
        self.locker_status.setText(f"Sending {source.name}…")
        logger.info("locker send requested source=%s remote_directory=%s", source, self.remote_relative or "/")
        self.locker_progress.setValue(0)

        def progress(done: int, total: int) -> None:
            self.locker_signals.progress.emit(int(done * 100 / total) if total else 100)

        def task() -> None:
            if source.is_dir():
                for child in source.rglob("*"):
                    if child.is_file():
                        suffix = child.relative_to(source.parent).as_posix()
                        client.upload(child, posixpath.join(self.remote_relative, suffix), progress)
            else:
                client.upload(source, posixpath.join(self.remote_relative, source.name), progress)
            self.locker_signals.operation_done.emit(f"Sent {source.name}")
        self._run_locker_task(task)

    def _download_selected(self) -> None:
        item = self.remote_tree.currentItem()
        if item is None:
            self._locker_error("Select a file or folder in the peer locker")
            return
        try:
            client = self._client()
        except ValueError as error:
            self._locker_error(str(error))
            return
        relative = item.data(0, Qt.ItemDataRole.UserRole)
        is_dir = item.data(0, Qt.ItemDataRole.UserRole + 1)
        destination = self.locker.resolve(self.local_relative) / posixpath.basename(relative)
        self.locker_status.setText(f"Receiving {posixpath.basename(relative)}…")
        logger.info("locker receive requested remote=%s destination=%s", relative, destination)
        self.locker_progress.setValue(0)

        def progress(done: int, total: int) -> None:
            self.locker_signals.progress.emit(int(done * 100 / total) if total else 100)

        def download_tree(remote: str, local: Path) -> None:
            local.mkdir(parents=True, exist_ok=True)
            for entry in client.list(remote):
                child = local / entry["name"]
                if entry["is_dir"]:
                    download_tree(entry["path"], child)
                else:
                    client.download(entry["path"], child, progress)

        def task() -> None:
            if is_dir:
                download_tree(relative, destination)
            else:
                client.download(relative, destination, progress)
            self.locker_signals.operation_done.emit(f"Received {destination.name}")
        self._run_locker_task(task)

    def _locker_done(self, message: str) -> None:
        logger.info("locker operation successful result=%s", message)
        self.locker_status.setText(message)
        self.locker_progress.setValue(100)
        self._refresh_local_locker()
        if self._selected_peer():
            self._load_remote(self.remote_relative)

    def _locker_error(self, message: str) -> None:
        logger.error("locker operation failed error=%s", message)
        self.connect_peer.setEnabled(True)
        self.locker_status.setText(message)
        QMessageBox.warning(self, "Locker", message)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("section")
        return label

    @property
    def direction(self) -> Direction:
        return Direction.SEND if self.send_radio.isChecked() else Direction.RECEIVE

    def _set_direction(self, direction: Direction) -> None:
        self._local_paths.clear()
        self.paths.clear()
        receiving = direction is Direction.RECEIVE
        self.local_label.setText("Local destination" if receiving else "Local files and folders")
        self.pick_files.setVisible(not receiving)
        self.pick_folder.setText("Choose destination" if receiving else "Add folder")
        self.remote_path.setPlaceholderText("~/path/to/file-or-folder" if receiving else "~/Downloads/")
        self.start.setText("Receive files" if receiving else "Send files")

    def _choose_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Choose files to send")
        self._add_paths(files)

    def _choose_folder(self) -> None:
        title = "Choose local destination" if self.direction is Direction.RECEIVE else "Choose folder to send"
        folder = QFileDialog.getExistingDirectory(self, title)
        if not folder:
            return
        if self.direction is Direction.RECEIVE:
            self._local_paths = [folder]
            self.paths.clear()
            self.paths.addItem(folder)
        else:
            self._add_paths([folder])

    def _add_paths(self, paths: list[str]) -> None:
        for path in paths:
            if path not in self._local_paths:
                self._local_paths.append(path)
                self.paths.addItem(path)

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key() == Qt.Key.Key_Delete and self.direction is Direction.SEND:
            for item in self.paths.selectedItems():
                self._local_paths.remove(item.text())
                self.paths.takeItem(self.paths.row(item))
            return
        super().keyPressEvent(event)

    def _request(self) -> TransferRequest:
        return TransferRequest(
            direction=self.direction,
            host=self.host.currentText(),
            remote_path=self.remote_path.currentText(),
            local_paths=tuple(self._local_paths),
            archive=self.archive.isChecked(),
            compress=self.compress.isChecked(),
            partial=self.partial.isChecked(),
            dry_run=self.dry_run.isChecked(),
        )

    def _start_transfer(self) -> None:
        try:
            command = build_rsync_command(self._request())
        except ValueError as error:
            QMessageBox.warning(self, "Cannot start transfer", str(error))
            return
        if self._process is not None:
            return

        self.log.clear()
        self.log.appendPlainText("$ " + display_command(command))
        logger.info("direct transfer starting command=%s", display_command(command))
        self.status.setText("Connecting…")
        self.stats.setText("Waiting for rsync")
        self.progress.setValue(0)
        self._set_running(True)
        self._output_buffer = ""

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_output)
        process.started.connect(lambda: self.status.setText("Transferring…"))
        process.errorOccurred.connect(self._process_error)
        process.finished.connect(self._process_finished)
        self._process = process
        process.start(command[0], command[1:])

    def _read_output(self) -> None:
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardOutput()).decode(errors="replace")
        self._output_buffer += text
        chunks = self._output_buffer.replace("\r", "\n").split("\n")
        self._output_buffer = chunks.pop()
        for line in chunks:
            clean = line.strip()
            if not clean:
                continue
            parsed = parse_progress(clean)
            if parsed:
                self.progress.setValue(parsed.percent)
                self.stats.setText(f"{parsed.transferred}  •  {parsed.speed}  •  ETA {parsed.eta}")
            else:
                self.log.appendPlainText(clean)

    def _cancel_transfer(self) -> None:
        if self._process is None:
            return
        self.status.setText("Cancelling…")
        self._process.terminate()
        QTimer.singleShot(2500, self._kill_if_running)

    def _kill_if_running(self) -> None:
        if self._process is not None and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()

    def _process_error(self, error: QProcess.ProcessError) -> None:
        logger.error("direct transfer process error=%s", error.name)
        if error == QProcess.ProcessError.FailedToStart:
            self.log.appendPlainText("Could not start rsync. Check that it is installed and on PATH.")

    def _process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._read_output()
        cancelled = self.status.text().startswith("Cancelling")
        if exit_code == 0:
            self.progress.setValue(100)
            self.status.setText("Dry run complete" if self.dry_run.isChecked() else "Transfer complete")
            self.stats.setText("Everything finished successfully")
        elif cancelled:
            self.status.setText("Transfer cancelled")
            self.stats.setText("Partial data was kept and can be resumed")
        else:
            self.status.setText("Transfer failed")
            self.stats.setText(f"rsync exited with code {exit_code}; see details below")
        process = self._process
        self._process = None
        if process is not None:
            process.deleteLater()
        self._set_running(False)
        logger.info("direct transfer finished exit_code=%s status=%s", exit_code, self.status.text())

    def _set_running(self, running: bool) -> None:
        self.start.setEnabled(not running)
        self.cancel.setEnabled(running)
        for widget in (
            self.send_radio, self.receive_radio, self.host, self.remote_path,
            self.pick_files, self.pick_folder, self.compress, self.archive,
            self.partial, self.dry_run,
        ):
            widget.setEnabled(not running)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._process is not None:
            answer = QMessageBox.question(
                self, "Transfer in progress", "Cancel the transfer and close FastFiles?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._process.kill()
        self.discovery.close()
        if self.locker_service is not None:
            self.locker_service.stop()
        event.accept()
