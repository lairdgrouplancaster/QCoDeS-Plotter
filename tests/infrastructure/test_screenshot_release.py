"""Release screenshots bind reviewed bytes to inputs, without golden pixels."""

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from scripts import capture_demo_screenshots as capture
from scripts import docs_hooks
from scripts import screenshot_support as support


@pytest.fixture
def candidate(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / "src/qplot").mkdir(parents=True)
    (tmp_path / "src/qplot/main.py").write_text("candidate source\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.7.0"\n')
    assets = tmp_path / "docs/assets"
    assets.mkdir(parents=True)
    (tmp_path / "docs/demo-data.md").write_text(
        "\n".join(f"![{name}](assets/{name})" for name in support.SIZES)
    )
    for name, dimensions in support.SIZES.items():
        image = Image.new("RGB", dimensions, "white")
        draw = ImageDraw.Draw(image)
        for y in range(0, dimensions[1], 20):
            for x in range(0, dimensions[0], 20):
                draw.rectangle((x, y, x + 9, y + 9), fill=(x % 256, y % 256, 128))
        image.save(assets / name)
    record = {
        "schema": 1, "source_commit": "a" * 40, "application_version": "1.7.0",
        "generation_id": "123/1", "environment": {"qt": "test"},
        "inputs": support.fingerprint(tmp_path), "images": support.validate_images(assets),
        "checks": dict.fromkeys(("selected_data", "rendered", "shutdown", "database_unchanged"), True),
    }
    (assets / support.RECORD).write_text(json.dumps(record))
    return tmp_path, assets


def test_commit_identity_is_informational_but_cycle_and_source_are_required(candidate):
    root, assets = candidate
    # No commit matching the record exists in this checkout. Content is enough.
    support.verify_release(root, generation_id="123/1")
    with pytest.raises(ValueError, match="preparation cycle"):
        support.verify_release(root, generation_id="456/1")
    with pytest.raises(ValueError, match="clean"):
        support.verify_release(root, require_clean=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Screenshot test", "-c", "user.email=test@example.invalid",
                    "commit", "--quiet", "-m", "Synthetic final candidate"], cwd=root, check=True)
    # Committing generated files changes HEAD and makes the candidate clean;
    # it must not invalidate identical screenshot-relevant inputs.
    support.verify_release(root, generation_id="123/1", require_clean=True)
    (root / "src/qplot/main.py").write_text("changed candidate\n")
    with pytest.raises(ValueError, match="inputs changed"):
        support.verify_release(root)


def test_fingerprint_excludes_generated_files_and_prose_but_detects_inventory_changes(candidate):
    root, assets = candidate
    before = support.fingerprint(root)
    (root / "src/qplot/main.py").write_bytes(b"candidate source\r\n")
    assert support.fingerprint(root) == before
    (assets / support.RECORD).write_text("changed record")
    (assets / "qplot-main-window.png").write_bytes(b"changed image")
    (root / "docs/guide.md").write_text("new prose")
    assert support.fingerprint(root) == before
    new_source = root / "src/qplot/new.py"
    new_source.write_text("new module")
    assert support.fingerprint(root) != before
    new_source.unlink()
    (root / "src/qplot/main.py").unlink()
    assert support.fingerprint(root) != before


@pytest.mark.parametrize("fault", ["blank", "partial", "truncated", "jpeg", "size", "missing"])
def test_image_checks_reject_invalid_or_incomplete_output(candidate, fault):
    root, assets = candidate
    path = assets / "qplot-main-window.png"
    if fault == "blank":
        Image.new("RGB", support.SIZES[path.name], "white").save(path)
    elif fault == "partial":
        with Image.open(path) as image:
            image.paste("white", (560, 380, 1120, 760))
            image.save(path)
    elif fault == "truncated":
        path.write_bytes(path.read_bytes()[:200])
    elif fault == "jpeg":
        Image.new("RGB", support.SIZES[path.name], "white").save(path, format="JPEG")
    elif fault == "size":
        Image.new("RGB", (80, 60), "white").save(path)
    else:
        path.unlink()
    with pytest.raises((OSError, ValueError)):
        support.validate_images(assets, exact_dimensions=True)


def test_valid_changed_png_is_detected_as_unreviewed(candidate):
    root, assets = candidate
    path = assets / "qplot-line-plot.png"
    with Image.open(path) as image:
        image.putpixel((3, 3), (255, 0, 0))
        image.save(path)
    support.validate_images(assets)
    with pytest.raises(ValueError, match="reviewed set"):
        support.verify_release(root)


def test_missing_reference_and_failed_shutdown_reject_release(candidate):
    root, assets = candidate
    record_path = assets / support.RECORD
    record = json.loads(record_path.read_text())
    record["checks"]["shutdown"] = False
    record_path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="application checks"):
        support.verify_release(root)
    (root / "docs/demo-data.md").write_text("No screenshots linked")
    with pytest.raises(ValueError, match="does not reference"):
        support.validate_references(root)


def test_install_failure_restores_entire_previous_set(candidate, tmp_path, monkeypatch):
    root, staged = candidate
    approved = root / "approved"
    approved.mkdir()
    names = [*support.SIZES, support.RECORD]
    for name in names:
        (approved / name).write_bytes(f"previous {name}".encode())
    before = {name: (approved / name).read_bytes() for name in names}
    replace = support.os.replace

    def fail_third(source, destination):
        if Path(destination).name == names[2]:
            raise OSError("test replacement failure")
        return replace(source, destination)

    monkeypatch.setattr(support.os, "replace", fail_third)
    with pytest.raises(OSError, match="replacement failure"):
        support.install_set(staged, approved)
    assert {name: (approved / name).read_bytes() for name in names} == before
    assert sorted(path.name for path in approved.iterdir()) == sorted(names)


@pytest.mark.parametrize("name", [*support.SIZES, support.RECORD])
def test_install_preflights_every_destination_for_database_safety(candidate, name):
    root, staged = candidate
    approved = root / "approved"
    approved.mkdir()
    protected = approved / name
    protected.write_bytes(b"SQLite format 3\x00" + b"protected bytes")
    state = protected.stat()
    with pytest.raises(OSError, match="SQLite"):
        support.install_set(staged, approved)
    assert protected.stat() == state
    assert list(approved.iterdir()) == [protected]


def test_documentation_links_use_checked_out_revision(candidate, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("READTHEDOCS_GIT_IDENTIFIER", "native-and-app/v1.7.0")
    monkeypatch.delenv("READTHEDOCS_GIT_COMMIT_HASH", raising=False)
    converted = docs_hooks.on_page_markdown(
        "[Contributing](../CONTRIBUTING.md#native) [Demo](demo-data.md)",
        page=SimpleNamespace(file=SimpleNamespace(src_uri="distribution.md")),
        config={"repo_url": "https://example.org/repo"}, files=None,
    )
    assert "blob/native-and-app%2Fv1.7.0/CONTRIBUTING.md#native" in converted
    assert "[Demo](demo-data.md)" in converted


def test_website_requires_unaltered_approved_files(candidate):
    import shutil

    root, assets = candidate
    built = root / "site/assets"
    shutil.copytree(assets, built)
    config = {"docs_dir": str(root / "docs"), "site_dir": str(root / "site")}
    docs_hooks.on_post_build(config=config)
    (built / "qplot-heatmap.png").write_bytes(b"changed website image")
    with pytest.raises(ValueError, match="omitted or altered"):
        docs_hooks.on_post_build(config=config)


def test_staging_cleanup_failure_restores_approved_set(candidate, monkeypatch):
    from contextlib import contextmanager

    root, approved = candidate
    before = {path.name: path.read_bytes() for path in approved.iterdir()}
    temporary_directory = support.tempfile.TemporaryDirectory

    @contextmanager
    def failed_cleanup(*args, **kwargs):
        with temporary_directory(*args, **kwargs) as directory:
            yield directory
        raise OSError("staging cleanup failure")

    monkeypatch.setattr(support.tempfile, "TemporaryDirectory", failed_cleanup)
    with pytest.raises(OSError, match="cleanup failure"):
        support.install_set(dict.fromkeys(before, b"new files"), approved)
    assert {path.name: path.read_bytes() for path in approved.iterdir()} == before


@pytest.mark.parametrize("fault", ["bad-png", "failed-shutdown", "cleanup-error"])
def test_parent_preserves_approved_set_after_generation_or_cleanup_failure(candidate, monkeypatch, fault):
    import shutil
    import sys

    root, approved = candidate
    before = {path.name: path.read_bytes() for path in approved.iterdir()}
    monkeypatch.setitem(sys.modules, "screenshot_support", support)
    monkeypatch.setattr(capture, "WORK_DIR", root / "private")

    def worker(staged, work, timeout, **kwargs):
        for name in support.SIZES:
            shutil.copyfile(approved / name, staged / name)
        checks = dict.fromkeys(("selected_data", "rendered", "shutdown", "database_unchanged"), True)
        if fault == "bad-png":
            (staged / "qplot-line-plot.png").write_bytes(b"incomplete")
        elif fault == "failed-shutdown":
            checks["shutdown"] = False
        else:
            raise RuntimeError("cleanup failed")
        (staged / "capture-result.json").write_text(json.dumps({"checks": checks, "environment": {}}))

    monkeypatch.setattr(capture, "run_worker", worker)
    with pytest.raises((OSError, RuntimeError)):
        capture.main(["--smoke-test", "--output", str(approved)])
    assert {path.name: path.read_bytes() for path in approved.iterdir()} == before
    assert list((root / "private").iterdir()) == []


def test_smoke_mode_cannot_replace_documentation_assets(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "screenshot_support", support)
    with pytest.raises(ValueError, match="separate output"):
        capture.main(["--smoke-test", "--output", str(capture.DEFAULT_ASSET_DIR)])


def test_source_archive_smoke_record_is_never_release_evidence(candidate):
    root, assets = candidate
    record = json.loads((assets / support.RECORD).read_text())
    record["schema"] = 0
    (assets / support.RECORD).write_text(json.dumps(record))
    with pytest.raises(ValueError, match="schema"):
        support.verify_release(root)
