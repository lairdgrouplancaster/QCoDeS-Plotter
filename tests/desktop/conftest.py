"""Explicit host capabilities and private native desktop-test state."""

import errno
import sys
import types
import uuid

import pytest


@pytest.fixture
def directory_symlinks(tmp_path):
    """Probe before exercising application code, without hiding its failures."""
    target = tmp_path / "symlink-capability-target"
    target.mkdir()
    link = tmp_path / "symlink-capability-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EPERM, errno.ENOTSUP) or getattr(error, "winerror", None) in (5, 50, 1314):
            pytest.skip(f"Directory symlinks unavailable to this test process: {error}")
        raise
    else:
        link.unlink()


@pytest.fixture
def native_registry(monkeypatch):
    """Use the real registry API, rooted entirely in one disposable HKCU key."""
    if sys.platform != "win32":
        pytest.skip("Native Windows registry required")
    import winreg

    path = r"Software\qPlot-tests-" + uuid.uuid4().hex
    try:
        root = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS)
    except PermissionError as error:
        pytest.skip(f"Private registry keys unavailable to this test process: {error}")

    registry = types.ModuleType("winreg")
    registry.__dict__.update(vars(winreg))
    registry.HKEY_CURRENT_USER = root
    monkeypatch.setitem(sys.modules, "winreg", registry)

    def delete_children(key):
        while winreg.QueryInfoKey(key)[0]:
            name = winreg.EnumKey(key, 0)
            with winreg.OpenKey(key, name, 0, winreg.KEY_ALL_ACCESS) as child:
                delete_children(child)
            winreg.DeleteKey(key, name)

    try:
        yield registry
    finally:
        try:
            delete_children(root)
        finally:
            root.Close()
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        with pytest.raises(FileNotFoundError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
