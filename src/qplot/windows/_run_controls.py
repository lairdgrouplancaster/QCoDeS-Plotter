import os

from PyQt6 import (
    QtCore,
    QtGui,
)
from PyQt6 import (
    QtWidgets as qtw,
)
from PyQt6.QtGui import QIntValidator

from qplot.datahandling.file_identity import (
    database_instance,
    database_instances_differ,
)
from qplot.datahandling.trusted_live_service import TRUSTED_LIVE_MODE

from ._auto_plot import cancel_auto_plot, enqueue_auto_plot
from ._commands import create_action, plot_measurement_command_spec
from ._config_persistence import (
    persist_config_value,
    set_widget_value_without_signals,
)
from ._help import show_quick_start
from ._refresh_interval import (
    RefreshIntervalSpinBox,
    connect_refresh_interval_edits,
    refresh_interval_value,
    remember_refresh_interval,
    set_refresh_interval,
)
from ._shared_refresh import apply_shared_refresh_interval, refresh_plot_sources
from ._widgets import (
    RunList,
    moreInfo,
)
from ._widgets._run_formatting import run_is_complete

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

    ds: object | None

    def initRefresh(self):
        """
        Initialise the main window refresh controls.

        Refresh checks for any new runs added to the dataset.

        """
        self.spinBox = RefreshIntervalSpinBox(
            refresh_description="How often the run list and live plots check for new data.",
        )
        # Stack the step buttons instead of reserving a wide native button pair.
        self.spinBox.setStyleSheet(
            "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 18px; }"
        )
        set_refresh_interval(
            self.spinBox, self.config.get("user_preference.default_refresh_rate"),
        )

        self._automatic_refresh_epoch = 0
        self._automatic_refresh_load_generation = None
        self._automatic_refresh_instance = None
        self._automatic_refresh_shutdown = False
        connect_refresh_interval_edits(self.spinBox, self.monitorIntervalChanged)
        # Keep exactly one connection.  The timeout is deliberately routed
        # through a lifecycle guard instead of directly to refreshMain:
        # stopping a QTimer does not make a timeout already queued on the GUI
        # event loop disappear.
        self.monitor.timeout.connect(self._automatic_refresh_timeout)

        self.refreshStateLabel = qtw.QLabel()
        self.refreshStateLabel.setObjectName("automaticRefreshStatus")
        self.refreshStateLabel.setAccessibleName("Automatic refresh status")
        RunControlsMixin._update_refresh_state_label(self)

        self.autoPlotBox = qtw.QCheckBox("Auto-plot")
        self.autoPlotBox.setChecked(self.config.get(AUTO_PLOT_KEY))
        self.autoPlotBox.setToolTip(
            "Open newly detected runs and the newest running run when enabled"
            )
        self.autoPlotBox.toggled.connect(self._auto_plot_changed)

        self.refreshDatabaseButton = qtw.QToolButton()
        self.refreshDatabaseButton.setObjectName("refreshIconButton")
        self.refreshDatabaseButton.setIcon(
            self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_BrowserReload)
            )
        self.refreshDatabaseButton.setToolTip("Refresh the run list and all live plots (R)")
        self.refreshDatabaseButton.setAccessibleName("Refresh run list and live plots")
        self.refreshDatabaseButton.setFixedSize(28, 26)
        self.refreshDatabaseButton.clicked.connect(lambda: RunControlsMixin.refresh_all(self))

        self.closeAllPlotsButton = qtw.QToolButton()
        self.closeAllPlotsButton.setObjectName("closeAllPlotsButton")
        self.closeAllPlotsButton.setIcon(
            self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_TitleBarCloseButton)
            )
        self.closeAllPlotsButton.setText("Close all plots")
        self.closeAllPlotsButton.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
        self.closeAllPlotsButton.setToolTip("Close all plot windows (Ctrl+Shift+W)")
        self.closeAllPlotsButton.setAccessibleName("Close all plot windows")
        self.closeAllPlotsButton.setFixedHeight(26)
        self.closeAllPlotsButton.setEnabled(False)
        self.closeAllPlotsButton.clicked.connect(self.closeAll)

    def initRunDisplay(self):
        sublayout = qtw.QHBoxLayout()
        sublayout.setContentsMargins(8, 0, 8, 2)
        sublayout.setSpacing(6)

        self.runIdLabel = qtw.QLabel("Run:")
        sublayout.addWidget(self.runIdLabel)

        self.selected_run_id = None

        self.run_idBox = qtw.QLineEdit()
        self.run_idBox.setMaximumWidth(58)
        self.run_idBox.setFixedWidth(58)
        self.run_idBox.setValidator(QIntValidator())
        self.run_idBox.setPlaceholderText("ID")
        self.run_idBox.setToolTip("Run ID to plot or export as CSV")
        self.run_idBox.textEdited.connect(self.update_run_id)
        self.run_idBox.editingFinished.connect(self.sync_run_id_selection)
        self.run_idBox.returnPressed.connect(self._open_entered_run)
        sublayout.addWidget(self.run_idBox)

        self.measurementLabel = qtw.QLabel("Measurement:")
        sublayout.addWidget(self.measurementLabel)

        self.measurementBox = qtw.QLineEdit()
        self.measurementBox.setMaximumWidth(46)
        self.measurementBox.setFixedWidth(46)
        self.measurementBox.setText("*")
        self.measurementBox.setToolTip(
            "Enter a measurement number (starting at 1), or * for every measurement in the run, to plot or export as CSV."
        )
        self.measurementBox.returnPressed.connect(self._open_entered_run)
        sublayout.addWidget(self.measurementBox)
        sublayout.addSpacing(6)  # Together with layout spacing, a 12 px action gap.

        self.plotRunButton = qtw.QToolButton()
        self.plotRunButton.setObjectName("plotIconButton")
        self.plotRunButton.setIcon(
            self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_MediaPlay)
            )
        self.plotRunButton.setText("Plot")
        self.plotRunButton.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
        self.plotRunButton.setToolTip(
            "Plot the run and measurement specified by the ID and Measurement fields (Ctrl+Return)."
        )
        self.plotRunButton.setAccessibleName("Plot measurement")
        self.plotRunButton.setFixedHeight(26)
        self.plotRunButton.clicked.connect(self._open_entered_run)
        sublayout.addWidget(self.plotRunButton)

        self.exportCsvButton = qtw.QToolButton()
        self.exportCsvButton.setObjectName("exportIconButton")
        self.exportCsvButton.setIcon(
            self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_DialogSaveButton)
            )
        self.exportCsvButton.setText("Export CSV…")
        self.exportCsvButton.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
        def update_export_scope():
            run = self.run_idBox.text().strip()
            measurement = self.measurementBox.text().strip()
            scope = "all measurements" if measurement == "*" else f"measurement {measurement}"
            description = f"Export {scope} from run {run} as CSV (raw values)."
            self.exportCsvButton.setToolTip(description)
            self.exportCsvButton.setAccessibleDescription(description)
        self.run_idBox.textChanged.connect(update_export_scope)
        self.measurementBox.textChanged.connect(update_export_scope)
        update_export_scope()
        self.exportCsvButton.setAccessibleName("Export raw measurement values to CSV")
        self.exportCsvButton.setFixedHeight(26)
        self.exportCsvButton.clicked.connect(self.exportRunCsv)
        sublayout.addWidget(self.exportCsvButton)

        sublayout.addSpacing(6)
        sublayout.addWidget(self.autoPlotBox)

        sublayout.addStretch()

        sublayout.addWidget(self.closeAllPlotsButton)

        self.targetLayout.addSpacing(60)
        self.refreshIntervalLabel = qtw.QLabel("Refresh every")
        self.refreshIntervalLabel.setBuddy(self.spinBox)
        self.targetLayout.addWidget(self.refreshIntervalLabel)
        self.targetLayout.addWidget(self.spinBox)
        self.targetLayout.addWidget(self.refreshDatabaseButton)

        self.l.addLayout(self.targetLayout)
        self.l.addWidget(self.databaseLoadFrame)
        self.l.addLayout(sublayout)

        self.RunList = RunList(config=self.config)
        self.RunList.selected.connect(self.updateSelected)
        self.RunList.nonSingleSelection.connect(self.clear_non_single_run_selection)
        self.RunList.itemSelectionChanged.connect(self._sync_database_generation_controls)
        self.run_idBox.textChanged.connect(self._sync_database_generation_controls)
        self.measurementBox.textChanged.connect(self._sync_database_generation_controls)
        self.RunList.plot.connect(self.openPlot)
        self.RunList.previewPlotRequested.connect(self.open_run_preview_plot)
        self.RunList.previewExportRequested.connect(self.export_run_preview_csv)
        self.RunList.batchExportRequested.connect(self.export_selected_previews_csv)
        self.RunList.previewStatsRequested.connect(self.show_run_preview_stats)
        self.RunList.verticalScrollBar().valueChanged.connect(
            lambda _: self._run_table_view_changed()
            )

        self.infoBox = moreInfo(preview_size=self.preview_size)
        self.infoBox.openSnapshotPlots.connect(self.open_selected_run_all)
        self.infoBox.exportSnapshotCsv.connect(self.export_selected_runs_csv)
        self.infoBox.preview.plotRequested.connect(self.open_preview_plot)
        self.infoBox.preview.exportRequested.connect(self.export_preview_csv)
        self.infoBox.preview.batchExportRequested.connect(self.export_selected_previews_csv)
        self.infoBox.preview.statsRequested.connect(self.show_preview_stats)
        self.infoBox.preview.previewsReady.connect(self.RunList.set_run_previews)
        self.infoBox.preview.previewGenerationChanged.connect(
            self.RunList.set_run_preview_generating
            )
        self.infoBox.preview.databaseReplaced.connect(
            self._reload_replaced_database
            )
        if self.fileTextbox.text() and self.RunList.topLevelItemCount():
            self.infoBox.preview.set_database_runs(
                self.fileTextbox.text(),
                self.RunList.all_run_metadata(),
                )
            self._prioritize_preview_runs()

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

    def _match_run_action_button_widths(self):
        """Size the run actions after they inherit the current font and theme."""
        buttons = (self.plotRunButton, self.exportCsvButton)
        for button in buttons:
            button.ensurePolished()
        width = max(button.sizeHint().width() for button in buttons)
        for button in buttons:
            button.setFixedWidth(width)

    def _sync_available_actions(self, reads_enabled):
        """Enable commands only when their database or run target is available."""
        instance = getattr(self, "_loaded_database_instance", None)
        has_database = getattr(instance, "identity", None) is not None
        database_ready = reads_enabled and has_database
        run_list = getattr(self, "RunList", None)
        has_runs = bool(database_ready and run_list is not None and run_list.topLevelItemCount())
        selected_ids = run_list.selected_run_ids() if has_runs else []

        target_available = False
        if has_runs:
            try:
                run_id = int(self.run_idBox.text())
            except ValueError:
                pass
            else:
                target_available = bool(run_list.findItems(
                    str(run_id), QtCore.Qt.MatchFlag.MatchExactly, 0,
                ))
        measurement = self.measurementBox.text().strip()
        try:
            valid_measurement = measurement in ("", "*") or int(measurement) > 0
        except ValueError:
            valid_measurement = False
        can_plot = target_available and valid_measurement
        can_refresh = reads_enabled and (has_database or bool(getattr(self, "windows", ())))

        def enable(names, enabled):
            for name in names:
                control = getattr(self, name, None)
                if isinstance(control, qtw.QLineEdit):
                    # Losing focus during source retirement must not commit a
                    # typed selection against the reader that just closed.
                    set_widget_value_without_signals(control, control.setEnabled, bool(enabled))
                elif control is not None:
                    control.setEnabled(bool(enabled))

        enable(("copyDatabasePathButton", "databaseInfoButton", "openDatabaseFolderButton",
                "openDatabaseFolderAction", "exportDatabaseAction", "autoPlotBox"), database_ready)
        enable(("closeDatabaseAction",), has_database)
        enable(("refreshIntervalLabel", "spinBox", "refreshDatabaseButton", "refreshAction",
                "emptyStateRefreshButton"), can_refresh)
        enable(("runIdLabel", "run_idBox", "measurementLabel", "measurementBox"), has_runs)
        enable(("RunList",), database_ready)
        enable(("infoBox", "plotSelectedRunAction", "plotTableSelectionAction"), len(selected_ids) == 1)
        enable(("plotRunButton", "exportCsvButton", "plotEnteredRunAction"), can_plot)
        enable(("exportSelectedRunsAction", "exportSelectedDatabaseAction"), bool(selected_ids))
        for action in getattr(self, "open_param_actions", ()):
            action.setEnabled(target_available)

    def initShortcuts(self):
        """
        Register keyboard shortcuts for common run actions.

        """
        plot_entered = create_action("run.plot_entered", self)
        plot_entered.triggered.connect(lambda _: self.plotRunButton.click())
        self.addAction(plot_entered)
        self.plotEnteredRunAction = plot_entered

        plot_selected_all = create_action("run.plot_selected_all", self)
        plot_selected_all.triggered.connect(self.open_selected_run_all)
        self.addAction(plot_selected_all)
        self.plotSelectedRunAction = plot_selected_all
        table_enter = QtGui.QAction("Plot selected run", self.RunList)
        table_enter.setShortcuts([QtGui.QKeySequence("Return"), QtGui.QKeySequence("Enter")])
        table_enter.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        table_enter.triggered.connect(self.open_selected_run_all)
        self.RunList.addAction(table_enter)
        self.plotTableSelectionAction = table_enter

        self.open_param_actions = []
        for itr in range(9):
            action = create_action(plot_measurement_command_spec(itr), self)
            action.triggered.connect(lambda _, index=itr: self.open_param_by_index(index))
            self.addAction(action)
            self.open_param_actions.append(action)
        self._sync_database_generation_controls()

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
            "Use Load database… above, or drop a QCoDeS .db file onto the database field."
            )
        detail.setObjectName("mainEmptyStateDetail")
        detail.setWordWrap(True)
        text_layout.addWidget(detail)
        self.emptyStateDetail = detail
        layout.addLayout(text_layout, 1)

        refresh_button = qtw.QToolButton()
        refresh_button.setObjectName("databaseIconButton")
        refresh_button.setIcon(self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_BrowserReload))
        refresh_button.setText("Refresh")
        refresh_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        refresh_button.setToolTip("Refresh the run list and all live plots")
        refresh_button.setAccessibleName("Refresh run list and live plots")
        refresh_button.clicked.connect(lambda: RunControlsMixin.refresh_all(self))
        layout.addWidget(refresh_button)
        self.emptyStateRefreshButton = refresh_button

        help_button = qtw.QToolButton()
        help_button.setObjectName("databaseIconButton")
        help_button.setIcon(self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_DialogHelpButton))
        help_button.setText("Quick start")
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
        sync_controls = getattr(self, "_sync_database_generation_controls", None)
        if callable(sync_controls):
            sync_controls()
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
                "Use Load database… above, or drop a QCoDeS .db file onto the database field."
                )

        self._set_empty_state_button_visible("emptyStateRefreshButton", False)
        self._set_empty_state_button_visible("emptyStateHelpButton", True)

    def _sync_loaded_empty_state(self, database_path):
        title = getattr(self, "emptyStateTitle", None)
        if title is not None:
            title.setText("Waiting for measurements")

        detail = getattr(self, "emptyStateDetail", None)
        if detail is not None:
            detail.setText(self._loaded_empty_database_detail(database_path))

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
            return refresh_interval_value(spin_box)
        except (TypeError, ValueError):
            return 0.0

    @QtCore.pyqtSlot()
    def refresh_all(self):
        """Manually refresh the run table and each live plot source once."""
        refresh_plot_sources(self)
        self.refreshMain(retry_preview=True)

    @QtCore.pyqtSlot(float)
    def monitorIntervalChanged(self, interval):
        """
        Updates the shared refresh interval for the run list and live plots.

        """
        if not self._save_refresh_interval(interval):
            self._sync_empty_state()
            return
        remember_refresh_interval(self.spinBox, interval)
        self._apply_refresh_interval(interval)
        self._sync_empty_state()

    @QtCore.pyqtSlot(bool)
    def _auto_plot_changed(self, checked):
        """
        Persists the Auto-plot checkbox state.

        """
        previous_checked = self.config.get(AUTO_PLOT_KEY)

        def rollback():
            checkbox = getattr(self, "autoPlotBox", None)
            if checkbox is not None:
                set_widget_value_without_signals(
                    checkbox,
                    checkbox.setChecked,
                    previous_checked,
                    )

        if not persist_config_value(
                self,
                self.config,
                AUTO_PLOT_KEY,
                bool(checked),
                "the Auto-plot preference",
                rollback,
                ):
            return
        if checked:
            self._auto_plot_current_running_run()
        else:
            cancel_auto_plot(self)

    def _auto_plot_current_running_run(self):
        """
        Opens the newest incomplete run already present in the run list.

        """
        generation_gate = getattr(
            self,
            "_database_generation_transaction_blocks_path",
            None,
        )
        if callable(generation_gate) and generation_gate():
            return None
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
        enqueue_auto_plot(self, [metadata["guid"]])
        return metadata["guid"]

    def _apply_refresh_interval(self, interval):
        """
        Reconcile the automatic-refresh timer with the committed DB lifecycle.

        A positive preference is retained even when there is no database to
        refresh.  It becomes active only after a DatabaseInstance has been
        accepted by database_load_finished.

        """
        generation_gate = getattr(
            self,
            "_database_generation_transaction_blocks_path",
            None,
        )
        if callable(generation_gate) and generation_gate():
            state = getattr(self, "_test_database_replacement_state", None)
            if state is not None:
                state.monitor_was_active = interval > 0
                state.monitor_interval_ms = max(1, round(interval * 1000))
        RunControlsMixin._update_automatic_refresh_timer(self, interval)
        apply_shared_refresh_interval(self, interval)

    def _committed_refresh_database_instance(self):
        """Return the accepted source instance, never inferring it from text."""
        instance = getattr(self, "_loaded_database_instance", None)
        if instance is None or getattr(instance, "identity", None) is None:
            return None
        return instance

    def _automatic_refresh_should_run(self, interval):
        """Whether the main refresh timer is presently allowed to run."""
        if (
                interval <= 0
                or RunControlsMixin._committed_refresh_database_instance(self)
                is None
                ):
            return False
        if getattr(self, "_automatic_refresh_shutdown", False):
            return False
        if (
                getattr(self, "_shutdown_started", False)
                or getattr(self, "_shutdown_ready", False)
                or getattr(self, "_database_load_active", False)
                or getattr(self, "_database_view_released_for_generation", False)
                ):
            return False
        generation_gate = getattr(
            self,
            "_database_generation_transaction_blocks_path",
            None,
        )
        return not (callable(generation_gate) and generation_gate())

    def _stop_automatic_refresh_timer(self):
        """Stop the timer and invalidate timeout events already queued."""
        self._automatic_refresh_epoch = (
            getattr(self, "_automatic_refresh_epoch", 0) + 1
        )
        self._automatic_refresh_load_generation = None
        self._automatic_refresh_instance = None
        monitor = getattr(self, "monitor", None)
        stop = getattr(monitor, "stop", None)
        if callable(stop):
            stop()
        RunControlsMixin._update_refresh_state_label(self)

    def _update_refresh_state_label(self):
        label = getattr(self, "refreshStateLabel", None)
        if label is None:
            return
        if RunControlsMixin._committed_refresh_database_instance(self) is None:
            state = "unavailable"
            explanation = "Load a database to enable automatic refresh."
        elif self.monitor.isActive():
            state = "on"
            explanation = "The run list is checked automatically at the selected interval."
        elif RunControlsMixin._current_refresh_interval(self) <= 0:
            state = "off"
            explanation = "Automatic refresh is disabled. Use Refresh to check manually."
        else:
            state = "paused"
            explanation = (
                "Automatic refresh is configured but is waiting for an available, "
                "loaded database."
            )
        label.setText(f"Auto-refresh: {state}")
        label.setToolTip(explanation)

    def _update_automatic_refresh_timer(self, interval=None):
        """Start or pause automatic refresh according to authoritative state."""
        if interval is None:
            interval = self._current_refresh_interval()
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            interval = 0.0

        RunControlsMixin._stop_automatic_refresh_timer(self)
        if not RunControlsMixin._automatic_refresh_should_run(self, interval):
            return False

        instance = RunControlsMixin._committed_refresh_database_instance(self)
        self._automatic_refresh_load_generation = getattr(
            self,
            "_database_load_generation",
            0,
        )
        self._automatic_refresh_instance = instance
        monitor = getattr(self, "monitor", None)
        start = getattr(monitor, "start", None)
        if not callable(start):
            return False
        start(max(1, round(interval * 1000)))
        RunControlsMixin._update_refresh_state_label(self)
        return True

    @QtCore.pyqtSlot()
    def _automatic_refresh_timeout(self):
        """Refresh only if this timeout still belongs to the committed source."""
        interval = self._current_refresh_interval()
        expected_instance = getattr(self, "_automatic_refresh_instance", None)
        if (
                not RunControlsMixin._automatic_refresh_should_run(self, interval)
                or expected_instance is None
                or expected_instance
                != RunControlsMixin._committed_refresh_database_instance(self)
                or getattr(self, "_automatic_refresh_load_generation", None)
                != getattr(self, "_database_load_generation", 0)
                ):
            RunControlsMixin._stop_automatic_refresh_timer(self)
            return

        # A file can disappear or be atomically replaced between timer ticks.
        # Let the normal replacement lifecycle handle that, but never launch a
        # refresh for the old instance.
        database_path = expected_instance.logical_path
        try:
            current_instance = database_instance(database_path)
        except OSError:
            current_instance = None
        if current_instance is None or database_instances_differ(
                expected_instance,
                current_instance,
                ):
            RunControlsMixin._stop_automatic_refresh_timer(self)
            reload_database = getattr(self, "_reload_replaced_database", None)
            if callable(reload_database):
                reload_database(database_path)
            return

        self.refreshMain(automatic=True)

    def _save_refresh_interval(self, interval):
        """
        Persists the main refresh interval as the user's default.

        """
        interval = float(interval)
        current_interval = float(
            self.config.get("user_preference.default_refresh_rate")
            )

        if current_interval == interval:
            return True

        def rollback():
            spin_box = getattr(self, "spinBox", None)
            if spin_box is not None:
                set_refresh_interval(spin_box, current_interval)

        return persist_config_value(
            self,
            self.config,
            "user_preference.default_refresh_rate",
            interval,
            "the refresh interval",
            rollback,
            )

    @QtCore.pyqtSlot(str)
    def update_run_id(self, text):
        """
        Updates the run ID target entered into the run text box.

        """
        cancel_selected_detail = getattr(
            self,
            "_cancel_selected_run_detail",
            None,
        )
        if callable(cancel_selected_detail):
            cancel_selected_detail()
        self._selected_run_guid = None
        self.RunList.blockSignals(True)
        self.RunList.clearSelection()
        self.RunList.blockSignals(False)
        release_selected = getattr(self, "_release_selected_dataset", None)
        if callable(release_selected):
            release_selected()
        else:
            self.ds = None
            self._selected_dataset_key = None
        self.infoBox.clear()
        derived_bridge = getattr(self, "_trusted_derived_bridge", None)
        if derived_bridge is not None:
            derived_bridge.request_priority_update()

        try:
            self.selected_run_id = int(text)
        except ValueError:
            self.selected_run_id = None

    @QtCore.pyqtSlot()
    def _open_entered_run(self):
        """Commit the typed target before accepting an asynchronous plot request."""
        # Return is emitted before editingFinished. Without this sync, the
        # request captures a missing GUID and the subsequent selection update
        # makes that same requested run look stale to the publication guard.
        self.sync_run_id_selection()
        self.openRun()

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
        # A typed Run ID selects exactly one row, even while Ctrl+Return is
        # held. ExtendedSelection otherwise interprets Ctrl as a toggle.
        self.RunList.setCurrentItem(
            item, 0, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect,
        )
        self.RunList.scrollToItem(item, qtw.QAbstractItemView.ScrollHint.PositionAtCenter)


    def _run_table_view_changed(self):
        generation_gate = getattr(
            self,
            "_database_generation_transaction_blocks_path",
            None,
        )
        if callable(generation_gate) and generation_gate():
            return
        prioritize_details = getattr(self, "_prioritize_database_detail_runs", None)
        if callable(prioritize_details):
            prioritize_details()
        derived_bridge = getattr(self, "_trusted_derived_bridge", None)
        if derived_bridge is not None:
            derived_bridge.request_priority_update()
        self._prioritize_preview_runs()


    def _prioritize_preview_runs(self, run_ids=None):
        generation_gate = getattr(
            self,
            "_database_generation_transaction_blocks_path",
            None,
        )
        if callable(generation_gate) and generation_gate():
            return
        if getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE:
            derived_bridge = getattr(self, "_trusted_derived_bridge", None)
            if derived_bridge is not None:
                derived_bridge.request_priority_update()
            return
        preview = getattr(getattr(self, "infoBox", None), "preview", None)
        prioritize = getattr(preview, "prioritize_runs", None)
        if not callable(prioritize):
            return

        selected_run_ids, visible_run_ids = self._preview_priority_run_ids(run_ids)
        prioritize(
            selected_run_ids=selected_run_ids,
            visible_run_ids=visible_run_ids,
            )


    def _preview_priority_run_ids(self, run_ids=None):
        selected_run_ids = []
        visible_run_ids = []
        seen_selected = set()
        seen_visible = set()

        def add(target, seen, candidate):
            if candidate is None:
                return
            try:
                key = int(candidate)
            except (TypeError, ValueError):
                key = candidate
            if key in seen:
                return
            target.append(candidate)
            seen.add(key)

        if isinstance(run_ids, (list, tuple, set)):
            for run_id in run_ids:
                add(selected_run_ids, seen_selected, run_id)
        else:
            add(selected_run_ids, seen_selected, run_ids)

        run_list = getattr(self, "RunList", None)
        selected = getattr(run_list, "selected_run_ids", None)
        if callable(selected):
            for run_id in selected():
                add(selected_run_ids, seen_selected, run_id)

        visible = getattr(run_list, "visible_run_ids", None)
        if callable(visible):
            for run_id in visible():
                add(visible_run_ids, seen_visible, run_id)

        return selected_run_ids, visible_run_ids

    def _sync_refresh_interval(self):
        interval = self.config.get("user_preference.default_refresh_rate")
        if not hasattr(self, "spinBox"):
            return

        set_refresh_interval(self.spinBox, interval)
        self._apply_refresh_interval(interval)

    def _configured_preview_size(self):
        return int(self.config.get("GUI.preview_size"))

    def _details_pane_height(self):
        return max(260, int(self.preview_size) + 84)
