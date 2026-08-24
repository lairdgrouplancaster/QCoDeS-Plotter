# Distribution

This project currently targets source installs from GitHub. The packaging
metadata in `pyproject.toml` is already usable for editable installs, direct
Git installs, and local wheel builds.

The authoritative package version is `project.version` in `pyproject.toml`.
At runtime, `qplot.__version__` reads the installed package metadata through
`importlib.metadata`.

## Current Install Path

Recommended user install for the latest full release:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.5.0
```

Recommended development install:

```console
python -m pip install -e ".[dev]"
```

Both commands expose the `qplot`, `qplot-cfg`, and `qplot-generate-db` entry
points.

## Current Beta

The current beta is `1.6.0-b1`. The package metadata uses the PEP 440 normal
form `1.6.0b1`; the GitHub release tag should be `v1.6.0-b1`.

Beta test install:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.6.0-b1
```

## Current Release

The current release is `1.5.0`. The package metadata uses the PEP 440 form
`1.5.0`; the GitHub release tag should be `v1.5.0`.

Release install:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.5.0
```

## Package Validation

Build local release artifacts from a clean source tree with:

```console
python scripts/validate_distribution.py --check-clean
python -m build
```

Validate the contents and testability of the built artifacts, then validate
their metadata with:

```console
python scripts/validate_distribution.py dist
python -m twine check dist/*
```

For a platform job that intentionally builds only one wheel, use
`python scripts/validate_distribution.py --wheel-only dist`; it performs the
same wheel-content and installed-reader smoke checks without requiring an sdist.

The source distribution deliberately contains all source tests and fixtures,
shared `tests/conftest.py`, project metadata, documentation, developer scripts,
package source, schemas, CSV resources, and the trusted-reader C source and
SQLite ABI header. Virtual environments, build output, caches, coverage output,
bytecode, `.DS_Store`, and compiled `.so`, `.pyd`, `.dll`, and `.dylib` files are
excluded. The artifact validator independently rejects those compiled files in
an sdist, so every wheel must build the native extension from source.

The Stage 2 trusted live reader adds the `cp311-abi3` native extension
`qplot.datahandling._trusted_vfs_native`, so qPlot wheels are platform-specific.
A source or Git install requires a C compiler suitable for its Python. The
runtime dependency is the pinned APSW 3.53.4.0 build; CI requires its platform
wheel rather than silently compiling an unreviewed substitute. The setuptools
build selects C11 mode explicitly for MSVC while leaving Clang and GCC flags
untouched, so the native source's compile-time ABI assertions remain active on
Windows as well as POSIX builds.

The CI workflow builds from clean checkouts on Python 3.12. The Linux package
job builds the sdist and Linux wheel, compares their contents with the source
tree, runs the extracted sdist's complete test suite in an isolated virtual
environment, and installs and exercises the wheel in another environment.
Dedicated ARM64 macOS, Intel macOS, and Windows jobs build their platform wheels
from source and run the same installed-wheel reader smoke test. That smoke test
keeps a temporary WAL writer open, reads committed WAL-only data through
`TrustedLiveReader`, checks that mutating SQL is rejected, and verifies that the
main database, WAL, and rollback journal were not changed; SHM coordination
changes are allowed.
The Windows test suites and installed-wheel smoke run under a disposable local
standard account because the trusted reader rejects the hosted runner's
elevated token; CI separately verifies that elevated context is refused.
Every artifact receives a `twine check` before upload. CI does not publish to
PyPI or attach artifacts to GitHub releases. Cross-platform acceptance applies
only after the Linux, ARM64 macOS, Intel macOS, and unprivileged Windows jobs
have all passed for the exact source revision.

## Release Checklist

Before creating a tagged release:

1. Update the version in `pyproject.toml`. For prereleases, use the PEP 440
   package form, such as `1.6.0b1`, even if the Git tag includes a separator,
   such as `v1.6.0-b1`.
2. Move relevant entries from `CHANGELOG.md`'s Unreleased section into the new
   release section.
3. Run `python -m ruff check .`.
4. Run `python -m mypy`.
5. Run `python -m pytest`.
6. Run `python scripts/validate_distribution.py --check-clean`.
7. Run `python -m build`.
8. Run `python scripts/validate_distribution.py dist`.
9. Run `python -m twine check dist/*`.
10. Confirm the validator ran the extracted sdist tests and installed trusted
    WAL-reader wheel smoke check.
11. Confirm unprivileged Windows, ARM64 macOS, Intel macOS, and Linux wheel jobs
    passed for the exact source.
12. Run the manual GUI check from `CONTRIBUTING.md`.
13. Confirm README install and compatibility notes still match the release.
14. Create a GitHub release from the tag and include user-facing changes.

## Future Options

PyPI publishing would make user installs simpler, but should wait until the
project has a clear release owner and versioning process. When that happens,
extend the package job into a protected tag-only publish workflow.

Standalone desktop installers may help non-Python users, but they should be
treated as a separate distribution target. The installer needs explicit testing
for QCoDeS database access, Qt platform plugins, themes, configuration files,
and the `qplot-cfg` helper.
