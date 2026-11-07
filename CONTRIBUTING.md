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

Use standard CPython 3.12–3.14 in a virtual environment.

Windows:

```console
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS:

```console
python3.12 -m venv .venv-mac
source .venv-mac/bin/activate
```

Linux:

```console
python3.12 -m venv .venv-linux
source .venv-linux/bin/activate
```

Native wheels cover Windows x64, macOS ARM64/Intel and glibc Linux x86_64.
Linux is exercised in headless CI; Windows and macOS are supported desktop
platforms. See the [platform matrix](docs/distribution.md#supported-platforms).

The root distribution `qplotter` is pure Python. Install the compatible
`qplotter-native==1.0.1` platform wheel with the application in editable
mode. After the native release is on PyPI, no separate download is needed:

```console
python -m pip install -U pip
python -m pip install --only-binary=:all: -e ".[dev]"
```

Before publication, obtain the native wheel from the validated CI artifacts
for your revision/platform and supply its exact path (replace the placeholder):

```console
python -m pip install --only-binary=:all: "dist/<matching-native-wheel>.whl" -e ".[dev]"
```

Every runtime and development dependency is installed as a wheel. Editing
Python modules in `src/qplot/` is reflected by the next application process;
the native binary is not rebuilt. Start it with `python -m qplot`.
The native import package is `qplot_native`; application imports remain `qplot`.
For an environment containing the old `qplot` distribution, follow the
[replacement instructions](README.md#replacing-an-older-installation) before
installing editable mode. Never leave both application distributions installed.
When reusing an old editable checkout, also remove its generated legacy
metadata before installing the new editable application:

```console
python -c "import shutil; from pathlib import Path; [shutil.rmtree(p) for p in (Path('src/qplot.egg-info'), Path('src/qcodes_plotter.egg-info'), Path('native/src/qcodes_plotter_native.egg-info')) if p.is_dir()]"
```

These ignored directories contain generated packaging metadata. A fresh
checkout does not contain them. Removing old metadata prevents pip from
reporting the old distribution alongside `qplotter` after editable installation.

### Native development (explicit compiler workflow)

The native package lives in `native/` and builds independently. Install a C
compiler and platform SDK: MSVC Build Tools with the C++ workload/Windows SDK
on Windows, Xcode Command Line Tools on macOS, or a C toolchain and Python
development headers on Linux. Then, from the repository root:

```console
python -m pip install --only-binary apsw ./native
python -m pip install --only-binary=:all: -e ".[dev]"
python -m build native --outdir dist
python -m pytest --no-cov tests/datahandling/test_trusted_live.py tests/datahandling/test_readonly.py
```

The full `python -m pytest` includes all database-safety and concurrent-writer
checks. After changing C sources, run
`python -m pip install --no-deps --force-reinstall ./native` to rebuild; do not
expect an application editable install to rebuild native code. Remove stale
`native/build/` output before switching compilers or architectures.

Native sdists are retained as CI artifacts and can also be built with
`python -m build native --sdist --outdir dist`. They are intentionally excluded
from PyPI so normal installation cannot silently compile a fallback. Preserve
`cp311-abi3`, the pinned APSW/SQLite ABI/source ID and all database protections.
When those change, release a new native version and coordinate the application
pin and compatibility checks; Python-only releases can retain the existing pin.

If the virtual environment is not activated, call its Python executable
directly:

Windows:

```console
.\.venv\Scripts\python.exe -m pytest
```

macOS:

```console
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python -m pytest
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
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache ../.venv-mac/bin/python -m pytest
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

On macOS, prefix Python commands that may import Matplotlib with
`MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache` and reuse that directory.

While editing, run the test files relevant to the change. For example, using
the project environment directly:

Windows:

```console
.\.venv\Scripts\python.exe -m pytest tests/test_config.py
```

macOS:

```console
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python -m pytest tests/test_config.py
```

Replace the example path with the affected test files. Run the full automated
suite when the scope of a change warrants it, using the activated project
environment (with the Matplotlib prefix on macOS):

```console
python -m pytest
```

Coverage is opt-in so ordinary and focused runs avoid its instrumentation and
report generation. To collect branch coverage and write `coverage.xml`, add
the coverage options explicitly:

```console
python -m pytest --cov=qplot --cov-report=term-missing:skip-covered --cov-report=xml
```

On macOS without an activated environment, use:

```console
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python -m pytest --cov=qplot --cov-report=term-missing:skip-covered --cov-report=xml
```

Coverage uses the existing branch settings and exclusions in `pyproject.toml`.

For release or packaging changes, start from a clean source tree, build the
two source distributions and wheels, validate all four artifacts, and check their
metadata:

```console
python scripts/validate_distribution.py --check-clean
python -m build native --outdir dist
python -m build
python scripts/validate_distribution.py dist
python -m twine check dist/*
```

The artifact validator checks both sdists against the current source tree and
source-distribution policy, requires the native sdist's trusted-reader C source
and header, rejects ignored files and stale compiled native binaries, and runs
all tests with both extracted sdists installed in a fresh virtual environment.
It installs both local wheels into another fresh environment for version,
resource, console-script, and native extension checks. From outside the
repository it then runs a real guarded Python
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

### CI timing and scheduling

Full macOS and Windows jobs each run two disjoint partitions with two xdist
workers per job. `tests/_ci_scheduling.py` assigns whole files using the committed
`tests/ci_timings.json` profile, then starts expensive files first with
`--dist=loadfile --no-loadscope-reorder`. Tests within a file keep their original
order. Both partitions and all workers read the same profile from the checkout;
CI does not fetch changing timing data while collecting tests.

Known file costs scale with the number of collected tests. New files use the
profile's average cost per test, so they are still included immediately. These
are scheduling estimates, not performance assertions. The initial profile uses
elapsed file spans from successful macOS ARM64 Python 3.12 jobs linked in its
`source` field; it excludes final worker/session teardown and is not a Windows
benchmark.

Each source-suite job uploads a `qplot-test-timings-*` artifact containing JUnit
results, including setup and teardown durations. To refresh the profile, download
reports from successful **full-suite** runs, covering both partitions. Reports
from several platforms can be supplied together; the updater keeps the slowest
observed total for each file:

```console
python scripts/update_ci_timings.py /path/to/partition-1.xml /path/to/partition-2.xml
```

Review and commit the updated profile. Do not refresh it from compatibility-only
or interrupted runs, which omit tests. For a local measurement, add
`-o junit_family=xunit1 --junitxml=/path/to/results.xml` to the test command.
Failed or malformed reports are rejected before the updater writes the profile.

Ruff runs immediately in an independent required job. Mypy continues to use the
current revision's installed application and native artifacts. Ordinary pull
requests and pushes to `main` retain the full Windows and ARM64 macOS suites on
Python 3.12 and 3.14, their compatibility suites on 3.13, and Intel macOS
compatibility suites on all three versions. Compiler-free ordinary and editable
installation checks also run on all three versions for Windows and both macOS
architectures. Ubuntu still runs Ruff, application/native builds, and mypy; the
Linux native wheel is needed for installed-artifact type checking.

Full validation additionally runs Linux compatibility and compiler-free
installation checks on Python 3.12–3.14, the complete Linux coverage suite, and
the extracted-sdist validation suite. It runs every Monday at 04:23 UTC, can be
requested through **Actions → CI → Run workflow**, and is explicitly required
by releases with `full-validation: true`. Changes to native code, database
readers, packaging, dependencies, or CI paths automatically select full
validation; an unavailable change comparison also selects full validation.
The weekly schedule and manual workflow become available once the workflow is
merged to `main`. The aggregate **Required checks** status keeps its existing
name and validates every job required by the selected mode.

GitHub Actions is configured to build the application wheel once and separate
`cp311-abi3` native wheels for Windows x64, macOS ARM64, macOS Intel, and Linux
x86_64. Linux publication wheels use cibuildwheel's `manylinux_2_28` image and
auditwheel repair. Each platform exercises the same run's matching native wheel
and shared application wheel on Python 3.12, 3.13, and 3.14 when its validation
jobs run. Pytest disables the checkout's `src` import
path in CI and verifies installed file hashes and module origins against the
downloaded wheels; the native origin is checked before loading the extension.
Both wheels are explicit local installation arguments, so these jobs do not
depend on either distribution being published. The workflow lives in
`.github/workflows/ci.yml` and is reused by the protected tag release workflow.
Application-only releases fetch the exact pinned native wheels from PyPI into
the same artifact pipeline; paired releases build new native wheels. Because
the trusted reader requires an unprivileged
process, its Windows tests and wheel exercise run through a disposable local
standard account; a separate probe confirms that the hosted runner's elevated
token is rejected. Release acceptance still requires successful full validation
of the exact tagged revision on Linux, ARM64 macOS, Intel macOS, and
unprivileged Windows; a successful ordinary pull-request run does not establish
that release acceptance.

The platform installation jobs require compiler-free ordinary and editable
acceptance in separate fresh environments. Run
`python scripts/validate_compiler_free_install.py --mode wheel dist` and
`python scripts/validate_compiler_free_install.py --mode editable dist` to
exercise them locally. Every runtime/development dependency must be a wheel;
compiler commands and absolute compiler subprocesses fail, and caches are
disabled. Both checks launch the real qPlot command and exercise trusted live
reading. The editable check changes Python code in a temporary extracted source
tree, proves the edit is imported without reinstalling, and verifies that the
native binary was not rebuilt. See `docs/distribution.md` for the full checks.

## Generated Files

Local installs, test runs, and package builds create generated files such as
`*.egg-info/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `build/`, and
`dist/`. These are ignored by Git and should not be committed.

It is safe to delete those directories after local installs or checks if they
get in the way of searches or file listings.

## Manual GUI Check

For changes that affect runtime behaviour or the GUI, run:

```console
python -m qplot
```

Use the activated project venv after automated tests pass. This exercises the
command-line launcher and propagates its exit status. On macOS, prefix this and
every test or other Python command that may import Matplotlib with
`MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache`; reuse that directory. For a
visible GUI check, leave `QT_QPA_PLATFORM` unset. For automated startup checks,
set `QT_QPA_PLATFORM=offscreen` and request shutdown after observing startup.

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
3. Run the relevant automated tests; use the full suite when the change's scope
   warrants it. The commands in Checks above use the project environment.
4. Run `python -m qplot` for application or GUI changes, using the platform
   environment settings in Manual GUI Check above.
5. Run `python -m build native --outdir dist`, `python -m build`, `python scripts/validate_distribution.py dist`, and
   `python -m twine check dist/*` for packaging or release changes.
6. Update `README.md`, `CONTRIBUTING.md`, `docs/architecture.md`, or
   `docs/configuration.md` when the setup, workflow, module boundaries, or
   config surface change.
7. Keep unrelated refactors out of feature or bug-fix commits.

## Project Map

See `docs/architecture.md` for the current module map and guidance on where to
make common changes.
