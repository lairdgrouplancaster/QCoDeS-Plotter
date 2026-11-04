# Error and warning dispositions

The [3 November resource review](code-quality-resource-lifetimes.md) supersedes
the retention of finding 456 below. It makes descriptor cleanup explicit and
fixes process-session reuse after an interrupted close. The other three warning
dispositions remain applicable.

The latest export, `qplot-code-quality-jn_sae33.json`, was made on
3 November 2026 at 09:12 UTC, after the successful scan of merged main commit
`c4e0e80c5873221386c1b239495b1fd6dc6fdaa4`. It has **360 findings: one error,
four warnings and 355 notes**. All nine structural warnings from PR #106 cleared,
and the typing-declaration notes fell by six as expected.

## Current assertion correction

Finding **1403**, `py/side-effect-in-assert`, concerns `assert window.close()` in
the lifecycle test added in PR #106. Call `close()` first and assert its returned
boolean separately. The fixture's database load now follows the same pattern.
Both operations therefore still run when Python removes assertions.

All ten lifecycle tests and Ruff passed. An optimized-bytecode comparison proves
both calls were omitted before this correction and remain afterward. The next
merged-main scan must confirm clearance. No findings have been dismissed or
suppressed. The four reviewed warning exceptions remain below.

## Confirmed structural fixes from PR #106

## Post-construction setup

| IDs | Rule | Change |
| --- | --- | --- |
| 1365–1368 | Constructor calls overridden axes, refresh, label and context-menu methods | `openWin` constructs the entire object first, then calls the final, single-use `initialize_plot_window` method before registration/showing. The feature hooks now observe completed concrete constructors. |

Frame methods create renderers without starting work. The owner-driven setup
builds the complete control tree, marks it ready, then submits the initial read
and starts periodic monitoring. Refresh requests before readiness do nothing.
Retrying a missing initial worker reloads data without recreating renderers.
Closing a failure before the overlay exists is supported.

The lifecycle regressions cover line/heatmap/cut constructors, early and late
control failures, hidden and visible sources, setup repetition and preservation
of dataset users/deletion timers. A failed control setup submits no read and
does not register a window. An existing source remains refreshable afterward.
The real-database tests verify protected source artifacts are unchanged.

## Shared method requirements

| IDs | Rule | Change |
| --- | --- | --- |
| 1362 | Feedback mixin conflicts | Feedback, refresh and export features use one shared declaration of their required host methods. The feedback feature supplies the implementation. |
| 598 | Heatmap layer/colorbar conflict | Both features use the same heatmap-item requirement, implemented by the layer feature. |
| 600–602 | Line/heatmap/cut conflicts | A common host base replaces duplicate requirements. Concrete windows explicitly select snap cursor handling; the heatmap explicitly selects sweep-aware axis handling. |

`PlotHost` is an empty cooperative base at runtime. Its method declarations are
under `TYPE_CHECKING`; the corresponding features and plot base override those
shared declarations. This separates a requirement from an implementation rather
than defining a second method in every dependent mixin. There are six fewer
ellipsis declarations overall in the affected sources.

A before/after runtime MRO inspection found **1,424 existing method owners
unchanged** across the plot base and three concrete windows. The only existing
method-owner changes are the four deliberate Qt delegates, which invoke their
previous owners dynamically. The inspection also records the new setup method and explicit base renderer
contract.
Ruff and project mypy, including the new host module, pass. Three additional
modules outside the configured mypy set retain the same 187 existing diagnostics;
none were added.

## Reviewed exceptions

| ID | Severity | Location / contract | Specific reason to retain |
| --- | --- | --- | --- |
| 456 | Warning | `trusted_live._database_header_journal_mode` | The physically read-only descriptor is closed in `finally` through `_close_preflight_file_descriptor`. That helper closes exactly once and surfaces uncertain close outcomes; preflight-close fault tests verify quarantine. The analyser does not recognise this helper as a close operation. |
| 605 | Warning | `_ColorbarColormapTableItem` ordering | This Qt widget item supplies Qt's `<` sorting hook. It is an identity-bearing widget item, not a value object requiring general rich ordering. Sorting tests exercise the intended comparison. |
| 606 | Warning | `SortableTreeWidgetItem` ordering | `QTreeWidgetItem` sorting uses the `<` override. Additional rich comparisons are outside this widget's sorting contract. Numerical run sorting is tested through the actual Qt widget. |
| 78 | Warning | Snapshot request `_safe_equal` rejection | Arbitrary identities can raise from `__eq__` or return a non-boolean result. New tests demonstrate rejection of `RuntimeError`, `KeyboardInterrupt`, `SystemExit` and non-boolean comparisons. This exception handler is reachable and protects the Qt request boundary. |

Qt documents the `<` sorting hook for
[tree items](https://doc.qt.io/qt-6/qtreewidgetitem.html#operator-lt) and
[table items](https://doc.qt.io/qt-6/qtablewidgetitem.html#operator-lt).
Full rich ordering would introduce equality semantics unrelated to widget
sorting. The descriptor helper closes exactly once and reports uncertain
cleanup; retrying close can affect a reused descriptor. Executing regressions
cover arbitrary comparisons that raise or return non-boolean objects.

The new unused-import note for `Generic` is not actionable: the existing
`TrustedReadRequest(Generic[_ResultT])` declaration uses it. The Protocol and
typing-only declarations remain intentional interfaces.

Structural-change validation: the complete window and widget suites passed **2,965 tests and
82 subtests**, with 55 platform/feature skips. A final focused recheck passed
11 tests covering the lifecycle and real line/heatmap/cut plotting. Ruff and
project mypy (92 files) passed, and qPlot remained running through an eight-second
offscreen startup check on the final source.
