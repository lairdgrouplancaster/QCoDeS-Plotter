import os
from copy import deepcopy

from PyQt6 import (
    QtCore,
    QtGui,
)
from PyQt6 import (
    QtWidgets as qtw,
)

from ._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    CONFIRM_QUIT_KEY,
)

THEME_OPTIONS = (
    ("Light", "light"),
    ("Dark", "dark"),
    ("PyQt", "pyqt"),
    )

MOUSE_MODE_KEY = "user_preference.mouse_mode"
MOUSE_MODE_OPTIONS = (
    ("Rectangle zoom (1 button)", "rect"),
    ("Pan (3 button)", "pan"),
)

PREFERENCE_KEYS = (
    "user_preference.theme",
    "GUI.preview_size",
    MOUSE_MODE_KEY,
    "file.default_load_path",
    "user_preference.default_refresh_rate",
    CONFIRM_CLOSE_ALL_KEY,
    CONFIRM_QUIT_KEY,
    "runtime_settings.max_threads",
    "runtime_settings.del_grace_period",
    "runtime_settings.cloud_sync_timeout",
    )


class PreferencesDialog(qtw.QDialog):
    """
    Dialog for editing common config-backed qPlot preferences.

    """
    preferencesApplied = QtCore.pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config

        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.setMinimumWidth(420)

        self._build_ui()
        self.load_from_config()

    def _build_ui(self):
        layout = qtw.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        tabs = qtw.QTabWidget(self)
        tabs.addTab(self._appearance_tab(), "Appearance")
        tabs.addTab(self._interaction_tab(), "Interaction")
        tabs.addTab(self._files_tab(), "Files")
        tabs.addTab(self._confirmations_tab(), "Confirmations")
        tabs.addTab(self._runtime_tab(), "Runtime")
        layout.addWidget(tabs)

        self.buttonBox = qtw.QDialogButtonBox(
            qtw.QDialogButtonBox.StandardButton.Ok
            | qtw.QDialogButtonBox.StandardButton.Cancel
            | qtw.QDialogButtonBox.StandardButton.Apply
            | qtw.QDialogButtonBox.StandardButton.RestoreDefaults,
            self,
            )
        self.buttonBox.accepted.connect(self._accept_preferences)
        self.buttonBox.rejected.connect(self.reject)
        self.buttonBox.button(qtw.QDialogButtonBox.StandardButton.Apply).clicked.connect(
            self.apply_preferences
            )
        self.restoreDefaultsButton = self.buttonBox.button(
            qtw.QDialogButtonBox.StandardButton.RestoreDefaults
            )
        self.restoreDefaultsButton.setObjectName("restorePreferenceDefaultsButton")
        self.restoreDefaultsButton.setAccessibleName("Restore preference defaults")
        self.restoreDefaultsButton.setToolTip(
            "Restore the preferences shown here to their defaults"
            )
        self.restoreDefaultsButton.clicked.connect(self.restore_defaults)
        layout.addWidget(self.buttonBox)

    def _appearance_tab(self):
        tab = qtw.QWidget(self)
        form = qtw.QFormLayout(tab)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(8)

        self.themeCombo = qtw.QComboBox(tab)
        self.themeCombo.setObjectName("themePreferenceCombo")
        self.themeCombo.setAccessibleName("Theme")
        for label, value in THEME_OPTIONS:
            self.themeCombo.addItem(label, value)
        self._add_row(form, "&Theme:", self.themeCombo)

        self.previewSizeSpin = qtw.QSpinBox(tab)
        self.previewSizeSpin.setObjectName("previewSizePreferenceSpin")
        self.previewSizeSpin.setAccessibleName("Preview size")
        self.previewSizeSpin.setRange(50, 1000)
        self.previewSizeSpin.setSingleStep(50)
        self.previewSizeSpin.setSuffix(" px")
        self._add_row(form, "&Preview size:", self.previewSizeSpin)

        self.refreshRateSpin = qtw.QDoubleSpinBox(tab)
        self.refreshRateSpin.setObjectName("refreshRatePreferenceSpin")
        self.refreshRateSpin.setAccessibleName("Default refresh interval")
        self.refreshRateSpin.setRange(0.0, 86400.0)
        self.refreshRateSpin.setSingleStep(0.1)
        self.refreshRateSpin.setDecimals(1)
        self.refreshRateSpin.setSuffix(" s")
        self._add_row(form, "&Default refresh interval:", self.refreshRateSpin)

        return tab

    def _interaction_tab(self):
        tab = qtw.QWidget(self)
        form = qtw.QFormLayout(tab)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(8)

        self.mouseModeCombo = qtw.QComboBox(tab)
        self.mouseModeCombo.setObjectName("mouseModePreferenceCombo")
        self.mouseModeCombo.setAccessibleName("Mouse mode")
        for label, value in MOUSE_MODE_OPTIONS:
            self.mouseModeCombo.addItem(label, value)
        self._add_row(form, "&Mouse mode:", self.mouseModeCombo)

        return tab

    def _files_tab(self):
        tab = qtw.QWidget(self)
        form = qtw.QFormLayout(tab)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(8)

        path_widget = qtw.QWidget(tab)
        path_layout = qtw.QHBoxLayout(path_widget)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(6)

        self.defaultLoadPathEdit = qtw.QLineEdit(tab)
        self.defaultLoadPathEdit.setObjectName("defaultLoadPathPreferenceEdit")
        self.defaultLoadPathEdit.setAccessibleName("Default load location")
        self.defaultLoadPathEdit.setPlaceholderText("No default folder")
        path_layout.addWidget(self.defaultLoadPathEdit, 1)

        self.defaultLoadPathButton = qtw.QToolButton(tab)
        self.defaultLoadPathButton.setObjectName("defaultLoadPathPreferenceButton")
        self.defaultLoadPathButton.setIcon(
            self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_DirOpenIcon)
            )
        self.defaultLoadPathButton.setToolTip("Choose default load location")
        self.defaultLoadPathButton.setAccessibleName(
            "Choose default load location"
            )
        self.defaultLoadPathButton.clicked.connect(self.choose_default_load_path)
        path_layout.addWidget(self.defaultLoadPathButton)

        self._add_row(
            form,
            "&Default load location:",
            path_widget,
            self.defaultLoadPathEdit,
            )

        return tab

    def _confirmations_tab(self):
        tab = qtw.QWidget(self)
        layout = qtw.QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.confirmCloseAllCheck = qtw.QCheckBox(
            "Confirm before closing all plot windows",
            tab,
            )
        self.confirmCloseAllCheck.setObjectName("confirmCloseAllPreferenceCheck")
        self.confirmCloseAllCheck.setAccessibleName(
            "Confirm before closing all plot windows"
            )
        layout.addWidget(self.confirmCloseAllCheck)

        self.confirmQuitCheck = qtw.QCheckBox("Confirm before quit", tab)
        self.confirmQuitCheck.setObjectName("confirmQuitPreferenceCheck")
        self.confirmQuitCheck.setAccessibleName("Confirm before quit")
        layout.addWidget(self.confirmQuitCheck)

        layout.addStretch(1)
        return tab

    def _runtime_tab(self):
        tab = qtw.QWidget(self)
        form = qtw.QFormLayout(tab)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(8)

        self.maxThreadsSpin = qtw.QSpinBox(tab)
        self.maxThreadsSpin.setObjectName("maxThreadsPreferenceSpin")
        self.maxThreadsSpin.setAccessibleName("Maximum worker threads")
        self.maxThreadsSpin.setRange(1, 999)
        self._add_row(form, "&Maximum worker threads:", self.maxThreadsSpin)

        self.delGracePeriodSpin = qtw.QDoubleSpinBox(tab)
        self.delGracePeriodSpin.setObjectName("deleteGracePreferenceSpin")
        self.delGracePeriodSpin.setAccessibleName("Dataset release grace period")
        self.delGracePeriodSpin.setRange(0.0, 300.0)
        self.delGracePeriodSpin.setSingleStep(1.0)
        self.delGracePeriodSpin.setDecimals(1)
        self.delGracePeriodSpin.setSuffix(" s")
        self._add_row(
            form,
            "Dataset release &grace period:",
            self.delGracePeriodSpin,
            )

        self.cloudSyncTimeoutSpin = qtw.QDoubleSpinBox(tab)
        self.cloudSyncTimeoutSpin.setObjectName("cloudSyncPreferenceSpin")
        self.cloudSyncTimeoutSpin.setAccessibleName("Cloud sync timeout")
        self.cloudSyncTimeoutSpin.setRange(1.0, 3600.0)
        self.cloudSyncTimeoutSpin.setSingleStep(10.0)
        self.cloudSyncTimeoutSpin.setDecimals(1)
        self.cloudSyncTimeoutSpin.setSuffix(" s")
        self._add_row(form, "&Cloud sync timeout:", self.cloudSyncTimeoutSpin)

        return tab

    def _add_row(self, form, label_text, widget, buddy=None):
        label = qtw.QLabel(label_text, self)
        label.setBuddy(buddy or widget)
        form.addRow(label, widget)

    def load_from_config(self):
        """
        Loads current config values into the dialog widgets.

        """
        self.set_preference_values({
            key: self.config.get(key)
            for key in PREFERENCE_KEYS
            })

    def set_preference_values(self, values):
        """
        Loads preference values into the dialog widgets.

        """
        theme_index = self.themeCombo.findData(values["user_preference.theme"])
        self.themeCombo.setCurrentIndex(max(theme_index, 0))

        self.previewSizeSpin.setValue(int(values["GUI.preview_size"]))
        mouse_mode_index = self.mouseModeCombo.findData(values[MOUSE_MODE_KEY])
        self.mouseModeCombo.setCurrentIndex(max(mouse_mode_index, 0))
        self.defaultLoadPathEdit.setText(str(values["file.default_load_path"]))
        self.refreshRateSpin.setValue(
            float(values["user_preference.default_refresh_rate"])
            )
        self.confirmCloseAllCheck.setChecked(
            bool(values[CONFIRM_CLOSE_ALL_KEY])
            )
        self.confirmQuitCheck.setChecked(
            bool(values[CONFIRM_QUIT_KEY])
            )
        self.maxThreadsSpin.setValue(
            int(values["runtime_settings.max_threads"])
            )
        self.delGracePeriodSpin.setValue(
            float(values["runtime_settings.del_grace_period"])
            )
        self.cloudSyncTimeoutSpin.setValue(
            float(values["runtime_settings.cloud_sync_timeout"])
            )

    def preference_values(self):
        """
        Returns the current widget values keyed by config path.

        """
        values = {
            "user_preference.theme": self.themeCombo.currentData(),
            "GUI.preview_size": int(self.previewSizeSpin.value()),
            MOUSE_MODE_KEY: self.mouseModeCombo.currentData(),
            "file.default_load_path": self.defaultLoadPathEdit.text().strip(),
            "user_preference.default_refresh_rate": self.refreshRateSpin.value(),
            CONFIRM_CLOSE_ALL_KEY: self.confirmCloseAllCheck.isChecked(),
            CONFIRM_QUIT_KEY: self.confirmQuitCheck.isChecked(),
            "runtime_settings.max_threads": int(self.maxThreadsSpin.value()),
            "runtime_settings.del_grace_period": self.delGracePeriodSpin.value(),
            "runtime_settings.cloud_sync_timeout": self.cloudSyncTimeoutSpin.value(),
            }
        return {
            key: values[key]
            for key in PREFERENCE_KEYS
            }

    def default_preference_values(self):
        """
        Returns schema defaults for the preferences shown in this dialog.

        """
        return {
            key: self._schema_default(key)
            for key in PREFERENCE_KEYS
            }

    def _schema_default(self, key):
        section, name = key.split(".")
        schema = self.config.schema["properties"][section]["properties"][name]
        return deepcopy(schema["default"])

    def apply_preferences(self):
        """
        Persists the dialog values and emits preferencesApplied on success.

        """
        try:
            for key, value in self.preference_values().items():
                if self.config.get(key) != value:
                    self.config.update(key, value)
        except Exception as err:
            qtw.QMessageBox.critical(
                self,
                "Preferences Not Saved",
                f"Could not save preferences:\n{err}",
                )
            return False

        self.preferencesApplied.emit()
        return True

    def restore_defaults(self):
        """
        Restores the preferences shown in this dialog to schema defaults.

        """
        reply = qtw.QMessageBox.question(
            self,
            "Restore Preference Defaults",
            "Restore the preferences shown in this dialog to their defaults?",
            qtw.QMessageBox.StandardButton.Yes | qtw.QMessageBox.StandardButton.No,
            qtw.QMessageBox.StandardButton.No,
            )
        if reply != qtw.QMessageBox.StandardButton.Yes:
            return False

        self.set_preference_values(self.default_preference_values())
        return self.apply_preferences()

    def choose_default_load_path(self):
        current_path = self.defaultLoadPathEdit.text().strip()
        if not os.path.isdir(current_path):
            current_path = ""

        folder = qtw.QFileDialog.getExistingDirectory(
            self,
            "Default Load Location",
            current_path,
            )
        if folder:
            self.defaultLoadPathEdit.setText(folder)

    def _accept_preferences(self):
        if self.apply_preferences():
            self.accept()


def create_preferences_action(window, triggered):
    """
    Creates the shared Preferences action used by main and plot windows.

    """
    action = QtGui.QAction("&Preferences...", window)
    action.setMenuRole(QtGui.QAction.MenuRole.PreferencesRole)
    action.setShortcut("Ctrl+,")
    action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
    action.setStatusTip("Open qPlot preferences")
    action.triggered.connect(triggered)
    return action
