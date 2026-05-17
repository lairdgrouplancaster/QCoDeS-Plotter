# Contributing

This project uses a standard Python package layout with source code in
`src/qplot` and tests in `tests`.

## Fork Workflow

If you are contributing through a personal fork, clone your fork and add the
main project as `upstream`:

```console
git clone https://github.com/<your-username>/QCoDeS-Plotter.git
cd QCoDeS-Plotter
git remote add upstream https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git
```

Before starting new work, update your branch from upstream:

```console
git fetch upstream
git checkout main
git merge upstream/main
```

## Development Environment

Use Python 3.11 or newer in a virtual environment.

Windows:

```console
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```console
python3.11 -m venv .venv-mac
source .venv-mac/bin/activate
```

Install qPlot in editable mode with the development dependencies:

```console
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

If the virtual environment is not activated, call its Python executable
directly:

Windows:

```console
.\.venv\Scripts\python.exe -m pytest
```

macOS/Linux:

```console
./.venv-mac/bin/python -m pytest
```

Some local checkouts keep `.venv` next to the repository rather than inside it.
In that layout, use:

```console
..\.venv\Scripts\python.exe -m pytest
```

## Checks

Run the lightweight static check before committing:

```console
python -m ruff check .
```

Run the scoped type check:

```console
python -m mypy
```

Run the automated test suite:

```console
python -m pytest
```

Pytest prints branch coverage for the `qplot` package and writes `coverage.xml`
for CI or editor integrations.

The test suite runs PyQt in headless mode. The shared Qt setup lives in
`tests/conftest.py`; do not add per-test `QT_QPA_PLATFORM` setup or one-off
`QApplication` creation unless a test has a specific reason to override the
shared setup.

GitHub Actions runs the same Ruff, mypy, and pytest checks on Windows 2025 and
macOS with Python 3.11, 3.12, 3.13, and 3.14 for pushes and pull requests. The
workflow lives in `.github/workflows/ci.yml`.

## Generated Files

Local installs and test runs create generated files such as `*.egg-info/`,
`__pycache__/`, `.pytest_cache/`, and `.ruff_cache/`. These are ignored by Git
and should not be committed.

It is safe to delete those directories after local installs or checks if they
get in the way of searches or file listings.

## Manual GUI Check

For changes that affect runtime behavior or the GUI, run:

```console
python scripts/manual_run.py
```

Use this after the automated tests pass. It starts the app through the same
installed package entry path that users exercise.

Other local helper scripts are documented in [scripts/README.md](scripts/README.md).
Demo-data and screenshot workflow notes live in [docs/demo-data.md](docs/demo-data.md).
Release and packaging notes are documented in [docs/distribution.md](docs/distribution.md).

## Configuration Changes

Config keys, defaults, and validation rules are defined in
`src/qplot/configuration/config_schema.json` and documented in
[docs/configuration.md](docs/configuration.md).

When adding or changing a config key, update the schema, the relevant tests, and
the configuration reference in the same change.

## Pre-Commit Checklist

Before committing:

1. Run `python -m ruff check .`.
2. Run `python -m mypy`.
3. Run `python -m pytest`.
4. Run `python scripts/manual_run.py` for application or GUI changes.
5. Update `README.md`, `CONTRIBUTING.md`, `docs/architecture.md`, or
   `docs/configuration.md` when the setup, workflow, module boundaries, or
   config surface change.
6. Keep unrelated refactors out of feature or bug-fix commits.

## Project Map

See `docs/architecture.md` for the current module map and guidance on where to
make common changes.
