from __future__ import annotations

import logging
import posixpath
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .availability import check_peers
from .core import (
    Direction,
    TransferRequest,
    build_rsync_command,
    display_command,
    parse_progress,
    read_ssh_aliases,
    remote_spec,
)
from .locker import (
    PROTOCOL_VERSION,
    Locker,
    LockerConfig,
    LockerService,
    Peer,
    PeerClient,
    PeerDiscovery,
    TransferCancelled,
    parse_peer_address,
)
from .logging_config import log_path
from .profiles import DirectProfile, PeerProfile, ProfileStore, SecretStore
from .transfers import copy_from_peer, copy_to_peer

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
QLabel#notice { color: #8a5100; background: #fff5dd; border-radius: 6px; padding: 10px; }
QLabel#error { color: #b42318; }
QScrollArea { border: 0; background: #f5f7fb; }
QPushButton { min-height: 22px; }
QComboBox, QLineEdit { min-height: 26px; }
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
QPushButton#danger:disabled { color: #98a2b3; }
QProgressBar { height: 8px; border-radius: 4px; text-align: center; color: transparent; }
QProgressBar::chunk { border-radius: 4px; }
"""


class LockerSignals(QObject):
    peers = Signal(object)
    task_done = Signal(object)
    task_error = Signal(object)
    progress = Signal(object)
    availability = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self, config: LockerConfig | None = None, *, start_services: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("FastFiles")
        self.resize(1120, 840)
        self.setMinimumSize(800, 600)
        self._process: QProcess | None = None
        self._output_buffer = ""
        self._cancel_requested = False
        self._active_request: TransferRequest | None = None
        self._connected_client: PeerClient | None = None
        self._remote_info: dict = {}
        self._locker_busy = False
        self._locker_cancel = threading.Event()
        self._task_id = 0
        self._checking_peers = False
        self._probe_cancel = threading.Event()
        self._peer_status: dict[tuple[str, int], str] = {}
        self._discovered: list[Peer] = []
        self._workers = ThreadPoolExecutor(max_workers=3, thread_name_prefix="fastfiles-ui")
        self._closed = False
        self._local_paths: list[str] = []
        self.profile_store = ProfileStore()
        self.secret_store = SecretStore()
        self.locker_config = config or LockerConfig.load()
        self.locker = Locker(
            self.locker_config.locker_path,
            self.locker_config.allow_patterns,
            self.locker_config.deny_patterns,
        )
        self.locker_service: LockerService | None = None
        sharing_status = "Local sharing not started"
        if start_services:
            try:
                self.locker_service = LockerService(self.locker_config)
                self.locker_service.start()
                sharing_status = f"Sharing on {self.locker_config.bind_address}:{self.locker_service.port}"
            except OSError as error:
                self.locker_service = None
                sharing_status = (
                    f"Local sharing unavailable: {error}. Another FastFiles instance may be running."
                )
                logger.warning("%s", sharing_status)
                try:
                    local_address = self.locker_config.bind_address
                    local_address = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(local_address, local_address)
                    local_peer = Peer("local", "Local service", local_address, self.locker_config.port)
                    info = PeerClient(local_peer, self.locker_config.access_code, timeout=1).info()
                    if (
                        info.get("device_id") == self.locker_config.device_id
                        and info.get("locker_path") == str(self.locker.root)
                        and info.get("protocol") == PROTOCOL_VERSION
                    ):
                        sharing_status = f"Shared by the background service on port {self.locker_config.port}; restart that service after config changes"
                except (OSError, ValueError):
                    pass
        self.locker_signals = LockerSignals()
        self.peers: dict[str, Peer] = {}
        self.local_relative = ""
        self.remote_relative = ""
        self._build_ui()
        self.sharing_status.setText(sharing_status)
        self.sharing_status.setVisible("unavailable" in sharing_status or not start_services)
        self.own_code.setToolTip(sharing_status)
        self._set_direction(Direction.SEND)
        self.locker_signals.peers.connect(self._update_peers)
        self.locker_signals.task_done.connect(self._task_done)
        self.locker_signals.task_error.connect(self._task_error)
        self.locker_signals.progress.connect(self._locker_progress_changed)
        self.locker_signals.availability.connect(self._availability_ready)
        self.discovery = None
        if start_services:
            try:
                self.discovery = PeerDiscovery(self.locker_config.device_id, self.locker_signals.peers.emit)
            except OSError as error:
                logger.warning("Discovery unavailable: %s", error)
        self._update_peers([])
        self._refresh_local_locker()
        self.peer_timer = QTimer(self)
        self.peer_timer.setInterval(15_000)
        self.peer_timer.timeout.connect(self._check_availability)
        if start_services:
            self.peer_timer.start()
            QTimer.singleShot(0, self._check_availability)

    @staticmethod
    def _scroll_page(page: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        return area

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        tabs = self.tabs = QTabWidget()
        tabs.setDocumentMode(True)
        locker_page = self._build_locker_page()
        direct_page = QWidget()
        direct_container = QWidget()
        direct_shell = QVBoxLayout(direct_container)
        direct_shell.setContentsMargins(0, 0, 0, 0)
        direct_shell.addWidget(self._scroll_page(direct_page), 1)
        direct_footer = QWidget()
        footer = QVBoxLayout(direct_footer)
        footer.setContentsMargins(28, 8, 28, 18)
        direct_shell.addWidget(direct_footer)
        tabs.addTab(locker_page, "Locker")
        tabs.addTab(direct_container, "Direct SSH")
        root_layout.addWidget(tabs)

        outer = QVBoxLayout(direct_page)
        outer.setContentsMargins(28, 22, 28, 22)
        outer.setSpacing(16)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        title = QLabel("Direct SSH")
        title.setObjectName("title")
        subtitle = QLabel("Secure, resumable transfers over your existing SSH setup")
        subtitle.setObjectName("subtitle")
        outer.addWidget(title)
        outer.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

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

        remote_fields = QGridLayout()
        remote_fields.setHorizontalSpacing(18)
        remote_fields.addWidget(self._label("Remote host"), 0, 0)
        self.host = QComboBox()
        self.host.setEditable(True)
        self.host.setPlaceholderText("server, user@server, or SSH alias")
        self.host.addItems(read_ssh_aliases())
        self.host.setCurrentIndex(-1)
        self.host.lineEdit().setPlaceholderText("user@hostname, IP address, or SSH alias")
        self.host.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.host.setToolTip(
            "SSH aliases come from your SSH config. They are not an online availability list."
        )
        remote_fields.addWidget(self.host, 1, 0)

        self.remote_label = self._label("Remote destination folder")
        remote_fields.addWidget(self.remote_label, 0, 1)
        self.remote_path = QComboBox()
        self.remote_path.setEditable(True)
        self.remote_path.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.remote_path.setPlaceholderText("~/Downloads/")
        self.remote_path.addItems(["~/Downloads/", "~/Desktop/", "~/"])
        self.remote_path.setCurrentText("~/")
        remote_fields.addWidget(self.remote_path, 1, 1)
        remote_fields.setColumnStretch(0, 1)
        remote_fields.setColumnStretch(1, 1)
        layout.addLayout(remote_fields)

        path_header = QHBoxLayout()
        self.local_label = self._label("Local files")
        path_header.addWidget(self.local_label)
        path_header.addStretch()
        self.pick_files = QPushButton("Add files")
        self.pick_folder = QPushButton("Add folder")
        self.remove_paths = QPushButton("Remove")
        path_header.addWidget(self.pick_files)
        path_header.addWidget(self.pick_folder)
        path_header.addWidget(self.remove_paths)
        layout.addLayout(path_header)

        self.paths = QListWidget()
        self.paths.setMinimumHeight(100)
        self.paths.setMaximumHeight(100)
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
        self.dry_run.setToolTip("Preview only. No files will be transferred.")
        for widget in (self.compress, self.archive, self.partial, self.dry_run):
            options.addWidget(widget)
        layout.addLayout(options)
        outer.addWidget(card)
        self.destination_preview = QLabel()
        self.destination_preview.setObjectName("section")
        self.destination_preview.setWordWrap(True)
        footer.addWidget(self.destination_preview)

        progress_card = QFrame()
        progress_card.setObjectName("card")
        progress_layout = QVBoxLayout(progress_card)
        progress_layout.setContentsMargins(16, 10, 16, 10)
        self.status = QLabel("Ready")
        self.status.setObjectName("section")
        self.stats = QLabel("Choose what you want to transfer")
        self.stats.setObjectName("muted")
        self.stats.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        progress_layout.addWidget(self.status)
        progress_layout.addWidget(self.stats)
        progress_layout.addWidget(self.progress)
        footer.addWidget(progress_card)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setPlaceholderText("Transfer details will appear here")
        self.log.setMaximumHeight(125)
        self.log.setMinimumHeight(100)
        self.log.hide()
        outer.addWidget(self.log)
        hint = QLabel(
            "SSH keys / ssh-agent required. Same-name files may be replaced. Locker rules apply only to Locker."
        )
        hint.setToolTip("Confirm new servers in a terminal first: ssh <host>")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        actions = QHBoxLayout()
        self.cancel = QPushButton("Cancel")
        self.cancel.setObjectName("danger")
        self.cancel.setEnabled(False)
        self.start = QPushButton("Start transfer")
        self.start.setObjectName("primary")
        actions.addWidget(self.cancel)
        open_log = QPushButton("Open log")
        open_log.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_path()))))
        actions.addWidget(open_log)
        details = QPushButton("Show details")
        details.setCheckable(True)
        details.toggled.connect(self.log.setVisible)
        actions.addWidget(details)
        actions.addStretch()
        actions.addWidget(self.start)
        footer.addLayout(actions)

        self.setCentralWidget(root)
        self.setStyleSheet(STYLE)

        self.send_radio.toggled.connect(lambda checked: checked and self._set_direction(Direction.SEND))
        self.receive_radio.toggled.connect(lambda checked: checked and self._set_direction(Direction.RECEIVE))
        self.pick_files.clicked.connect(self._choose_files)
        self.pick_folder.clicked.connect(self._choose_folder)
        self.start.clicked.connect(self._start_transfer)
        self.cancel.clicked.connect(self._cancel_transfer)
        self.remove_paths.clicked.connect(self._remove_paths)
        delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.paths)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_shortcut.activated.connect(self._remove_paths)
        self.host.currentTextChanged.connect(self._update_destination_preview)
        self.remote_path.currentTextChanged.connect(self._update_destination_preview)
        self.dry_run.toggled.connect(self._update_destination_preview)
        self.direct_profile.currentIndexChanged.connect(self._load_direct_profile)
        self.save_direct_profile.clicked.connect(self._save_direct_profile)
        self.delete_direct_profile.clicked.connect(self._delete_direct_profile)
        self._refresh_direct_profiles()
        for label in self.findChildren(QLabel):
            label.setTextFormat(Qt.TextFormat.PlainText)
        # Material styles add padding; enforce usable hit targets before layouts
        # calculate their minimum sizes. The surrounding pages can scroll.
        for widget in self.findChildren(QPushButton):
            widget.setMinimumHeight(36)
        for widget in self.findChildren(QComboBox) + self.findChildren(QLineEdit):
            widget.setMinimumHeight(38)
        for widget in self.findChildren(QCheckBox) + self.findChildren(QRadioButton):
            widget.setMinimumHeight(28)

    def _build_locker_page(self) -> QWidget:
        container = QWidget()
        shell = QVBoxLayout(container)
        shell.setContentsMargins(0, 0, 0, 0)
        page = QWidget()
        shell.addWidget(self._scroll_page(page), 1)
        footer_widget = QWidget()
        footer = QVBoxLayout(footer_widget)
        footer.setContentsMargins(28, 8, 28, 18)
        shell.addWidget(footer_widget)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 16, 28, 12)
        outer.setSpacing(10)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        title = QLabel("Locker")
        title.setObjectName("title")
        subtitle = QLabel("Your shared folder, connected to the machines you choose.")
        subtitle.setObjectName("subtitle")
        outer.addWidget(title)
        outer.addWidget(subtitle)
        notice = QLabel(
            "Locker uses unencrypted HTTP. Use it on a trusted LAN or encrypted VPN; use Direct SSH across the internet."
        )
        notice.setWordWrap(True)
        notice.setObjectName("notice")
        outer.addWidget(notice)

        connection = QFrame()
        connection.setObjectName("card")
        connection_layout = QVBoxLayout(connection)
        connection_layout.setContentsMargins(18, 14, 18, 14)
        own_row = QHBoxLayout()
        own_row.addWidget(self._label(f"This device: {self.locker_config.device_name}"))
        own_row.addStretch()
        own_row.addWidget(QLabel("My pairing code"))
        self.own_code = QLineEdit(self.locker_config.access_code)
        self.own_code.setReadOnly(True)
        self.own_code.setEchoMode(QLineEdit.EchoMode.Password)
        self.own_code.setMaximumWidth(110)
        own_row.addWidget(self.own_code)
        reveal = QPushButton("Show")
        reveal.setCheckable(True)
        reveal.toggled.connect(
            lambda shown: self.own_code.setEchoMode(
                QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
            )
        )
        own_row.addWidget(reveal)
        connection_layout.addLayout(own_row)
        self.sharing_status = QLabel()
        self.sharing_status.setWordWrap(True)
        self.sharing_status.setObjectName("muted")
        connection_layout.addWidget(self.sharing_status)
        peer_row = QHBoxLayout()
        self.peer_combo = QComboBox()
        self.peer_combo.setEditable(True)
        self.peer_combo.setMinimumWidth(230)
        self.peer_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.peer_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.peer_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.peer_combo.lineEdit().setPlaceholderText("Select a machine or type IP / hostname[:port]")
        self.peer_code = QLineEdit()
        self.peer_code.setPlaceholderText("Pairing code")
        self.peer_code.setMaxLength(6)
        self.peer_code.setMaximumWidth(150)
        self.peer_code.setEchoMode(QLineEdit.EchoMode.Password)
        self.connect_peer = QPushButton("Connect")
        self.save_peer = QPushButton("Save as…")
        self.delete_peer = QPushButton("Delete saved")
        self.check_peers = QPushButton("Check now")
        peer_row.addWidget(self.peer_combo, 1)
        peer_row.addWidget(self.peer_code)
        peer_row.addWidget(self.connect_peer)
        connection_layout.addLayout(peer_row)
        profile_row = QHBoxLayout()
        self.remember_code = QCheckBox("Remember code in keyring")
        profile_row.addWidget(self.remember_code)
        profile_row.addStretch()
        profile_row.addWidget(self.save_peer)
        profile_row.addWidget(self.delete_peer)
        profile_row.addWidget(self.check_peers)
        connection_layout.addLayout(profile_row)
        self.availability_label = QLabel("Saved IPs and discovered machines are checked every 15 seconds.")
        self.availability_label.setObjectName("muted")
        self.availability_label.setWordWrap(True)
        connection_layout.addWidget(self.availability_label)
        outer.addWidget(connection)

        browsers = QHBoxLayout()
        browsers.setSpacing(16)
        local_card, self.local_tree, self.local_path_label, self.local_back = self._browser_card("My locker")
        remote_card, self.remote_tree, self.remote_path_label, self.remote_back = self._browser_card(
            "Peer locker"
        )
        self.remote_path_label.setText("Not connected")
        browsers.addWidget(local_card)
        browsers.addWidget(remote_card)
        outer.addLayout(browsers, 1)
        self.local_empty = QLabel(
            "Your locker is empty. Open the folder below to add files, then click Refresh."
        )
        self.remote_empty = QLabel("Select a machine and enter its pairing code to browse its locker.")
        for label, card in ((self.local_empty, local_card), (self.remote_empty, remote_card)):
            label.setObjectName("muted")
            label.setWordWrap(True)
            card.layout().addWidget(label)

        actions = QHBoxLayout()
        self.download_peer = QPushButton("← Receive")
        self.upload_peer = QPushButton("Send →")
        self.download_peer.setEnabled(False)
        self.upload_peer.setEnabled(False)
        self.open_locker = QPushButton("Open my locker")
        self.refresh_lockers = QPushButton("Refresh")
        self.cancel_locker = QPushButton("Cancel")
        actions.addWidget(self.open_locker)
        actions.addWidget(self.refresh_lockers)
        actions.addStretch()
        actions.addWidget(self.cancel_locker)
        actions.addWidget(self.download_peer)
        actions.addWidget(self.upload_peer)
        footer.addLayout(actions)
        self.replace_files = QCheckBox("Replace existing files")
        self.replace_files.setToolTip(
            "Off by default. Completed files are kept if a later file fails or you cancel."
        )
        footer.addWidget(self.replace_files)
        self.locker_status = QLabel("Ready to connect")
        self.locker_status.setObjectName("muted")
        self.locker_status.setWordWrap(True)
        self.locker_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.locker_progress = QProgressBar()
        self.locker_progress.setRange(0, 100)
        self.locker_progress.setValue(0)
        footer.addWidget(self.locker_progress)
        footer.addWidget(self.locker_status)

        self.connect_peer.clicked.connect(self._connect_peer)
        self.save_peer.clicked.connect(self._save_peer_profile)
        self.delete_peer.clicked.connect(self._delete_peer_profile)
        self.check_peers.clicked.connect(self._check_availability)
        self.peer_combo.currentTextChanged.connect(self._peer_changed)
        self.peer_code.textEdited.connect(self._disconnect_peer)
        self.local_back.clicked.connect(self._local_back)
        self.remote_back.clicked.connect(self._remote_back)
        self.local_tree.itemDoubleClicked.connect(self._local_open)
        self.remote_tree.itemDoubleClicked.connect(self._remote_open)
        self.upload_peer.clicked.connect(self._upload_selected)
        self.download_peer.clicked.connect(self._download_selected)
        self.refresh_lockers.clicked.connect(self._refresh_lockers)
        self.cancel_locker.clicked.connect(self._cancel_locker_transfer)
        self.local_tree.itemSelectionChanged.connect(self._update_locker_controls)
        self.remote_tree.itemSelectionChanged.connect(self._update_locker_controls)
        self.open_locker.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.locker.root)))
        )
        return container

    def _refresh_direct_profiles(self, selected: str = "") -> None:
        self.direct_profile.blockSignals(True)
        self.direct_profile.clear()
        self.direct_profile.addItem("Choose a saved machine", "")
        try:
            profiles = self.profile_store.direct()
        except (OSError, ValueError) as error:
            profiles = []
            self.stats.setText(str(error))
            logger.error("Cannot load profiles: %s", error)
        for profile in profiles:
            self.direct_profile.addItem(profile.name, profile.name)
        index = self.direct_profile.findData(selected)
        self.direct_profile.setCurrentIndex(max(index, 0))
        self.direct_profile.blockSignals(False)

    def _load_direct_profile(self) -> None:
        name = self.direct_profile.currentData()
        try:
            profile = next((item for item in self.profile_store.direct() if item.name == name), None)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Saved machines", str(error))
            return
        if profile:
            self.host.setCurrentText(profile.host)
            self.remote_path.setCurrentText(profile.remote_path)

    def _save_direct_profile(self) -> None:
        name, accepted = QInputDialog.getText(self, "Save machine", "Alias")
        if not accepted or not name.strip():
            return
        try:
            profile = DirectProfile(
                name.strip(), self.host.currentText().strip(), self.remote_path.currentText().strip()
            )
            existing = any(item.name == profile.name for item in self.profile_store.direct())
            if existing and not self._confirm(
                "Replace saved machine", f"Replace the saved settings for {profile.name}?"
            ):
                return
            self.profile_store.save_direct(profile)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Save machine", str(error))
            return
        self._refresh_direct_profiles(profile.name)

    def _delete_direct_profile(self) -> None:
        name = self.direct_profile.currentData()
        if name and self._confirm("Delete saved machine", f"Remove {name} from saved machines?"):
            try:
                self.profile_store.delete_direct(name)
                self._refresh_direct_profiles()
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, "Saved machines", str(error))

    def _save_peer_profile(self) -> None:
        try:
            peer = self._selected_peer()
        except ValueError as error:
            self._locker_error(str(error))
            return
        if not peer:
            self._locker_error("Enter or select a peer before saving it")
            return
        name, accepted = QInputDialog.getText(self, "Save peer", "Alias", text=peer.name)
        if not accepted or not name.strip():
            return
        code = self.peer_code.text().strip()
        try:
            profile = PeerProfile(name.strip(), peer.address, peer.port)
            existing = any(item.name == profile.name for item in self.profile_store.peers())
            if existing and not self._confirm(
                "Replace saved machine", f"Replace the saved address for {profile.name}?"
            ):
                return
            if self.remember_code.isChecked() and not re.fullmatch(r"[0-9]{6}", code):
                raise ValueError("Enter a six-digit pairing code to remember it, or uncheck Remember code")
            self.profile_store.save_peer(profile)
        except (OSError, ValueError) as error:
            self._locker_error(str(error))
            return
        if self.remember_code.isChecked():
            try:
                self.secret_store.set(profile.secret_id, code)
            except Exception as error:
                QMessageBox.warning(
                    self,
                    "Saved machine",
                    f"Address saved. Code could not be stored in the system keyring ({type(error).__name__}). You can enter it when connecting.",
                )
        else:
            self.secret_store.delete(profile.secret_id)
        self._update_peers(self._discovered)
        self.peer_combo.setCurrentIndex(self.peer_combo.findData(f"saved:{profile.name}"))
        self._check_availability()

    def _delete_peer_profile(self) -> None:
        key = self.peer_combo.currentData() or ""
        if not key.startswith("saved:"):
            return
        name = key.removeprefix("saved:")
        if not self._confirm("Delete saved machine", f"Remove {name} and its remembered code?"):
            return
        try:
            profile = next(item for item in self.profile_store.peers() if item.name == name)
            self.profile_store.delete_peer(name)
            if not any(item.secret_id == profile.secret_id for item in self.profile_store.peers()):
                self.secret_store.delete(profile.secret_id)
            self._disconnect_peer()
            self.peer_combo.setCurrentIndex(-1)
            self.peer_combo.setEditText("")
            self._update_peers(self._discovered)
        except (OSError, ValueError, StopIteration) as error:
            self._locker_error(str(error))

    def _confirm(self, title: str, message: str) -> bool:
        return QMessageBox.question(self, title, message) == QMessageBox.StandardButton.Yes

    def _browser_card(self, title: str) -> tuple[QFrame, QTreeWidget, QLabel, QPushButton]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        header = QHBoxLayout()
        label = self._label(title)
        back = QPushButton("Up")
        header.addWidget(label)
        header.addStretch()
        header.addWidget(back)
        path = QLabel("/")
        path.setObjectName("muted")
        path.setWordWrap(True)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Name", "Size"])
        tree.setRootIsDecorated(False)
        tree.setMinimumHeight(180)
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(True)
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
            icon = QStyle.StandardPixmap.SP_DirIcon if entry["is_dir"] else QStyle.StandardPixmap.SP_FileIcon
            item.setIcon(0, self.style().standardIcon(icon))
            item.setToolTip(0, entry["name"])
            tree.addTopLevelItem(item)

    def _refresh_local_locker(self) -> None:
        try:
            self._fill_tree(self.local_tree, self.locker.list(self.local_relative))
            self.local_path_label.setText(str(self.locker.root / self.local_relative))
            self.local_empty.setVisible(self.local_tree.topLevelItemCount() == 0)
            self._update_locker_controls()
        except (OSError, ValueError) as error:
            self._locker_error(str(error))

    def _update_peers(self, peers: list[Peer]) -> None:
        self._discovered = peers
        if self._locker_busy:
            return  # Keep the endpoint shown in the form fixed until completion.
        current_text = self.peer_combo.currentText()
        current_id = (
            self.peer_combo.currentData()
            if current_text == self.peer_combo.itemText(self.peer_combo.currentIndex())
            else None
        )
        previous = self.peers.get(current_id)
        try:
            profiles = self.profile_store.peers()
        except (OSError, ValueError) as error:
            profiles = []
            self.availability_label.setText(str(error))
            logger.error("Cannot read saved peers: %s", error)
        saved = [Peer(f"saved:{p.name}", p.name, p.address, p.port) for p in profiles]
        endpoints = {(peer.address, peer.port) for peer in saved}
        combined = saved + [peer for peer in peers if (peer.address, peer.port) not in endpoints]
        self.peers = {peer.device_id: peer for peer in combined}
        self.peer_combo.blockSignals(True)
        self.peer_combo.clear()
        for peer in combined:
            status = self._peer_status.get((peer.address, peer.port), "Checking")
            origin = "Saved" if peer.device_id.startswith("saved:") else "Discovered"
            self.peer_combo.addItem(
                f"{status} · {peer.name} · {self._endpoint(peer)} · {origin}", peer.device_id
            )
            self.peer_combo.setItemData(
                self.peer_combo.count() - 1,
                QColor("#16724b" if status == "Online" else "#667085"),
                Qt.ItemDataRole.ForegroundRole,
            )
        index = self.peer_combo.findData(current_id) if current_id else -1
        self.peer_combo.setCurrentIndex(index)
        if index < 0:
            if previous is not None:
                current_text = self._endpoint(previous)
            self.peer_combo.setEditText(current_text)
        self.peer_combo.blockSignals(False)
        self._peer_changed()
        self._update_locker_controls()

    @staticmethod
    def _endpoint(peer: Peer) -> str:
        host = f"[{peer.address}]" if ":" in peer.address else peer.address
        return f"{host}:{peer.port}"

    def _disconnect_peer(self, *_args) -> None:
        if self._locker_busy:
            return
        self._connected_client = None
        self._remote_info = {}
        self.remote_relative = ""
        self.remote_tree.clear()
        self.remote_path_label.setText("Not connected")
        self.remote_empty.setText("Connect to a machine to see its files.")
        self.remote_empty.show()
        self.locker_status.setText("Enter this machine’s pairing code, then Connect.")
        self._update_locker_controls()

    def _peer_changed(self, *_args) -> None:
        if self._locker_busy:
            return
        try:
            peer = self._selected_peer()
        except ValueError:
            peer = None
        endpoint = (peer.address, peer.port) if peer else None
        if endpoint == getattr(self, "_selection_endpoint", None):
            return
        self._selection_endpoint = endpoint
        self._disconnect_peer()
        self.peer_code.clear()
        self.remember_code.setChecked(False)
        if peer and peer.device_id.startswith("saved:"):
            remembered = self.secret_store.get(f"peer:{peer.address}:{peer.port}")
            if remembered:
                self.peer_code.setText(remembered)
                self.remember_code.setChecked(True)
        self._update_locker_controls()

    def _selected_peer(self) -> Peer | None:
        peer = self.peers.get(self.peer_combo.currentData())
        selected_text = self.peer_combo.itemText(self.peer_combo.currentIndex())
        if peer and self.peer_combo.currentText() == selected_text:
            return peer
        text = self.peer_combo.currentText().strip()
        return parse_peer_address(text) if text else None

    def _client(self) -> PeerClient:
        if self._connected_client is None:
            raise ValueError("Connect to the machine before transferring files")
        return self._connected_client

    def _check_availability(self) -> None:
        if self._checking_peers or self._closed:
            return
        peers = list(self.peers.values())
        try:
            manual = self._selected_peer()
            if manual is not None:
                peers.append(manual)
        except ValueError:
            pass
        if not peers:
            return
        self._checking_peers = True
        self.check_peers.setEnabled(False)
        self.availability_label.setText("Checking for FastFiles on saved and discovered machines…")

        def task() -> None:
            result = check_peers(peers, self._probe_cancel)
            if not self._closed:
                self.locker_signals.availability.emit(result)

        self._workers.submit(task)

    def _availability_ready(self, result: dict) -> None:
        self._checking_peers = False
        self._peer_status.update(result)
        self._update_peers(self._discovered)
        online = sum(value == "Online" for value in result.values())
        self.availability_label.setText(
            f"{online} of {len(result)} machines online · checked every 15 seconds · offline entries can be edited or retried"
        )
        self.check_peers.setEnabled(True)

    def _update_locker_controls(self) -> None:
        if not hasattr(self, "replace_files"):
            return
        idle = not self._locker_busy
        connected = self._connected_client is not None
        for widget in (
            self.peer_combo,
            self.peer_code,
            self.remember_code,
            self.connect_peer,
            self.save_peer,
            self.replace_files,
        ):
            widget.setEnabled(idle)
        self.delete_peer.setEnabled(idle and str(self.peer_combo.currentData()).startswith("saved:"))
        self.local_tree.setEnabled(idle)
        self.remote_tree.setEnabled(idle and connected)
        self.local_back.setEnabled(idle and bool(self.local_relative))
        self.remote_back.setEnabled(idle and connected and bool(self.remote_relative))
        self.refresh_lockers.setEnabled(idle)
        self.cancel_locker.setEnabled(self._locker_busy and not self._locker_cancel.is_set())
        self.upload_peer.setEnabled(
            idle
            and connected
            and not self._remote_info.get("read_only", False)
            and bool(self.local_tree.selectedItems())
        )
        self.download_peer.setEnabled(
            idle and connected and not self.locker_config.read_only and bool(self.remote_tree.selectedItems())
        )

    def _run_locker_task(self, kind: str, task) -> None:
        if self._locker_busy:
            return
        self._locker_busy = True
        self._locker_cancel.clear()
        self._task_id += 1
        task_id = self._task_id
        self._update_locker_controls()

        def run() -> None:
            try:
                result = task()
                if not self._closed:
                    self.locker_signals.task_done.emit((task_id, kind, result))
            except Exception as error:
                logger.error("Locker %s failed: %s", kind, error)
                if not self._closed:
                    self.locker_signals.task_error.emit(
                        (task_id, kind, isinstance(error, TransferCancelled), str(error))
                    )

        self._workers.submit(run)

    def _connect_peer(self) -> None:
        try:
            peer = self._selected_peer()
            if not peer:
                raise ValueError("Choose a machine or enter its IP / hostname")
            code = self.peer_code.text().strip()
            if not re.fullmatch(r"[0-9]{6}", code):
                raise ValueError("Enter the machine’s six-digit pairing code")
            client = PeerClient(peer, code)
        except ValueError as error:
            self._locker_error(str(error))
            return
        self.locker_status.setText(f"Connecting to {client.peer.name}…")
        logger.info("peer connection starting name=%s endpoint=%s", client.peer.name, client.peer.base_url)
        self._connected_client = None
        self.remote_tree.clear()

        def task() -> None:
            info = client.info()
            if info.get("protocol") != PROTOCOL_VERSION:
                raise OSError("Update FastFiles on both machines; the peer uses an older protocol")
            if info.get("device_id") == self.locker_config.device_id:
                raise OSError("This address points to this machine’s own locker")
            if not isinstance(info.get("locker_path"), str) or type(info.get("read_only")) is not bool:
                raise OSError("Invalid peer connection response")
            items = client.list("")
            return client, info, items

        self._run_locker_task("connect", task)

    def _load_remote(self, relative: str) -> None:
        try:
            client = self._client()
        except ValueError as error:
            self._locker_error(str(error))
            return
        self.locker_status.setText("Loading peer locker…")
        self._run_locker_task("browse", lambda: (relative, client.list(relative)))

    def _show_remote_items(self, result: tuple[str, list[dict]]) -> None:
        relative, items = result
        self.remote_relative = relative
        root = self._remote_info.get("locker_path", "/")
        self.remote_path_label.setText(posixpath.join(root, relative))
        self._fill_tree(self.remote_tree, items)
        self.remote_empty.setText("This folder has no shared files.")
        self.remote_empty.setVisible(not items)
        self._update_locker_controls()

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

    def _refresh_lockers(self) -> None:
        self._refresh_local_locker()
        if self._connected_client:
            self._load_remote(self.remote_relative)

    def _transfer_progress(self, done: int, total: int) -> None:
        if not self._closed:
            self.locker_signals.progress.emit((self._task_id, done, total))

    def _locker_progress_changed(self, value) -> None:
        task_id, done, total = value
        if task_id == self._task_id and self._locker_busy:
            self.locker_progress.setValue(min(99, int(done * 100 / total)) if total else 0)

    def _cancel_locker_transfer(self) -> None:
        self._locker_cancel.set()
        self.locker_status.setText("Cancelling… waiting for the current network request to return")
        self._update_locker_controls()

    def _upload_selected(self) -> None:
        item = next(iter(self.local_tree.selectedItems()), None)
        if item is None:
            self._locker_error("Select a file or folder in your locker")
            return
        try:
            client = self._client()
            relative = item.data(0, Qt.ItemDataRole.UserRole)
            source = self.locker.resolve(relative)
        except (OSError, ValueError) as error:
            self._locker_error(str(error))
            return
        remote_directory = self.remote_relative
        overwrite = self.replace_files.isChecked()
        self.locker_status.setText(f"Sending {source.name}…")
        logger.info(
            "locker send requested source=%s remote_directory=%s", source, self.remote_relative or "/"
        )
        self.locker_progress.setValue(0)

        def task():
            result = copy_to_peer(
                self.locker,
                client,
                relative,
                remote_directory,
                self._transfer_progress,
                self._locker_cancel,
                overwrite=overwrite,
            )
            return result

        self._run_locker_task("send", task)

    def _download_selected(self) -> None:
        item = next(iter(self.remote_tree.selectedItems()), None)
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
        local_directory = self.local_relative
        overwrite = self.replace_files.isChecked()
        destination = self.locker.root / local_directory / posixpath.basename(relative)
        self.locker_status.setText(f"Receiving {posixpath.basename(relative)}…")
        logger.info("locker receive requested remote=%s destination=%s", relative, destination)
        self.locker_progress.setValue(0)

        self._run_locker_task(
            "receive",
            lambda: copy_from_peer(
                self.locker,
                client,
                relative,
                is_dir,
                local_directory,
                self._transfer_progress,
                self._locker_cancel,
                overwrite=overwrite,
            ),
        )

    def _task_done(self, value) -> None:
        task_id, kind, result = value
        if task_id != self._task_id:
            return
        self._locker_busy = False
        if kind == "connect":
            if self._locker_cancel.is_set():
                self._disconnect_peer()
                return
            self._connected_client, self._remote_info, items = result
            self._show_remote_items(("", items))
            peer = self._connected_client.peer
            mode = " · read-only" if self._remote_info.get("read_only") else ""
            self.locker_status.setText(f"Connected to {peer.name} · {self._endpoint(peer)}{mode}")
            logger.info(
                "Connected endpoint=%s remote_root=%s", self._endpoint(peer), self._remote_info["locker_path"]
            )
        elif kind == "browse":
            self._show_remote_items(result)
            self.locker_status.setText(f"Browsing {self.remote_path_label.text()}")
        elif kind in ("send", "receive"):
            self.locker_progress.setValue(100)
            verb = "Sent" if kind == "send" else "Received"
            message = f"{verb} {result.files} file(s), {result.directories} folder(s), {self._human_size(result.bytes)} → {result.destination}"
            self.locker_status.setText(message)
            logger.info("Locker copy complete: %s", message)
            self._refresh_local_locker()
            # Refresh the remote view while retaining the useful completion receipt.
            client, relative = self._connected_client, self.remote_relative
            if client:
                self._run_locker_task(
                    "refresh_after_copy", lambda: (message, relative, client.list(relative))
                )
        elif kind == "refresh_after_copy":
            message, relative, items = result
            self._show_remote_items((relative, items))
            self.locker_status.setText(message)
        if not self._locker_busy:
            self._update_peers(self._discovered)
        self._update_locker_controls()

    def _task_error(self, value) -> None:
        task_id, kind, cancelled, message = value
        if task_id != self._task_id:
            return
        self._locker_busy = False
        if kind in ("connect", "browse"):
            self._disconnect_peer()
        prefix = (
            "Cancelled"
            if cancelled
            else "Transfer stopped"
            if kind in ("send", "receive")
            else "Connection failed"
        )
        if kind == "refresh_after_copy":
            self.locker_status.setText(self.locker_status.text() + f" · Could not refresh listing: {message}")
        else:
            self.locker_status.setText(f"{prefix}: {message}")
        if kind in ("send", "receive"):
            self.locker_status.setText(self.locker_status.text() + " · Any completed files were kept.")
        self._update_peers(self._discovered)
        self._update_locker_controls()

    def _locker_error(self, message: str) -> None:
        logger.error("locker operation failed error=%s", message)
        self.connect_peer.setEnabled(True)
        self.locker_status.setText(message)
        self._update_locker_controls()
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
        self.remote_label.setText(
            "Remote source file or folder" if receiving else "Remote destination folder"
        )
        self.local_label.setText("Local destination" if receiving else "Local files and folders")
        self.pick_files.setVisible(not receiving)
        self.pick_folder.setText("Choose destination" if receiving else "Add folder")
        self.remote_path.setPlaceholderText("~/path/to/file-or-folder" if receiving else "~/Downloads/")
        self.start.setText("Receive files" if receiving else "Send files")
        self._update_destination_preview()

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
            self._update_destination_preview()
        else:
            self._add_paths([folder])

    def _add_paths(self, paths: list[str]) -> None:
        for path in paths:
            if path not in self._local_paths:
                self._local_paths.append(path)
                self.paths.addItem(path)
        self._update_destination_preview()

    def _remove_paths(self) -> None:
        if self._process is not None:
            return
        for item in self.paths.selectedItems():
            self._local_paths.remove(item.text())
            self.paths.takeItem(self.paths.row(item))
        self._update_destination_preview()

    def _update_destination_preview(self, *_args) -> None:
        if not hasattr(self, "destination_preview"):
            return
        host = self.host.currentText().strip() or "Choose a machine"
        path = self.remote_path.currentText().strip()
        if self.direction is Direction.SEND:
            destination = (
                f"{host}:{path}"
                if self.host.currentText().strip()
                else "Choose a machine, then add files to send"
            )
        else:
            destination = self._local_paths[0] if self._local_paths else "Choose a local destination"
        prefix = "PREVIEW ONLY — no files will be copied" if self.dry_run.isChecked() else "Destination"
        self.destination_preview.setText(f"{prefix}: {destination}")

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
        if self._process is not None:
            return
        try:
            request = self._request()
            command = build_rsync_command(request)
            missing = [tool for tool in ("ssh", "rsync") if shutil.which(tool) is None]
            if missing:
                raise ValueError("Install the required tools: " + ", ".join(missing))
            for path in request.local_paths:
                if not Path(path).exists():
                    raise ValueError(f"Local path does not exist: {path}")
            if request.direction is Direction.RECEIVE and not Path(request.local_paths[0]).is_dir():
                raise ValueError("Choose a local destination folder")
        except ValueError as error:
            QMessageBox.warning(self, "Cannot start transfer", str(error))
            return
        self._active_request = request
        self._cancel_requested = False
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
        process.started.connect(
            lambda: self.status.setText("Previewing…" if request.dry_run else "Transferring…")
        )
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
                logger.info("rsync: %s", clean)

    def _cancel_transfer(self) -> None:
        if self._process is None:
            return
        self._cancel_requested = True
        self.status.setText("Cancelling…")
        process = self._process
        process.terminate()
        QTimer.singleShot(2500, lambda: self._kill_if_running(process))

    def _kill_if_running(self, process: QProcess) -> None:
        if self._process is process and process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()

    def _process_error(self, error: QProcess.ProcessError) -> None:
        logger.error("direct transfer process error=%s", error.name)
        if error == QProcess.ProcessError.FailedToStart:
            self.log.appendPlainText("Could not start rsync. Check that it is installed and on PATH.")
            self._process_finished(-1, QProcess.ExitStatus.CrashExit)

    def _process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if self._process is None:
            return
        self._read_output()
        if self._output_buffer.strip():
            self.log.appendPlainText(self._output_buffer.strip())
            logger.info("rsync: %s", self._output_buffer.strip())
        self._output_buffer = ""
        request = self._active_request
        if exit_code == 0 and _status == QProcess.ExitStatus.NormalExit:
            self.progress.setValue(100)
            dry_run = request.dry_run if request else False
            self.status.setText("Preview complete — no files copied" if dry_run else "Transfer complete")
            destination = (
                remote_spec(request.host, request.remote_path)
                if request and request.direction is Direction.SEND
                else (request.local_paths[0] if request else "")
            )
            self.stats.setText(
                f"{'Previewed' if dry_run else 'Completed'} destination: {destination} · See the itemized log for files changed."
            )
        elif self._cancel_requested:
            self.status.setText("Transfer cancelled")
            self.stats.setText("Completed files were kept. Retry to resume if Resume partial was enabled.")
        else:
            self.status.setText("Transfer failed")
            self.log.show()
            self.stats.setText(
                f"rsync exited with code {exit_code}. Check the log below; for authentication or host-key errors, run ssh <host> in a terminal first."
            )
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
            self.send_radio,
            self.receive_radio,
            self.host,
            self.remote_path,
            self.pick_files,
            self.pick_folder,
            self.compress,
            self.archive,
            self.partial,
            self.dry_run,
            self.direct_profile,
            self.save_direct_profile,
            self.delete_direct_profile,
            self.paths,
            self.remove_paths,
        ):
            widget.setEnabled(not running)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._locker_busy:
            self.locker_status.setText(
                "A Locker operation is still running. Cancel it or wait for it to finish before closing."
            )
            self.tabs.setCurrentIndex(0)
            event.ignore()
            return
        if self._process is not None:
            answer = QMessageBox.question(
                self, "Transfer in progress", "Cancel the transfer and close FastFiles?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._process.kill()
            self._process.waitForFinished(3000)
        self._closed = True
        self._probe_cancel.set()
        self.peer_timer.stop()
        self._workers.shutdown(wait=False, cancel_futures=True)
        if self.discovery is not None:
            self.discovery.close()
        if self.locker_service is not None:
            self.locker_service.stop()
        event.accept()
