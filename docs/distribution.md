# Distribution

This project currently targets source installs from GitHub. The packaging
metadata in `pyproject.toml` is already usable for editable installs, direct
Git installs, and local wheel builds.

The authoritative package version is `project.version` in `pyproject.toml`.
At runtime, `qplot.__version__` reads the installed package metadata through
`importlib.metadata`.

## Current Install Path

Recommended user install. This intentionally targets the latest full release,
not the current prerelease:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.4.0
```

Recommended development install:

```console
python -m pip install -e ".[dev]"
```

Both commands expose the `qplot` and `qplot-cfg` entry points.

## Current Beta

The current beta is `1.5.0-b5`. The package metadata uses the PEP 440 normal
form `1.5.0b5`; the GitHub release tag should be `v1.5.0-b5`.

Beta test install:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.5.0-b5
```

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

1. Update the version in `pyproject.toml`. For prereleases, use the PEP 440
   package form, such as `1.5.0b5`, even if the Git tag includes a separator,
   such as `v1.5.0-b5`.
2. Move relevant entries from `CHANGELOG.md`'s Unreleased section into the new
   release section.
3. Run `python -m ruff check .`.
4. Run `python -m mypy`.
5. Run `python -m pytest`.
6. Run `python -m build`.
7. Run `python -m twine check dist/*`.
8. Create a fresh virtual environment, install only the built wheel, and check
   `qplot-cfg -version` plus `python -c "import qplot; print(qplot.__version__)"`.
9. Run the manual GUI check from `CONTRIBUTING.md`.
10. Confirm README install and compatibility notes still match the release.
11. Create a GitHub release from the tag and include user-facing changes.

## Future Options

PyPI publishing would make user installs simpler, but should wait until the
project has a clear release owner and versioning process. When that happens,
extend the package job into a protected tag-only publish workflow.

Standalone desktop installers may help non-Python users, but they should be
treated as a separate distribution target. The installer needs explicit testing
for QCoDeS database access, Qt platform plugins, themes, configuration files,
and the `qplot-cfg` helper.
