# Distribution

This project currently targets source installs from GitHub. The packaging
metadata in `pyproject.toml` is already usable for editable installs, direct
Git installs, and local wheel builds.

The authoritative package version is `project.version` in `pyproject.toml`.
At runtime, `qplot.__version__` reads the installed package metadata through
`importlib.metadata`.

## Current Install Path

Recommended user install:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@main
```

Recommended development install:

```console
python -m pip install -e ".[dev]"
```

Both commands expose the `qplot` and `qplot-cfg` entry points.

## Package Validation

Build local release artifacts with:

```console
python -m build
```

Validate the built source distribution and wheel metadata with:

```console
python -m twine check dist/*
```

The CI workflow builds and checks these artifacts once per commit on Python
3.12, then uploads them as workflow artifacts. It does not publish them to PyPI
or attach them to GitHub releases.

## Release Checklist

Before creating a tagged release:

1. Update the version in `pyproject.toml`.
2. Move relevant entries from `CHANGELOG.md`'s Unreleased section into the new
   release section.
3. Run `python -m ruff check .`.
4. Run `python -m mypy`.
5. Run `python -m pytest`.
6. Run `python -m build`.
7. Run `python -m twine check dist/*`.
8. Run the manual GUI check from `CONTRIBUTING.md`.
9. Confirm README install and compatibility notes still match the release.
10. Create a GitHub release from the tag and include user-facing changes.

## Future Options

PyPI publishing would make user installs simpler, but should wait until the
project has a clear release owner and versioning process. When that happens,
extend the package job into a protected tag-only publish workflow.

Standalone desktop installers may help non-Python users, but they should be
treated as a separate distribution target. The installer needs explicit testing
for QCoDeS database access, Qt platform plugins, themes, configuration files,
and the `qplot-cfg` helper.
