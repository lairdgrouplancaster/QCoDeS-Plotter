# Testing qPlot

Tests protect the current QCoDeS viewing requirements: safe database access,
faithful measurements, responsive live updates and working application controls.
Organise tests by the behaviour they protect. Development stages and the names
of bug-finding exercises do not define test ownership.

Worker lifecycle tests should exercise construction, signal wiring and pool
submission failures as well as exceptions inside a running worker. For example,
`windows/test_generation_startup.py` verifies that failed generation startup
restores the accepted database view and refresh timer without changing the
source. The smaller fault-injection cases in `windows/test_testdata_gui.py`
also cover new destinations and signal-connection failure.

## Finding the relevant tests

| Directory | Behaviour covered |
| --- | --- |
| `tests/datahandling` | Database access, metadata, current QCoDeS layouts, live readers, snapshots, caches and data exports |
| `tests/workers` | Loading measurements, array expansion, heatmap aggregation, resource limits and cancellation |
| `tests/tools` | Numerical helpers, geometry, operations, statistics and plot results |
| `tests/windows` | Main and plot windows, controls, transforms, exports, preferences and GUI lifecycle |
| `tests/widgets` | Run lists, details, previews and operation widgets |
| `tests/configuration` | Settings validation, persistence, recovery, diagnostics and auxiliary-file protection |
| `tests/generation` | Explicit synthetic-data generation, specifications and output protection |
| `tests/lifecycle` | Process supervision, shutdown protocol and startup handshakes |
| `tests/desktop` | Desktop launchers, file associations and native application identity |
| `tests/updates` | Update checks and their presentation |
| `tests/infrastructure` | Packaging, CI, documentation screenshots, release checks and diagnostic scripts |
| `tests/support` | Shared fixtures, data builders, independent oracles and assertions; no collected test modules |

`windows` means application windows, not the Windows operating system.
Tests for operating-system behaviour use explicit platform conditions wherever
they live. A directory describes a feature, not its execution cost or whether
it needs Qt.

Name a test after its condition and observable outcome, for example
`test_failed_recovery_retains_defaults_in_memory_and_original_on_disk`.
Give parametrised cases useful IDs when the input value alone does not explain
the distinction. A module's opening description should state its purpose.

Put a helper in `tests/support` when another test module needs it. Import
fixtures explicitly, preserving their pytest names; do not import a `test_*`
module to obtain its helpers. Keep small helpers used by only one module beside
their tests. Avoid a general fixture framework where a short function suffices.
Existing dedicated harnesses such as `tests/_artifact_audit.py` remain available;
the separate audit process deliberately imports only the standard library.
The root `conftest.py` registers `tests.support` for pytest assertion rewriting
so failures inside shared assertions retain useful value comparisons.

## Running tests

Use the project virtual environment. With it activated, run a feature or a
specific behaviour first:

```console
python -m pytest tests/configuration
python -m pytest tests/windows/test_paused_plot_manual_refresh.py
python -m pytest tests/datahandling -k cancellation
```

Run the full suite for changes to shared test support or cross-feature behaviour:

```console
python -m pytest -n 2 --dist=loadfile --no-loadscope-reorder
```

Whole-file scheduling keeps each file's tests together. The committed timing
profile estimates costs; it is not a performance requirement. Update paths in
`tests/ci_timings.json` and the `.github/pytest-compatibility*.txt` selectors when
moving or renaming selected tests. Do not remove compatibility checks merely to
make a reorganisation pass.

Coverage is opt-in:

```console
python -m pytest -n 2 --dist=loadfile --no-loadscope-reorder --cov=qplot --cov-report=term-missing:skip-covered --cov-report=xml
```

That command measures pytest and its threads. To measure normally exiting
children as well, run the focused process suite separately:

```console
python -m pytest --cov=qplot --cov-config=.github/coverage-subprocess.toml --cov-report=term-missing:skip-covered "@.github/pytest-subprocess-coverage.txt"
```

Use separate `COVERAGE_FILE` paths if retaining both reports for comparison.
The full CI coverage job does this and appends the child measurements to each
parent partition before uploading it. Ordinary platform and deadline tests
continue to run without subprocess instrumentation.

The opt-in profile uses coverage.py's subprocess and multiprocessing support;
pytest-cov 7 delegates subprocess measurement to coverage.py. It does not add
signal handlers or patch `os._exit`, and it does not change application launch
arguments. Processes forcibly killed by deadline tests may therefore have no
saved measurements. Keep their behavioural checks even when coverage cannot
record their final execution. See the [coverage.py process guide](https://coverage.readthedocs.io/en/latest/subprocess.html)
and [pytest-cov subprocess support](https://pytest-cov.readthedocs.io/en/stable/subprocess-support.html).

Without an activated environment on Windows, use `.venv\Scripts\python.exe`,
or `..\.venv\Scripts\python.exe` for a neighbouring environment. On macOS use
`MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python` and reuse
that cache directory for each command.

The existing GitHub CI workflow uses routine desktop validation for ordinary
pull requests. A PR confined to application code, ordinary tests or documentation
runs the macOS ARM64 compiler-free installation check on Python 3.14. Changes to
packaging, dependencies, the native or trusted reader, or CI run that check on
Python 3.12, 3.13 and 3.14. The planner also uses all three when it cannot verify
the changed paths. Both ARM64 full-suite partitions and all Python/PyQt
compatibility checks still run on ordinary PRs. Pushes to `main` and full
validation retain all three installation checks.

For a broad test reorganisation, run CI on the candidate branch
with **full-validation** enabled as well. That includes the complete Python
matrix, Linux checks, combined coverage and source-distribution validation.
A local Windows run cannot substitute for those platform results.

## Static analysis

The explicit mypy targets in `pyproject.toml` cover 180 of the 215 application
modules. The other 35 have individual reasons in `[tool.qplot.typing.deferred]`.
`follow_imports = "silent"` allows mypy to use imported types while suppressing
diagnostics from modules outside the explicit targets; this is partial coverage,
not a claim that every function is fully annotated.

When adding or renaming a module, add its exact `src/qplot/...py` path to
`tool.mypy.files`, or record a specific reason for deferring it. When bringing a
module into the checked set, remove its deferral. The inventory check fails for
unaccounted modules, stale paths, duplicate targets, overlapping entries and
empty reasons. Prefer checking new code; deferrals are visible work remaining.

Use a separate analysis venv so the validated Qt stubs do not replace the Qt
version used for runtime testing. `requirements/analysis.txt` pins mypy and
PyQt6 for both contributors and CI. Application dependencies retain the ranges
in `pyproject.toml`; this is an analysis-tool pin, not a complete dependency lock.
The mypy target is Python 3.12 on Linux, matching CI on every contributor OS.
This does not cover every platform-specific branch. Extending static checking
to Windows and macOS remains separate work; runtime CI still tests both systems.

From the repository root on Windows:

```powershell
.venv\Scripts\python.exe -m venv .venv-analysis
.venv-analysis\Scripts\python.exe -m pip install --only-binary=:all: -e . -r requirements/analysis.txt
.venv-analysis\Scripts\python.exe scripts/check_typing_inventory.py
.venv-analysis\Scripts\python.exe -m mypy
```

Use `..\.venv\Scripts\python.exe` for the first command if the development
environment is beside the repository. On macOS/Linux:

```bash
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python -m venv .venv-analysis
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-analysis/bin/python -m pip install --only-binary=:all: -e . -r requirements/analysis.txt
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-analysis/bin/python scripts/check_typing_inventory.py
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-analysis/bin/python -m mypy
```

For an unpublished native dependency, include the matching locally built native
wheel in the install command, as described in [contributing](../CONTRIBUTING.md).
CI installs its current-revision application/native wheels before the same
analysis requirements. No extracted stubs or `PYTHONPATH` overrides are needed.
Keep runtime tests in the development venv.

## Isolation and measurement safety

`tests/conftest.py` provides the process-wide QApplication, restores QCoDeS's
default database location and audits snapshot-connection cleanup. Integration
isolation is the default. Only audited `pure` tests bypass it; neither those
tests nor their fixtures may need Qt initialisation or database fixtures.

Generate test measurements only in owned temporary locations. Preserve the
database-access invariant when exercising qPlot: its source main database and
WAL handles stay physically read-only, and it must not modify the main database,
WAL or rollback journal. The pinned SQLite VFS may perform the permitted shared
memory operations on the exact colocated SHM file. Keep native audit checks and
file-content checks: either alone gives incomplete evidence. Reader deadlines,
cancellation and checkpoint release also need dedicated checks.

Shared fixtures must release their writers, readers, windows and subprocesses,
including when setup or an assertion fails. Retain separate-process file audits
where closing an audit descriptor in the reader process could release POSIX
locks. Use bounded event-driven waits for asynchronous work; an arbitrary sleep
is not evidence that the requested state has arrived.

At an explicit test-writer boundary, establish a stable artefact baseline before
submitting the reader work being checked. Windows can publish pending writer
timestamps when the first audit handle closes. Keep exact post-read comparisons;
do not ignore timestamps or replace a baseline after a reader protection failure.

Desktop tests that require symlinks request the `directory_symlinks` capability
fixture. Native Windows registry tests request `native_registry`, which redirects
the real API to a disposable HKCU test root and removes it afterwards. Capability
probes may skip before application code runs; failures inside the application
must still fail the test. Never use real file-association keys as test storage.

## Deciding whether a failure is a defect

Establish the intended behaviour from the current project requirements and
documentation, then compare both the assertion and the implementation against
it. A passing implementation is not the specification, and an existing test
is not automatically correct.

Classify a failure before changing an expectation:

* **Application defect:** the software violates a current requirement. Preserve
  a regression case while fixing the software.
* **Obsolete expectation:** a documented requirement has changed. Update the
  assertion and explain the current requirement it now protects.
* **Test defect:** a fixture, mock, timing assumption or assertion is incorrect.
  Repair the test without weakening the product requirement.
* **Environment limitation:** a platform, filesystem capability or dependency
  is unavailable. Report the limitation; use a narrow, explained skip only
  when the condition genuinely does not apply.

When intent remains ambiguous, resolve that product decision explicitly. Do not
add settings migrations to satisfy old settings tests: incompatible settings
may reset to validated current defaults. Keep safe persistence and backup
checks. Unsupported or malformed database rejection tests protect the read
boundary and are not a promise to support old QCoDeS schemas.

## Preview and plot lifetimes

`widgets/test_preview_controller.py` covers startup faults, reentrant source
changes, synchronous and duplicate completion, cancelled generations and queued
timers. `test_preview_startup.py` checks the real Qt adapter, widget destruction
and a real worker retry with exact source-file observations. Existing priority,
selection, cache and database-replacement tests remain in use.

`windows/test_plot_lifetime.py` checks native widget retirement after close,
retained source ownership and closure during an active publication. These tests
do not forcibly delete the plot under examination. Test teardown still retires
the whole main window after asserting the behaviour.

The [workflow performance assessment](performance-assessment.md) describes the
opt-in measurements. Profiling CLI tests validate real line/heatmap rendering,
refresh completion, report creation, source preservation and shutdown without
asserting machine-dependent elapsed-time or memory thresholds.

## Simplifying coverage

Database-loading lifecycle tests have two boundaries:

* `test_database_load_controller.py` supplies a small host with no widgets or
  background threads. It checks ownership, startup and completion failures,
  stale callbacks, cancellation, retry supersession and publication cleanup.
* `test_database_worker_errors.py` exercises the real main window, Qt error
  presentation and workers against temporary QCoDeS databases. Its startup
  failure cases preserve the prior view and database artefacts, check reader
  closure, and then perform a successful retry.

Focused window doubles use `seed_load` from `tests/_database_load_fakes.py` to
arrange controller state. Production window observations remain read-only.
Existing publication rollback, source replacement, shutdown, refresh and export
tests continue to exercise their separate boundaries.

`test_database_refresh_controller.py` applies the same small-host approach to
refresh ownership, coalescing, stale callbacks, queued cancellation, startup
failure and nested page/completion events. Refresh window doubles use
`seed_refresh` from `tests/_database_refresh_fakes.py`; they do not make the
production observations writable. Real-window refresh startup failures verify
that the accepted reader, selection and source artefacts survive and that a
subsequent refresh succeeds. Existing source checks and basic-page rollback
assertions remain in the UI tests, including Qt receiver disconnection on
window destruction.
The coalesced-refresh regression also destroys the QObject before its queued
timer fires and checks that no further worker is submitted.

Plot refresh follows the same boundary. `test_plot_refresh_controller.py` checks
startup faults, cancellation, reentrant replacement, duplicate completion,
coalescing and publication ownership without worker threads.
`test_plot_refresh_startup.py` injects failures into real line and heatmap windows,
preserves the displayed arrays and source database, then performs a successful
retry. It also verifies GUI-thread publication and pool ownership through concrete
rendering. Focused window doubles use `tests/support/plot_refresh.py` to arrange
controller state; the production observations remain read-only. Existing cache
rollback, source-sidecar, overlay, volume, cut and shutdown tests remain in use.

`test_plot_publication.py` exercises exact snapshots, explicit terminal outcomes
and conditional shared-cache rollback without constructing windows.
`test_plot_publication_integration.py` injects failures and cancellation during
real line and heatmap rendering, and starts nested refreshes from preparation
and completion callbacks. `test_volume_publication.py` verifies metadata recovery
and retry after dependent-view failures in volumes and cuts. These tests retain
source-artefact assertions; sidecar-replacement tests also cover final completion
before commit.

`test_heatmap_viewport.py` covers range decisions, debounce ownership and staged
publication outcomes without constructing windows. Real SQL heatmap cases in
`test_heatmap_viewport_integration.py` exercise failed zoom/full-view rendering
and retry, rapid changes during publication, nested newer results, cancellation
on close or source rejection, and retained plots after selecting another
database. They check rendered values and preservation of source artefacts.
`test_heatmap_layer_integration.py` also checks that a hidden SQL source can
reload while retained and cancels pending viewport work on final removal.

`tools/test_heatmap_resolution.py` checks immutable reporting facts, conservative
fallbacks, missing metadata, cropped views and explanatory wording without
constructing windows. `windows/test_heatmap_resolution_state.py` covers report
publication outcomes. The real SQL cases in
`windows/test_heatmap_resolution_integration.py` check open dialogue updates,
failed renders and retries, nested newer publications, full-resolution transitions
and source rejection, retaining protected-source artefact assertions. Hidden-source
cases verify that completed publication snapshots are released even without
warning controls. Their SQL heatmap fixture lives in
`tests/support/windows/sql_heatmap.py`.

Consolidate tests that protect the same behaviour with the same setup into
clearly named parametrised cases. Keep independent expected values and oracles.
In particular, numerical expectations must not be obtained by calling the
implementation under test. Preserve tests for distinct loading routes, dtype
boundaries, missing values, cancellation phases and source-file lifetimes even
when their outputs look similar.

Before removing a test, identify the requirement it protects and its remaining
replacement. Separate moves and renames from expectation changes in the review.
Compare collection before and after structural changes so cases cannot disappear
silently. Coverage percentages help locate unexercised code, but do not establish
assertion quality. Python coverage also does not measure native C branches, and
a Windows run cannot validate macOS or POSIX-only behaviour.
