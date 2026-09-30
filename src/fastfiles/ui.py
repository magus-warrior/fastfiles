from __future__ import annotations

import logging
import posixpath
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QDir, QFile, QObject, QProcess, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
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

from .access import can_traverse, path_permitted
from .activity import ActivityStore
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
from .locker_widgets import ComputerList, LockerTree
from .logging_config import log_path
from .policy import relative_path
from .profiles import DirectProfile, PeerProfile, ProfileStore, SecretStore
from .transfers import (
    copy_many_from_peer,
    copy_many_to_peer,
    import_to_locker,
    upload_paths_to_peer,
)

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
QLabel#dropArea {
  color: #315b91; background: #f0f6ff;
  border: 2px dashed #91afd4; border-radius: 8px; padding: 10px;
}
QLabel#dropArea[dragActive="true"] { background: #dcecff; border-color: #2563eb; }
QLabel#dropArea:disabled { color: #98a2b3; border-color: #dfe3eb; background: #f5f7fb; }
QLabel#notice { color: #8a5100; background: #fff5dd; border-radius: 6px; padding: 10px; }
QLabel#error { color: #b42318; }
QScrollArea { border: 0; background: #f5f7fb; }
QPushButton { min-height: 22px; text-transform: none; border-radius: 6px; }
QPushButton#sendAction { background: #2563eb; color: white; border: 1px solid #2563eb; font-weight: 600; }
QPushButton#sendAction:hover { background: #1d4ed8; }
QPushButton#sendAction:disabled { background: #e8edf5; color: #7c879b; border-color: #e8edf5; }
QLabel#guide { color: #344054; background: #edf4ff; border-radius: 7px; padding: 10px 12px; }
QLabel#emptyState { color: #667085; padding: 18px; }
QLabel#stepTitle { color: #172033; font-size: 17px; font-weight: 600; }
QPushButton:focus { border: 2px solid #1d4ed8; }
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


class FileDropArea(QLabel):
    paths_dropped = Signal(list)

    def __init__(self, text: str):
        super().__init__(text)
        self.setAcceptDrops(True)
        self.setObjectName("dropArea")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setMinimumHeight(64)
        self.setAccessibleName(text)
        self.directories_only = False

    def _paths(self, event) -> list[str]:
        if not self.isEnabled() or not event.mimeData().hasUrls():
            return []
        urls = event.mimeData().urls()
        if not urls or any(not url.isLocalFile() for url in urls):
            return []
        paths = list(dict.fromkeys(url.toLocalFile() for url in urls))
        if self.directories_only and (len(paths) != 1 or not Path(paths[0]).is_dir()):
            return []
        if any(not (Path(path).is_file() or Path(path).is_dir()) for path in paths):
            return []
        return paths

    def _highlight(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def dragEnterEvent(self, event) -> None:
        if self._paths(event):
            self._highlight(True)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event) -> None:
        self._highlight(False)
        event.accept()

    def dropEvent(self, event) -> None:
        self._highlight(False)
        paths = self._paths(event)
        if not paths:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.paths_dropped.emit(paths)


class LockerSignals(QObject):
    peers = Signal(object)
    task_done = Signal(object)
    task_error = Signal(object)
    progress = Signal(object)
    availability = Signal(object)
    snapshot = Signal(object)


class MainWindow(QMainWindow):
    def __init__(
        self, config: LockerConfig | None = None, *, start_services: bool = True,
        config_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("FastFiles")
        self.setMinimumSize(800, 600)
        available = self.screen().availableGeometry()
        self.resize(min(1400, max(800, available.width() - 40)),
                    min(960, max(600, available.height() - 60)))
        self._process: QProcess | None = None
        self._output_buffer = ""
        self._cancel_requested = False
        self._active_request: TransferRequest | None = None
        self._connected_client: PeerClient | None = None
        self._connection_problem = ""
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
        self._drag_scope = uuid.uuid4().hex
        self._refreshing = False
        self._view_revision = 0
        self._pending_drop = None
        self._activity_ids: set[str] = set()
        self._activity_loaded = False
        self._local_paths: list[str] = []
        self.profile_store = ProfileStore()
        self.secret_store = SecretStore()
        self.locker_config = config or LockerConfig.load()
        self.config_path = config_path
        self.locker = Locker(
            self.locker_config.locker_path,
            self.locker_config.allow_patterns,
            self.locker_config.deny_patterns,
        )
        self.activity_store = ActivityStore(self.locker.root)
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
                    credential = "" if self.locker_config.uses_computer_keys else self.locker_config.access_code
                    info = PeerClient(local_peer, credential, timeout=1).info()
                    if (
                        info.get("device_id") == self.locker_config.device_id
                        and info.get("protocol") == PROTOCOL_VERSION
                    ):
                        if info.get("locker_path") == str(self.locker.root):
                            sharing_status = f"Shared by the background service on port {self.locker_config.port}; restart that service after config changes"
                        elif self.locker_config.uses_computer_keys:
                            sharing_status = f"Background service detected on port {self.locker_config.port}; restart it to apply this folder and its sharing permissions"
                except (OSError, ValueError):
                    pass
        self.locker_signals = LockerSignals()
        self.peers: dict[str, Peer] = {}
        self.local_relative = ""
        self.remote_relative = ""
        self._build_ui()
        self.sharing_status.setText(sharing_status)
        self.sharing_status.setVisible("unavailable" in sharing_status or not start_services)
        if "unavailable" in sharing_status:
            self.own_details.show()
        self.own_code.setToolTip(sharing_status)
        self.own_code.setEnabled(not self.locker_config.uses_computer_keys)
        self._set_direction(Direction.SEND)
        self.locker_signals.peers.connect(self._update_peers)
        self.locker_signals.task_done.connect(self._task_done)
        self.locker_signals.task_error.connect(self._task_error)
        self.locker_signals.progress.connect(self._locker_progress_changed)
        self.locker_signals.availability.connect(self._availability_ready)
        self.locker_signals.snapshot.connect(self._snapshot_ready)
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
        self.locker_timer = QTimer(self)
        self.locker_timer.setInterval(3000)
        self.locker_timer.timeout.connect(self._poll_lockers)
        self.locker_timer.start()
        self._refresh_activity()

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
        self.edit_direct_profile = QPushButton("Edit…")
        self.edit_direct_profile.clicked.connect(lambda: self._edit_profile(False))
        profiles.addWidget(self.edit_direct_profile)
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

        self.direct_drop = FileDropArea("Drop files and folders here to add them to the transfer")
        self.direct_drop.paths_dropped.connect(self._drop_direct_paths)
        layout.addWidget(self.direct_drop)
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
        self.locker_scroll = self._scroll_page(page)
        shell.addWidget(self.locker_scroll, 1)
        footer_widget = QWidget()
        footer = QVBoxLayout(footer_widget)
        footer.setContentsMargins(20, 8, 20, 14)
        shell.addWidget(footer_widget)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(20, 12, 20, 12)
        outer.setSpacing(10)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        heading = QHBoxLayout()
        title = QLabel("Send files")
        title.setObjectName("title")
        heading.addWidget(title)
        heading.addStretch()
        help_button = QPushButton("How it works")
        help_button.clicked.connect(self._show_locker_help)
        heading.addWidget(help_button)
        pair_toggle = self.pair_toggle = QPushButton("My pairing code")
        pair_toggle.setCheckable(True)
        heading.addWidget(pair_toggle)
        self.sharing_settings = QPushButton("Permissions…")
        self.sharing_settings.clicked.connect(self._show_sharing_settings)
        heading.addWidget(self.sharing_settings)
        outer.addLayout(heading)
        subtitle = QLabel("Connect to another computer, then choose files or a folder to send. Your originals stay here.")
        self.locker_subtitle = subtitle
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        self.own_details = QWidget()
        details_layout = QVBoxLayout(self.own_details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        own_row = QHBoxLayout()
        own_row.addWidget(self._label(f"This computer: {self.locker_config.device_name}"), 1)
        own_row.addWidget(QLabel("Pairing code"))
        self.own_code = QLineEdit(self.locker_config.access_code)
        self.own_code.setReadOnly(True)
        self.own_code.setEchoMode(QLineEdit.EchoMode.Password)
        self.own_code.setFixedWidth(90)
        own_row.addWidget(self.own_code)
        reveal = QPushButton("Show")
        reveal.setCheckable(True)
        reveal.toggled.connect(lambda shown: self.own_code.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password))
        own_row.addWidget(reveal)
        details_layout.addLayout(own_row)
        self.sharing_status = QLabel()
        self.sharing_status.setWordWrap(True)
        self.sharing_status.setObjectName("muted")
        details_layout.addWidget(self.sharing_status)
        notice = QLabel("Shared over your trusted LAN or VPN. Locker traffic is unencrypted.")
        notice.setObjectName("muted")
        notice.setWordWrap(True)
        details_layout.addWidget(notice)
        outer.addWidget(self.own_details)
        self.own_details.hide()
        pair_toggle.toggled.connect(self.own_details.setVisible)

        body = QHBoxLayout()
        body.setSpacing(12)
        sidebar = self.computer_sidebar = QWidget()
        sidebar.setMinimumWidth(145)
        sidebar.setMaximumWidth(170)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.addWidget(self._label("Your computers"))
        self.computers_hint = QLabel("Open FastFiles on the other computer. It will appear here on the same network.")
        self.computers_hint.setObjectName("muted")
        self.computers_hint.setWordWrap(True)
        side.addWidget(self.computers_hint)
        self.computers = ComputerList()
        self.computers.drag_scope = self._drag_scope
        self.computers.setMinimumHeight(235)
        self.computers.setWordWrap(True)
        self.computers.setToolTip("Select a computer to browse. Drop files on it to send to its saved folder.")
        self.computers.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.computers.customContextMenuRequested.connect(self._computer_menu)
        self.computers.itemClicked.connect(self._computer_selected)
        self.computers.transfer_dropped.connect(self._drop_on_computer)
        side.addWidget(self.computers, 1)
        self.check_peers = QPushButton("Find computers")
        self.check_peers.setToolTip("Check availability and show forgotten discovered computers again")
        side.addWidget(self.check_peers)
        self.availability_label = QLabel("Saved computers remain here when offline.")
        self.availability_label.setObjectName("muted")
        self.availability_label.setWordWrap(True)
        side.addWidget(self.availability_label)
        body.addWidget(sidebar)
        workspace = QVBoxLayout()
        workspace.setSpacing(8)
        body.addLayout(workspace, 1)

        connection = QFrame()
        connection.setObjectName("card")
        connection_layout = QVBoxLayout(connection)
        connection_layout.setContentsMargins(12, 10, 12, 10)
        connection_layout.addWidget(self._label("1  Choose the receiving computer"))
        peer_row = QHBoxLayout()
        self.peer_combo = QComboBox()
        self.peer_combo.setEditable(True)
        self.peer_combo.setMinimumWidth(90)
        self.peer_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.peer_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.peer_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.peer_combo.lineEdit().setPlaceholderText("Choose a computer or type its IP address")
        self.peer_code = QLineEdit()
        self.peer_combo.setAccessibleName("Receiving computer name or IP address")
        self.peer_code.setPlaceholderText("Pairing code")
        self.peer_code.setAccessibleName("The other computer's pairing code or access key")
        self.peer_code.setToolTip("On the other computer, click My pairing code. Enter that code here.")
        self.peer_code.setMaxLength(256)
        self.peer_code.setMinimumWidth(95)
        self.peer_code.setMaximumWidth(175)
        self.peer_code.setEchoMode(QLineEdit.EchoMode.Password)
        self.connect_peer = QPushButton("Connect")
        self.connect_peer.setObjectName("sendAction")
        self.connection_options = QPushButton("⋯")
        self.connection_options.setCheckable(True)
        self.connection_options.setChecked(False)
        self.connection_options.setFixedWidth(38)
        self.connection_options.setToolTip("Saved computer settings")
        self.connection_options.setAccessibleName("Saved computer settings")
        self.save_peer = QPushButton("Save as…")
        self.delete_peer = QPushButton("Forget")
        peer_row.addWidget(self.peer_combo, 1)
        peer_row.addWidget(self.peer_code)
        peer_row.addWidget(self.connect_peer)
        peer_row.addWidget(self.connection_options)
        peer_row.addWidget(self.delete_peer)
        connection_layout.addLayout(peer_row)
        self.profile_options = QWidget()
        profile_row = QHBoxLayout(self.profile_options)
        profile_row.setContentsMargins(0, 0, 0, 0)
        profile_row.addStretch()
        self.edit_peer = QPushButton("Edit…")
        self.edit_peer.clicked.connect(lambda: self._edit_profile(True))
        profile_row.addWidget(self.edit_peer)
        profile_row.addWidget(self.save_peer)
        self.delete_peer.setToolTip("Forget saved access and hide this computer from discovery. Find computers shows it again.")
        connection_layout.addWidget(self.profile_options)
        self.profile_options.hide()
        self.connection_options.toggled.connect(self.profile_options.setVisible)
        workspace.addWidget(connection)
        self.connection_summary = QLabel("Select a saved computer, or enter an address and connect.")
        self.connection_summary.setWordWrap(True)
        self.connection_summary.setObjectName("guide")
        workspace.addWidget(self.connection_summary)

        browsers = QHBoxLayout()
        browsers.setSpacing(10)
        local_card, self.local_tree, self.local_path_label, self.local_back = self._browser_card("My shared files")
        remote_card, self.remote_tree, self.remote_path_label, self.remote_back = self._browser_card("2  Send to this computer")
        self.local_tree.side = "local"
        self.remote_tree.side = "remote"
        for tree in (self.local_tree, self.remote_tree):
            tree.drag_scope = self._drag_scope
        self.remote_path_label.setText("Not connected")
        browsers.addWidget(local_card, 1)
        browsers.addWidget(remote_card, 1)
        workspace.addLayout(browsers, 1)
        self.local_tools = QWidget()
        local_tools = QHBoxLayout(self.local_tools)
        local_tools.setContentsMargins(0, 0, 0, 0)
        self.local_home = QPushButton("Home")
        self.local_inbox = QPushButton("Inbox")
        self.local_new_folder = QPushButton("New folder")
        self.local_new_folder.setToolTip("Create a folder in My locker")
        for button in (self.local_home, self.local_inbox, self.local_new_folder):
            local_tools.addWidget(button)
        local_card.layout().insertWidget(2, self.local_tools)
        self.remote_tools = QWidget()
        remote_tools = QHBoxLayout(self.remote_tools)
        remote_tools.setContentsMargins(0, 0, 0, 0)
        self.remote_home = QPushButton("Home")
        self.remote_new_folder = QPushButton("New folder")
        self.remote_new_folder.setToolTip("Create a folder on this computer")
        remote_tools.addWidget(self.remote_home)
        remote_tools.addWidget(self.remote_new_folder)
        remote_card.layout().insertWidget(2, self.remote_tools)
        self.remote_folders = QComboBox()
        self.remote_folders.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.remote_folders.setMinimumContentsLength(5)
        self.remote_folders.setToolTip("Folders saved for this computer")
        self.remote_folders.activated.connect(self._open_saved_folder)
        self.save_folder = QPushButton("☆ Favorite folder")
        self.save_folder.clicked.connect(self._save_remote_folder)
        folders_row = QHBoxLayout()
        folders_row.addWidget(self.remote_folders, 1)
        self.edit_favorite = QPushButton("Edit favorite…")
        self.edit_favorite.clicked.connect(lambda: self._favorite_menu(self.remote_folders.rect().bottomLeft()))
        folders_row.addWidget(self.edit_favorite)
        folders_row.addWidget(self.save_folder)
        self.quick_save_peer = QPushButton("Save computer")
        self.quick_save_peer.setToolTip("Save this computer, its access code, and its last folder together. Stored privately on this computer.")
        self.quick_save_peer.clicked.connect(self._quick_save_computer)
        folders_row.addWidget(self.quick_save_peer)
        self.remote_folders.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.remote_folders.customContextMenuRequested.connect(self._favorite_menu)
        workspace.insertLayout(2, folders_row)
        self.local_empty = QLabel("Files here are available for connected computers to download. Adding files here does not send them.")
        self.remote_empty = QLabel("Connect to see this computer's shared files.")
        for label, card in ((self.local_empty, local_card), (self.remote_empty, remote_card)):
            label.setObjectName("muted")
            label.setWordWrap(True)
            card.layout().addWidget(label)
        self.remote_empty.setObjectName("emptyState")
        self.remote_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.add_locker_files = QPushButton("Add files…")
        add_menu = QMenu(self.add_locker_files)
        add_menu.addAction("Add files…", lambda: self._pick_locker_items(remote=False, folder=False))
        add_menu.addAction("Add a folder…", lambda: self._pick_locker_items(remote=False, folder=True))
        self.add_locker_files.setMenu(add_menu)
        self.local_manage = QPushButton("Manage selected…")
        manage_menu = QMenu(self.local_manage)
        self.rename_local_action = manage_menu.addAction("Rename…", self._rename_local)
        self.trash_local_action = manage_menu.addAction("Delete — move to Trash…", self._trash_local)
        self.permanent_local_action = manage_menu.addAction("Delete permanently…", self._delete_local_permanently)
        self.local_manage.setMenu(manage_menu)
        local_actions = QHBoxLayout()
        local_actions.addWidget(self.add_locker_files, 1)
        local_actions.addWidget(self.local_manage, 1)
        self.delete_local = QPushButton("Delete…")
        self.delete_local.setToolTip("Move selected files and folders to Trash")
        self.delete_local.clicked.connect(self._trash_local)
        local_actions.addWidget(self.delete_local)
        local_card.layout().insertLayout(2, local_actions)
        for key, callback in (("F2", self._rename_local), ("Delete", self._trash_local)):
            shortcut = QShortcut(QKeySequence(key), self.local_tree)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(callback)
        send_row = QHBoxLayout()
        self.send_files = QPushButton("Send files…")
        self.send_files.setObjectName("sendAction")
        self.send_folder = QPushButton("Send folder…")
        self.send_files.clicked.connect(lambda: self._pick_locker_items(remote=True, folder=False))
        self.send_folder.clicked.connect(lambda: self._pick_locker_items(remote=True, folder=True))
        send_row.addWidget(self.send_files, 1)
        send_row.addWidget(self.send_folder, 1)
        remote_card.layout().insertLayout(2, send_row)
        self.locker_drop = FileDropArea("Or drop files here to share them")
        self.locker_drop.setMinimumHeight(36)
        self.locker_drop.paths_dropped.connect(self._drop_locker_paths)
        local_card.layout().addWidget(self.locker_drop)
        self.remote_drop = FileDropArea("Or drop files here to send them")
        self.remote_drop.setMinimumHeight(36)
        self.remote_drop.paths_dropped.connect(lambda paths: self._drop_external_remote(paths, self.remote_relative))
        remote_card.layout().addWidget(self.remote_drop)
        self.local_tree.paths_dropped.connect(self._drop_external_local)
        self.remote_tree.paths_dropped.connect(self._drop_external_remote)
        self.local_tree.items_dropped.connect(self._drop_items_local)
        self.remote_tree.items_dropped.connect(self._drop_items_remote)
        for tree in (self.local_tree, self.remote_tree):
            tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            tree.customContextMenuRequested.connect(lambda point, view=tree: self._tree_menu(view, point))
        outer.addLayout(body, 1)

        history_toggle = QPushButton("Transfer history")
        history_toggle.setCheckable(True)
        self.activity_list = QListWidget()
        self.activity_list.setMaximumHeight(160)
        self.activity_list.setWordWrap(True)
        self.activity_list.hide()
        self.activity_list.itemDoubleClicked.connect(self._open_activity)
        history_toggle.toggled.connect(self._toggle_history)
        outer.addWidget(self.activity_list)
        self.incoming_notice = QLabel("Files received here appear automatically. Originals stay on the sending computer.")
        self.incoming_notice.setWordWrap(True)
        self.incoming_notice.setObjectName("muted")
        self.incoming_notice.hide()
        footer.addWidget(self.incoming_notice)
        self.copy_destination = QLabel()
        self.copy_destination.setWordWrap(True)
        self.copy_destination.setObjectName("section")
        footer.addWidget(self.copy_destination)
        actions = QHBoxLayout()
        self.download_peer = QPushButton("Receive selected")
        self.upload_peer = QPushButton("Send selected")
        self.download_peer.setEnabled(False)
        self.upload_peer.setEnabled(False)
        self.open_locker = QPushButton("Open folder")
        self.refresh_lockers = QPushButton("Refresh")
        self.cancel_locker = QPushButton("Cancel")
        for button in (self.open_locker, self.refresh_lockers, self.cancel_locker):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self.download_peer)
        actions.addWidget(self.upload_peer)
        footer.addLayout(actions)
        self.replace_files = QCheckBox("Replace existing files")
        self.replace_files.setToolTip("Off by default. Copying keeps originals; completed files remain if cancelled.")
        options_row = QHBoxLayout()
        options_row.addWidget(self.replace_files)
        options_row.addStretch()
        options_row.addWidget(history_toggle)
        footer.addLayout(options_row)
        self.locker_status = QLabel("Choose a computer to get started.")
        self.locker_status.setObjectName("muted")
        self.locker_status.setWordWrap(True)
        self.locker_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.locker_progress = QProgressBar()
        self.locker_progress.setRange(0, 100)
        self.locker_progress.setValue(0)
        self.locker_progress.setMaximumHeight(8)
        footer.addWidget(self.locker_progress)
        footer.addWidget(self.locker_status)
        self.connect_peer.clicked.connect(self._connect_peer)
        self.peer_code.returnPressed.connect(self._connect_peer)
        self.save_peer.clicked.connect(self._save_peer_profile)
        self.delete_peer.clicked.connect(self._delete_peer_profile)
        self.check_peers.clicked.connect(self._find_computers)
        self.peer_combo.currentTextChanged.connect(self._peer_changed)
        self.peer_code.textEdited.connect(self._disconnect_peer)
        self.local_back.clicked.connect(self._local_back)
        self.remote_back.clicked.connect(self._remote_back)
        self.local_home.clicked.connect(lambda: self._go_local(""))
        self.local_inbox.clicked.connect(self._go_inbox)
        self.local_new_folder.clicked.connect(lambda: self._new_folder(False))
        self.remote_home.clicked.connect(lambda: self._load_remote(""))
        self.remote_new_folder.clicked.connect(lambda: self._new_folder(True))
        self.local_tree.itemDoubleClicked.connect(self._local_open)
        self.remote_tree.itemDoubleClicked.connect(self._remote_open)
        self.upload_peer.clicked.connect(self._upload_selected)
        self.download_peer.clicked.connect(self._download_selected)
        self.refresh_lockers.clicked.connect(self._refresh_lockers)
        self.cancel_locker.clicked.connect(self._cancel_locker_transfer)
        self.local_tree.itemSelectionChanged.connect(self._update_locker_controls)
        self.remote_tree.itemSelectionChanged.connect(self._update_locker_controls)
        self.open_locker.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.locker.root / self.local_relative))))
        return container

    def _pick_locker_items(self, *, remote: bool, folder: bool) -> None:
        if self._locker_busy:
            return
        if remote and not self._remote_writable(self.remote_relative):
            return
        if not remote and self.locker_config.read_only:
            return
        noun = "a folder" if folder else "files"
        destination = self._connected_client.peer.name if remote else "My shared files"
        title = f"Choose {noun} to {'send to' if remote else 'add to'} {destination}"
        mode = QFileDialog.FileMode.Directory if folder else QFileDialog.FileMode.ExistingFiles
        dialog = self._path_dialog(title, mode)
        dialog.setLabelText(QFileDialog.DialogLabel.Accept, "Send" if remote else "Add")
        if dialog.exec() != QFileDialog.DialogCode.Accepted:
            return
        paths = dialog.selectedFiles()
        if not paths:
            return
        if remote:
            self._drop_external_remote(paths, self.remote_relative)
        else:
            self._import_paths(paths, self.local_relative)

    def _update_connection_guide(self) -> None:
        if self._locker_busy:
            self.connection_summary.setText("Working… You can follow progress below or cancel the current action.")
        elif self._connection_problem:
            self.connection_summary.setText("Connection interrupted. Check that FastFiles is open on the other computer, then reconnect.")
        elif self._connected_client:
            name = self._connected_client.peer.name
            if self._remote_writable(self.remote_relative):
                self.connection_summary.setText(f"Connected to {name}. Choose Send files or Send folder — no need to add them to your Locker first.")
            else:
                self.connection_summary.setText(f"Connected to {name}. You can browse permitted files. To send, open a folder that allows uploads.")
        elif self.peer_combo.currentText().strip():
            self.connection_summary.setText("On the other computer, click My pairing code. Enter its code here, then click Connect.")
        else:
            self.connection_summary.setText("Open FastFiles on both computers. Choose the receiving computer from the list, or enter its IP address above.")

    def _show_locker_help(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Send your first files")
        dialog.setMinimumWidth(420)
        dialog.setMaximumWidth(560)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        for title, description in (
            ("1  Open FastFiles on both computers", "Use the same trusted network or VPN. Choose the receiving computer from Your computers, or enter its IP address."),
            ("2  Connect with its pairing code", "On the receiving computer, click My pairing code. Type that code on the sending computer and click Connect. If you were given an access key, use that instead."),
            ("3  Choose files and send", "Click Send files or Send folder on the right. Your selection goes directly to the destination shown above it. You can also drag files there. Originals stay on the sending computer."),
            ("What is My shared files?", "It is your Locker: files you make available for other computers to download. You do not need to put files there before sending them. To receive, select items on the right and click Receive selected."),
        ):
            heading = QLabel(title)
            heading.setObjectName("stepTitle")
            heading.setWordWrap(True)
            label = QLabel(description)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(heading)
            layout.addWidget(label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Got it")
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "remote_drop"):
            return
        compact = self.height() < 720
        self.computer_sidebar.setVisible(self.width() >= 1150)
        self.locker_subtitle.setVisible(not compact)
        self.local_tools.setVisible(not compact)
        self.remote_tools.setVisible(not compact and self._connected_client is not None)
        self.locker_drop.setVisible(not compact)
        self.remote_drop.setVisible(not compact)
        for tree in (self.local_tree, self.remote_tree):
            tree.setMinimumHeight(120 if compact else 180)
        self._compact = compact

    def _tree_menu(self, tree: LockerTree, point) -> None:
        remote = tree is self.remote_tree
        clicked = tree.itemAt(point)
        if clicked is not None and not clicked.isSelected():
            tree.clearSelection()
            tree.setCurrentItem(clicked)
        menu = QMenu(tree)
        if not remote:
            menu.addAction(self.rename_local_action)
            menu.addAction(self.trash_local_action)
            menu.addAction(self.permanent_local_action)
            menu.addSeparator()
        buttons = (
            (self.download_peer, self.remote_home, self.remote_new_folder, self.save_folder)
            if remote else (self.upload_peer, self.local_home, self.local_inbox, self.local_new_folder)
        )
        for button in buttons:
            action = menu.addAction(button.text())
            action.setEnabled(button.isEnabled())
            action.triggered.connect(button.click)
        menu.exec(tree.viewport().mapToGlobal(point))

    def _computer_menu(self, point) -> None:
        item = self.computers.itemAt(point)
        if item is None or self._locker_busy:
            return
        index = self.peer_combo.findData(item.data(Qt.ItemDataRole.UserRole))
        if index < 0:
            return
        self.peer_combo.setCurrentIndex(index)
        menu = QMenu(self.computers)
        for button in (self.connect_peer, self.edit_peer, self.save_peer, self.delete_peer):
            action = menu.addAction(button.text())
            action.setEnabled(button.isEnabled())
            action.triggered.connect(button.click)
        menu.exec(self.computers.viewport().mapToGlobal(point))

    def _rename_local(self) -> None:
        items = self.local_tree.selectedItems()
        if self._locker_busy or self.locker_config.read_only or len(items) != 1:
            return
        relative = items[0].data(0, Qt.ItemDataRole.UserRole)
        name, accepted = QInputDialog.getText(self, "Rename item", "New name", text=posixpath.basename(relative))
        if not accepted or name == posixpath.basename(relative):
            return
        try:
            if not name.strip() or relative_path(name) != name or "/" in name:
                raise ValueError("Enter a single file or folder name")
            source = self.locker.resolve(relative)
            target = self.locker.resolve(posixpath.join(posixpath.dirname(relative), name), must_exist=False)
            if source == self.locker.root:
                raise ValueError("The locker itself cannot be renamed")
            # Qt refuses to overwrite an existing destination, including a racing create.
            file = QFile(str(source))
            if not file.rename(str(target)):
                raise OSError(file.errorString())
            self._refresh_local_locker()
            self.locker_status.setText(f'Renamed to “{name}”.')
        except (OSError, ValueError) as error:
            self._locker_error(str(error))

    def _trash_local(self) -> None:
        items = self.local_tree.selectedItems()
        if self._locker_busy or self.locker_config.read_only or not items:
            return
        paths = [item.data(0, Qt.ItemDataRole.UserRole) for item in items]
        if not self._confirm("Move to Trash", f"Move {len(paths)} selected item(s) to Trash? "
                             "Selected folders include all their contents. Restore them using your file manager."):
            return
        moved = 0
        try:
            for relative in paths:
                source = self.locker.resolve(relative)
                if source == self.locker.root:
                    raise ValueError("The locker itself cannot be removed")
                file = QFile(str(source))
                if not file.moveToTrash():
                    raise OSError(f"Could not move {source.name} to Trash: {file.errorString()}. "
                                  "Use Manage selected → Delete permanently if you want to remove it without Trash.")
                moved += 1
            self.locker_status.setText(f"Moved {moved} item(s) to Trash.")
        except (OSError, ValueError) as error:
            self._locker_error(f"{moved} item(s) moved to Trash. {error}")
        finally:
            self._refresh_local_locker()

    def _delete_local_permanently(self) -> None:
        items = self.local_tree.selectedItems()
        if self._locker_busy or self.locker_config.read_only or not items:
            return
        paths = [item.data(0, Qt.ItemDataRole.UserRole) for item in items]
        if not self._confirm("Delete permanently", f"Permanently delete {len(paths)} selected item(s)? "
                             "This includes all contents of selected folders and cannot be undone."):
            return
        deleted = 0
        try:
            for relative in paths:
                source = self.locker.resolve(relative)
                if source == self.locker.root:
                    raise ValueError("The locker itself cannot be removed")
                if source.is_dir():
                    shutil.rmtree(source)
                else:
                    source.unlink()
                deleted += 1
            self.locker_status.setText(f"Permanently deleted {deleted} item(s).")
        except (OSError, ValueError) as error:
            self._locker_error(f"{deleted} item(s) deleted. {error}")
        finally:
            self._refresh_local_locker()

    def _edit_profile(self, remote: bool) -> None:
        if (remote and self._locker_busy) or (not remote and self._process is not None):
            return
        try:
            profile = (self._saved_profile() if remote else next(
                (p for p in self.profile_store.direct() if p.name == self.direct_profile.currentData()), None))
            if profile is None:
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("Edit saved computer")
            dialog.setMinimumWidth(400)
            layout = QVBoxLayout(dialog)
            form = QFormLayout()
            name = QLineEdit(profile.name)
            address = QLineEdit(self._endpoint(Peer("", "", profile.address, profile.port)) if remote else profile.host)
            form.addRow("Name", name)
            form.addRow("Address and port" if remote else "SSH host", address)
            folder = QLineEdit(profile.last_folder if remote else profile.remote_path)
            form.addRow("Default folder", folder)
            layout.addLayout(form)
            error_label = QLabel()
            error_label.setWordWrap(True)
            layout.addWidget(error_label)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            layout.addWidget(buttons)
            buttons.rejected.connect(dialog.reject)

            def save():
                try:
                    if remote:
                        peer = parse_peer_address(address.text().strip())
                        changed_endpoint = (peer.address, peer.port) != (profile.address, profile.port)
                        updated = replace(profile, name=name.text().strip(), address=peer.address, port=peer.port,
                                          last_folder=folder.text().strip(),
                                          device_id="" if changed_endpoint else profile.device_id)
                    else:
                        updated = DirectProfile(name.text().strip(), address.text().strip(), folder.text().strip())
                    self.profile_store.update_profile(profile.name, updated)
                except (OSError, ValueError) as error:
                    error_label.setText(str(error))
                    return
                dialog.accept()
                if remote:
                    self._disconnect_peer()
                    self._update_peers(self._discovered)
                    self.peer_combo.setCurrentIndex(self.peer_combo.findData(f"saved:{updated.name}"))
                    self._peer_changed()
                    self.locker_status.setText("Computer updated. Connect when ready.")
                else:
                    self._refresh_direct_profiles(updated.name)
                    self._load_direct_profile()

            buttons.accepted.connect(save)
            dialog.exec()
        except (OSError, ValueError) as error:
            self._locker_error(str(error))

    def _toggle_history(self, shown: bool) -> None:
        self.activity_list.setVisible(shown)
        if shown:
            QTimer.singleShot(0, lambda: self.locker_scroll.ensureWidgetVisible(self.activity_list))

    @staticmethod
    def _valid_credential(value: str) -> bool:
        return bool(value) and len(value) <= 256 and value.isascii() and all(33 <= ord(c) <= 126 for c in value)

    def _saved_profile(self) -> PeerProfile | None:
        peer = self._selected_peer()
        key = peer.device_id if peer else ""
        if not key.startswith("saved:"):
            return None
        return next((p for p in self.profile_store.peers() if p.name == key.removeprefix("saved:")), None)

    def _computer_selected(self, item: QListWidgetItem) -> None:
        if self._locker_busy:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        index = self.peer_combo.findData(key)
        if index >= 0:
            self.peer_combo.setCurrentIndex(index)
            if self._connected_client is None:
                self.locker_status.setText("Computer selected. Connect to browse, or use Forget to remove it.")

    def _drop_on_computer(self, key: str, payload: dict) -> None:
        if self._locker_busy:
            return
        index = self.peer_combo.findData(key)
        if index < 0:
            return
        self.peer_combo.setCurrentIndex(index)
        if self._connected_client:
            self._perform_pending_drop(payload)
        else:
            self._pending_drop = (key, payload)
            if self.peer_code.text().strip():
                self._connect_peer()
            else:
                self.locker_status.setText("Enter this computer's code or key and Connect to send the dropped items.")
                self.peer_code.setFocus()

    def _perform_pending_drop(self, payload: dict) -> None:
        if "paths" in payload:
            self._drop_external_remote(payload["paths"], self.remote_relative)
        elif payload.get("side") == "local":
            self._drop_items_remote("local", payload["items"], self.remote_relative, payload.get("connection_id", ""))

    def _refresh_saved_folders(self) -> None:
        self.remote_folders.blockSignals(True)
        self.remote_folders.clear()
        self.remote_folders.addItem("Favorite folders", None)
        try:
            profile = self._saved_profile()
            if profile:
                for name, path in profile.folders.items():
                    self.remote_folders.addItem(name, path)
        except (OSError, ValueError) as error:
            self.locker_status.setText(str(error))
        index = self.remote_folders.findData(self.remote_relative)
        if index >= 0:
            self.remote_folders.setCurrentIndex(index)
        self.remote_folders.blockSignals(False)

    def _remember_remote_folder(self) -> None:
        try:
            profile = self._saved_profile()
            if profile and self._connected_client:
                device_id = self._remote_info.get("device_id", "")
                if profile.last_folder != self.remote_relative or profile.device_id != device_id:
                    self.profile_store.save_peer(replace(profile, last_folder=self.remote_relative, device_id=device_id))
        except (OSError, ValueError) as error:
            self.locker_status.setText(f"Could not remember this folder: {error}")

    def _save_connected_computer(self) -> PeerProfile:
        client = self._client()
        profile = self._saved_profile()
        profiles = self.profile_store.peers()
        device_id = self._remote_info.get("device_id", "")
        if profile is None:
            profile = next((entry for entry in profiles if
                            (entry.address, entry.port) == (client.peer.address, client.peer.port)
                            and entry.device_id in ("", device_id)), None)
        if profile is None:
            reported_name = self._remote_info.get("device_name")
            base = reported_name.strip() if isinstance(reported_name, str) and reported_name.strip() else client.peer.name
            name = base
            used = {entry.name.casefold() for entry in profiles}
            number = 2
            while name.casefold() in used:
                name = f"{base} ({number})"
                number += 1
            profile = PeerProfile(name, client.peer.address, client.peer.port)
        profile = replace(profile, device_id=device_id, last_folder=self.remote_relative)
        self.secret_store.set(profile.secret_id, client.code)
        self.profile_store.save_peer(profile)
        self._update_peers(self._discovered)
        self.peer_combo.blockSignals(True)
        key = f"saved:{profile.name}"
        self.peer_combo.setCurrentIndex(self.peer_combo.findData(key))
        self.peer_combo.blockSignals(False)
        self._selection_identity = (profile.address, profile.port, key)
        client.peer = Peer(key, profile.name, profile.address, profile.port)
        self._connected_client = client
        self._refresh_saved_folders()
        self._update_locker_controls()
        return profile

    def _quick_save_computer(self) -> None:
        try:
            profile = self._save_connected_computer()
            self.locker_status.setText(f"{profile.name}, its access code, and last folder saved. Select it next time to connect.")
        except (OSError, ValueError) as error:
            self._locker_error(str(error))

    def _save_remote_folder(self) -> None:
        if self._locker_busy or not self._connected_client:
            return
        try:
            profile = self._save_connected_computer()
            folders = dict(profile.folders)
            existing = next((name for name, path in folders.items() if path == self.remote_relative), None)
            if existing is not None:
                del folders[existing]
                message = "Favorite removed. The folder and its files are unchanged."
            else:
                base = posixpath.basename(self.remote_relative) or "Home"
                name, number = base, 2
                while name in folders:
                    name = f"{base} ({number})"
                    number += 1
                folders[name] = self.remote_relative
                message = f"{name} saved in Favorite folders. The computer is saved too."
            self.profile_store.save_peer(replace(profile, folders=folders, last_folder=self.remote_relative))
            self._refresh_saved_folders()
            self._update_locker_controls()
            self.locker_status.setText(message)
        except (OSError, ValueError) as error:
            self._locker_error(str(error))

    def _favorite_menu(self, point) -> None:
        if self._locker_busy:
            return
        path = self.remote_folders.currentData()
        if path is None:
            return
        menu = QMenu(self.remote_folders)
        rename = menu.addAction("Rename favorite…")
        edit_path = menu.addAction("Edit folder path…")
        remove = menu.addAction("Remove favorite")
        action = menu.exec(self.remote_folders.mapToGlobal(point))
        if action is None:
            return
        try:
            profile = self._saved_profile()
            if profile is None:
                return
            old_name = self.remote_folders.currentText()
            folders = dict(profile.folders)
            if action == rename:
                name, accepted = QInputDialog.getText(self, "Rename favorite", "Name", text=old_name)
                name = name.strip()
                if not accepted or not name or name == old_name:
                    return
                if name in folders:
                    raise ValueError("Another favorite already has that name. Choose a different name.")
                folders[name] = path
            elif action == edit_path:
                new_path, accepted = QInputDialog.getText(
                    self, "Edit favorite folder", "Path inside this locker (empty for Home)", text=path)
                if not accepted:
                    return
                folders[old_name] = relative_path(new_path.strip())
            elif action != remove:
                return
            if action != edit_path:
                folders.pop(old_name, None)
            self.profile_store.save_peer(replace(profile, folders=folders))
            self._refresh_saved_folders()
            self._update_locker_controls()
        except (OSError, ValueError) as error:
            self._locker_error(str(error))

    def _open_saved_folder(self, index: int) -> None:
        path = self.remote_folders.itemData(index)
        if path is not None and self._connected_client:
            self._load_remote(path)

    def _go_local(self, relative: str) -> None:
        if self._locker_busy:
            return
        self._view_revision += 1
        self.local_relative = relative
        self._refresh_local_locker()

    def _go_inbox(self) -> None:
        try:
            target = self.locker.resolve("Inbox", must_exist=False)
            if not target.exists():
                if self.locker_config.read_only:
                    raise PermissionError("This locker is read-only")
                target.mkdir()
            self._go_local("Inbox")
        except (OSError, ValueError) as error:
            self._locker_error(str(error))

    def _new_folder(self, remote: bool) -> None:
        name, accepted = QInputDialog.getText(self, "New folder", "Folder name")
        if not accepted or not name.strip():
            return
        try:
            name = name.strip()
            if relative_path(name) != name or "/" in name:
                raise ValueError("Enter a single folder name")
            directory = self.remote_relative if remote else self.local_relative
            target = posixpath.join(directory, name)
            if remote:
                client = self._client()
                self._run_locker_task("new_folder", lambda: client.mkdir(target))
            else:
                if self.locker_config.read_only:
                    raise PermissionError("This locker is read-only")
                self.locker.resolve(target, must_exist=False).mkdir()
                self._refresh_local_locker()
        except (OSError, ValueError) as error:
            self._locker_error(str(error))

    def _remote_writable(self, directory: str) -> bool:
        if self._connection_problem or not self._connected_client or self._remote_info.get("read_only", False):
            return False
        if not self._remote_info.get("can_upload", True):
            return False
        patterns = self._remote_info.get("upload_patterns", ["**"])
        return can_traverse(directory, patterns)

    def _upload_targets_allowed(self, names: list[str], directory: str) -> bool:
        if not self._remote_writable(directory):
            return False
        patterns = self._remote_info.get("upload_patterns", ["**"])
        return all(path_permitted(posixpath.join(directory, name), patterns) for name in names)

    def _drop_external_local(self, paths: list[str], directory: str) -> None:
        if self._locker_busy or self.locker_config.read_only:
            return
        self._import_paths(paths, directory)

    def _drop_external_remote(self, paths: list[str], directory: str) -> None:
        if self._locker_busy:
            return
        try:
            client = self._client()
            if not self._upload_targets_allowed([Path(path).name for path in paths], directory):
                raise PermissionError("Uploading these items is not permitted here. Choose a folder that allows uploads.")
            overwrite = self.replace_files.isChecked()
            self._copy_names = ", ".join(Path(path).name for path in paths[:3])
            self.locker_status.setText(f"Sending {len(paths)} item(s) to {client.peer.name}/{directory or ''}…")
            self._run_locker_task("send", lambda: upload_paths_to_peer(
                client, paths, directory, self._transfer_progress, self._locker_cancel, overwrite=overwrite))
        except (OSError, ValueError) as error:
            self._locker_error(str(error))

    def _drop_items_local(self, side: str, items: list[dict], directory: str, connection_id: str) -> None:
        if side != "remote" or self._locker_busy or self.locker_config.read_only:
            return
        if not self._connected_client or connection_id != self._endpoint(self._connected_client.peer):
            self._locker_error("The source computer changed. Select the files again.")
            return
        self._receive_items([(item["path"], item["is_dir"]) for item in items], directory)

    def _drop_items_remote(self, side: str, items: list[dict], directory: str, connection_id: str) -> None:
        if side != "local" or self._locker_busy:
            return
        self._send_items([item["path"] for item in items], directory)

    def _show_sharing_settings(self) -> None:
        from .sharing_dialog import SharingDialog

        dialog = SharingDialog(self.locker_config, self.config_path, self)
        dialog.exec()
        self.own_code.setEnabled(not self.locker_config.uses_computer_keys)
        self.own_code.setToolTip("Computer-specific keys are required" if self.locker_config.uses_computer_keys else "Pairing code")
        self._update_locker_controls()
        if dialog.changed:
            if self.locker_service:
                self.sharing_status.setText("Sharing permissions updated for new requests.")
            else:
                self.sharing_status.setText("Sharing permissions saved. Restart the background service to apply them.")
            self.sharing_status.show()
            self.own_details.show()

    def _refresh_activity(self) -> None:
        try:
            entries = self.activity_store.recent()
        except (OSError, ValueError) as error:
            self.incoming_notice.setText(f"Could not read transfer history: {error}")
            self.incoming_notice.show()
            return
        ids = {entry["id"] for entry in entries}
        if ids == self._activity_ids and self._activity_loaded:
            return
        new = [entry for entry in entries if entry["id"] not in self._activity_ids and entry["kind"] == "received"]
        if new and self._activity_loaded:
            entry = new[0]
            self.incoming_notice.setText(f"Received {Path(entry['destination']).name} from {entry['source']} · See Transfer history")
            self.incoming_notice.setToolTip(entry["destination"])
            self.incoming_notice.show()
        self._activity_ids = ids
        self._activity_loaded = True
        self.activity_list.clear()
        for entry in entries:
            label = f"{entry['kind'].capitalize()} · {entry['files']} file(s) · {self._human_size(entry['bytes'])} · {entry['destination']}"
            item = QListWidgetItem(label)
            item.setToolTip(f"{entry['time']} · {entry['source']}")
            item.setData(Qt.ItemDataRole.UserRole, entry["destination"])
            self.activity_list.addItem(item)

    def _open_activity(self, item: QListWidgetItem) -> None:
        destination = item.data(Qt.ItemDataRole.UserRole)
        path = Path(destination)
        if path.is_absolute() and path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.is_dir() else path.parent)))

    def _poll_lockers(self) -> None:
        if self._closed or self._locker_busy or self._refreshing:
            return
        self._refresh_local_locker(quiet=True)
        self._refresh_activity()
        client = self._connected_client
        if client is None:
            return
        self._refreshing = True
        revision, directory = self._view_revision, self.remote_relative

        def task():
            try:
                result = (client.info(), client.list(directory))
                error = ""
            except (OSError, ValueError) as exc:
                result, error = None, str(exc)
            if not self._closed:
                self.locker_signals.snapshot.emit((client, directory, revision, result, error))
        self._workers.submit(task)

    def _snapshot_ready(self, value) -> None:
        self._refreshing = False
        client, directory, revision, result, error = value
        if (self._closed or self._locker_busy or client is not self._connected_client
                or directory != self.remote_relative or revision != self._view_revision):
            return
        if error:
            self._connection_problem = error
            self.connection_summary.setText(f"Connection needs attention: {error}")
            self._fill_tree(self.remote_tree, [])
            self.remote_empty.setText("This computer is unavailable or access has changed. Retrying automatically…")
            self.remote_empty.show()
            self._update_locker_controls()
            return
        info, _items = result
        if info.get("device_id") != self._remote_info.get("device_id") or info.get("protocol") != PROTOCOL_VERSION:
            self._disconnect_peer()
            self.connection_summary.setText("The computer identity or version changed. Connect again to verify it.")
            return
        self._connection_problem = ""
        self._remote_info, items = result
        self._show_remote_items((directory, items))

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
        self.edit_direct_profile.setEnabled(bool(self.direct_profile.currentData()))
        self.delete_direct_profile.setEnabled(bool(self.direct_profile.currentData()))

    def _load_direct_profile(self) -> None:
        name = self.direct_profile.currentData()
        self.edit_direct_profile.setEnabled(bool(name))
        self.delete_direct_profile.setEnabled(bool(name))
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
        name, accepted = QInputDialog.getText(self, "Save computer as", "Computer name", text=peer.name)
        if not accepted or not name.strip():
            return
        code = self.peer_code.text().strip()
        try:
            existing_profile = next((item for item in self.profile_store.peers() if item.name == name.strip()), None)
            profile = PeerProfile(name.strip(), peer.address, peer.port)
            if existing_profile and (existing_profile.address, existing_profile.port) == (peer.address, peer.port):
                profile = replace(existing_profile, name=name.strip())
            if self._connected_client:
                profile = replace(profile, device_id=self._remote_info.get("device_id", ""), last_folder=self.remote_relative)
            existing = existing_profile is not None
            if existing and (existing_profile.address, existing_profile.port) != (peer.address, peer.port) and not self._confirm(
                "Replace saved machine", f"Replace the saved address for {profile.name}?"
            ):
                return
            if not self._valid_credential(code):
                raise ValueError("Enter the pairing code or access key so this computer can be saved with its access")
            self.secret_store.set(profile.secret_id, code)
            self.profile_store.save_peer(profile)
        except (OSError, ValueError) as error:
            self._locker_error(str(error))
            return
        self._update_peers(self._discovered)
        self.peer_combo.blockSignals(True)
        self.peer_combo.setCurrentIndex(self.peer_combo.findData(f"saved:{profile.name}"))
        self.peer_combo.blockSignals(False)
        self._selection_identity = (peer.address, peer.port, f"saved:{profile.name}")
        if self._connected_client:
            self._connected_client.peer = Peer(f"saved:{profile.name}", profile.name, peer.address, peer.port)
            self._show_remote_items((self.remote_relative, self.remote_tree._last_items or []))
        self._refresh_saved_folders()
        self._check_availability()

    def _delete_peer_profile(self) -> None:
        if self._locker_busy:
            return
        try:
            peer = self._selected_peer()
            if peer is None:
                return
            profile = self._saved_profile()
            if not self._confirm("Forget computer", f"Forget {peer.name}? Saved access will be removed "
                                 "when no other alias uses it. This computer will stay hidden until you "
                                 "choose Find computers. Files on both computers are unchanged."):
                return
            self.profile_store.forget_peer(self._endpoint(peer), profile.name if profile else None)
            self._disconnect_peer()
            self.peer_combo.setCurrentIndex(-1)
            self.peer_combo.setEditText("")
            self._update_peers(self._discovered)
            self.locker_status.setText(f"Forgot {peer.name}. Find computers can show it again.")
            if profile and not any(item.secret_id == profile.secret_id for item in self.profile_store.peers()):
                self.secret_store.delete(profile.secret_id)
        except (OSError, ValueError) as error:
            self._locker_error(str(error))

    def _find_computers(self) -> None:
        if self._locker_busy:
            return
        try:
            self.profile_store.restore_hidden_peers()
            self._update_peers(self._discovered)
            self._check_availability()
        except (OSError, ValueError) as error:
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
        tree = LockerTree()
        tree.setHeaderLabels(["Name", "Size"])
        tree.setMinimumWidth(170)
        tree.setRootIsDecorated(False)
        tree.setMinimumHeight(180)
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(True)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        layout.addLayout(header)
        layout.addWidget(path)
        layout.addWidget(tree, 1)
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
        if getattr(tree, "_last_items", None) == items:
            return
        tree._last_items = items
        selected = {item.data(0, Qt.ItemDataRole.UserRole) for item in tree.selectedItems()}
        scroll = tree.verticalScrollBar().value()
        tree.blockSignals(True)
        tree.clear()
        for entry in items:
            size = "Folder" if entry["is_dir"] else self._human_size(entry["size"])
            item = QTreeWidgetItem([entry["name"], size])
            item.setData(0, Qt.ItemDataRole.UserRole, entry["path"])
            item.setData(0, Qt.ItemDataRole.UserRole + 1, entry["is_dir"])
            item.setData(0, Qt.ItemDataRole.UserRole + 2, entry)
            if entry.get("can_download") is False:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            icon = QStyle.StandardPixmap.SP_DirIcon if entry["is_dir"] else QStyle.StandardPixmap.SP_FileIcon
            item.setIcon(0, self.style().standardIcon(icon))
            access = ""
            if "can_download" in entry:
                access = " · " + ", ".join(
                    label for key, label in (("can_download", "Download"), ("can_upload", "Upload"))
                    if entry.get(key)
                )
            item.setToolTip(0, entry["name"] + access)
            tree.addTopLevelItem(item)
            item.setSelected(entry["path"] in selected)
        tree.verticalScrollBar().setValue(scroll)
        tree.blockSignals(False)

    def _refresh_local_locker(self, *, quiet: bool = False) -> None:
        try:
            self._fill_tree(self.local_tree, self.locker.list(self.local_relative))
            self.local_tree.directory = self.local_relative
            self.local_tree.connection_id = str(self.locker.root)
            self.local_path_label.setText("Shared files / " + self.local_relative)
            self.local_path_label.setToolTip(str(self.locker.root / self.local_relative))
            self.local_empty.setVisible(self.local_tree.topLevelItemCount() == 0)
            self._update_locker_controls()
        except (FileNotFoundError, NotADirectoryError):
            if self.local_relative:
                self.local_relative = ""
                self._view_revision += 1
                self._refresh_local_locker(quiet=quiet)
                self.incoming_notice.setText("The folder was removed. Showing My locker Home.")
                self.incoming_notice.show()
            else:
                self._fill_tree(self.local_tree, [])
                self.incoming_notice.setText("My locker folder is unavailable. Restore it or restart FastFiles.")
                self.incoming_notice.show()
        except (OSError, ValueError) as error:
            if quiet:
                self.incoming_notice.setText(f"Could not refresh My locker: {error}")
                self.incoming_notice.show()
            else:
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
            hidden = self.profile_store.hidden_peers()
        except (OSError, ValueError) as error:
            profiles = []
            hidden = set()
            self.availability_label.setText(str(error))
            logger.error("Cannot read saved peers: %s", error)
        saved = [Peer(f"saved:{p.name}", p.name, p.address, p.port) for p in profiles]
        endpoints = {(peer.address, peer.port) for peer in saved}
        combined = saved + [peer for peer in peers if (peer.address, peer.port) not in endpoints
                            and self._endpoint(peer) not in hidden]
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
        self.computers.blockSignals(True)
        self.computers.clear()
        for peer in combined:
            status = self._peer_status.get((peer.address, peer.port), "Checking")
            origin = "Saved" if peer.device_id.startswith("saved:") else "Discovered"
            item = QListWidgetItem(f"{peer.name}\n{status} · {origin}")
            item.setData(Qt.ItemDataRole.UserRole, peer.device_id)
            item.setToolTip(f"{self._endpoint(peer)} · Drop files to send")
            self.computers.addItem(item)
            if peer.device_id == current_id:
                self.computers.setCurrentItem(item)
        self.computers.blockSignals(False)
        self.computers_hint.setVisible(not combined)
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
        self._connection_problem = ""
        self._view_revision += 1
        self._remote_info = {}
        self.remote_relative = ""
        self.remote_tree.clear()
        self.remote_tree._last_items = None
        self.remote_tree.connection_id = ""
        self.remote_path_label.setText("Not connected")
        self.remote_empty.setText("Your files arrive here.\nConnect to the other computer above, then choose Send files or Send folder.")
        self.remote_empty.show()
        self.locker_status.setText("Enter this computer’s pairing code or access key, then Connect.")
        self.connection_summary.setText("Not connected")
        self._update_locker_controls()

    def _peer_changed(self, *_args) -> None:
        if self._locker_busy:
            return
        try:
            peer = self._selected_peer()
        except ValueError:
            peer = None
        endpoint = (peer.address, peer.port) if peer else None
        identity = (*endpoint, peer.device_id) if peer else None
        if identity == getattr(self, "_selection_identity", None):
            return
        self._selection_identity = identity
        self._pending_drop = None
        self._disconnect_peer()
        self.peer_code.clear()
        if peer and peer.device_id.startswith("saved:"):
            remembered = self.secret_store.get(f"peer:{peer.address}:{peer.port}")
            if remembered:
                self.peer_code.setText(remembered)
        self._refresh_saved_folders()
        self._update_locker_controls()

    def _selected_peer(self) -> Peer | None:
        peer = self.peers.get(self.peer_combo.currentData())
        selected_text = self.peer_combo.itemText(self.peer_combo.currentIndex())
        if peer and self.peer_combo.currentText() == selected_text:
            return peer
        text = self.peer_combo.currentText().strip()
        alias = next((peer for peer in self.peers.values() if peer.name.casefold() == text.casefold()), None)
        if alias:
            return alias
        return parse_peer_address(text) if text else None

    def _client(self) -> PeerClient:
        if self._connected_client is None:
            raise ValueError("Connect to the machine before transferring files")
        if self._connection_problem:
            raise ValueError("This computer is unavailable or access changed. Wait for it to reconnect or click Connect.")
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
        writable = self._remote_writable(self.remote_relative)
        for widget in (
            self.peer_combo, self.peer_code, self.connect_peer,
            self.save_peer, self.replace_files, self.computers, self.sharing_settings,
        ):
            widget.setEnabled(idle)
        self.computers.drop_enabled = idle
        self.edit_peer.setEnabled(idle and str(self.peer_combo.currentData()).startswith("saved:"))
        selected = self.local_tree.selectedItems()
        editable = idle and not self.locker_config.read_only
        self.local_manage.setText(f"Manage {len(selected)} selected…" if selected else "Manage selected…")
        self.local_manage.setEnabled(editable and bool(selected))
        self.rename_local_action.setEnabled(editable and len(selected) == 1)
        self.trash_local_action.setEnabled(editable and bool(selected))
        self.permanent_local_action.setEnabled(editable and bool(selected))
        self.delete_local.setEnabled(editable and bool(selected))
        try:
            selected_peer = self._selected_peer()
        except ValueError:
            selected_peer = None
        self.delete_peer.setEnabled(idle and selected_peer is not None)
        self.locker_drop.setEnabled(idle and not self.locker_config.read_only)
        self.remote_drop.setEnabled(idle and writable)
        self.local_tree.setEnabled(idle)
        self.local_tree.drop_enabled = idle and not self.locker_config.read_only
        self.remote_tree.setEnabled(idle and connected)
        self.remote_tree.drop_enabled = idle and writable
        self.local_back.setEnabled(idle and bool(self.local_relative))
        self.local_home.setEnabled(idle and bool(self.local_relative))
        self.local_inbox.setEnabled(idle)
        self.local_new_folder.setEnabled(idle and not self.locker_config.read_only)
        self.remote_back.setEnabled(idle and connected and bool(self.remote_relative))
        self.remote_home.setEnabled(idle and connected)
        self.remote_new_folder.setEnabled(idle and writable)
        self.remote_folders.setEnabled(idle and connected)
        self.save_folder.setEnabled(idle and connected)
        self.remote_folders.setVisible(connected)
        self.save_folder.setVisible(connected)
        self.edit_favorite.setVisible(connected)
        self.edit_favorite.setEnabled(idle and connected and self.remote_folders.currentData() is not None)
        self.quick_save_peer.setVisible(connected)
        try:
            saved_profile = self._saved_profile()
        except (OSError, ValueError):
            saved_profile = None
        self.quick_save_peer.setEnabled(idle and connected and saved_profile is None)
        self.quick_save_peer.setText("✓ Saved" if saved_profile else "Save computer")
        favorite = saved_profile is not None and self.remote_relative in saved_profile.folders.values()
        self.save_folder.setText("★ Favorited" if favorite else "☆ Favorite folder")
        self.save_folder.setToolTip("Click to remove this favorite" if favorite else
                                   "Save this folder and computer in one click. Right-click the favorite list to rename it.")
        self.refresh_lockers.setEnabled(idle)
        self.cancel_locker.setEnabled(self._locker_busy and not self._locker_cancel.is_set())
        self.cancel_locker.setVisible(self._locker_busy)
        self.locker_progress.setVisible(self._locker_busy or self.locker_progress.value() > 0)
        self.send_files.setEnabled(idle and writable)
        self.send_folder.setEnabled(idle and writable)
        self.add_locker_files.setEnabled(idle and not self.locker_config.read_only)
        self.remote_tree.setVisible(connected)
        self.remote_empty.setMinimumHeight(0 if connected else 100)
        self.remote_empty.setSizePolicy(QSizePolicy.Policy.Preferred,
                                        QSizePolicy.Policy.Preferred if connected else QSizePolicy.Policy.Expanding)
        self.remote_tools.setVisible(connected and self.height() >= 720)
        self.remote_empty.setVisible(not connected or self.remote_tree.topLevelItemCount() == 0)
        if not connected:
            self.remote_empty.setText("Your files arrive here.\nConnect above, then choose files or a whole folder to send.")
        self._update_connection_guide()
        upload_names = [posixpath.basename(item.data(0, Qt.ItemDataRole.UserRole)) for item in self.local_tree.selectedItems()]
        self.upload_peer.setEnabled(idle and bool(upload_names) and self._upload_targets_allowed(upload_names, self.remote_relative))
        downloadable = bool(self.remote_tree.selectedItems()) and all(
            (item.data(0, Qt.ItemDataRole.UserRole + 2) or {}).get("can_download", True)
            for item in self.remote_tree.selectedItems()
        )
        self.download_peer.setEnabled(
            idle and connected and not self._connection_problem and not self.locker_config.read_only and downloadable)
        self.copy_destination.setText(
            f"Sending to {self._connected_client.peer.name} / {self.remote_relative or 'Shared files'}"
            f"  ·  Receive into My shared files / {self.local_relative or 'Home'}" if connected else
            "Connect above to send files. Or add files on the left for others to download."
        )
        selected_count = len(upload_names)
        self.upload_peer.setText(f"Send {selected_count} selected" if selected_count else "Send selected")
        received_count = len(self.remote_tree.selectedItems())
        self.download_peer.setText(f"Receive {received_count} selected" if received_count else "Receive selected")
        self.upload_peer.setToolTip("Select files from My shared files to send to the open folder on the right.")
        self.download_peer.setToolTip("Select files on the right to copy into the open folder on the left.")
        self.send_files.setToolTip("Choose files anywhere on this computer and send them directly to the destination above.")
        self.send_folder.setToolTip("Send a whole folder, including its contents, directly to the destination above.")
        self.locker_drop.setToolTip(str(self.locker.root / self.local_relative))
        if connected:
            self.remote_drop.setToolTip(f"Copy to {self._connected_client.peer.name}/{self.remote_relative}")

    def _run_locker_task(self, kind: str, task) -> None:
        if self._locker_busy:
            return
        self._locker_busy = True
        self._view_revision += 1
        self._locker_cancel.clear()
        self._task_id += 1
        task_id = self._task_id
        if kind in ("send", "receive", "import"):
            self.locker_progress.setValue(0)
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
            if not self._valid_credential(code):
                raise ValueError("Enter the computer’s pairing code or access key")
            client = PeerClient(peer, code)
            profile = self._saved_profile()
            pending_drop = self._pending_drop
        except (OSError, ValueError) as error:
            self._locker_error(str(error))
            return
        self.locker_status.setText(f"Connecting to {client.peer.name}…")
        logger.info("peer connection starting name=%s endpoint=%s", client.peer.name, client.peer.base_url)
        self._connected_client = None
        self.remote_tree.clear()
        self.remote_tree._last_items = None

        def task() -> None:
            info = client.info()
            if info.get("protocol") != PROTOCOL_VERSION:
                raise OSError("Update FastFiles on both machines; the peer uses an older protocol")
            if info.get("device_id") == self.locker_config.device_id:
                raise OSError("This address points to this machine’s own locker")
            if not isinstance(info.get("locker_path"), str) or type(info.get("read_only")) is not bool:
                raise OSError("Invalid peer connection response")
            if profile and profile.device_id and info.get("device_id") != profile.device_id:
                raise OSError("This address belongs to a different computer. Forget the saved connection and pair again.")
            directory = profile.last_folder if profile else ""
            fallback = False
            try:
                items = client.list(directory)
            except OSError:
                if not directory or pending_drop:
                    raise
                directory, fallback = "", True
                items = client.list(directory)
            return client, info, directory, items, fallback

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
        self.remote_tree.directory = relative
        if self._connected_client:
            self.remote_tree.connection_id = self._endpoint(self._connected_client.peer)
        root = self._remote_info.get("locker_path", "/")
        self.remote_path_label.setText("Destination: /" + relative)
        self.remote_path_label.setToolTip(posixpath.join(root, relative))
        self._fill_tree(self.remote_tree, items)
        upload_only = not self._remote_info.get("can_download", True)
        self.remote_empty.setText("Upload-only access: received files stay private." if upload_only else "No shared files in this folder.")
        self.remote_empty.setVisible(not items)
        if self._connected_client:
            modes = []
            if self._remote_info.get("can_download", True):
                modes.append("download permitted files")
            if self._remote_info.get("can_upload", not self._remote_info.get("read_only", False)):
                modes.append("upload to permitted folders")
            self.connection_summary.setText(f"{self._connected_client.peer.name} · " + (" · ".join(modes) or "No file access"))
        self._remember_remote_folder()
        self._update_locker_controls()

    def _local_open(self, item: QTreeWidgetItem) -> None:
        if item.data(0, Qt.ItemDataRole.UserRole + 1):
            self._go_local(item.data(0, Qt.ItemDataRole.UserRole))
        else:
            try:
                path = self.locker.resolve(item.data(0, Qt.ItemDataRole.UserRole))
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            except (OSError, ValueError) as error:
                self._locker_error(str(error))

    def _remote_open(self, item: QTreeWidgetItem) -> None:
        if item.data(0, Qt.ItemDataRole.UserRole + 1):
            self._load_remote(item.data(0, Qt.ItemDataRole.UserRole))

    def _local_back(self) -> None:
        self._go_local(posixpath.dirname(self.local_relative))

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
        self._pending_drop = None
        self._locker_cancel.set()
        self.locker_status.setText("Cancelling… waiting for the current network request to return")
        self._update_locker_controls()

    def _upload_selected(self) -> None:
        self._send_items([item.data(0, Qt.ItemDataRole.UserRole) for item in self.local_tree.selectedItems()], self.remote_relative)

    def _send_items(self, sources: list[str], directory: str) -> None:
        if not sources:
            self._locker_error("Select files or folders in My locker")
            return
        try:
            client = self._client()
            if not self._upload_targets_allowed([posixpath.basename(path) for path in sources], directory):
                raise PermissionError("Uploading these items is not permitted here. Choose a folder that allows uploads.")
            for source in sources:
                self.locker.resolve(source)
        except (OSError, ValueError) as error:
            self._locker_error(str(error))
            return
        overwrite = self.replace_files.isChecked()
        self._copy_names = ", ".join(posixpath.basename(path) for path in sources[:3])
        self.locker_status.setText(f"Sending {len(sources)} item(s) to {client.peer.name}/{directory}…")
        self._run_locker_task("send", lambda: copy_many_to_peer(
            self.locker, client, sources, directory, self._transfer_progress, self._locker_cancel, overwrite=overwrite))

    def _download_selected(self) -> None:
        self._receive_items([
            (item.data(0, Qt.ItemDataRole.UserRole), item.data(0, Qt.ItemDataRole.UserRole + 1))
            for item in self.remote_tree.selectedItems()
        ], self.local_relative)

    def _receive_items(self, sources: list[tuple[str, bool]], directory: str) -> None:
        if not sources:
            self._locker_error("Select files or folders in the computer's locker")
            return
        if self.locker_config.read_only:
            self._locker_error("This locker is read-only")
            return
        try:
            client = self._client()
            entries = {entry["path"]: entry for entry in (self.remote_tree._last_items or [])}
            if not self._remote_info.get("can_download", True) or any(
                entries.get(path, {}).get("can_download") is False for path, _ in sources
            ):
                raise PermissionError("Download is not permitted for this selection. Open the folder to choose permitted files.")
        except (OSError, ValueError) as error:
            self._locker_error(str(error))
            return
        overwrite = self.replace_files.isChecked()
        self._copy_names = ", ".join(posixpath.basename(path) for path, _ in sources[:3])
        self.locker_status.setText(f"Receiving {len(sources)} item(s) into My locker/{directory}…")
        self._run_locker_task("receive", lambda: copy_many_from_peer(
            self.locker, client, sources, directory, self._transfer_progress, self._locker_cancel, overwrite=overwrite))

    def _task_done(self, value) -> None:
        task_id, kind, result = value
        if task_id != self._task_id:
            return
        self._locker_busy = False
        if kind == "connect":
            if self._locker_cancel.is_set():
                self._pending_drop = None
                self._disconnect_peer()
                return
            self._connected_client, self._remote_info, directory, items, fallback = result
            self._connection_problem = ""
            self._show_remote_items((directory, items))
            peer = self._connected_client.peer
            mode = " · read-only" if self._remote_info.get("read_only") else ""
            self.locker_status.setText(f"Connected to {peer.name} · {self._endpoint(peer)}{mode}")
            if fallback:
                self.locker_status.setText(self.locker_status.text() + " · Saved folder unavailable; showing Home.")
            self._refresh_saved_folders()
            try:
                profile = self._saved_profile()
                if profile:
                    self.secret_store.set(profile.secret_id, self._connected_client.code)
            except Exception as error:
                self.incoming_notice.setText(
                    f"Connected, but updated access could not be saved ({type(error).__name__}).")
                self.incoming_notice.show()
            logger.info(
                "Connected endpoint=%s remote_root=%s", self._endpoint(peer), self._remote_info["locker_path"]
            )
            pending, self._pending_drop = self._pending_drop, None
            if pending and pending[0] == self.peer_combo.currentData():
                self._perform_pending_drop(pending[1])
        elif kind == "browse":
            self._show_remote_items(result)
            self.locker_status.setText(f"Browsing {self.remote_path_label.text()}")
        elif kind == "new_folder":
            self._load_remote(self.remote_relative)
        elif kind in ("send", "receive", "import"):
            self.locker_progress.setValue(100)
            verb = {"send": "Sent", "receive": "Received", "import": "Added"}[kind]
            message = f"{verb} {result.files} file(s), {result.directories} folder(s), {self._human_size(result.bytes)} → {result.destination}"
            names = getattr(self, "_copy_names", "")
            if names:
                message += f" · {names}"
            self.locker_status.setText(message)
            logger.info("Locker copy complete: %s", message)
            try:
                source = self._connected_client.peer.name if kind == "receive" and self._connected_client else "This computer"
                self.activity_store.record(verb.lower(), source, result.destination, result.files, result.bytes)
                self._refresh_activity()
            except (OSError, ValueError) as error:
                self.incoming_notice.setText(f"Files copied; could not save history: {error}")
            self._refresh_local_locker()
            # Refresh the remote view while retaining the useful completion receipt.
            client, relative = self._connected_client, self.remote_relative
            if client and kind != "import":
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
        dropped_transfer_failed = kind == "connect" and self._pending_drop is not None
        if kind == "connect":
            self._pending_drop = None
        if kind in ("connect", "browse"):
            self._disconnect_peer()
        prefix = (
            "Cancelled"
            if cancelled
            else "Transfer stopped"
            if kind in ("send", "receive", "import")
            else "Connection failed"
        )
        if kind == "refresh_after_copy":
            self.locker_status.setText(self.locker_status.text() + f" · Could not refresh listing: {message}")
        else:
            self.locker_status.setText(f"{prefix}: {message}")
        if dropped_transfer_failed:
            self.locker_status.setText(self.locker_status.text() + " · Dropped items were not sent; drop them again after connecting.")
        if kind in ("send", "receive", "import"):
            self.locker_status.setText(self.locker_status.text() + " · Any completed files were kept.")
            self._refresh_local_locker()
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
        self.direct_drop.directories_only = receiving
        self.direct_drop.setText(
            "Drop one destination folder here" if receiving
            else "Drop files and folders here to add them to the transfer"
        )
        self.direct_drop.setAccessibleName(self.direct_drop.text())
        self.local_label.setText("Local destination" if receiving else "Local files and folders")
        self.pick_files.setVisible(not receiving)
        self.pick_folder.setText("Choose destination" if receiving else "Add folder")
        self.remote_path.setPlaceholderText("~/path/to/file-or-folder" if receiving else "~/Downloads/")
        self.start.setText("Receive files" if receiving else "Send files")
        self._update_destination_preview()

    def _choose_files(self) -> None:
        dialog = self._path_dialog("Choose files to send", QFileDialog.FileMode.ExistingFiles)
        if dialog.exec() == QFileDialog.DialogCode.Accepted:
            self._add_paths(dialog.selectedFiles())

    def _path_dialog(self, title: str, mode: QFileDialog.FileMode) -> QFileDialog:
        dialog = QFileDialog(self, title)
        # Native pickers can ignore Qt's hidden-entry filter.
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setFileMode(mode)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, mode == QFileDialog.FileMode.Directory)
        dialog.setFilter(dialog.filter() | QDir.Filter.Hidden)
        return dialog

    def _choose_folder(self) -> None:
        title = "Choose local destination" if self.direction is Direction.RECEIVE else "Choose folder to send"
        dialog = self._path_dialog(title, QFileDialog.FileMode.Directory)
        if dialog.exec() != QFileDialog.DialogCode.Accepted:
            return
        folder = dialog.selectedFiles()[0]
        if self.direction is Direction.RECEIVE:
            self._local_paths = [folder]
            self.paths.clear()
            self.paths.addItem(folder)
            self._update_destination_preview()
        else:
            self._add_paths([folder])

    def _drop_locker_paths(self, paths: list[str]) -> None:
        if self._locker_busy or self.locker_config.read_only:
            return
        self._import_paths(paths, self.local_relative)

    def _import_paths(self, paths: list[str], directory: str) -> None:
        overwrite = self.replace_files.isChecked()
        self._copy_names = ", ".join(Path(path).name for path in paths[:3])
        self.locker_status.setText(f"Copying dropped items into {self.locker.root / directory}…")
        self.locker_progress.setValue(0)
        self._run_locker_task(
            "import",
            lambda: import_to_locker(
                self.locker, paths, directory, self._transfer_progress,
                self._locker_cancel, overwrite=overwrite,
            ),
        )

    def _drop_direct_paths(self, paths: list[str]) -> None:
        if self._process is not None:
            return
        if self.direction is Direction.RECEIVE:
            if len(paths) != 1 or not Path(paths[0]).is_dir():
                return
            self._local_paths.clear()
            self.paths.clear()
        self._add_paths(paths)

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
            self.edit_direct_profile,
            self.delete_direct_profile,
            self.paths,
            self.remove_paths,
            self.direct_drop,
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
        self.locker_timer.stop()
        self._workers.shutdown(wait=False, cancel_futures=True)
        if self.discovery is not None:
            self.discovery.close()
        if self.locker_service is not None:
            self.locker_service.stop()
        event.accept()
