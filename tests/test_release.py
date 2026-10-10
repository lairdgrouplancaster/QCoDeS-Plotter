"""Release gates reject unvalidated bytes, unexpected platforms and PyPI files."""

import hashlib
import json
import shutil
import tarfile
import urllib.error
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import release

REPOSITORY = Path(__file__).resolve().parents[1]
IDENTITY = dict(revision="a" * 40, ref="refs/tags/native-and-app/v1.6.0b2", run_id="42")
NATIVE_TAGS = ["manylinux_2_28_x86_64", "macosx_11_0_arm64",
               "macosx_10_13_x86_64", "win_amd64"]


@pytest.fixture
def artifact_set(tmp_path, monkeypatch):
    """Small real archives exercise staging without compiling for other OSes."""
    repository = tmp_path / "source"
    for native in (False, True):
        name = "native/pyproject.toml" if native else "pyproject.toml"
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        package = release.PACKAGES["native" if native else "application"]
        version = release.validator.PINNED_NATIVE_VERSION if native else "1.6.0b2"
        dependencies = [f"apsw=={release.validator.PINNED_APSW_VERSION}"]
        if not native:
            dependencies.append(f"qplotter-native=={release.validator.PINNED_NATIVE_VERSION}")
        path.write_text(f'[project]\nname = "{package}"\nversion = "{version}"\n'
                        f'requires-python = ">=3.11"\ndependencies = {json.dumps(dependencies)}\n')
    source = {"src/qplot/__init__.py", "native/src/qplot_native/__init__.py"}
    monkeypatch.setattr(release.validator, "source_files", lambda _: source)
    monkeypatch.setattr(release.validator, "validate_sdist", Mock())
    metadata = release.projects(repository)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for native, tags in ((False, ["any"]), (True, NATIVE_TAGS)):
        project = metadata["native" if native else "application"]
        name = project["name"].replace("-", "_")
        version = project["version"]
        info = (f"Name: {project['name']}\nVersion: {version}\nRequires-Python: >=3.11\n"
                + "".join(f"Requires-Dist: {dep}\n" for dep in project["dependencies"]))
        for tag in tags:
            abi = "cp311-abi3" if native else "py3-none"
            with zipfile.ZipFile(artifacts / f"{name}-{version}-{abi}-{tag}.whl", "w") as archive:
                package = "qplot_native" if native else "qplot"
                archive.writestr(f"{package}/__init__.py", "")
                if native:
                    extension = ".pyd" if tag == "win_amd64" else ".abi3.so"
                    archive.writestr(f"qplot_native/_trusted_vfs_native{extension}", b"fake-test-binary")
                dist = f"{name}-{version}.dist-info"
                archive.writestr(f"{dist}/METADATA", info)
                archive.writestr(f"{dist}/WHEEL", f"Root-Is-Purelib: {str(not native).lower()}\nTag: {abi}-{tag}\n")
        with tarfile.open(artifacts / f"{name}-{version}.tar.gz", "w:gz") as archive:
            item = tarfile.TarInfo(f"{name}-{version}/PKG-INFO")
            data = info.encode()
            item.size = len(data)
            archive.addfile(item, BytesIO(data))
    return repository, artifacts, tmp_path / "release"


def test_stage_has_exact_upload_sets_and_binds_each_to_validated_run(artifact_set):
    repository, artifacts, output = artifact_set
    release.stage(repository, artifacts, output, **IDENTITY)
    native = release.verify_bundle(output / "native", **IDENTITY)
    application = release.verify_bundle(output / "application", **IDENTITY)
    assert len(native["files"]) == 4
    assert all(name.endswith(".whl") for name in native["files"])
    assert len(application["files"]) == 2
    assert any(name.endswith(".tar.gz") for name in application["files"])
    with pytest.raises(AssertionError, match="another workflow run"):
        release.verify_bundle(output / "native", **{**IDENTITY, "run_id": "41"})
    path = next((output / "native/dist").iterdir())
    path.write_bytes(b"substituted after validation")
    with pytest.raises(AssertionError, match="unvalidated artifact bytes"):
        release.verify_bundle(output / "native", **IDENTITY)


@pytest.mark.parametrize('platform', release.PLATFORMS)
def test_public_comparison_selects_only_matching_validated_wheels(artifact_set, platform):
    repository, artifacts, bundle = artifact_set
    release.stage(repository, artifacts, bundle, **IDENTITY)
    selected = bundle.parent / 'comparison'
    release.select_platform(bundle, platform, selected, **IDENTITY)
    files = list(selected.iterdir())
    assert len(files) == 2 and all(path.suffix == '.whl' for path in files)
    native = next(path for path in files if path.name.startswith('qplotter_native'))
    assert release.native_platform(native.name, '1.0.0') == platform
    assert native.read_bytes() == (artifacts / native.name).read_bytes()
    with pytest.raises(AssertionError, match='another workflow run'):
        release.select_platform(bundle, platform, bundle.parent / 'wrong', **{**IDENTITY, 'run_id': '0'})


@pytest.mark.parametrize("change", ["missing", "unrepaired", "extra", "wrong-version", "sdist-upload"])
def test_publication_rejects_incomplete_or_unexpected_artifacts(artifact_set, change):
    repository, artifacts, output = artifact_set
    native = next(artifacts.glob("*manylinux*.whl"))
    if change == "missing":
        native.unlink()
    elif change == "unrepaired":
        native.rename(artifacts / native.name.replace("manylinux_2_28_x86_64", "linux_x86_64"))
    elif change == "extra":
        (artifacts / "random.txt").write_text("unvalidated input")
    elif change == "wrong-version":
        native.rename(artifacts / native.name.replace("1.0.0", "0.9.0"))
    else:
        release.stage(repository, artifacts, output, **IDENTITY)
        shutil.copyfile(next(artifacts.glob("qplotter_native*.tar.gz")), output / "native/dist/unwanted.tar.gz")
        with pytest.raises(AssertionError):
            release.verify_bundle(output / "native", **IDENTITY)
        return
    with pytest.raises(AssertionError):
        release.stage(repository, artifacts, output, **IDENTITY)


def test_preflight_requires_exact_tag_version_and_coordinated_pins(artifact_set, monkeypatch):
    repository, _, _ = artifact_set
    monkeypatch.setenv("GITHUB_REPOSITORY", release.REPOSITORY)
    version = release.projects(repository)["application"]["version"]
    assert release.preflight(repository, f"refs/tags/native-and-app/v{version}")
    assert not release.preflight(repository, f"refs/tags/v{version}")
    for ref in ("refs/heads/main", "refs/tags/v0.1", f"refs/tags/v{version}-extra"):
        with pytest.raises(AssertionError, match="tag/version mismatch"):
            release.preflight(repository, ref)
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/a-fork")
    with pytest.raises(AssertionError):
        release.preflight(repository, f"refs/tags/v{version}")


def remote_file(filename, data=b"validated"):
    return dict(filename=filename, packagetype="bdist_wheel", yanked=False,
                url=f"https://files.pythonhosted.org/{filename}", size=len(data),
                digests={"sha256": hashlib.sha256(data).hexdigest()})


def test_published_native_reuse_rejects_source_fallback_and_missing_platform(monkeypatch):
    files = [remote_file(f"qplotter_native-1.0.0-cp311-abi3-{tag}.whl") for tag in NATIVE_TAGS]
    monkeypatch.setattr(release, "release_files", lambda *_: files)
    assert release.published_native_files("1.0.0") == files
    files.append(dict(filename="qplotter_native-1.0.0.tar.gz", packagetype="sdist"))
    with pytest.raises(AssertionError, match="sdists must not"):
        release.published_native_files("1.0.0")
    files.pop()
    files.pop()
    with pytest.raises(AssertionError):
        release.published_native_files("1.0.0")


def test_pypi_verification_redownloads_every_validated_byte_and_rejects_substitution(monkeypatch):
    files = [remote_file(f"qplotter_native-1.0.0-cp311-abi3-{tag}.whl") for tag in NATIVE_TAGS]
    receipt = dict(package="qplotter-native", version="1.0.0",
                   files={item["filename"]: item["digests"]["sha256"] for item in files})
    monkeypatch.setattr(release, "release_files", lambda *_: files)
    reader = Mock(return_value=b"validated")
    monkeypatch.setattr(release, "read_url", reader)
    release.verify_pypi(receipt)
    assert reader.call_count == 4
    reader.return_value = b"corrupted"
    with pytest.raises(AssertionError, match="published bytes differ"):
        release.verify_pypi(receipt)
    files.append(remote_file("qplotter_native-1.0.0.tar.gz"))
    with pytest.raises(AssertionError, match="unexpected artifacts"):
        release.verify_pypi(receipt)


def test_native_verification_waits_for_indexing_but_fails_closed_on_bad_hash(monkeypatch):
    item = remote_file("qplotter_native-1.0.0-cp311-abi3-win_amd64.whl")
    receipt = dict(package="qplotter-native", version="1.0.0",
                   files={item["filename"]: item["digests"]["sha256"]})
    unavailable = urllib.error.HTTPError("url", 404, "not indexed", {}, None)
    reader = Mock(side_effect=[unavailable, [item]])
    monkeypatch.setattr(release, "release_files", reader)
    monkeypatch.setattr(release, "read_url", lambda _: b"validated")
    sleeper = Mock()
    monkeypatch.setattr(release.time, "sleep", sleeper)
    release.verify_pypi(receipt, wait_seconds=30)
    assert sleeper.call_count == 1
    item["digests"]["sha256"] = "0" * 64
    monkeypatch.setattr(release, "release_files", lambda *_: [item])
    with pytest.raises(AssertionError, match="PyPI digest differs"):
        release.verify_pypi(receipt, wait_seconds=30)
    assert sleeper.call_count == 1


def test_fetch_native_checks_remote_digest_and_exact_metadata(artifact_set, monkeypatch):
    repository, artifacts, _ = artifact_set
    path = next(artifacts.glob("*win_amd64.whl"))
    item = remote_file(path.name, path.read_bytes())
    monkeypatch.setattr(release, "published_native_files", lambda _: [item])
    monkeypatch.setattr(release, "read_url", lambda _: path.read_bytes())
    destination = artifacts.parent / "downloaded"
    release.fetch_native(repository, "qplotter-native-windows-x64", destination)
    assert (destination / path.name).read_bytes() == path.read_bytes()
    item["digests"]["sha256"] = "0" * 64
    with pytest.raises(AssertionError, match="PyPI digest mismatch"):
        release.fetch_native(repository, "windows-x64", artifacts.parent / "bad-download")


def test_native_index_verification_uses_exact_binary_pin_without_cache(artifact_set, monkeypatch):
    repository, artifacts, _ = artifact_set
    path = next(artifacts.glob("*win_amd64.whl"))
    item = remote_file(path.name, path.read_bytes())
    monkeypatch.setattr(release, "published_native_files", lambda _: [item])
    destination = artifacts.parent / "index-download"
    calls = []

    def download(command, **kwargs):
        calls.append((command, kwargs))
        shutil.copyfile(path, destination / path.name)

    monkeypatch.setattr(release.subprocess, "run", download)
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://unrelated-index.invalid")
    release.fetch_native(repository, "windows-x64", destination, from_index=True)
    command, options = calls[0]
    assert "--only-binary=:all:" in command and "--no-cache-dir" in command
    assert "--no-deps" in command and "--abi" in command and "abi3" in command
    assert command[-1] == "qplotter-native==1.0.0"
    assert command[command.index("--index-url") + 1] == "https://pypi.org/simple"
    assert "PIP_EXTRA_INDEX_URL" not in options["env"]
    assert options["cwd"] == destination


def test_workflow_gates_uploads_and_verifies_native_before_application():
    workflow_path = REPOSITORY / ".github/workflows/release.yml"
    if not workflow_path.is_file():
        pytest.skip("Hosted workflows are not included in sdists")
    text = workflow_path.read_text()
    assert 'tags: ["v*", "native-and-app/v*"]' in text
    assert "uses: ./.github/workflows/ci.yml" in text
    assert "reuse-native: ${{ needs.preflight.outputs.publish-native != 'true' }}" in text
    assert "needs: validate" in text
    assert "run-id:" not in text and "github-token:" not in text
    assert "password:" not in text and "skip-existing:" not in text
    assert text.count("id-token: write") == 2
    for package in ("native", "application"):
        # Parse by job boundaries, not indented step/property boundaries.
        job = text.split(f"  publish-{package}:\n", 1)[1].split(f"\n  verify-{package}:\n", 1)[0]
        assert f"name: pypi-{package}" in job
        assert f"verify-bundle release/{package}" in job
        assert f"packages-dir: release/{package}/dist/" in job
        assert "python -m build" not in job and "pip install" not in job
    verification = text.split("  verify-native:\n", 1)[1].split("  publish-application:\n", 1)[0]
    assert "needs: [prepare, publish-native]" in verification
    assert "needs.publish-native.result == 'skipped'" in verification
    assert "verify-pypi release/native" in verification
    assert "validate_compiler_free_install.py --mode wheel" in verification
    assert "needs: [prepare, verify-native]" in text
    assert "needs.verify-native.result == 'success'" in text
    ci = (REPOSITORY / ".github/workflows/ci.yml").read_text()
    assert "workflow_call:" in ci
    assert "if: ${{ !inputs.reuse-native }}" in ci
    assert "if: inputs.reuse-native" in ci
    assert "python scripts/release.py fetch-native" in ci
    public = text.split('  public-installations:\n', 1)[1]
    assert 'needs: [prepare, verify-application]' in public
    assert 'python-version: ["3.11", "3.12", "3.13", "3.14"]' in public
    assert 'platform: [linux-x86_64, macos-arm64, macos-intel, windows-x64]' in public
    assert '--public-pypi' in public
    assert '--mode wheel public-expected' in public
    assert '--mode editable public-expected' in public
    assert 'run-unprivileged-windows.ps1' in public
    assert 'verify receipts' not in public.lower() or 'select-platform --bundle release' in public


def test_receipt_does_not_allow_unlisted_files_or_path_traversal(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "dist").mkdir(parents=True)
    receipt = dict(schema=1, repository=release.REPOSITORY, package="qplotter",
                   version="1.6.0b2", files={"../escape.whl": "0" * 64}, **IDENTITY)
    (bundle / "manifest.json").write_text(json.dumps(receipt))
    with pytest.raises(AssertionError):
        release.verify_bundle(bundle, **IDENTITY)
