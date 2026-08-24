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

macOS:

```console
python3 -m venv .venv-mac
source .venv-mac/bin/activate
```

Linux:

```console
python3 -m venv .venv-linux
source .venv-linux/bin/activate
```

Linux is useful for source-level development, but it is not currently part of
the supported desktop GUI test matrix.

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

macOS:

```console
./.venv-mac/bin/python -m pytest
```

Linux:

```console
./.venv-linux/bin/python -m pytest
```

Some local checkouts keep the virtual environment next to the repository rather
than inside it. In that layout, use:

Windows:

```console
..\.venv\Scripts\python.exe -m pytest
```

macOS:

```console
../.venv-mac/bin/python -m pytest
```

Linux:

```console
../.venv-linux/bin/python -m pytest
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

For release or packaging changes, start from a clean source tree, build the
source distribution and wheel, validate both artifacts, and check their
metadata:

```console
python scripts/validate_distribution.py --check-clean
python -m build
python scripts/validate_distribution.py dist
python -m twine check dist/*
```

The artifact validator checks the sdist against the current source tree and
source-distribution policy, requires the trusted-reader C source and header,
rejects ignored files and stale compiled native binaries, and runs all tests
from an extracted sdist in a fresh virtual environment. It installs the wheel
into another fresh environment for version, resource, console-script, and native
extension checks. From outside the repository it then runs a real guarded Python
script that opens a temporary WAL database through
`TrustedLiveReaderSupervisor`. This exercises the installed package-level helper
target with multiprocessing `spawn`, queries committed data while the writer
remains open, observes a later writer commit through the same helper, rejects
mutating SQL, rejects an oversized nine-column live SQLite row with the distinct
result-limit error, and proves that the same clean helper remains usable after
both length-limit tiers are restored. It then injects uncertain per-statement
limit restoration, proves that helper incarnation is retired, and recovers only
through a fresh explicit helper. It also confirms writer checkpoint progress
and verifies that the protected database artifacts remain unchanged during
reader-only phases.

The reader's 32 MiB pre-yield row figure is a conservative logical
Python-object/payload accounting envelope for standard APSW tuple/scalar
conversion, not an allocator-reserved-byte, RSS, fragmentation, arena, or
SQLite VM-memory ceiling. Result-limit regressions may use `sys.getsizeof` for
that logical envelope but must not assert allocator size or process RSS. The
independent aggregate raw text/blob-payload bound is 8 MiB.

The test suite runs PyQt in headless mode. The shared Qt setup lives in
`tests/conftest.py`; do not add per-test `QT_QPA_PLATFORM` setup or one-off
`QApplication` creation unless a test has a specific reason to override the
shared setup.

GitHub Actions runs the same Ruff, mypy, and pytest checks on Windows 2025 and
macOS with Python 3.11, 3.12, 3.13, and 3.14 for pushes and pull requests. On
Python 3.12 it validates the sdist and Linux wheel and separately builds and
exercises installed macOS and Windows wheels. The workflow lives in
`.github/workflows/ci.yml`. Because the trusted reader requires an unprivileged
process, its Windows tests and wheel exercise run through a disposable local
standard account; a separate probe confirms that the hosted runner's elevated
token is rejected. Configuring these jobs is not cross-platform acceptance for
a source revision: its Linux, ARM64 macOS, Intel macOS, and unprivileged Windows
hosted jobs must all finish successfully for that exact revision.

## Generated Files

Local installs, test runs, and package builds create generated files such as
`*.egg-info/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `build/`, and
`dist/`. These are ignored by Git and should not be committed.

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
5. Run `python -m build`, `python scripts/validate_distribution.py dist`, and
   `python -m twine check dist/*` for packaging or release changes.
6. Update `README.md`, `CONTRIBUTING.md`, `docs/architecture.md`, or
   `docs/configuration.md` when the setup, workflow, module boundaries, or
   config surface change.
7. Keep unrelated refactors out of feature or bug-fix commits.

## Project Map

See `docs/architecture.md` for the current module map and guidance on where to
make common changes.
