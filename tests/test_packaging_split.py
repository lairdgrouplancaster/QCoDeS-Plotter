"""Distribution boundaries and fail-closed native compatibility checks."""

from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
from types import SimpleNamespace

import pytest

from qplot.datahandling import trusted_live


def test_application_pins_native_and_apsw_together():
    requirements = distribution("qplotter").requires
    assert f"qplotter-native=={trusted_live.TRUSTED_READER_NATIVE_VERSION}" in requirements
    apsw_pin = f"apsw=={trusted_live.TRUSTED_READER_APSW_VERSION}"
    assert apsw_pin in requirements
    assert apsw_pin in distribution("qplotter-native").requires
    assert version("qplotter-native") == trusted_live.TRUSTED_READER_NATIVE_VERSION


def test_native_extension_is_located_in_separate_import_package():
    path = trusted_live._native_extension_path()
    assert path.is_file()
    assert path.parent.name == "qplot_native"
    assert path.name in {"_trusted_vfs_native.abi3.so", "_trusted_vfs_native.pyd"}


@pytest.mark.parametrize("installed_version", [None, "0.9.0", "1.0.1"])
def test_native_missing_or_wrong_version_fails_before_import(monkeypatch, installed_version):
    def metadata_version(name):
        assert name == "qplotter-native"
        if installed_version is None:
            raise PackageNotFoundError(name)
        return installed_version

    def forbidden_import(name):
        pytest.fail(f"incompatible native package was imported: {name}")

    monkeypatch.setattr(trusted_live, "version", metadata_version)
    monkeypatch.setattr(trusted_live.importlib, "import_module", forbidden_import)
    with pytest.raises(trusted_live.TrustedLiveReaderUnavailableError, match="qplotter-native=="):
        trusted_live._native_extension_path()


@pytest.mark.parametrize("field,value", [("sqlite_version", "0.0.0"), ("vfs_name", "wrong")])
def test_native_wrong_sqlite_or_vfs_fails_before_loading(monkeypatch, field, value):
    attributes = {
        "sqlite_version": trusted_live.TRUSTED_READER_SQLITE_VERSION,
        "vfs_name": trusted_live.TRUSTED_READER_VFS_NAME,
        "__file__": str(Path("unused.abi3.so")),
    }
    attributes[field] = value
    monkeypatch.setattr(trusted_live, "version", lambda name: trusted_live.TRUSTED_READER_NATIVE_VERSION)
    monkeypatch.setattr(trusted_live.importlib, "import_module", lambda name: SimpleNamespace(**attributes))
    with pytest.raises(trusted_live.TrustedLiveReaderUnavailableError, match="incompatible"):
        trusted_live._native_extension_path()
