# Error and warning dispositions

The latest review uses `qplot-code-quality-k6zxb1vu.json`, exported on
2 November 2026 at 16:05 UTC, against merged-main commit `4bfcaa38d931876f90be191833e43905f787f6e3`.
Its **399 findings** confirm that PR #104 removed all 48 targeted findings.
The previous reviews and their original locations remain in
[the JSON audit](code-quality-review.json).

This follow-up changes code for **both remaining errors and two warnings**.
The remaining **13 warnings** have the specific reasons below. These are source
review conclusions; a successful merged-main scan must confirm clearance.
GitHub findings have not been dismissed or suppressed.

## Code changes

| Finding IDs | Severity / rule | Resolution |
| --- | --- | --- |
| 612, 613 | Error: missing superclass initialization | Introduce a nominal `TrustedDerivedReadService` contract for the coordinator and Qt bridge. The concrete broker and deterministic doubles implement it independently. Doubles no longer inherit uninitialized broker state. Nominal type checks, exact database identity checks, session/namespace fencing and cancellation remain; replacement binding now also checks the service type before changing state. |
| 607 | Warning: constructor calls an override | The base trace dialog initializes its own swap controls directly. The mixed dialog synchronizes its additional controls after they exist. A regression subclass rejects early synchronization, proving the base constructor no longer calls into partially constructed subclass state. |

| 1198, 1347 | Warning: unreachable post-cleanup checks; note: unused job | Catch the context body exception explicitly and assert its identity after cleanup. This strengthens exception preservation and makes the job-done, helper-stop and checkpoint assertions visible to control-flow analysis. |

Six binding regressions verify rejection of unrelated objects and services bound
to another database at construction, replacement and Qt binding, without changing
the existing binding. The normal service and bridge suites exercise the concrete
broker and deterministic implementations.

## Reviewed exceptions

| ID | Severity | Location / contract | Specific reason to retain |
| --- | --- | --- | --- |
| 1362 | Warning | `plotWidget` feedback mixins | Runtime MRO inspection resolves all four flagged feedback methods solely to `PlotWindowFeedbackMixin`. The other definitions are typing-only declarations, excluded from runtime. |
| 598 | Warning | `MixedPlotMixin._heatmap_colorbar_items` | Runtime inspection finds the implementation in `Plot2DLayerMixin`; the colorbar mixin's duplicate declaration exists only under `TYPE_CHECKING`. |
| 600 | Warning | `plot1d` mixins | The runtime `mouseMoved` override intentionally selects `Plot1DSnapMixin` ahead of the base plot handler. Other flagged pairs are typing declarations; actual owners are recorded in the JSON evidence. Plot/appearance tests exercise this resolution. |
| 601 | Warning | `plot2d` mixins | Runtime resolution intentionally selects snap handling from `Plot1DSnapMixin` and `change_axis` from `Plot2DSweepMixin`. Other flagged pairs are typing declarations. Replacing these with the base handlers would change cursor/sweep behavior. |
| 602 | Warning | `sweeper` mixins | Cuts deliberately use the snap mixin's `mouseMoved` override. Other flagged members resolve to the ordinary plot/feedback implementations; typing-only duplicates do not exist at runtime. |
| 456 | Warning | `trusted_live._database_header_journal_mode` | The physically read-only descriptor is closed in `finally` through `_close_preflight_file_descriptor`. That helper closes exactly once and surfaces uncertain close outcomes; preflight-close fault tests verify quarantine. The analyser does not recognise this helper as a close operation. |
| 605 | Warning | `_ColorbarColormapTableItem` ordering | This Qt widget item supplies Qt's `<` sorting hook. It is an identity-bearing widget item, not a value object requiring general rich ordering. Sorting tests exercise the intended comparison. |
| 606 | Warning | `SortableTreeWidgetItem` ordering | `QTreeWidgetItem` sorting uses the `<` override. Additional rich comparisons are outside this widget's sorting contract. Numerical run sorting is tested through the actual Qt widget. |
| 1365 | Warning | `plotWidget.__init__` calls `initAxes` | This is a deliberate construction hook after shared plot/config/dataset state is established. `sweeper` sets its cut-specific identity before calling the base constructor. The heatmap override first calls the base axes setup. |
| 1366 | Warning | `plotWidget.__init__` calls `initRefresh` | The heatmap override first builds the base refresh toolbar, then inserts its colour-autoscale control. It uses already-created shared state, not the later heatmap image. |
| 1367 | Warning | `plotWidget.__init__` calls `initLabels` | The label hook runs after frame/axes setup. The heatmap override installs label/view-range controls using that established state. Real plot and appearance tests exercise construction. |
| 1368 | Warning | `plotWidget.__init__` calls `initContextMenu` | The override first creates the base menu, then connects actions to handlers. Later image-dependent operations execute when an action fires, after construction; connecting them does not execute those operations during initialization. |
| 78 | Warning | Snapshot request `_safe_equal` rejection | Arbitrary identities can raise from `__eq__` or return a non-boolean result. New tests demonstrate rejection of `RuntimeError`, `KeyboardInterrupt`, `SystemExit` and non-boolean comparisons. This exception handler is reachable and protects the Qt request boundary. |

Adding a full rich ordering to identity-bearing Qt items would introduce equality
semantics unrelated to Qt sorting. Qt documents its `<` sorting hook for
[tree items](https://doc.qt.io/qt-6/qtreewidgetitem.html#operator-lt) and
[table items](https://doc.qt.io/qt-6/qtablewidgetitem.html#operator-lt).

The five mixin warnings largely compare typing declarations with their runtime
implementations. The recorded runtime method owners and plot tests support their
current behavior. Adding dozens of forwarding methods solely to silence these
reports would obscure that composition. The four plot initialization hooks are
deliberate template methods used by heatmaps and sweeps after their required
shared state is established. The file-descriptor close helper deliberately
closes exactly once and reports uncertain cleanup; retrying close can close an
unrelated reused descriptor. The remaining unreachable-code report has executing regressions for
malformed comparisons.

## Informational findings

Twenty-four `should-use-with` notes lead to simpler test ownership scopes. The
unused-job note is addressed alongside the context-body exception warning. The
cleanup keeps fault injection, explicit close operations under test, post-close
checks and protected database assertions. It also moves setup observations inside
the managed lifetime. The one retained note uses an explicit two-second close
bound in both the tested operation and its fallback cleanup; a default context
exit would change that bound. New request Protocol declarations are intentional
interfaces and may themselves appear as informational ineffectual statements.

Validation: **642 tests and 29 subtests passed**, with one Windows-only NTFS
case skipped on macOS. Project mypy passed on 91 source files, Ruff passed, and
qPlot remained running through an eight-second offscreen startup check.
