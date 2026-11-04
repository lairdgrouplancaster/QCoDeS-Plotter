"""Sidecar icons never install open actions or discard another app's settings."""

import json
import sys
import uuid
from copy import deepcopy

import pytest

from qplot import _desktop_sidecars as sidecars


class FakeKey:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.keys = {sidecars._CLASSES: {}}
        self.fail_write = None

    def OpenKey(self, root, path, *_args):
        assert root is self.HKEY_CURRENT_USER
        if path not in self.keys:
            raise FileNotFoundError(path)
        return FakeKey(path)

    def CreateKey(self, root, path):
        assert root is self.HKEY_CURRENT_USER
        parts = path.split("\\")
        for count in range(2, len(parts) + 1):
            self.keys.setdefault("\\".join(parts[:count]), {})
        return FakeKey(path)

    def QueryValueEx(self, key, name):
        try:
            return deepcopy(self.keys[key.path][name])
        except KeyError:
            raise FileNotFoundError(name) from None

    def SetValueEx(self, key, name, _reserved, kind, value):
        if key.path == self.fail_write:
            raise PermissionError("injected registry write failure")
        self.keys[key.path][name] = (deepcopy(value), kind)

    def DeleteValue(self, key, name):
        try:
            del self.keys[key.path][name]
        except KeyError:
            raise FileNotFoundError(name) from None

    def QueryInfoKey(self, key):
        prefix = key.path + "\\"
        children = sum(path.startswith(prefix) and "\\" not in path[len(prefix):]
                       for path in self.keys)
        return children, len(self.keys[key.path]), 0

    def DeleteKey(self, root, path):
        assert root is self.HKEY_CURRENT_USER
        if path not in self.keys:
            raise FileNotFoundError(path)
        if any(other.startswith(path + "\\") for other in self.keys):
            raise OSError("key has subkeys")
        del self.keys[path]


@pytest.fixture
def registry(monkeypatch):
    registry = FakeRegistry()
    monkeypatch.setattr(sidecars, "_registry", lambda: registry)
    return registry


def icon_key(extension):
    return sidecars._CLASSES + "\\" + extension + r"\DefaultIcon"


def test_new_registration_only_installs_icons_and_removes_empty_keys(tmp_path, registry):
    original = deepcopy(registry.keys)
    records = sidecars.install_sidecar_icons(tmp_path / "qPlot Ω space")
    assert set(registry.keys) - set(original) == {
        sidecars._CLASSES + "\\" + extension + suffix
        for extension in (".db-wal", ".db-shm")
        for suffix in ("", r"\DefaultIcon")
    }
    for record in records:
        assert registry.keys[icon_key(record["extension"])] == {"": (record["icon"], 1)}
        assert registry.keys[sidecars._CLASSES + "\\" + record["extension"]] == {}
    sidecars.remove_sidecar_icons(json.loads(json.dumps(records)))
    assert registry.keys == original
    sidecars.remove_sidecar_icons(records)
    assert registry.keys == original


def test_prepare_is_read_only_and_saved_records_can_be_installed(tmp_path, registry):
    original = deepcopy(registry.keys)
    records = sidecars.prepare_sidecar_icons(tmp_path)
    assert registry.keys == original
    saved = json.loads(json.dumps(records))
    assert sidecars.install_sidecar_icons(tmp_path, records=saved) == records
    sidecars.remove_sidecar_icons(saved)
    assert registry.keys == original


def test_prepared_records_cannot_target_another_launcher(tmp_path, registry):
    records = sidecars.prepare_sidecar_icons(tmp_path / "first")
    original = deepcopy(registry.keys)
    with pytest.raises(ValueError, match="destination"):
        sidecars.install_sidecar_icons(tmp_path / "other", records=records)
    assert registry.keys == original


def test_changes_after_preparation_are_preserved(tmp_path, registry):
    records = sidecars.prepare_sidecar_icons(tmp_path)
    with registry.CreateKey(registry.HKEY_CURRENT_USER, icon_key(".db-shm")) as key:
        registry.SetValueEx(key, "", 0, 1, "other-app.ico")
    original = deepcopy(registry.keys)
    with pytest.raises(ValueError, match="changed after preparing"):
        sidecars.install_sidecar_icons(tmp_path, records=records)
    assert registry.keys == original


def test_prepared_backup_survives_failed_install_and_failed_rollback(tmp_path, registry, monkeypatch):
    with registry.CreateKey(registry.HKEY_CURRENT_USER, icon_key(".db-wal")) as key:
        registry.SetValueEx(key, "", 0, 2, "%Old%\\icon.ico")
    original = deepcopy(registry.keys)
    records = sidecars.prepare_sidecar_icons(tmp_path)
    saved = json.loads(json.dumps(records))
    write = registry.SetValueEx

    def fail_install_and_restore(key, name, reserved, kind, value):
        if key.path == icon_key(".db-shm") or kind == 2:
            raise PermissionError("injected registry write failure")
        write(key, name, reserved, kind, value)

    monkeypatch.setattr(registry, "SetValueEx", fail_install_and_restore)
    with pytest.raises(PermissionError, match="injected"):
        sidecars.install_sidecar_icons(tmp_path, records=records)
    assert records == saved
    assert registry.keys[icon_key(".db-wal")][""][1] == 1
    monkeypatch.setattr(registry, "SetValueEx", write)
    sidecars.remove_sidecar_icons(saved)
    assert registry.keys == original


@pytest.mark.parametrize("previous", [
    (r"%SystemRoot%\old.ico,0", 2),
    (b"\x00\x80\xff", 3),
    (["unusual", "icon"], 7),
    (1234, 4),
])
def test_existing_icon_types_and_open_bindings_are_restored(tmp_path, registry, previous):
    for extension in (".db-wal", ".db-shm"):
        extension_path = sidecars._CLASSES + "\\" + extension
        with registry.CreateKey(registry.HKEY_CURRENT_USER, icon_key(extension)) as key:
            registry.SetValueEx(key, "", 0, previous[1], previous[0])
        registry.keys[extension_path][""] = ("Other.DatabaseTool", 1)
        registry.keys[extension_path]["FriendlyTypeName"] = ("Other label", 1)
    original = deepcopy(registry.keys)
    records = sidecars.install_sidecar_icons(tmp_path)
    sidecars.remove_sidecar_icons(json.loads(json.dumps(records)))
    assert registry.keys == original


def test_other_apps_later_values_and_subkeys_are_preserved(tmp_path, registry):
    records = sidecars.install_sidecar_icons(tmp_path)
    registry.keys[icon_key(".db-wal")][""] = ("another-app.ico", 1)
    registry.keys[icon_key(".db-shm")]["OtherMetadata"] = ("keep", 1)
    unrelated = sidecars._CLASSES + r"\.db-shm\shell\inspect\command"
    with registry.CreateKey(registry.HKEY_CURRENT_USER, unrelated) as key:
        registry.SetValueEx(key, "", 0, 1, "other-app.exe")
    sidecars.remove_sidecar_icons(records)
    assert registry.keys[icon_key(".db-wal")][""] == ("another-app.ico", 1)
    assert registry.keys[icon_key(".db-shm")] == {"OtherMetadata": ("keep", 1)}
    assert registry.keys[unrelated][""] == ("other-app.exe", 1)


def test_failed_second_registration_rolls_back_first_and_empty_keys(tmp_path, registry):
    with registry.CreateKey(registry.HKEY_CURRENT_USER, icon_key(".db-wal")) as key:
        registry.SetValueEx(key, "", 0, 2, "%Old%\\icon.ico")
    original = deepcopy(registry.keys)
    registry.fail_write = icon_key(".db-shm")
    with pytest.raises(PermissionError, match="injected"):
        sidecars.install_sidecar_icons(tmp_path)
    assert registry.keys == original


@pytest.mark.parametrize("damage", [
    lambda record: record.update(extension=r".db\shell\open\command"),
    lambda record: record.update(icon="foreign.ico"),
    lambda record: record.pop("previous"),
    lambda record: record.update(created_icon_key="true"),
    lambda record: record.update(previous={"type": 3, "data": "xyz", "binary": True}),
])
def test_invalid_later_record_prevents_all_removal_mutations(tmp_path, registry, damage):
    records = sidecars.install_sidecar_icons(tmp_path)
    damage(records[1])
    original = deepcopy(registry.keys)
    with pytest.raises(ValueError):
        sidecars.remove_sidecar_icons(records)
    assert registry.keys == original


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows registry")
def test_native_registry_roundtrip_uses_isolated_test_keys(tmp_path, monkeypatch):
    import winreg

    base = r"Software\qPlot-sidecar-test-" + uuid.uuid4().hex
    monkeypatch.setattr(sidecars, "_CLASSES", base)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base):
        pass
    records = []
    try:
        records = sidecars.install_sidecar_icons(tmp_path / "qPlot Ω space")
        for record in records:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, icon_key(record["extension"])) as key:
                assert winreg.QueryValueEx(key, "") == (record["icon"], winreg.REG_SZ)
        sidecars.remove_sidecar_icons(json.loads(json.dumps(records)))
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as key:
            assert winreg.QueryInfoKey(key)[:2] == (0, 0)
    finally:
        sidecars.remove_sidecar_icons(records)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base)
