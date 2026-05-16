# Distribution

This project currently targets source installs from GitHub. The packaging
metadata in `pyproject.toml` is already usable for editable installs, direct
Git installs, and local wheel builds.

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

## Release Checklist

Before creating a tagged release:

1. Update the version in `pyproject.toml` and `src/qplot/_version.py`.
2. Run `python -m ruff check .`.
3. Run `python -m pytest`.
4. Run the manual GUI check from `CONTRIBUTING.md`.
5. Confirm README install and compatibility notes still match the release.
6. Create a GitHub release from the tag and include user-facing changes.

## Future Options

PyPI publishing would make user installs simpler, but should wait until the
project has a clear release owner and versioning process. When that happens,
add a build/publish workflow that runs only from protected release tags.

Standalone desktop installers may help non-Python users, but they should be
treated as a separate distribution target. The installer needs explicit testing
for QCoDeS database access, Qt platform plugins, themes, configuration files,
and the `qplot-cfg` helper.
