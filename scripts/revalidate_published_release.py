"""Recheck an existing public release with corrected validation tools; never upload."""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path

if __package__:
    from . import release
    from . import validate_compiler_free_install as acceptance
else:
    import release
    import validate_compiler_free_install as acceptance


def validate_installation(repository: Path, bundle: Path, *, tag: str,
                          run_id: str, platform: str, mode: str) -> None:
    """Use the original release's native pin with the corrected validation tools.

    Publishing still requires the current pin. Only this read-only recheck
    scopes the pin to the tagged source; the current APSW/SQLite ABI checks,
    receipt identities, public hashes and installed-reader smokes remain intact.
    """
    native = tomllib.loads((repository / 'native/pyproject.toml').read_text())['project']
    current_pin = release.validator.PINNED_NATIVE_VERSION
    try:
        release.validator.PINNED_NATIVE_VERSION = native['version']
        _validate_installation(repository, bundle, tag=tag, run_id=run_id,
                               platform=platform, mode=mode)
    finally:
        release.validator.PINNED_NATIVE_VERSION = current_pin


def _validate_installation(repository: Path, bundle: Path, *, tag: str,
                           run_id: str, platform: str, mode: str) -> None:
    """Bind original receipts to the tagged source and public package bytes."""
    repository = repository.resolve()
    assert mode in ('wheel', 'editable'), mode
    assert re.fullmatch(r'[1-9][0-9]*', run_id), 'invalid original release run ID'
    ref = f'refs/tags/{tag}'
    # A historical release keeps its recorded Python minimum. Publication
    # still requires the current minimum through release.preflight's default.
    application = tomllib.loads((repository / 'pyproject.toml').read_text())['project']
    python_requirement = application['requires-python']
    release.preflight(repository, ref, application_requires_python=python_requirement)
    metadata = release.projects(repository, application_requires_python=python_requirement)
    revision = subprocess.check_output(
        ['git', '-c', f'safe.directory={repository}', 'rev-parse', 'HEAD'],
        cwd=repository, text=True,
    ).strip()
    identity = dict(revision=revision, ref=ref, run_id=run_id)
    for package in ('application', 'native'):
        receipt = release.verify_bundle(bundle / package, **identity)
        assert receipt['package'] == release.PACKAGES[package], 'wrong receipt package'
        assert receipt['version'] == metadata[package]['version'], 'wrong receipt version'
        release.verify_pypi(receipt)
    with tempfile.TemporaryDirectory(prefix=f'qplot-public-recheck-{mode}-') as name:
        temporary = Path(name)
        selected = temporary / 'comparison'
        release.select_platform(bundle, platform, selected, **identity)
        artifacts = acceptance.validator.find_artifacts([selected], wheel_only=True)
        acceptance.accept_installation(repository, artifacts, temporary,
                                       editable=mode == 'editable', public_pypi=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True,
                        help='Clean checkout of the original release tag, not the validation tools')
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--run-id', required=True, help='Original publication workflow run ID')
    parser.add_argument('--platform', required=True, choices=release.PLATFORMS)
    parser.add_argument('--mode', required=True, choices=('wheel', 'editable'))
    args = parser.parse_args()
    validate_installation(args.repository, args.bundle, tag=args.tag,
                          run_id=args.run_id, platform=args.platform, mode=args.mode)


if __name__ == '__main__':
    main()
