# Error and warning dispositions

This review prioritises every error and warning in
`qplot-code-quality-9lqtf8bk.json`, exported on 2 November 2026 at 08:19 UTC.
Finding IDs and locations refer to that export; the source tree matches base
commit `0b69f50c7b4001a302d771cd344b423b7442c1d9`.

There are **28 findings: 2 errors and 26 warnings**. PR #104 targets **11
warnings** with code changes. The remaining **17 findings (2 errors and 15
warnings)** have the individual reasons below. A merged-main scan must confirm
the changes; reviewed exceptions may remain visible in GitHub until explicitly
dismissed there.

## Code changes

| Finding IDs | Rule | Resolution |
| --- | --- | --- |
| 551, 552, 555, 556, 1356, 1357 | Implicit string concatenation | Explicit constant-string addition makes each intended list entry or subprocess program clear. Bytecode and constants are identical in all four affected files. |
| 1350 | Variable defined multiple times | Remove the redundant result sentinel from the cancellation test. `pytest.raises(InterruptedError)` already fails on every normal return; kill, reap, deadline and protected-database assertions remain. |
| 79, 80 | Unreachable code | Exhaust stepped generators through a shared helper that returns `StopIteration.value`. Numerical assertions now follow an ordinary function return instead of relying on exception suppression for normal result extraction. |
| 1363, 1364 | Conflicting test-harness attributes | Explicit overrides select the intended action/lifecycle harness implementations when combining concrete mixins. The database-path parameter follows the production method contract. |

Explicit overrides follow the [CodeQL recommendation for conflicting
attributes](https://codeql.github.com/codeql-query-help/python/py-conflicting-attributes/).

## Reviewed exceptions

| ID | Severity | Location / contract | Specific reason to retain |
| --- | --- | --- | --- |
| 612 | Error | `tests/windows/test_trusted_derived_qt.py`, `_Service.__init__` | This identity-only test double must satisfy the bridge's concrete `TrustedLiveReadService` type check. Calling the real initializer would start reader workers instead of isolating bridge binding. Work is supplied by the fake coordinator. Its class docstring records this contract. |
| 613 | Error | `tests/datahandling/test_trusted_work_coordinator.py`, `_Service.__init__` | This deterministic service double supplies observations, requests, close and wait operations. The coordinator checks the concrete service type. Running the parent initializer would create real reader resources and defeat the fake service lifecycle. Its class docstring records this contract. |
| 1362 | Warning | `plotWidget` feedback mixins | Runtime MRO inspection resolves all four flagged feedback methods solely to `PlotWindowFeedbackMixin`. The other definitions are typing-only declarations, excluded from runtime. |
| 598 | Warning | `MixedPlotMixin._heatmap_colorbar_items` | Runtime inspection finds the implementation in `Plot2DLayerMixin`; the colorbar mixin's duplicate declaration exists only under `TYPE_CHECKING`. |
| 600 | Warning | `plot1d` mixins | The runtime `mouseMoved` override intentionally selects `Plot1DSnapMixin` ahead of the base plot handler. Other flagged pairs are typing declarations; actual owners are recorded in the JSON evidence. Plot/appearance tests exercise this resolution. |
| 601 | Warning | `plot2d` mixins | Runtime resolution intentionally selects snap handling from `Plot1DSnapMixin` and `change_axis` from `Plot2DSweepMixin`. Other flagged pairs are typing declarations. Replacing these with the base handlers would change cursor/sweep behavior. |
| 602 | Warning | `sweeper` mixins | Cuts deliberately use the snap mixin's `mouseMoved` override. Other flagged members resolve to the ordinary plot/feedback implementations; typing-only duplicates do not exist at runtime. |
| 456 | Warning | `trusted_live._database_header_journal_mode` | The physically read-only descriptor is closed in `finally` through `_close_preflight_file_descriptor`. That helper closes exactly once and surfaces uncertain close outcomes; preflight-close fault tests verify quarantine. The analyser does not recognise this helper as a close operation. |
| 605 | Warning | `_ColorbarColormapTableItem` ordering | This Qt widget item supplies Qt's `<` sorting hook. It is an identity-bearing widget item, not a value object requiring general rich ordering. Sorting tests exercise the intended comparison. |
| 606 | Warning | `SortableTreeWidgetItem` ordering | `QTreeWidgetItem` sorting uses the `<` override. Additional rich comparisons are outside this widget's sorting contract. Numerical run sorting is tested through the actual Qt widget. |
| 607 | Warning | Appearance-dialog constructor hook | `PlotAppearanceDialog.sync_swap_axes_control` first synchronises the base controls, then checks whether `heatmap_controls` exists before using it. The hook is safe while constructing the base dialog; mixed/trace appearance tests exercise it. |
| 1365 | Warning | `plotWidget.__init__` calls `initAxes` | This is a deliberate construction hook after shared plot/config/dataset state is established. `sweeper` sets its cut-specific identity before calling the base constructor. The heatmap override first calls the base axes setup. |
| 1366 | Warning | `plotWidget.__init__` calls `initRefresh` | The heatmap override first builds the base refresh toolbar, then inserts its colour-autoscale control. It uses already-created shared state, not the later heatmap image. |
| 1367 | Warning | `plotWidget.__init__` calls `initLabels` | The label hook runs after frame/axes setup. The heatmap override installs label/view-range controls using that established state. Real plot and appearance tests exercise construction. |
| 1368 | Warning | `plotWidget.__init__` calls `initContextMenu` | The override first creates the base menu, then connects actions to handlers. Later image-dependent operations execute when an action fires, after construction; connecting them does not execute those operations during initialization. |
| 78 | Warning | Snapshot request `_safe_equal` rejection | Arbitrary identities can raise from `__eq__` or return a non-boolean result. New tests demonstrate rejection of `RuntimeError`, `KeyboardInterrupt`, `SystemExit` and non-boolean comparisons. This exception handler is reachable and protects the Qt request boundary. |
| 1198 | Warning | Supervisor context-manager exception test | `pytest.raises` suppresses the expected body exception after the supervisor's `__exit__` observes it. The following closed/job-done/process-stop/checkpoint assertions are reachable and execute in the passing test. The warning reflects incomplete modelling of the outer test context manager. |

Qt documents the [tree item comparison
hook](https://doc.qt.io/qt-6/qtreewidgetitem.html#operator-lt) and the
[table item comparison hook](https://doc.qt.io/qt-6/qtablewidgetitem.html#operator-lt).

The JSON audit includes an entry for all 28 IDs and actual runtime method owners
for the five retained mixin findings. The additional review passed 438 tests,
including the malformed-comparison regressions and native cleanup-fault tests;
one Windows-only NTFS case was skipped locally. Generator tests were also
rechecked after the final helper annotation (63 passed). Ruff and project mypy
passed. A further 320 real plot, sweep, mixed-plot and appearance tests passed,
including 58 subtests, validating the retained constructor and mixin contracts.
