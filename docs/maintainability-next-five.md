# Five maintainability improvements after PR #120

Reviewed `main` at `fa917dc0d24fdd3de3857475bd358c05e49ebf35` on 5 November 2026 after fetching the configured
remote. This revision includes [PR #120](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/120)
and the follow-ups in PRs #121 and #122. The findings below address remaining
maintenance costs rather than repeating those changes.

| # | Finding on the reviewed revision | Improvement | Contract to preserve |
| --- | --- | --- | --- |
| 1 | `_plot_axis_scaling.py` repeats the four axes' names, sides, dimensions, aliases, assignment attributes, and tooltips across separate mappings. | Define one immutable axis specification registry and derive its consumers from it. | Axis aliases, tab order, labels, line/heatmap assignments, enabled states, and range controls. |
| 2 | `_native_transforms.py` interleaves precision-sensitive differences, interpolation, and FFT calculations with PyQtGraph rendering. | Move unchanged numerical helpers into a dedicated arithmetic module and retain the existing rendering entry points. | Exact arithmetic, dtypes, exceptional values, interpolation repairs, FFT coordinate policy, and display behavior. |
| 3 | `database.py` embeds the database-access subprocess as a Python source string. | Give the probe a runnable module with an explicit argument and error contract. | Identity checks, quarantine flag, child deadline, lock-byte inspection, bounded parent timeout/cancellation, and read-only database access. |
| 4 | `database.py` owns database-info text formatting alongside SQL reads and Qt workers. | Isolate pure report/row formatting from data acquisition and worker publication. | Row order, text, byte units, local timestamp formatting, missing-value fallbacks, and all read/publication scopes. |
| 5 | `_macos_identity.py` duplicates the interpreter identity assertion and subprocess options in desktop installation and runtime-bundle creation. | Share the bounded interpreter preflight with explicit expected identity and launcher inputs. | Isolated Python arguments, environment, two-second timeout, error propagation, and each caller's signing/publication order. |

## Deliberate behavior retained

The review considered PR #120's final description and discussion. Its broad
`BaseException` cleanup at ownership/shutdown boundaries, Protocol ellipses,
Qt sorting hooks, test assertion sentinels, snapshot equality fallback, and
cross-module protocol constants remain intentional. No protocol format or
settings schema changes are needed.

The [project database-access invariant](../Agents.md) remains in force.
The probe still delegates to the existing read-only implementation. No database
handle permissions, transaction budgets, checkpoint behavior, schema handling,
or permitted SHM operations change. Settings migrations are not introduced.

These changes also retain the useful distinctions documented in PR #122:
reader ownership, query timeout caps, queue-expiry scheduling, and export
publication lifetimes are outside this refactor.

## Validation

All Python checks use the project `.venv-mac/bin/python` and
`MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache`.

- Database/probe/report checks: 149 distinct tests passed, including full
  read-only coverage, real probe subprocesses, WAL replacement lineage,
  database-info UI, and package-import checks. Three existing QCoDeS warnings
  concern missing raw data in synthetic fixtures.
- Axis/UI checks: 210 tests and 19 subtests passed, covering axis dialogs,
  filtered autoscaling, global range validation, heatmap appearance, and plot
  overlays.
- Native transform/numerical checks: 819 passed, 36 skips for extended
  `longdouble` precision unavailable on this platform. This covers FFT
  coordinate mapping/interpolation, integer and floating-point transforms,
  clipping, averaging, labels, snapping, and derivative regressions.
- Desktop/macOS identity checks: 45 passed, three Windows-only skips. Native
  Finder-open and installed/runtime bundle identity checks passed outside the
  sandbox, which otherwise prevents AppleScript compilation.
- Ruff, mypy across 137 source files, and patch checks passed.
- Real offscreen `python -m qplot` startup observed the main window and running
  event loop, then requested normal shutdown after two seconds. Settings/logs
  were isolated in a temporary directory; exit status was zero. This check
  required local process permissions for shutdown-supervisor IPC.
- Wheel and sdist builds and Twine metadata checks passed. Both artifacts
  contain the three new modules with exact source bytes. The sdist includes
  the new tests and findings table. An extracted-wheel check outside the
  checkout imported the numerical/formatting helpers and ran the real probe
  subprocess against a synthetic database without changing its bytes,
  modification time, or sidecars.
- All 11 extracted numerical definitions and `NativePlotDataItem` are
  AST-identical to reviewed `main`. All 33 retained/extracted database
  definitions other than the rewritten access launcher are also AST-identical.
  Expanding the macOS helper calls reproduces all seven original functions.
- Differential comparisons matched 120 child-probe cases (including malformed
  arguments, exceptions, exact stderr, deadlines, and read flags) and 2,784
  axis-assignment cases, plus aliases and axis metadata.
- Independent review found no functional regressions. The numeric extraction
  separates arithmetic from rendering; it does not change the eager imports
  in the existing `qplot.tools` package.
