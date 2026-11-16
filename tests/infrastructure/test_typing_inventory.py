"""New and renamed application modules must not silently escape mypy."""

import json
from pathlib import Path

import pytest

from scripts.check_typing_inventory import inventory_errors, main

pytestmark = pytest.mark.pure


def write_project(root, *, checked=(), deferred=None, modules=()):
    for name in modules:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    text = f"[tool.mypy]\nfiles = {json.dumps(checked)}\n[tool.qplot.typing.deferred]\n"
    for path, reason in (deferred or {}).items():
        text += f"{json.dumps(path)} = {json.dumps(reason)}\n"
    (root / "pyproject.toml").write_text(text, encoding="utf-8")


def test_repository_accounts_for_every_application_module():
    assert inventory_errors(Path(__file__).resolve().parents[2]) == []


def test_exact_checked_and_reasoned_deferred_modules_pass(tmp_path):
    write_project(
        tmp_path,
        checked=["src/qplot/__init__.py"],
        deferred={"src/qplot/window.py": "Needs a typed widget host."},
        modules=["src/qplot/__init__.py", "src/qplot/window.py"],
    )
    assert main(["--root", str(tmp_path)]) == 0


def test_new_module_fails_with_actionable_path(tmp_path, capsys):
    write_project(
        tmp_path,
        checked=["src/qplot/__init__.py"],
        modules=["src/qplot/__init__.py", "src/qplot/new/worker.py"],
    )
    assert main(["--root", str(tmp_path)]) == 1
    assert (
        "Module needs a mypy target or deferral reason: src/qplot/new/worker.py"
        in capsys.readouterr().err
    )


def test_renaming_reports_both_stale_deferral_and_untracked_module(tmp_path):
    write_project(
        tmp_path,
        deferred={"src/qplot/old.py": "Needs typed callbacks."},
        modules=["src/qplot/new.py"],
    )
    assert inventory_errors(tmp_path) == [
        "Stale or non-canonical inventory path: src/qplot/old.py",
        "Module needs a mypy target or deferral reason: src/qplot/new.py",
    ]


@pytest.mark.parametrize(
    "checked,deferred,expected",
    [
        (["src/qplot/a.py"] * 2, {}, "Duplicate mypy target"),
        (
            ["src/qplot/a.py"],
            {"src/qplot/a.py": "Was deferred."},
            "Checked module still deferred",
        ),
        ([], {"src/qplot/a.py": "  "}, "Missing deferral reason"),
        ([], {"src/qplot/a.py": 42}, "Missing deferral reason"),
        (["src/qplot/*.py"], {}, "Stale or non-canonical inventory path"),
        (["src/qplot/deleted.py"], {}, "Stale or non-canonical inventory path"),
        (["src\\qplot\\a.py"], {}, "Stale or non-canonical inventory path"),
    ],
)
def test_invalid_inventory_entries_fail(tmp_path, checked, deferred, expected):
    write_project(
        tmp_path, checked=checked, deferred=deferred, modules=["src/qplot/a.py"]
    )
    assert any(error.startswith(expected) for error in inventory_errors(tmp_path))


@pytest.mark.parametrize("config", ["[invalid", "[tool.mypy]\nfiles = 42", ""])
def test_malformed_configuration_returns_failure(tmp_path, capsys, config):
    (tmp_path / "pyproject.toml").write_text(config, encoding="utf-8")
    assert main(["--root", str(tmp_path)]) == 1
    assert "Invalid typing inventory" in capsys.readouterr().err


def test_missing_source_tree_cannot_pass_vacuously(tmp_path):
    write_project(tmp_path)
    assert inventory_errors(tmp_path) == ["No application modules found in src/qplot"]
