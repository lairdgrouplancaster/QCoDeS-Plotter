"""Failed Windows file registration restores existing bindings and icon values."""

import sys
from copy import deepcopy

import pytest

from qplot import desktop
from tests.test_desktop_sidecars import FakeRegistry

BASE = r"Software\Classes"
APP = BASE + "\\" + desktop._PROGID
COMMAND = APP + r"\shell\open\command"
ICON = APP + r"\DefaultIcon"
OPEN_WITH = BASE + r"\.db\OpenWithProgids"


class FailingRegistry(FakeRegistry):
    def __init__(self):
        super().__init__()
        self.write_count = 0
        self.failure_at = None
        self.restore_failure = None

    def SetValueEx(self, key, name, reserved, kind, value):
        self.write_count += 1
        if self.write_count == self.failure_at:
            raise PermissionError("injected installation failure")
        if (key.path, name, value) == self.restore_failure:
            raise PermissionError("injected restoration failure")
        super().SetValueEx(key, name, reserved, kind, value)


@pytest.fixture
def registry(monkeypatch):
    registry = FailingRegistry()
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    return registry


def put(registry, path, name, value, kind=1):
    with registry.CreateKey(registry.HKEY_CURRENT_USER, path) as key:
        registry.SetValueEx(key, name, 0, kind, value)


@pytest.mark.parametrize("failed_write", range(1, 8))
def test_partial_registration_restores_exact_prior_state(tmp_path, registry, failed_write):
    put(registry, BASE + r"\.db", "", "Another.DatabaseViewer")
    put(registry, OPEN_WITH, "Another.DatabaseViewer", "")
    before = deepcopy(registry.keys)
    registry.write_count = 0
    registry.failure_at = failed_write

    with pytest.raises(PermissionError, match="installation failure"):
        desktop._windows_associations(tmp_path)

    assert registry.keys == before


def test_existing_native_value_types_and_unrelated_metadata_are_restored(tmp_path, registry):
    put(registry, COMMAND, "", desktop._windows_command(tmp_path), kind=2)
    put(registry, APP, "", b"original binary description", kind=3)
    put(registry, ICON, "", "%OriginalIcon%", kind=2)
    put(registry, APP + r"\Application", "ApplicationDescription", ["prior", "description"], kind=7)
    put(registry, APP + r"\Application", "Unrelated", "preserve")
    before = deepcopy(registry.keys)
    registry.write_count = 0
    registry.failure_at = 7

    with pytest.raises(PermissionError, match="installation failure"):
        desktop._windows_associations(tmp_path)

    assert registry.keys == before


def test_foreign_launcher_is_rejected_before_any_write(tmp_path, registry):
    put(registry, COMMAND, "", desktop._windows_command(tmp_path / "other"))
    put(registry, ICON, "", "other.ico,0")
    before = deepcopy(registry.keys)
    registry.write_count = 0

    with pytest.raises(ValueError, match="Another qPlot launcher"):
        desktop._windows_associations(tmp_path)

    assert registry.write_count == 0
    assert registry.keys == before


def test_backup_read_failure_prevents_all_mutation(tmp_path, registry, monkeypatch):
    original_open = registry.OpenKey

    def failing_open(root, path, *args):
        if path == ICON:
            raise PermissionError("cannot read prior icon")
        return original_open(root, path, *args)

    monkeypatch.setattr(registry, "OpenKey", failing_open)
    before = deepcopy(registry.keys)
    with pytest.raises(PermissionError, match="prior icon"):
        desktop._windows_associations(tmp_path)
    assert registry.write_count == 0
    assert registry.keys == before


def test_failed_restoration_retains_command_ownership_for_removal(tmp_path, registry):
    put(registry, ICON, "", "original.ico,0", kind=2)
    registry.write_count = 0
    registry.failure_at = 4
    registry.restore_failure = (ICON, "", "original.ico,0")

    with pytest.raises(desktop.WindowsAssociationRollbackError, match="keep the launcher"):
        desktop._windows_associations(tmp_path)

    assert registry.keys[COMMAND][""][0] == desktop._windows_command(tmp_path)
    assert registry.keys[ICON][""][0] == f'"{tmp_path / "qplot-database.ico"}",0'
    registry.restore_failure = None
    desktop._windows_associations(tmp_path, remove=True)
    assert not any(path == APP or path.startswith(APP + "\\") for path in registry.keys)
