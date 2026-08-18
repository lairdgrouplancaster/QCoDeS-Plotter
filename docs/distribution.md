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

Both commands expose the `qplot` and `qplot-cfg` entry points.

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

The source distribution deliberately contains all tracked tests and fixtures,
shared `tests/conftest.py`, project metadata, documentation, developer scripts,
package source, schemas, and CSV resources. Virtual environments, build output,
caches, coverage output, bytecode, `.DS_Store`, and other generated files are
excluded by `MANIFEST.in`.

The CI workflow builds from a clean checkout and checks these artifacts once per
commit on Python 3.12. It compares their file lists with tracked source and test
files, runs the extracted sdist's complete test suite in an isolated virtual
environment, installs and smoke-tests the wheel in another isolated environment,
runs `twine check`, and then uploads both artifacts. It does not publish them to
PyPI or attach them to GitHub releases.

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
10. Confirm the validator ran the extracted sdist tests and wheel smoke checks.
11. Run the manual GUI check from `CONTRIBUTING.md`.
12. Confirm README install and compatibility notes still match the release.
13. Create a GitHub release from the tag and include user-facing changes.

## Future Options

PyPI publishing would make user installs simpler, but should wait until the
project has a clear release owner and versioning process. When that happens,
extend the package job into a protected tag-only publish workflow.

Standalone desktop installers may help non-Python users, but they should be
treated as a separate distribution target. The installer needs explicit testing
for QCoDeS database access, Qt platform plugins, themes, configuration files,
and the `qplot-cfg` helper.
