"""About information follows the running source or installed artifact."""

import json
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from qplot import _metadata


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname="qplotter"\nversion="2.0.0"\n')
    git(root, "init")
    git(root, "add", "pyproject.toml")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "Initial")
    return root


def test_checkout_distinguishes_release_development_and_local_changes(checkout):
    info = _metadata.checkout_info(checkout)
    assert info["version"] == "2.0.0"
    assert info["commit"] == git(checkout, "rev-parse", "HEAD")
    assert info["released"] is False
    git(checkout, "tag", "v1.0.0")
    assert _metadata.checkout_info(checkout)["released"] is False
    git(checkout, "tag", "native-and-app/v2.0.0")
    assert _metadata.checkout_info(checkout)["released"] is True
    (checkout / "untracked.py").write_text("# Local work\n")
    info = _metadata.checkout_info(checkout)
    assert info["released"] is False and info["dirty"] is True


def test_checkout_without_git_executable_still_reports_source_version(checkout, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(_metadata.subprocess, "run", missing)
    assert _metadata.checkout_info(checkout) == {"version": "2.0.0", "checkout": True}


def installed_package(tmp_path, monkeypatch):
    package = tmp_path / "installed/qplot"
    package.mkdir(parents=True)
    monkeypatch.setattr(_metadata, "__file__", str(package / "_metadata.py"))
    monkeypatch.setattr(_metadata, "package_version", lambda: "2.0.0")
    monkeypatch.setattr(_metadata, "distribution", lambda name: SimpleNamespace(read_text=lambda name: None))
    return package


def test_installed_build_uses_packaged_provenance_and_ignores_enclosing_git(tmp_path, monkeypatch):
    package = installed_package(tmp_path, monkeypatch)
    git(tmp_path, "init")
    build = dict(version="2.0.0", commit="a" * 40, released=False, pull_request="123")
    (package / "_build_info.json").write_text(json.dumps(build))
    assert _metadata.version_info() == build


@pytest.mark.parametrize("content", ['broken json', '[]', '{"version": "1.0.0"}'])
def test_missing_or_invalid_build_info_falls_back_to_pip_vcs_commit(tmp_path, monkeypatch, content):
    package = installed_package(tmp_path, monkeypatch)
    (package / "_build_info.json").write_text(content)
    direct = json.dumps({"vcs_info": {"vcs": "git", "commit_id": "b" * 40}})
    monkeypatch.setattr(_metadata, "distribution", lambda name: SimpleNamespace(read_text=lambda name: direct))
    assert _metadata.version_info() == {"version": "2.0.0", "commit": "b" * 40}


def test_checkout_version_overrides_stale_editable_metadata(checkout, monkeypatch):
    monkeypatch.setattr(_metadata, "__file__", str(checkout / "src/qplot/_metadata.py"))
    monkeypatch.setattr(_metadata, "package_version", lambda: "1.0.0")
    assert _metadata.version_info()["version"] == "2.0.0"


def test_ci_stamp_records_pr_and_release_and_rejects_dirty_build(checkout, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/write_build_info.py"
    main = runpy.run_path(str(script))["main"]
    monkeypatch.setitem(main.__globals__, "ROOT", checkout)
    (checkout / "src/qplot").mkdir(parents=True)
    monkeypatch.setenv("GITHUB_REF", "refs/pull/123/merge")
    main()
    output = checkout / "src/qplot/_build_info.json"
    info = json.loads(output.read_text())
    assert info["pull_request"] == "123"
    assert info["commit"] == git(checkout, "rev-parse", "HEAD")
    assert info["released"] is False
    with pytest.raises(RuntimeError, match="clean Git checkout"):
        main()
    output.unlink()
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v2.0.0")
    main()
    assert json.loads(output.read_text())["released"] is True
