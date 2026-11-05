# Focused maintainability follow-up to PR #121

Reviewed `main` at `34b1bfb4bd107ade7096af730a879235fdc12b46` on
5 November 2026, after fetching the merge of
[PR #121](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/121).
This pass selects four bounded changes with a concrete maintenance benefit.

| # | Finding | Improvement | Preserved contract |
| --- | --- | --- | --- |
| 1 | Trusted and snapshot selection duplicate generation invalidation, metadata capture, selected-ID updates, dataset release, and signal-preserving text updates. | Share the selection mutation phase, returning named generation/metadata state. | Callback order, source/row preflight, mode-specific behavior, and every later publication fence remain explicit. |
| 2 | Eight branches in supervisor reply polling repeat the same completed-job and helper/job ownership checks. | Use one lock-held reply ownership check, with each caller's existing diagnostic. | Only the obsolete job is completed; lock scopes, session checks, IPC, deadlines, and helper cleanup retain their original order. |
| 3 | `openPlot` duplicates duplicate-window detection and construction for 1D and 2D plots. | Select the widget class once and share the launch sequence. | Parameter-object equality, subclass checks, iteration, opened/skipped counts, status text, source validation, and dataset cleanup. |
| 4 | `_measurement_dataframe` embeds nullable-integer, object, and narrow-float pandas policy in the GUI controller. | Move pure dataframe assembly into the existing `datahandling/export_data.py` module, receiving measurement columns lazily. | Column order/prefixes, exact numeric values and dtypes, extraction timing, lazy pandas import, controller method, source checks, and atomic publication. |

## Scope and intentional decisions

The read-only database invariant in [Agents.md](../Agents.md), settings
compatibility policy, and the intentional decisions in PRs #120 and #121 remain
in force. This pass changes neither database permissions nor schemas, protocol
formats, transaction budgets, shutdown behavior, or settings formats.

The review considered and deferred consolidating retired-reader ownership:
that would span plot retention, shutdown, generation, and several controller
interfaces. Broker transaction wrappers retain their different timeout caps;
queue-expiry paths retain their different schedule-recomputation points.
CSV acquisition/publication scopes retain their different close timing and
error presentation. These distinctions are useful, rather than duplicated
policy requiring a common abstraction.

PR #121's automated comments were also reviewed. The colorbar filter registry
and missing-cell color are consumed by other modules, the cache limit imports
are deliberate re-exports, and ignoring an already absent registry key during
rollback is intentional. None is a reason to remove those contracts.

## Validation

All Python checks used `.venv-mac/bin/python` with
`MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache`.

- Selection/UI group: 282 tests and two subtests passed, including eight new
  cases for nested selection and restoration of blocked signals after failure.
- Supervisor/service group: 160 tests passed, including 24 cases covering
  helper replacement, active-job replacement, and prior completion at all eight
  external polling boundaries.
- Plot/export group: 315 tests and two subtests passed. Five existing warnings
  concern reparsing extreme floating-point CSV values.
- Ruff, mypy across 134 source files, and patch checks passed.
- Real offscreen `python -m qplot` startup observed a visible main window and
  running event loop, then requested normal shutdown after two seconds. Settings
  were isolated in a temporary directory; the process exited with status zero.
- Expanding the new helper calls reproduces both selection methods and the
  supervisor polling method from reviewed `main` exactly at the AST level.
  All 53 other supervisor methods are unchanged.
- Differential comparisons matched 73 dataframe cases, including dtypes,
  values, columns, CSV text, read order, and failure timing. Another 96 plot
  launch scenarios matched source checks, widget choice, counts, status,
  errors, preview requests, and cleanup.
- Independent review found no regressions in the selection, supervisor, or
  plot/export changes; a separate 128-case dataframe comparison also matched.
