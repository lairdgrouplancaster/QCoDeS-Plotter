from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_RENDERED_NODES,
    TRUSTED_PRESENTATION_MAX_RENDERED_TEXT_BYTES,
    TrustedPresentationView,
)
from qplot.datahandling.trusted_snapshot import (
    TRUSTED_SNAPSHOT_LOAD_MORE_TEXT,
    TRUSTED_SNAPSHOT_PAGE_MAX_CHILDREN,
    TRUSTED_SNAPSHOT_PAGE_MAX_DATA_NODES_WITH_CONTINUATION,
    TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES,
    TrustedSnapshotContainerHandle,
    TrustedSnapshotContinuation,
    TrustedSnapshotFullValueHandle,
    TrustedSnapshotPage,
    TrustedSnapshotView,
)

from .._commands import command_spec

COPY_SELECTION_SHORTCUTS = command_spec("copy.selection").resolved_shortcuts()
COPY_CELL_SHORTCUTS = command_spec("copy.cell").resolved_shortcuts()
FULL_VALUE_ID_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 40
FULL_VALUE_PATH_ROLE = FULL_VALUE_ID_ROLE + 1
SNAPSHOT_SESSION_ROLE = FULL_VALUE_ID_ROLE + 2
SNAPSHOT_CONTAINER_ROLE = FULL_VALUE_ID_ROLE + 3
SNAPSHOT_PARENT_ROLE = FULL_VALUE_ID_ROLE + 4
SNAPSHOT_REQUEST_CONTINUATION_ROLE = FULL_VALUE_ID_ROLE + 5
SNAPSHOT_CONTINUATION_ROLE = FULL_VALUE_ID_ROLE + 6
SNAPSHOT_FULL_VALUE_ROLE = FULL_VALUE_ID_ROLE + 7
SNAPSHOT_PAGE_PENDING_ROLE = FULL_VALUE_ID_ROLE + 8
SNAPSHOT_CONTAINER_LOADED_ROLE = FULL_VALUE_ID_ROLE + 9
SNAPSHOT_LOAD_MORE_ROLE = FULL_VALUE_ID_ROLE + 10
DEFAULT_EXPANDED_SNAPSHOT_PATH = "/Snapshot/station"


def _snapshot_page_display_text_bytes(
    rendered_text_bytes: int,
    continuation: object | None,
) -> int:
    return rendered_text_bytes + (
        len(TRUSTED_SNAPSHOT_LOAD_MORE_TEXT.encode("utf-8"))
        if continuation is not None
        else 0
    )


@dataclass(frozen=True, slots=True)
class SnapshotTreePageRequest:
    """One bounded request emitted by the Snapshot tree.

    Handles and continuations are opaque, immutable core values.  In
    particular, this envelope never owns the retained Snapshot source.
    """

    session_token: str
    parent_handle: TrustedSnapshotContainerHandle
    continuation: TrustedSnapshotContinuation | None


@dataclass(frozen=True, slots=True)
class SnapshotTreeValueRequest:
    """One exact Snapshot scalar request tied to the page that displayed it."""

    session_token: str
    handle: TrustedSnapshotFullValueHandle
    path: str
    parent_handle: TrustedSnapshotContainerHandle | None
    continuation: TrustedSnapshotContinuation | None


def copy_action(label, shortcuts, slot, parent):
    action = QtGui.QAction(label, parent)
    action.setShortcuts(shortcuts)
    action.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
    if hasattr(action, "setShortcutVisibleInContextMenu"):
        action.setShortcutVisibleInContextMenu(True)
    action.triggered.connect(slot)
    return action


class WrappedValueDelegate(qtw.QStyledItemDelegate):
    WRAP_FLAGS = (
        QtCore.Qt.AlignmentFlag.AlignLeft
        | QtCore.Qt.AlignmentFlag.AlignTop
        | QtCore.Qt.TextFlag.TextWrapAnywhere
    )

    def paint(self, painter, option, index):
        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        widget = opt.widget
        style = widget.style() if widget is not None else qtw.QApplication.style()
        if style is None:
            super().paint(painter, option, index)
            return
        text = opt.text
        opt.text = ""

        style.drawControl(
            qtw.QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget
        )

        text_rect = style.subElementRect(
            qtw.QStyle.SubElement.SE_ItemViewItemText, opt, widget
        )
        text_rect.adjust(0, 2, 0, -2)
        painter.save()
        painter.setFont(opt.font)
        role = (
            QtGui.QPalette.ColorRole.HighlightedText
            if opt.state & qtw.QStyle.StateFlag.State_Selected
            else QtGui.QPalette.ColorRole.Text
        )
        painter.setPen(opt.palette.color(role))
        painter.drawText(text_rect, self.WRAP_FLAGS, text)
        painter.restore()

    def sizeHint(self, option, index):
        opt = qtw.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        width = opt.rect.width()
        if width <= 0 and isinstance(opt.widget, qtw.QTreeView):
            width = opt.widget.columnWidth(index.column())
        width = max(24, width - 6)

        metrics = QtGui.QFontMetrics(opt.font)
        text_rect = metrics.boundingRect(
            QtCore.QRect(0, 0, width, 100_000), self.WRAP_FLAGS, opt.text
        )
        base = super().sizeHint(option, index)
        return QtCore.QSize(base.width(), max(base.height(), text_rect.height() + 6))


class TrustedFullValueDialog(qtw.QDialog):
    """One reusable, read-only viewer for the current selected-run scalar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._exact_text: str | None = None
        self.setObjectName("trustedFullValueDialog")
        self.setWindowTitle("Complete metadata value")
        self.resize(760, 520)

        self.path_label = qtw.QLabel(self)
        self.path_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.text_edit = qtw.QPlainTextEdit(self)
        self.text_edit.setObjectName("trustedFullValueText")
        self.text_edit.setReadOnly(True)
        self.text_edit.setLineWrapMode(qtw.QPlainTextEdit.LineWrapMode.NoWrap)

        self.select_all_button = qtw.QPushButton("Select All", self)
        self.copy_button = qtw.QPushButton("Copy", self)
        self.close_button = qtw.QPushButton("Close", self)
        self.select_all_button.clicked.connect(self.text_edit.selectAll)
        self.copy_button.clicked.connect(self.copy_value)
        self.close_button.clicked.connect(self.close)

        buttons = qtw.QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.select_all_button)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.close_button)
        layout = qtw.QVBoxLayout(self)
        layout.addWidget(self.path_label)
        layout.addWidget(self.text_edit, 1)
        layout.addLayout(buttons)

    def show_value(self, *, path: str, text: str, utf8_bytes: int) -> None:
        self._exact_text = text
        self.path_label.setText(f"{path} — {utf8_bytes} UTF-8 bytes")
        self.text_edit.setPlainText(text)
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)
        self.text_edit.setTextCursor(cursor)
        self.show()
        self.raise_()
        self.activateWindow()

    @QtCore.pyqtSlot()
    def copy_value(self) -> None:
        if self._exact_text is not None:
            copy_to_clipboard(self._exact_text)

    def discard_value(self) -> None:
        self._exact_text = None
        self.text_edit.clear()
        self.path_label.clear()
        self.close()


class infoTree(qtw.QTreeWidget):
    fullValueRequested = QtCore.pyqtSignal(str, str)
    snapshotPageRequested = QtCore.pyqtSignal(object)
    snapshotFullValueRequested = QtCore.pyqtSignal(object)

    def __init__(
        self,
        expand_all=True,
        truncate_values=False,
        *,
        expand_top_level=False,
    ):
        super().__init__()
        self.expand_all = expand_all
        self.expand_top_level = expand_top_level
        self.truncate_values = truncate_values
        self._snapshot_session_token = ""
        self._snapshot_root_handle: TrustedSnapshotContainerHandle | None = None
        self._snapshot_pending_requests: list[SnapshotTreePageRequest] = []
        self.setHeaderLabels(["Key", "Value"])
        self.setColumnCount(2)
        self.setWordWrap(True)
        self.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
        self.setUniformRowHeights(False)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setItemDelegateForColumn(1, WrappedValueDelegate(self))
        header = self.header()
        if header is not None:
            header.setSectionResizeMode(0, qtw.QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Stretch)
            header.setStretchLastSection(True)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.openCopyMenu)
        self.itemActivated.connect(self._activate_full_value)
        self.itemDoubleClicked.connect(self._activate_full_value)
        self.itemExpanded.connect(self._request_expanded_snapshot_container)

        self.copy_value_action = copy_action(
            "Copy Value", COPY_CELL_SHORTCUTS, self.copyValue, self
        )
        self.copy_selection_action = copy_action(
            "Copy Selection", COPY_SELECTION_SHORTCUTS, self.copySelection, self
        )
        self.view_full_value_action = QtGui.QAction("View Complete Value…", self)
        self.view_full_value_action.triggered.connect(self._request_current_full_value)
        self.addActions(
            [
                self.view_full_value_action,
                self.copy_value_action,
                self.copy_selection_action,
            ]
        )

    @property
    def snapshotSessionToken(self) -> str:
        return self._snapshot_session_token

    def expandDefaultSnapshotContainer(self) -> bool:
        """Open the QCoDeS station node and request only its first lazy page."""

        self._require_owner_thread()
        if not self._snapshot_session_token:
            return False
        for index in range(self.topLevelItemCount()):
            item = self.topLevelItem(index)
            if item is None:
                continue
            if (
                item.data(0, FULL_VALUE_PATH_ROLE) != DEFAULT_EXPANDED_SNAPSHOT_PATH
                or item.data(0, SNAPSHOT_CONTAINER_ROLE) is None
            ):
                continue
            if item.isExpanded():
                self._request_expanded_snapshot_container(item)
            else:
                item.setExpanded(True)
            return True
        return False

    def clear(self) -> None:
        self._snapshot_session_token = ""
        self._snapshot_root_handle = None
        self._snapshot_pending_requests = []
        super().clear()

    def setInfo(self, info):
        self.clear()

        if not info:
            self.addTopLevelItem(qtw.QTreeWidgetItem(["No data", ""]))
            return
        if not isinstance(info, dict):
            item = qtw.QTreeWidgetItem(
                ["Value", format_value(info, 180 if self.truncate_values else None)]
            )
            item.setToolTip(1, format_value(info))
            self.addTopLevelItem(item)
            return

        items = dictToTree(info, truncate_values=self.truncate_values)
        for item in items:
            self.addTopLevelItem(item)
            item.setExpanded(True)

        if self.expand_all:
            self.expandAll()
        header = self.header()
        if header is not None:
            header.setSectionResizeMode(0, qtw.QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Stretch)
        cast(Any, self).doItemsLayout()

    def setBoundedView(
        self,
        view: TrustedSnapshotView | TrustedPresentationView,
        *,
        expanded_paths: set[str] | frozenset[str] | None = None,
        expand_all: bool | None = None,
    ) -> None:
        """Construct one pre-bounded tree without Python recursion."""
        if isinstance(view, TrustedSnapshotView):
            self._set_snapshot_view(view)
            return
        if not isinstance(view, TrustedPresentationView):
            raise TypeError("view must be a trusted bounded tree view")

        self.clear()
        if not view.nodes:
            self.addTopLevelItem(qtw.QTreeWidgetItem(["No data", ""]))
            return

        qt_items: list[qtw.QTreeWidgetItem] = []
        rendered_bytes = 0
        for node_index, node in enumerate(
            view.nodes[:TRUSTED_PRESENTATION_MAX_RENDERED_NODES]
        ):
            node = cast(Any, node)
            rendered_bytes += len(node.key.encode("utf-8")) + len(
                node.value.encode("utf-8")
            )
            if rendered_bytes > TRUSTED_PRESENTATION_MAX_RENDERED_TEXT_BYTES:
                break
            item = qtw.QTreeWidgetItem([node.key, node.value])
            item.setToolTip(1, getattr(node, "tooltip", node.value))
            item.setToolTip(0, getattr(node, "tooltip", node.key))
            path = str(getattr(node, "path", "") or "")
            full_key_id = str(getattr(node, "full_key_id", "") or "")
            full_value_id = str(getattr(node, "full_value_id", "") or "")
            if path:
                item.setData(0, FULL_VALUE_PATH_ROLE, path)
                item.setData(1, FULL_VALUE_PATH_ROLE, path)
            if full_key_id:
                item.setData(0, FULL_VALUE_ID_ROLE, full_key_id)
            if full_value_id:
                item.setData(1, FULL_VALUE_ID_ROLE, full_value_id)
            parent_index = node.parent_index
            if (
                parent_index is None
                or parent_index < 0
                or parent_index >= node_index
                or parent_index >= len(qt_items)
            ):
                self.addTopLevelItem(item)
            else:
                qt_items[parent_index].addChild(item)
            qt_items.append(item)

        if not qt_items:
            self.addTopLevelItem(
                qtw.QTreeWidgetItem(
                    ["Snapshot unavailable", "The bounded view model is empty."]
                )
            )
            return

        should_expand_all = self.expand_all if expand_all is None else expand_all
        if should_expand_all:
            for item in qt_items:
                item.setExpanded(True)
        elif expanded_paths is not None:
            for item in qt_items:
                path = item.data(0, FULL_VALUE_PATH_ROLE)
                if path:
                    item.setExpanded(str(path) in expanded_paths)
                elif self.expand_top_level and item.parent() is None:
                    item.setExpanded(True)
        elif self.expand_top_level:
            for item in qt_items:
                if item.parent() is None:
                    item.setExpanded(True)
        header = self.header()
        if header is not None:
            header.setSectionResizeMode(0, qtw.QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Stretch)
        cast(Any, self).doItemsLayout()

    def _set_snapshot_view(self, view: TrustedSnapshotView) -> None:
        """Install only the Snapshot root page; retained source stays off Qt."""

        self._require_owner_thread()
        session_token = getattr(view, "session_token", "")
        root_handle = getattr(view, "root_handle", None)
        continuation = getattr(view, "continuation", None)
        nodes = tuple(view.nodes)
        self.clear()

        if not isinstance(session_token, str) or not session_token:
            self._set_legacy_snapshot_nodes(nodes)
            return
        page_node_limit = (
            TRUSTED_SNAPSHOT_PAGE_MAX_DATA_NODES_WITH_CONTINUATION
            if continuation is not None
            else TRUSTED_SNAPSHOT_PAGE_MAX_CHILDREN
        )
        if len(nodes) > page_node_limit:
            self.addTopLevelItem(
                qtw.QTreeWidgetItem(
                    ["Snapshot unavailable", "The bounded view model is oversized."]
                )
            )
            return

        built = self._build_snapshot_items(
            nodes,
            session_token=session_token,
            parent_handle=root_handle,
            request_continuation=None,
            parent_path="/Snapshot",
        )
        if built is None:
            self.addTopLevelItem(
                qtw.QTreeWidgetItem(
                    ["Snapshot unavailable", "The bounded view model is invalid."]
                )
            )
            return

        roots, _all_items, rendered_bytes = built
        if (
            _snapshot_page_display_text_bytes(rendered_bytes, continuation)
            > TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES
        ):
            self.addTopLevelItem(
                qtw.QTreeWidgetItem(
                    ["Snapshot unavailable", "The bounded view model is oversized."]
                )
            )
            return

        self._snapshot_session_token = session_token
        self._snapshot_root_handle = root_handle
        blocker = QtCore.QSignalBlocker(self)
        try:
            if roots:
                self.addTopLevelItems(roots)
            if continuation is not None:
                self.addTopLevelItem(
                    self._snapshot_continuation_item(
                        session_token,
                        cast(TrustedSnapshotContainerHandle, root_handle),
                        cast(TrustedSnapshotContinuation, continuation),
                        "/Snapshot",
                    )
                )
            if not roots and continuation is None:
                self.addTopLevelItem(qtw.QTreeWidgetItem(["No data", ""]))
        finally:
            del blocker
        self._finish_bounded_layout()

    def _set_legacy_snapshot_nodes(self, nodes: tuple[object, ...]) -> None:
        """Render old/terminal views while no lazy source handle is available."""

        if not nodes:
            self.addTopLevelItem(qtw.QTreeWidgetItem(["No data", ""]))
            return
        qt_items: list[qtw.QTreeWidgetItem] = []
        rendered_bytes = 0
        for node_index, raw_node in enumerate(
            nodes[:TRUSTED_SNAPSHOT_PAGE_MAX_CHILDREN]
        ):
            node = cast(Any, raw_node)
            if not isinstance(node.key, str) or not isinstance(node.value, str):
                break
            rendered_bytes += len(node.key.encode("utf-8")) + len(
                node.value.encode("utf-8")
            )
            if rendered_bytes > TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES:
                break
            item = qtw.QTreeWidgetItem([node.key, node.value])
            tooltip = getattr(node, "tooltip", node.value)
            if isinstance(tooltip, str):
                item.setToolTip(0, tooltip)
                item.setToolTip(1, tooltip)
            parent_index = node.parent_index
            if (
                parent_index is None
                or type(parent_index) is not int
                or parent_index < 0
                or parent_index >= node_index
                or parent_index >= len(qt_items)
            ):
                self.addTopLevelItem(item)
            else:
                qt_items[parent_index].addChild(item)
            qt_items.append(item)
        if not qt_items:
            self.addTopLevelItem(
                qtw.QTreeWidgetItem(
                    ["Snapshot unavailable", "The bounded view model is empty."]
                )
            )
            return
        self._finish_bounded_layout()

    def _build_snapshot_items(
        self,
        nodes: tuple[object, ...],
        *,
        session_token: str,
        parent_handle: object,
        request_continuation: object | None,
        parent_path: str,
    ) -> (
        tuple[
            list[qtw.QTreeWidgetItem],
            list[qtw.QTreeWidgetItem],
            int,
        ]
        | None
    ):
        """Build a detached, bounded page so rejection never partially mutates Qt."""

        if len(nodes) > TRUSTED_SNAPSHOT_PAGE_MAX_CHILDREN:
            return None
        roots: list[qtw.QTreeWidgetItem] = []
        qt_items: list[qtw.QTreeWidgetItem] = []
        rendered_bytes = 0
        for node_index, raw_node in enumerate(nodes):
            node = cast(Any, raw_node)
            key = getattr(node, "key", None)
            value = getattr(node, "value", None)
            parent_index = getattr(node, "parent_index", None)
            if not isinstance(key, str) or not isinstance(value, str):
                return None
            if parent_index is not None and (
                type(parent_index) is not int
                or parent_index < 0
                or parent_index >= node_index
                or parent_index >= len(qt_items)
            ):
                return None
            rendered_bytes += len(key.encode("utf-8")) + len(value.encode("utf-8"))
            if rendered_bytes > TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES:
                return None

            item = qtw.QTreeWidgetItem([key, value])
            tooltip = getattr(node, "tooltip", value)
            if not isinstance(tooltip, str):
                return None
            item.setToolTip(0, tooltip)
            item.setToolTip(1, tooltip)
            path_parent = (
                parent_path
                if parent_index is None
                else str(
                    qt_items[parent_index].data(0, FULL_VALUE_PATH_ROLE) or parent_path
                )
            )
            path = f"{path_parent.rstrip('/')}/{key}"
            for column in (0, 1):
                item.setData(column, SNAPSHOT_SESSION_ROLE, session_token)
                item.setData(column, SNAPSHOT_PARENT_ROLE, parent_handle)
                item.setData(
                    column,
                    SNAPSHOT_REQUEST_CONTINUATION_ROLE,
                    request_continuation,
                )
                item.setData(column, FULL_VALUE_PATH_ROLE, path)

            container_handle = getattr(node, "container_handle", None)
            if container_handle is not None:
                item.setData(0, SNAPSHOT_CONTAINER_ROLE, container_handle)
                item.setData(1, SNAPSHOT_CONTAINER_ROLE, container_handle)
                item.setData(0, SNAPSHOT_CONTAINER_LOADED_ROLE, False)
                item.setChildIndicatorPolicy(
                    qtw.QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                )

            full_key_handle = getattr(node, "full_key_handle", None)
            full_value_handle = getattr(node, "full_value_handle", None)
            if full_key_handle is not None:
                item.setData(0, SNAPSHOT_FULL_VALUE_ROLE, full_key_handle)
            if full_value_handle is not None:
                item.setData(1, SNAPSHOT_FULL_VALUE_ROLE, full_value_handle)

            if parent_index is None:
                roots.append(item)
            else:
                qt_items[parent_index].addChild(item)
            qt_items.append(item)
        return roots, qt_items, rendered_bytes

    @staticmethod
    def _snapshot_continuation_item(
        session_token: str,
        parent_handle: TrustedSnapshotContainerHandle,
        continuation: TrustedSnapshotContinuation,
        parent_path: str,
    ) -> qtw.QTreeWidgetItem:
        item = qtw.QTreeWidgetItem([TRUSTED_SNAPSHOT_LOAD_MORE_TEXT, ""])
        for column in (0, 1):
            item.setData(column, SNAPSHOT_SESSION_ROLE, session_token)
            item.setData(column, SNAPSHOT_PARENT_ROLE, parent_handle)
            item.setData(column, SNAPSHOT_CONTINUATION_ROLE, continuation)
            item.setData(column, SNAPSHOT_LOAD_MORE_ROLE, True)
            item.setData(column, FULL_VALUE_PATH_ROLE, parent_path)
        item.setToolTip(0, "Load the next bounded page of Snapshot entries.")
        item.setToolTip(1, item.toolTip(0))
        return item

    @QtCore.pyqtSlot(qtw.QTreeWidgetItem)
    def _request_expanded_snapshot_container(self, item) -> None:
        if not self._snapshot_session_token:
            return
        container_handle = item.data(0, SNAPSHOT_CONTAINER_ROLE)
        if container_handle is None or bool(
            item.data(0, SNAPSHOT_CONTAINER_LOADED_ROLE)
        ):
            return
        self._emit_snapshot_page_request(item, container_handle, None)

    def _emit_snapshot_page_request(
        self,
        item: qtw.QTreeWidgetItem,
        parent_handle: TrustedSnapshotContainerHandle,
        continuation: TrustedSnapshotContinuation | None,
    ) -> bool:
        self._require_owner_thread()
        session_token = item.data(0, SNAPSHOT_SESSION_ROLE)
        if (
            not isinstance(session_token, str)
            or not session_token
            or session_token != self._snapshot_session_token
            or parent_handle is None
            or item.treeWidget() is not self
            or any(
                request.parent_handle == parent_handle
                for request in self._snapshot_pending_requests
            )
        ):
            return False
        if continuation is not None and (
            not bool(item.data(0, SNAPSHOT_LOAD_MORE_ROLE))
            or item.data(0, SNAPSHOT_CONTINUATION_ROLE) != continuation
        ):
            return False
        request = SnapshotTreePageRequest(
            session_token,
            parent_handle,
            continuation,
        )
        self._snapshot_pending_requests.append(request)
        item.setData(0, SNAPSHOT_PAGE_PENDING_ROLE, request)
        item.setData(1, SNAPSHOT_PAGE_PENDING_ROLE, request)
        self.snapshotPageRequested.emit(request)
        return True

    def acceptSnapshotPage(self, page: TrustedSnapshotPage) -> bool:
        """Atomically accept one exact pending Snapshot page on the owner thread."""

        self._require_owner_thread()
        if not isinstance(page, TrustedSnapshotPage):
            return False
        parent_handle = page.parent_handle
        request_continuation = page.request_continuation
        nodes = page.nodes
        continuation = page.continuation
        rendered_text_bytes = page.rendered_text_bytes
        if (
            not self._snapshot_session_token
            or parent_handle is None
            or not isinstance(nodes, tuple)
            or type(rendered_text_bytes) is not int
            or rendered_text_bytes < 0
            or _snapshot_page_display_text_bytes(
                rendered_text_bytes,
                continuation,
            )
            > TRUSTED_SNAPSHOT_PAGE_MAX_TEXT_BYTES
        ):
            return False
        page_node_limit = (
            TRUSTED_SNAPSHOT_PAGE_MAX_DATA_NODES_WITH_CONTINUATION
            if continuation is not None
            else TRUSTED_SNAPSHOT_PAGE_MAX_CHILDREN
        )
        if len(nodes) > page_node_limit:
            return False
        request = next(
            (
                candidate
                for candidate in self._snapshot_pending_requests
                if candidate.session_token == self._snapshot_session_token
                and candidate.parent_handle == parent_handle
                and candidate.continuation == request_continuation
            ),
            None,
        )
        if request is None:
            return False

        parent_item = self._snapshot_item_for_container(parent_handle)
        if parent_handle == self._snapshot_root_handle:
            parent_item = None
        elif parent_item is None:
            return False
        request_item = self._snapshot_request_item(request, parent_item)
        if request_item is None:
            return False
        parent_path = (
            "/Snapshot"
            if parent_item is None
            else str(parent_item.data(0, FULL_VALUE_PATH_ROLE) or "/Snapshot")
        )
        built = self._build_snapshot_items(
            nodes,
            session_token=self._snapshot_session_token,
            parent_handle=parent_handle,
            request_continuation=request_continuation,
            parent_path=parent_path,
        )
        if built is None:
            return False
        roots, _all_items, built_bytes = built
        if built_bytes != rendered_text_bytes:
            return False
        self._snapshot_pending_requests.remove(request)
        blocker = QtCore.QSignalBlocker(self)
        try:
            if parent_item is not None:
                parent_item.setData(0, SNAPSHOT_CONTAINER_LOADED_ROLE, True)
                parent_item.setData(0, SNAPSHOT_PAGE_PENDING_ROLE, None)
                parent_item.setData(1, SNAPSHOT_PAGE_PENDING_ROLE, None)
            if request_continuation is not None:
                self._remove_snapshot_item(request_item)
            if roots:
                if parent_item is None:
                    self.addTopLevelItems(roots)
                else:
                    parent_item.addChildren(roots)
            if continuation is not None:
                more_item = self._snapshot_continuation_item(
                    self._snapshot_session_token,
                    parent_handle,
                    continuation,
                    parent_path,
                )
                if parent_item is None:
                    self.addTopLevelItem(more_item)
                else:
                    parent_item.addChild(more_item)
            if parent_item is not None and parent_item.childCount() == 0:
                parent_item.setChildIndicatorPolicy(
                    qtw.QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless
                )
        finally:
            del blocker
        self._finish_bounded_layout()
        return True

    def rejectSnapshotPage(self, request: object) -> bool:
        """Release one exact pending page without accepting stale page content."""

        self._require_owner_thread()
        if not isinstance(request, SnapshotTreePageRequest):
            return False
        matched = next(
            (
                candidate
                for candidate in self._snapshot_pending_requests
                if candidate == request
                and candidate.session_token == self._snapshot_session_token
            ),
            None,
        )
        if matched is None:
            return False
        self._snapshot_pending_requests.remove(matched)
        item = self._snapshot_request_item(matched, None)
        if item is not None:
            item.setData(0, SNAPSHOT_PAGE_PENDING_ROLE, None)
            item.setData(1, SNAPSHOT_PAGE_PENDING_ROLE, None)
        return True

    def invalidateSnapshot(self) -> None:
        """Drop the page session and every item/opaque role it owned."""

        self._require_owner_thread()
        self.clear()

    def _snapshot_item_for_container(
        self,
        container_handle: object,
    ) -> qtw.QTreeWidgetItem | None:
        iterator = qtw.QTreeWidgetItemIterator(self)
        while (item := iterator.value()) is not None:
            if item.data(0, SNAPSHOT_CONTAINER_ROLE) == container_handle:
                return item
            iterator += 1
        return None

    def _snapshot_request_item(
        self,
        request: SnapshotTreePageRequest,
        parent_item: qtw.QTreeWidgetItem | None,
    ) -> qtw.QTreeWidgetItem | None:
        iterator = qtw.QTreeWidgetItemIterator(self)
        while (item := iterator.value()) is not None:
            if item.data(0, SNAPSHOT_PAGE_PENDING_ROLE) == request:
                if (
                    parent_item is None
                    or item is parent_item
                    or item.parent() is parent_item
                ):
                    return item
            iterator += 1
        return None

    def _remove_snapshot_item(self, item: qtw.QTreeWidgetItem) -> None:
        parent = item.parent()
        if parent is None:
            index = self.indexOfTopLevelItem(item)
            if index >= 0:
                self.takeTopLevelItem(index)
        else:
            index = parent.indexOfChild(item)
            if index >= 0:
                parent.takeChild(index)

    def _finish_bounded_layout(self) -> None:
        header = self.header()
        if header is not None:
            header.setSectionResizeMode(0, qtw.QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, qtw.QHeaderView.ResizeMode.Stretch)
        cast(Any, self).doItemsLayout()

    def _require_owner_thread(self) -> None:
        if QtCore.QThread.currentThread() != self.thread():
            raise RuntimeError(
                "Snapshot tree mutation must run on its Qt owner thread."
            )

    def expandedPaths(self) -> frozenset[str]:
        expanded = set()
        iterator = qtw.QTreeWidgetItemIterator(self)
        while True:
            item = iterator.value()
            if item is None:
                break
            path = item.data(0, FULL_VALUE_PATH_ROLE)
            if path and item.isExpanded():
                expanded.add(str(path))
            iterator += 1
        return frozenset(expanded)

    @QtCore.pyqtSlot(qtw.QTreeWidgetItem, int)
    def _activate_full_value(self, item, column) -> None:
        requested_column = 1 if int(column) == 1 else 0
        if bool(item.data(0, SNAPSHOT_LOAD_MORE_ROLE)):
            parent_handle = item.data(0, SNAPSHOT_PARENT_ROLE)
            continuation = item.data(0, SNAPSHOT_CONTINUATION_ROLE)
            if parent_handle is not None and continuation is not None:
                self._emit_snapshot_page_request(
                    item,
                    parent_handle,
                    continuation,
                )
            return
        if self._activate_snapshot_full_value(item, requested_column):
            return
        identifier = str(item.data(requested_column, FULL_VALUE_ID_ROLE) or "")
        value_kind = "value" if requested_column == 1 else "key"
        if not identifier:
            requested_column = 1 - requested_column
            identifier = str(item.data(requested_column, FULL_VALUE_ID_ROLE) or "")
            value_kind = "value" if requested_column == 1 else "key"
        if identifier:
            path = str(
                item.data(requested_column, FULL_VALUE_PATH_ROLE) or item.text(0)
            )
            self.fullValueRequested.emit(
                identifier,
                f"{path} ({value_kind})",
            )

    def _activate_snapshot_full_value(
        self,
        item: qtw.QTreeWidgetItem,
        requested_column: int,
    ) -> bool:
        handle = item.data(requested_column, SNAPSHOT_FULL_VALUE_ROLE)
        value_kind = "value" if requested_column == 1 else "key"
        if handle is None:
            requested_column = 1 - requested_column
            handle = item.data(requested_column, SNAPSHOT_FULL_VALUE_ROLE)
            value_kind = "value" if requested_column == 1 else "key"
        if handle is None:
            return False
        session_token = item.data(requested_column, SNAPSHOT_SESSION_ROLE)
        parent_handle = item.data(requested_column, SNAPSHOT_PARENT_ROLE)
        if (
            not isinstance(session_token, str)
            or session_token != self._snapshot_session_token
            or item.treeWidget() is not self
        ):
            return True
        path = item.data(requested_column, FULL_VALUE_PATH_ROLE)
        if not isinstance(path, str) or not path:
            path = item.text(0)
        self.snapshotFullValueRequested.emit(
            SnapshotTreeValueRequest(
                session_token,
                handle,
                f"{path} ({value_kind})",
                parent_handle,
                item.data(requested_column, SNAPSHOT_REQUEST_CONTINUATION_ROLE),
            )
        )
        return True

    @QtCore.pyqtSlot()
    def _request_current_full_value(self) -> None:
        item = self.currentItem()
        if item is not None:
            self._activate_full_value(item, self.currentColumn())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cast(Any, self).doItemsLayout()

    def openCopyMenu(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return

        index = self.indexAt(pos)
        column = index.column() if index.isValid() else 0
        self.setCurrentItem(item, column)

        menu = qtw.QMenu(self)
        if any(
            item.data(col, FULL_VALUE_ID_ROLE)
            or item.data(col, SNAPSHOT_FULL_VALUE_ROLE) is not None
            for col in (0, 1)
        ):
            menu.addAction(self.view_full_value_action)
            menu.addSeparator()
        menu.addAction(self.copy_value_action)

        copy_row = QtGui.QAction("Copy Row", menu)
        copy_row.triggered.connect(lambda: copy_to_clipboard(row_text(item)))
        menu.addAction(copy_row)

        if self.selectedItems():
            menu.addAction(self.copy_selection_action)

        viewport = self.viewport()
        if viewport is not None:
            menu.exec(viewport.mapToGlobal(pos))

    def copyValue(self):
        item = self.currentItem()
        if item is not None:
            copy_to_clipboard(item.text(1))

    def copySelection(self):
        items = self.selectedItems()
        current_item = self.currentItem()
        if not items and current_item is not None:
            items = [current_item]
        copy_to_clipboard("\n".join(row_text(item) for item in items))


class CopyableTableWidget(qtw.QTableWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.openCopyMenu)

        self.copy_cell_action = copy_action(
            "Copy Cell", COPY_CELL_SHORTCUTS, self.copyCell, self
        )
        self.copy_selection_action = copy_action(
            "Copy Selection", COPY_SELECTION_SHORTCUTS, self.copySelection, self
        )
        self.addActions([self.copy_cell_action, self.copy_selection_action])

    def openCopyMenu(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return

        self.setCurrentItem(item)

        menu = qtw.QMenu(self)
        menu.addAction(self.copy_cell_action)
        menu.addAction(self.copy_selection_action)

        viewport = self.viewport()
        if viewport is not None:
            menu.exec(viewport.mapToGlobal(pos))

    def copyCell(self):
        item = self.currentItem()
        if item is not None:
            copy_to_clipboard(item.text())

    def copySelection(self):
        ranges = self.selectedRanges()
        if not ranges and self.currentItem() is not None:
            self.copyCell()
            return

        sections = []
        for selected_range in ranges:
            rows = []
            for row in range(selected_range.topRow(), selected_range.bottomRow() + 1):
                values = []
                for col in range(
                    selected_range.leftColumn(),
                    selected_range.rightColumn() + 1,
                ):
                    item = self.item(row, col)
                    values.append(item.text() if item is not None else "")
                rows.append("\t".join(values))
            sections.append("\n".join(rows))

        copy_to_clipboard("\n".join(section for section in sections if section))


def dictToTree(d: dict, truncate_values=False):
    items = []
    for k, v in d.items():
        if not isinstance(v, dict):
            item = qtw.QTreeWidgetItem(
                [str(k), format_value(v, 180 if truncate_values else None)]
            )
            item.setToolTip(1, format_value(v))
        else:
            item = qtw.QTreeWidgetItem([k, ""])
            for child in dictToTree(v, truncate_values=truncate_values):
                item.addChild(child)
        items.append(item)
    return items


def snapshot_parameters(snapshot):
    if not isinstance(snapshot, dict):
        return {}

    out = {}
    parameter_dicts = []

    params = snapshot.get("parameters")
    if isinstance(params, dict):
        parameter_dicts.append(params)

    station = snapshot.get("station")
    if isinstance(station, dict):
        params = station.get("parameters")
        if isinstance(params, dict):
            parameter_dicts.append(params)

        instruments = station.get("instruments")
        if isinstance(instruments, dict):
            for instrument in instruments.values():
                if isinstance(instrument, dict) and isinstance(
                    instrument.get("parameters"), dict
                ):
                    parameter_dicts.append(instrument["parameters"])

    for params in parameter_dicts:
        for key, details in params.items():
            if not isinstance(details, dict):
                continue
            for name in (key, details.get("name"), details.get("full_name")):
                if name:
                    out[str(name)] = details

    return out


def format_value(value, max_len=None):
    if value is None:
        text = ""
    elif isinstance(value, float):
        text = f"{value:.6g}"
    elif isinstance(value, (list, tuple)):
        text = ", ".join(format_value(item) for item in value)
    else:
        text = str(value)

    text = text.replace("\n", " ")
    if max_len is not None and len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def row_text(item):
    return "\t".join(item.text(col) for col in range(item.columnCount()))


def copy_to_clipboard(text):
    clipboard = qtw.QApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)
