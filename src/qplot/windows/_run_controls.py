import os

from PyQt6 import (
    QtCore,
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

from ._commands import create_action, plot_measurement_command_spec
from ._config_persistence import (
    persist_config_value,
    set_widget_value_without_signals,
)
from ._help import show_quick_start
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

    def initRefresh(self):
        """
        Initialise the main window refresh controls.

        Refresh checks for any new runs added to the dataset.

        """
        self.spinBox = qtw.QDoubleSpinBox()
        self.spinBox.setRange(0.0, 86_400.0)
        self.spinBox.setSingleStep(0.1)
        self.spinBox.setDecimals(1)
        self.spinBox.setSuffix(" s")
        self.spinBox.setFixedWidth(84)
        self.spinBox.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.spinBox.setToolTip("Refresh interval in seconds")
        self.spinBox.setValue(self.config.get("user_preference.default_refresh_rate"))

        self._automatic_refresh_epoch = 0
        self._automatic_refresh_load_generation = None
        self._automatic_refresh_instance = None
        self._automatic_refresh_shutdown = False
        self.spinBox.valueChanged.connect(self.monitorIntervalChanged)
        # Keep exactly one connection.  The timeout is deliberately routed
        # through a lifecycle guard instead of directly to refreshMain:
        # stopping a QTimer does not make a timeout already queued on the GUI
        # event loop disappear.
        self.monitor.timeout.connect(self._automatic_refresh_timeout)

        self.autoPlotBox = qtw.QCheckBox()
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

        self.RunList = RunList(config=self.config)
        self.RunList.selected.connect(self.updateSelected)
        self.RunList.nonSingleSelection.connect(self.clear_non_single_run_selection)
        self.RunList.plot.connect(self.openPlot)
        self.RunList.previewPlotRequested.connect(self.open_run_preview_plot)
        self.RunList.previewExportRequested.connect(self.export_run_preview_csv)
        self.RunList.verticalScrollBar().valueChanged.connect(
            lambda _: self._run_table_view_changed()
            )

        self.infoBox = moreInfo(preview_size=self.preview_size)
        self.infoBox.preview.plotRequested.connect(self.open_preview_plot)
        self.infoBox.preview.exportRequested.connect(self.export_preview_csv)
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

    def initShortcuts(self):
        """
        Register keyboard shortcuts for common run actions.

        """
        plot_entered = create_action("run.plot_entered", self)
        plot_entered.triggered.connect(lambda _: self.plotRunButton.click())
        self.addAction(plot_entered)

        plot_selected_all = create_action("run.plot_selected_all", self)
        plot_selected_all.triggered.connect(self.open_selected_run_all)
        self.addAction(plot_selected_all)

        self.open_param_actions = []
        for itr in range(9):
            action = create_action(plot_measurement_command_spec(itr), self)
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
        if not self._save_refresh_interval(interval):
            self._sync_empty_state()
            return
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

    def _auto_plot_current_running_run(self):
        """
        Opens the newest incomplete run already present in the run list.

        """
        # Stage 4 leaves plot-data materialisation on the legacy snapshot path.
        # A trusted session may enter that path only through an explicit plot or
        # export action; enabling a persisted preference is not such an action.
        if getattr(self, "_database_access_mode", None) == TRUSTED_LIVE_MODE:
            return None
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
        self.openPlot(metadata["guid"])
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
                set_widget_value_without_signals(
                    spin_box,
                    spin_box.setValue,
                    current_interval,
                    )

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
        self._prioritize_preview_runs()


    def _prioritize_preview_runs(self, run_ids=None):
        generation_gate = getattr(
            self,
            "_database_generation_transaction_blocks_path",
            None,
        )
        if callable(generation_gate) and generation_gate():
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

        self.spinBox.blockSignals(True)
        self.spinBox.setValue(interval)
        self.spinBox.blockSignals(False)
        self._apply_refresh_interval(self.spinBox.value())

    def _configured_preview_size(self):
        return int(self.config.get("GUI.preview_size"))

    def _details_pane_height(self):
        return max(260, int(self.preview_size) + 84)
