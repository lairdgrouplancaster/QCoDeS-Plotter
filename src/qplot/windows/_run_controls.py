import os

from PyQt6 import (
    QtCore,
    QtGui,
)
from PyQt6 import (
    QtWidgets as qtw,
)
from PyQt6.QtGui import QIntValidator

from ._help import show_quick_start
from ._widgets import (
    RunList,
    moreInfo,
)
from ._widgets._run_formatting import run_is_complete
from ._widgets.preview import PREVIEW_SIZE

AUTO_PLOT_KEY = "user_preference.auto_plot"


def _run_timestamp_sort_key(metadata):
    try:
        return float(metadata.get("run_timestamp") or 0)
    except (TypeError, ValueError):
        return 0.0


class RunControlsMixin:
    """
    Main-window controls for run selection, refresh, and empty-state display.

    Expects the host window to provide database and plotting actions such as
    refreshMain, openRun, openPlot, and exportRunCsv.

    """

    def initRefresh(self):
        """
        Initialise the main window refresh controls.

        Refresh checks for any new runs added to the dataset.

        """
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(1)
        self.spinBox.setSuffix(" s")
        self.spinBox.setFixedWidth(84)
        self.spinBox.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.spinBox.setToolTip("Refresh interval in seconds")
        self.spinBox.setValue(self.config.get("user_preference.default_refresh_rate"))

        self.spinBox.valueChanged.connect(self.monitorIntervalChanged)
        self.monitor.timeout.connect(self.refreshMain)

        self.autoPlotBox = qtw.QCheckBox()
        self.autoPlotBox.setChecked(self.config.get(AUTO_PLOT_KEY))
        self.autoPlotBox.setToolTip("Automatically open plots for newly detected runs")
        self.autoPlotBox.toggled.connect(self._auto_plot_changed)

        self.refreshDatabaseButton = qtw.QToolButton()
        self.refreshDatabaseButton.setObjectName("refreshIconButton")
        self.refreshDatabaseButton.setIcon(
            self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_BrowserReload)
            )
        self.refreshDatabaseButton.setToolTip("Refresh the database run list (R)")
        self.refreshDatabaseButton.setAccessibleName("Refresh database")
        self.refreshDatabaseButton.setFixedSize(28, 26)
        self.refreshDatabaseButton.clicked.connect(self.refreshMain)

        self.closeAllPlotsButton = qtw.QToolButton()
        self.closeAllPlotsButton.setObjectName("closeAllPlotsButton")
        self.closeAllPlotsButton.setIcon(
            self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_TitleBarCloseButton)
            )
        self.closeAllPlotsButton.setToolTip("Close all plot windows (Ctrl+Shift+W)")
        self.closeAllPlotsButton.setAccessibleName("Close all plot windows")
        self.closeAllPlotsButton.setFixedSize(28, 26)
        self.closeAllPlotsButton.clicked.connect(self.closeAll)

    def initRunDisplay(self):
        sublayout = qtw.QHBoxLayout()
        sublayout.setContentsMargins(8, 0, 8, 2)
        sublayout.setSpacing(6)

        sublayout.addWidget(qtw.QLabel("ID:"))

        self.selected_run_id = None

        self.run_idBox = qtw.QLineEdit()
        self.run_idBox.setMaximumWidth(58)
        self.run_idBox.setFixedWidth(58)
        self.run_idBox.setValidator(QIntValidator())
        self.run_idBox.setPlaceholderText("ID")
        self.run_idBox.setToolTip("Run ID to plot")
        self.run_idBox.textEdited.connect(self.update_run_id)
        self.run_idBox.editingFinished.connect(self.sync_run_id_selection)
        self.run_idBox.returnPressed.connect(self.openRun)
        sublayout.addWidget(self.run_idBox)

        sublayout.addWidget(qtw.QLabel("Measurement:"))

        self.measurementBox = qtw.QLineEdit()
        self.measurementBox.setMaximumWidth(46)
        self.measurementBox.setFixedWidth(46)
        self.measurementBox.setText("*")
        self.measurementBox.setToolTip("Measurement to plot; * to plot all")
        self.measurementBox.returnPressed.connect(self.openRun)
        sublayout.addWidget(self.measurementBox)

        self.plotRunButton = qtw.QToolButton()
        self.plotRunButton.setObjectName("plotIconButton")
        self.plotRunButton.setIcon(
            self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_MediaPlay)
            )
        self.plotRunButton.setToolTip("Plot (Ctrl+Return)")
        self.plotRunButton.setAccessibleName("Plot measurement")
        self.plotRunButton.setFixedSize(28, 26)
        self.plotRunButton.clicked.connect(self.openRun)
        sublayout.addWidget(self.plotRunButton)

        self.exportCsvButton = qtw.QToolButton()
        self.exportCsvButton.setObjectName("exportIconButton")
        self.exportCsvButton.setIcon(
            self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_DialogSaveButton)
            )
        self.exportCsvButton.setToolTip("Export CSV")
        self.exportCsvButton.setAccessibleName("Export measurement to CSV")
        self.exportCsvButton.setFixedSize(28, 26)
        self.exportCsvButton.clicked.connect(self.exportRunCsv)
        sublayout.addWidget(self.exportCsvButton)

        sublayout.addStretch()

        sublayout.addWidget(qtw.QLabel("Auto-plot"))
        sublayout.addWidget(self.autoPlotBox)

        sublayout.addSpacing(12)
        sublayout.addWidget(qtw.QLabel("Refresh:"))
        sublayout.addWidget(self.spinBox)
        sublayout.addWidget(self.refreshDatabaseButton)

        self.l.addLayout(self.targetLayout)
        self.l.addWidget(self.databaseLoadFrame)
        self.l.addLayout(sublayout)

        self.RunList = RunList()
        self.RunList.selected.connect(self.updateSelected)
        self.RunList.plot.connect(self.openPlot)
        self.RunList.previewPlotRequested.connect(self.open_run_preview_plot)
        self.RunList.previewExportRequested.connect(self.export_run_preview_csv)

        self.infoBox = moreInfo(preview_size=self.preview_size)
        self.infoBox.preview.plotRequested.connect(self.open_preview_plot)
        self.infoBox.preview.exportRequested.connect(self.export_preview_csv)
        self.infoBox.preview.previewsReady.connect(self.RunList.set_run_previews)
        if self.fileTextbox.text() and self.RunList.topLevelItemCount():
            self.infoBox.preview.set_database_runs(
                self.fileTextbox.text(),
                self.RunList.all_run_metadata(),
                )

        self._init_empty_state()
        self.runInfoSplitter = qtw.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.runInfoSplitter.setHandleWidth(8)
        self.runInfoSplitter.setChildrenCollapsible(True)
        self.runInfoSplitter.setOpaqueResize(True)
        self.runInfoSplitter.addWidget(self.RunList)
        self.runInfoSplitter.addWidget(self.infoBox)
        self.runInfoSplitter.setCollapsible(0, False)
        self.runInfoSplitter.setCollapsible(1, True)
        self.runInfoSplitter.setStretchFactor(0, 3)
        self.runInfoSplitter.setStretchFactor(1, 2)
        self.runInfoSplitter.setSizes([380, self._details_pane_height()])
        self.runInfoSplitter.handle(1).setToolTip(
            "Drag to resize the run list and details panes"
            )
        self.l.addWidget(self.emptyStateFrame)
        self.l.addWidget(self.runInfoSplitter, 1)
        self._sync_empty_state()

    def initShortcuts(self):
        """
        Register keyboard shortcuts for common run actions.

        """
        plot_entered = QtGui.QAction("Plot Entered Run and Measurement", self)
        plot_entered.setShortcut("Ctrl+Return")
        plot_entered.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        plot_entered.setStatusTip("Plot the run and measurement entered above")
        plot_entered.triggered.connect(lambda _: self.plotRunButton.click())
        self.addAction(plot_entered)

        plot_selected_all = QtGui.QAction("Plot All Measurements in Selected Run", self)
        plot_selected_all.setShortcut("Ctrl+Shift+Return")
        plot_selected_all.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        plot_selected_all.setStatusTip("Plot all measurements in the selected run")
        plot_selected_all.triggered.connect(self.open_selected_run_all)
        self.addAction(plot_selected_all)

        self.open_param_actions = []
        for itr in range(9):
            action = QtGui.QAction(f"Plot Measurement {itr + 1} in Selected Run", self)
            action.setShortcut(f"Ctrl+{itr + 1}")
            action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
            action.setStatusTip(f"Plot measurement {itr + 1} in the selected run")
            action.triggered.connect(lambda _, index=itr: self.open_param_by_index(index))
            self.addAction(action)
            self.open_param_actions.append(action)

    def _init_empty_state(self):
        """
        Create the empty-database prompt shown before any runs are available.

        """
        self.emptyStateFrame = qtw.QFrame()
        self.emptyStateFrame.setObjectName("mainEmptyState")
        self.emptyStateFrame.setFrameShape(qtw.QFrame.Shape.NoFrame)
        layout = qtw.QHBoxLayout(self.emptyStateFrame)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        icon = qtw.QLabel()
        icon.setPixmap(
            self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_DialogOpenButton).pixmap(24, 24)
            )
        layout.addWidget(icon)

        text_layout = qtw.QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)

        title = qtw.QLabel("No database loaded")
        title.setObjectName("mainEmptyStateTitle")
        text_layout.addWidget(title)
        self.emptyStateTitle = title

        detail = qtw.QLabel(
            "Drop a QCoDeS .db file onto the database field, or load one now."
            )
        detail.setObjectName("mainEmptyStateDetail")
        detail.setWordWrap(True)
        text_layout.addWidget(detail)
        self.emptyStateDetail = detail
        layout.addLayout(text_layout, 1)

        load_button = qtw.QToolButton()
        load_button.setObjectName("databaseIconButton")
        load_button.setIcon(self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_DialogOpenButton))
        load_button.setText("Load Database...")
        load_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        load_button.setToolTip("Load a QCoDeS .db database")
        load_button.setAccessibleName("Load database")
        load_button.clicked.connect(self.getfile)
        layout.addWidget(load_button)
        self.emptyStateLoadButton = load_button

        refresh_button = qtw.QToolButton()
        refresh_button.setObjectName("databaseIconButton")
        refresh_button.setIcon(self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_BrowserReload))
        refresh_button.setText("Refresh")
        refresh_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        refresh_button.setToolTip("Check for new measurements")
        refresh_button.setAccessibleName("Refresh database")
        refresh_button.clicked.connect(self.refreshMain)
        layout.addWidget(refresh_button)
        self.emptyStateRefreshButton = refresh_button

        help_button = qtw.QToolButton()
        help_button.setObjectName("databaseIconButton")
        help_button.setIcon(self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_DialogHelpButton))
        help_button.setText("Quick Start")
        help_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        help_button.setToolTip("Show the basic qPlot workflow")
        help_button.setAccessibleName("Show quick start")
        help_button.clicked.connect(lambda: show_quick_start(self))
        layout.addWidget(help_button)
        self.emptyStateHelpButton = help_button

    def _sync_empty_state(self):
        """
        Show the empty prompt while no runs are available.

        """
        frame = getattr(self, "emptyStateFrame", None)
        if frame is None:
            return

        database_path = ""
        if hasattr(self, "fileTextbox"):
            database_path = self.fileTextbox.text()

        run_count = 0
        run_list = getattr(self, "RunList", None)
        if run_list is not None and hasattr(run_list, "topLevelItemCount"):
            run_count = run_list.topLevelItemCount()

        loading = getattr(self, "_database_load_active", False)
        has_runs = run_count > 0
        frame.setVisible(not loading and not has_runs)

        if loading or has_runs:
            return

        if database_path:
            sync_loaded = getattr(self, "_sync_loaded_empty_state", None)
            if callable(sync_loaded):
                sync_loaded(database_path)
        else:
            sync_empty = getattr(self, "_sync_no_database_empty_state", None)
            if callable(sync_empty):
                sync_empty()

    def _sync_no_database_empty_state(self):
        title = getattr(self, "emptyStateTitle", None)
        if title is not None:
            title.setText("No database loaded")

        detail = getattr(self, "emptyStateDetail", None)
        if detail is not None:
            detail.setText(
                "Drop a QCoDeS .db file onto the database field, or load one now."
                )

        self._set_empty_state_button_visible("emptyStateLoadButton", True)
        self._set_empty_state_button_visible("emptyStateRefreshButton", False)
        self._set_empty_state_button_visible("emptyStateHelpButton", True)

    def _sync_loaded_empty_state(self, database_path):
        title = getattr(self, "emptyStateTitle", None)
        if title is not None:
            title.setText("Waiting for measurements")

        detail = getattr(self, "emptyStateDetail", None)
        if detail is not None:
            detail.setText(self._loaded_empty_database_detail(database_path))

        self._set_empty_state_button_visible("emptyStateLoadButton", True)
        self._set_empty_state_button_visible("emptyStateRefreshButton", True)
        self._set_empty_state_button_visible("emptyStateHelpButton", False)

    def _set_empty_state_button_visible(self, attr, visible):
        button = getattr(self, attr, None)
        if button is not None:
            button.setVisible(visible)

    def _loaded_empty_database_detail(self, database_path):
        basename = database_path
        if database_path:
            basename = os.path.basename(database_path) or database_path

        interval = self._current_refresh_interval()
        if interval > 0:
            return (
                f"{basename} is loaded. qPlot will add measurements as they "
                f"appear, checking every {interval:g} s."
                )

        return (
            f"{basename} is loaded. Refresh is set to manual; press Refresh "
            "to check for measurements."
            )

    def _current_refresh_interval(self):
        spin_box = getattr(self, "spinBox", None)
        if spin_box is None or not hasattr(spin_box, "value"):
            return 0.0

        try:
            return float(spin_box.value())
        except (TypeError, ValueError):
            return 0.0

    @QtCore.pyqtSlot(float)
    def monitorIntervalChanged(self, interval):
        """
        Updates the refresh interval for checking for new runs in database.

        """
        self._save_refresh_interval(interval)
        self._apply_refresh_interval(interval)
        self._sync_empty_state()

    @QtCore.pyqtSlot(bool)
    def _auto_plot_changed(self, checked):
        """
        Persists the Auto-plot checkbox state.

        """
        self.config.update(AUTO_PLOT_KEY, bool(checked))
        if checked:
            self._auto_plot_current_running_run()

    def _auto_plot_current_running_run(self):
        """
        Opens the newest incomplete run already present in the run list.

        """
        run_list = getattr(self, "RunList", None)
        if run_list is None or not hasattr(run_list, "all_run_metadata"):
            return None

        running_runs = [
            metadata
            for metadata in run_list.all_run_metadata().values()
            if metadata.get("guid") and not run_is_complete(metadata)
            ]
        if not running_runs:
            return None

        metadata = max(
            running_runs,
            key=_run_timestamp_sort_key,
            )
        self.openPlot(metadata["guid"])
        return metadata["guid"]

    def _apply_refresh_interval(self, interval):
        """
        Applies the current refresh interval to the main-window timer.

        """
        self.monitor.stop()
        if interval > 0:
            self.monitor.start(int(interval * 1000))

    def _save_refresh_interval(self, interval):
        """
        Persists the main refresh interval as the user's default.

        """
        interval = float(interval)
        try:
            current_interval = float(
                self.config.get("user_preference.default_refresh_rate")
                )
        except (KeyError, TypeError, ValueError):
            current_interval = None

        if current_interval != interval:
            self.config.update("user_preference.default_refresh_rate", interval)

    @QtCore.pyqtSlot(str)
    def update_run_id(self, text):
        """
        Updates the run ID target entered into the run text box.

        """
        self.RunList.blockSignals(True)
        self.RunList.clearSelection()
        self.RunList.blockSignals(False)
        self.ds = None
        self.infoBox.clear()

        try:
            self.selected_run_id = int(text)
        except ValueError:
            self.selected_run_id = None

    @QtCore.pyqtSlot()
    def sync_run_id_selection(self):
        """
        Selects the typed run ID in the table if it is currently visible.

        """
        if self.selected_run_id is None:
            return

        matches = self.RunList.findItems(
            str(self.selected_run_id),
            QtCore.Qt.MatchFlag.MatchExactly,
            0,
            )
        if not matches:
            return

        item = matches[0]
        self.RunList.setCurrentItem(item)
        self.RunList.scrollToItem(item, qtw.QAbstractItemView.ScrollHint.PositionAtCenter)

    def _sync_refresh_interval(self):
        interval = self.config.get("user_preference.default_refresh_rate")
        if not hasattr(self, "spinBox"):
            return

        self.spinBox.blockSignals(True)
        self.spinBox.setValue(interval)
        self.spinBox.blockSignals(False)
        self._apply_refresh_interval(self.spinBox.value())

    def _configured_preview_size(self):
        try:
            return int(self.config.get("GUI.preview_size"))
        except (KeyError, TypeError, ValueError):
            return PREVIEW_SIZE

    def _details_pane_height(self):
        return max(260, int(self.preview_size) + 84)
