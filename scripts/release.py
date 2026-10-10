"""Stage validated releases and verify exact published artifacts (never upload)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path

if __package__:
    from . import validate_distribution as validator
else:
    import validate_distribution as validator

REPOSITORY = "lairdgrouplancaster/QCoDeS-Plotter"
PLATFORMS = ("linux-x86_64", "macos-arm64", "macos-intel", "windows-x64")
PACKAGES = {"application": "qplotter", "native": "qplotter-native"}


def projects(repository: Path) -> dict:
    application = tomllib.loads((repository / "pyproject.toml").read_text())["project"]
    native = tomllib.loads((repository / "native/pyproject.toml").read_text())["project"]
    assert application["name"] == PACKAGES["application"]
    assert native["name"] == PACKAGES["native"]
    assert native["version"] == validator.PINNED_NATIVE_VERSION
    assert f"qplotter-native=={native['version']}" in application["dependencies"]
    for project in (application, native):
        assert f"apsw=={validator.PINNED_APSW_VERSION}" in project["dependencies"]
        assert project["requires-python"] == ">=3.11"
    return {"application": application, "native": native}


def preflight(repository: Path, ref: str) -> bool:
    metadata = projects(repository)
    paired = ref.startswith("refs/tags/native-and-app/v")
    prefix = "refs/tags/native-and-app/v" if paired else "refs/tags/v"
    assert ref == prefix + metadata["application"]["version"], "tag/version mismatch"
    assert os.environ.get("GITHUB_REPOSITORY", REPOSITORY) == REPOSITORY
    return paired


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native_platform(filename: str, version: str) -> str:
    prefix = f"qplotter_native-{version}-cp311-abi3-"
    assert filename.startswith(prefix) and filename.endswith(".whl"), filename
    tags = filename[len(prefix):-4].split(".")
    if tags == ["win_amd64"]:
        return "windows-x64"
    if all(re.fullmatch(r"macosx_\d+_\d+_arm64", tag) for tag in tags):
        return "macos-arm64"
    if all(re.fullmatch(r"macosx_\d+_\d+_x86_64", tag) for tag in tags):
        return "macos-intel"
    if "manylinux_2_28_x86_64" in tags and all(
        re.fullmatch(r"manylinux_2_\d+_x86_64", tag)
        and int(tag.split("_")[2]) <= 28 for tag in tags
    ):
        return "linux-x86_64"
    raise AssertionError(f"unsupported publication platform: {filename}")


def check_metadata(path: Path, project: dict) -> None:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            assert len(names) == 1
            metadata = Parser().parsestr(archive.read(names[0]).decode())
            wheel = Parser().parsestr(archive.read(names[0].replace("METADATA", "WHEEL")).decode())
            filename_tags = path.stem.split("-")[-1].split(".")
            prefix = "cp311-abi3-" if project["name"] == PACKAGES["native"] else "py3-none-"
            assert set(wheel.get_all("Tag", [])) == {prefix + tag for tag in filename_tags}
    else:
        with tarfile.open(path, "r:gz") as archive:
            names = [item for item in archive.getmembers() if item.name.count("/") == 1
                     and item.name.endswith("/PKG-INFO")]
            assert len(names) == 1
            with archive.extractfile(names[0]) as stream:
                metadata = Parser().parsestr(stream.read().decode())
    assert metadata["Name"] == project["name"]
    assert metadata["Version"] == project["version"]
    assert metadata["Requires-Python"] == project["requires-python"]
    requirements = metadata.get_all("Requires-Dist", [])
    assert all(requirement in requirements for requirement in project["dependencies"])


def stage(repository: Path, artifacts: Path, output: Path, *, ref: str,
          revision: str, run_id: str) -> None:
    """Called only after the reusable CI workflow has completely succeeded."""
    preflight(repository, ref)
    metadata = projects(repository)
    source = validator.source_files(repository)
    application_version = metadata["application"]["version"]
    native_version = metadata["native"]["version"]
    files = list(artifacts.iterdir())
    expected_application = {
        f"qplotter-{application_version}-py3-none-any.whl",
        f"qplotter-{application_version}.tar.gz",
    }
    native_sdist = f"qplotter_native-{native_version}.tar.gz"
    grouped = {"application": [], "native": []}
    platforms = set()
    for path in files:
        assert path.is_file() and not path.is_symlink(), path
        if path.name in expected_application:
            grouped["application"].append(path)
            native = False
        elif path.name == native_sdist or path.suffix == ".whl":
            native = True
            if path.suffix == ".whl":
                platform = native_platform(path.name, native_version)
                assert platform not in platforms, f"duplicate native platform: {platform}"
                platforms.add(platform)
                grouped["native"].append(path)
        else:
            raise AssertionError(f"unexpected release input: {path}")
        check_metadata(path, metadata["native" if native else "application"])
        if path.suffix == ".whl":
            validator.validate_wheel(path, source, native=native)
        else:
            validator.validate_sdist(path, source, native=native)
    assert {path.name for path in grouped["application"]} == expected_application
    assert platforms == set(PLATFORMS), f"missing native platforms: {platforms}"
    assert native_sdist in {path.name for path in files}, "missing validated native sdist"
    assert not output.exists(), "release staging must start in an empty directory"
    for package, paths in grouped.items():
        destination = output / package / "dist"
        destination.mkdir(parents=True)
        for path in paths:
            shutil.copyfile(path, destination / path.name)
        receipt = {
            "schema": 1, "repository": REPOSITORY, "revision": revision,
            "ref": ref, "run_id": run_id, "package": PACKAGES[package],
            "version": metadata[package]["version"],
            "files": {path.name: digest(path) for path in sorted(paths)},
        }
        (destination.parent / "manifest.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("Staged application wheel/sdist and four native wheels; native sdist excluded.")


def verify_bundle(bundle: Path, *, revision: str, ref: str, run_id: str) -> dict:
    receipt = json.loads((bundle / "manifest.json").read_text())
    assert receipt["schema"] == 1 and receipt["repository"] == REPOSITORY
    assert receipt["revision"] == revision and receipt["ref"] == ref
    assert receipt["run_id"] == run_id, "artifacts came from another workflow run"
    package = next(key for key, value in PACKAGES.items() if value == receipt["package"])
    assert receipt["files"], "empty publication bundle"
    assert {path.name for path in (bundle / "dist").iterdir()} == set(receipt["files"])
    for filename, expected in receipt["files"].items():
        assert Path(filename).name == filename
        path = bundle / "dist" / filename
        assert path.is_file() and not path.is_symlink()
        assert digest(path) == expected, f"unvalidated artifact bytes: {filename}"
    if package == "native":
        assert {native_platform(name, receipt["version"]) for name in receipt["files"]} == set(PLATFORMS)
        assert len(receipt["files"]) == len(PLATFORMS)
    else:
        version = receipt["version"]
        assert set(receipt["files"]) == {
            f"qplotter-{version}-py3-none-any.whl", f"qplotter-{version}.tar.gz",
        }
    return receipt


def read_url(url: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme == "https" and parsed.hostname in {"pypi.org", "files.pythonhosted.org"}
    request = urllib.request.Request(url, headers={"User-Agent": "qplot-release-verification"})
    with urllib.request.urlopen(request, timeout=30) as response:
        final = urllib.parse.urlsplit(response.url)
        assert final.scheme == "https" and final.hostname in {"pypi.org", "files.pythonhosted.org"}
        return response.read()


def select_platform(bundle: Path, platform: str, output: Path, **identity: str) -> None:
    """Select comparison receipts, never installation inputs, for public checks."""
    assert platform in PLATFORMS
    application = verify_bundle(bundle / "application", **identity)
    native = verify_bundle(bundle / "native", **identity)
    assert not output.exists(), "platform selection requires an empty destination"
    output.mkdir(parents=True)
    app = next(name for name in application["files"] if name.endswith(".whl"))
    extension = next(name for name in native["files"]
                     if native_platform(name, native["version"]) == platform)
    for package, name in (("application", app), ("native", extension)):
        shutil.copyfile(bundle / package / "dist" / name, output / name)


def release_files(package: str, version: str) -> list[dict]:
    url = f"https://pypi.org/pypi/{package}/{urllib.parse.quote(version, safe='')}/json"
    metadata = json.loads(read_url(url))
    assert metadata["info"]["name"] == package and metadata["info"]["version"] == version
    files = metadata["urls"]
    assert len({item["filename"] for item in files}) == len(files), "duplicate PyPI filenames"
    assert files and all(not item["yanked"] for item in files), "missing/yanked PyPI release"
    return files


def published_native_files(version: str) -> list[dict]:
    files = release_files(PACKAGES["native"], version)
    assert all(item["packagetype"] == "bdist_wheel" for item in files), "native sdists must not be on PyPI"
    assert len(files) == len(PLATFORMS)
    assert {native_platform(item["filename"], version) for item in files} == set(PLATFORMS)
    return files


def download_file(item: dict, destination: Path) -> None:
    assert destination.name == item["filename"]
    data = read_url(item["url"])
    assert len(data) == item["size"]
    assert hashlib.sha256(data).hexdigest() == item["digests"]["sha256"], "PyPI digest mismatch"
    destination.write_bytes(data)


def fetch_native(repository: Path, platform: str, output: Path, *, from_index: bool = False) -> None:
    platform = platform.removeprefix("qplotter-native-")
    assert platform in PLATFORMS
    native = projects(repository)["native"]
    files = published_native_files(native["version"])
    item = next(item for item in files if native_platform(item["filename"], native["version"]) == platform)
    output.mkdir(parents=True, exist_ok=True)
    assert not list(output.glob("*.whl")), "native fetch destination already contains a wheel"
    path = output / item["filename"]
    if from_index:
        env = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
        env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_CACHE_DIR="1")
        wheel_platform = path.stem.split("-")[-1].split(".")[0]
        subprocess.run([
            sys.executable, "-m", "pip", "download", "--only-binary=:all:",
            "--no-cache-dir", "--no-deps", "--index-url", "https://pypi.org/simple",
            "--platform", wheel_platform, "--implementation", "cp",
            "--python-version", "3.11", "--abi", "abi3", "--dest", str(output.resolve()),
            f"qplotter-native=={native['version']}",
        ], check=True, cwd=output, env=env)
        assert {file.name for file in output.glob("*.whl")} == {item["filename"]}
        assert path.stat().st_size == item["size"]
        assert digest(path) == item["digests"]["sha256"], "PyPI index wheel digest mismatch"
    else:
        download_file(item, path)
    check_metadata(path, native)
    validator.validate_wheel(path, validator.source_files(repository), native=True)
    print(f"Fetched exact pinned PyPI wheel with verified SHA-256: {path.name}")


def verify_pypi(receipt: dict, *, wait_seconds: int = 0) -> None:
    """Verify listing AND redownloaded bytes; mismatches fail without retries."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            files = release_files(receipt["package"], receipt["version"])
            expected = receipt["files"]
            remote = {item["filename"]: item for item in files}
            if set(remote) < set(expected):
                raise FileNotFoundError("PyPI has not indexed every artifact yet")
            assert set(remote) == set(expected), "unexpected artifacts in PyPI release"
            if receipt["package"] == PACKAGES["native"]:
                assert all(item["packagetype"] == "bdist_wheel" for item in files)
            for name, sha256 in expected.items():
                item = remote[name]
                assert item["digests"]["sha256"] == sha256, f"PyPI digest differs: {name}"
                data = read_url(item["url"])
                assert len(data) == item["size"]
                assert hashlib.sha256(data).hexdigest() == sha256, f"published bytes differ: {name}"
            print(f"Verified all published bytes for {receipt['package']}=={receipt['version']}.")
            return
        except urllib.error.HTTPError as error:
            if error.code != 404 or time.monotonic() >= deadline:
                raise
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise
        time.sleep(min(10, max(0, deadline - time.monotonic())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    stage_parser = commands.add_parser("stage")
    stage_parser.add_argument("--artifacts", type=Path, required=True)
    stage_parser.add_argument("--outdir", type=Path, required=True)
    fetch_parser = commands.add_parser("fetch-native")
    fetch_parser.add_argument("--platform", required=True)
    fetch_parser.add_argument("--outdir", type=Path, required=True)
    fetch_parser.add_argument("--from-index", action="store_true",
                              help="also verify pip resolves the pinned wheel through PyPI's simple index")
    select = commands.add_parser("select-platform")
    select.add_argument("--bundle", type=Path, required=True)
    select.add_argument("--platform", required=True, choices=PLATFORMS)
    select.add_argument("--outdir", type=Path, required=True)
    for command in ("verify-bundle", "verify-pypi"):
        check = commands.add_parser(command)
        check.add_argument("bundle", type=Path)
        if command == "verify-pypi":
            check.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    if args.command == "preflight":
        paired = preflight(repository, os.environ["GITHUB_REF"])
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write(f"publish-native={str(paired).lower()}\n")
        print("Paired release" if paired else "Application-only release with published native wheels")
    elif args.command == "fetch-native":
        fetch_native(repository, args.platform, args.outdir, from_index=args.from_index)
    else:
        identity = dict(revision=os.environ["GITHUB_SHA"], ref=os.environ["GITHUB_REF"],
                        run_id=os.environ["GITHUB_RUN_ID"])
        if args.command == "stage":
            stage(repository, args.artifacts, args.outdir, **identity)
        elif args.command == "select-platform":
            select_platform(args.bundle, args.platform, args.outdir, **identity)
        else:
            receipt = verify_bundle(args.bundle, **identity)
            if args.command == "verify-pypi":
                verify_pypi(receipt, wait_seconds=args.wait_seconds)
            else:
                print("Publication bundle matches the validated revision, run and artifact hashes.")


if __name__ == "__main__":
    main()
