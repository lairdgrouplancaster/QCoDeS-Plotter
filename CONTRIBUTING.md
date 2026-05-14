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
py -3.11 -m venv .venv-win
.\.venv-win\Scripts\Activate.ps1
```

macOS/Linux:

```console
python3.11 -m venv .venv
source .venv/bin/activate
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
.\.venv-win\Scripts\python.exe -m pytest
```

macOS/Linux:

```console
./.venv/bin/python -m pytest
```

Some local checkouts keep `.venv-win` next to the repository rather than inside
it. In that layout, use:

```console
..\.venv-win\Scripts\python.exe -m pytest
```

## Checks

Run the lightweight static check before committing:

```console
python -m ruff check .
```

Run the automated test suite:

```console
python -m pytest
```

The test suite runs PyQt in headless mode. The shared Qt setup lives in
`tests/conftest.py`; do not add per-test `QT_QPA_PLATFORM` setup or one-off
`QApplication` creation unless a test has a specific reason to override the
shared setup.

## Manual GUI Check

For changes that affect runtime behavior or the GUI, run:

```console
python scripts/manual_run.py
```

Use this after the automated tests pass. It starts the app through the same
installed package entry path that users exercise.

## Pre-Commit Checklist

Before committing:

1. Run `python -m ruff check .`.
2. Run `python -m pytest`.
3. Run `python scripts/manual_run.py` for application or GUI changes.
4. Update `README.md`, `CONTRIBUTING.md`, or `docs/architecture.md` when the
   setup, workflow, or module boundaries change.
5. Keep unrelated refactors out of feature or bug-fix commits.

## Project Map

See `docs/architecture.md` for the current module map and guidance on where to
make common changes.
