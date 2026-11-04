# Deeper review of CodeQL notes: 3 November 2026

The later [resource and callback review](code-quality-resource-lifetimes.md)
supersedes retention of lambda findings 712, 1449 and 1451. Their targets already
have suitable Qt slot signatures; direct connections also provide explicit Qt
receiver lifetime management.

This follow-up revisits notes previously retained too broadly as intentional.
It uses the 360-finding `qplot-code-quality-jn_sae33.json` export from
09:12 UTC on 3 November and source at merged-main commit
`c4e0e80c5873221386c1b239495b1fd6dc6fdaa4`.
The [review record](code-quality-imports-globals.json) identifies all 36 targets
and records the export hash. These dispositions supersede earlier retention
reasons for those findings in `code-quality-review.json`.

## Consistent imports: 29 findings in 22 files

Tests that already import an implementation module for monkeypatching now use
that same module namespace for its classes and functions. Duplicate selected
imports are removed. Inspection found no assignments or parameters shadowing
the replaced names, no monkeypatches to the replaced source attributes and no
other test modules consuming the removed test-module imports. Existing shared
fixture helpers and patch targets remain available.
After normalizing module qualification, all non-import syntax is identical in
the 15 affected worker, numerical and coordinator test files.

Native code explicitly imports the `ctypes.wintypes` submodule. This binds the
same module and type objects as the previous selected import; it does not
redefine Win32 structures or pointer types. The platform guards and lazy import
scopes remain intact. Removing import nodes from the syntax trees produces
identical before/after trees for the native shutdown, file identity, read-only
access, compiler-free validator and read-only test files. This also verifies
that embedded subprocess programs were not accidentally rewritten.

The desktop installer uses its bootstrap submodule's package binding for both
the bootstrap and package paths. The caller-owned launcher test imports the
public `run` function explicitly while retaining the entrypoint module patch.
This follows the [CodeQL import rule's recommendation](https://codeql.github.com/codeql-query-help/python/py-import-and-import-from/)
to use one consistent reference to an imported module.

## Unused globals: all six findings

| Finding | Change and ownership reason |
| --- | --- |
| 506–508 | Move the three hidden-colorbar default tuples into their sole consuming module. Their values and filtering behavior are preserved. |
| 509 | Keep shared colorbar labels private behind a formatter used by both the colormap table and appearance controls. Unknown names still display unchanged. |
| 621 | Remove the redundant exception-hook-installed flag; the saved original hook already records installation state. Repeated installation, reset and reinstall are covered by lifecycle regressions. |
| 1349 | Retain the profiling widget on its QApplication. Removing its reference would lose the required Python keepalive; explicit ownership preserves its lifetime. |

These are ownership and readability improvements, rather than deletion of all
flagged assignments. The [CodeQL unused-global guidance](https://codeql.github.com/codeql-query-help/python/py-unused-global-variable/)
also requires preserving useful side effects.

## Colorbar method contract: finding 1448

The typing-only host declaration incorrectly said `_set_colorbar_levels`
returned `None`, while the concrete implementation returns success as a boolean.
Both now declare that boolean result. The manual-range caller unpacks the
validated pair and passes two explicit endpoints, removing ambiguity about the
call's arity without changing range validation or control updates.

## Notes retained after closer inspection

The eight lambda notes are not all redundant forwarding. The plot refresh
button and action discard Qt's checked argument and look up the current refresh
method when invoked. The axis-range callback also looks up the current method.
Database refresh completion and the live UI harness defer method lookup until
signal delivery. Two fallback-test callbacks accept the service's keyword-only
`deadline` argument while constructing an unavailable request. A direct bound
method or constructor substitution would change these contracts.

The unused `Generic` import is used in
`TrustedReadRequest(Generic[_ResultT])`. Typing-only interface, Protocol and
overload ellipses remain declarations, not missing runtime implementations.
Shutdown exception handlers still require review against their individual
cleanup, publication and containment responsibilities; narrowing them as a
category would weaken those guarantees. Earlier individual audit records and
the four retained warning rationales remain applicable.

## Validation and limits

The focused checks passed 459 numerical, worker, scheduler and coordinator tests
(one platform skip), 401 native/read-only/launcher/colorbar tests and 10 subtests
(four Windows-only skips), and 119 diagnostics/colorbar tests and 17 subtests.
The last group overlaps the colorbar coverage in the 401-test run. Ruff and
project mypy (92 source files) pass. qPlot and `scripts/time_stress.py` each stayed
running for an eight-second offscreen startup check and were then terminated.

The native Windows cases still require Windows CI. No GitHub finding was
dismissed or suppressed. A successful scan of merged main must establish which
findings clear. The new assert-side-effect error in this export is addressed
separately by PR #107; this follow-up is based on main and does not duplicate it.

## Fresh export: 3 November, 15:57 UTC

`qplot-code-quality-pcugdsiq.json` contains **325 findings: 320 notes, four
warnings and one error**. All 29 mixed-import findings and all six unused-global
findings have cleared. Counts for the other rule categories are unchanged;
several finding IDs moved with source locations.

Signature note 1448 remains: its message identifies the auto-level call in
`_plot2d_colorbar.py`, not the manual-range caller previously clarified. That
call and the two remaining manual-level calls in `plot2d.py` now unpack the
pair and pass both endpoints explicitly. The previous boolean return-contract
correction remains in place. No color-range policy or validation changes.

The exported assertion error, now 1461, refers to the old line 79 in
`test_plot_initialization.py`. Main already contains PR #107's fix: `close()`
and `load_file()` execute before their assertions, and both survive compilation
with Python optimization enabled. The two merge-triggered quality scans
overlapped: the scan of newer main `12ee805` finished at 13:55:37 UTC, while the
scan of its parent `94b41d2` finished at 13:55:58 UTC. The stale error was created
at 13:55:53 UTC. This export therefore cannot establish that the assertion fix
failed. A new scan of the latest main, without an older scan finishing after
it, is needed to settle the alert state.

Validation of the caller follow-up: 116 colorbar, appearance and initialization
tests and 10 subtests passed; Ruff, project mypy and an eight-second offscreen
qPlot startup check passed. The 22 variable-array record tests also passed
locally. A current-main Windows Python 3.14 CI job failed one variable-array
plot/export case; that job was rerun to check reproducibility. A local pass
does not establish that the Windows failure is fixed.

### Hidden heatmap viewport race

The unchanged Windows retry passed all 3,244 tests and 45 subtests (81 skips),
confirming that the variable-array failure was intermittent. Further tracing
found a concrete pre-paint race: a hidden heatmap's ViewBox can still have its
data auto-range pending when the visible-data reload timer fires. Reading
`viewRange()` at that point returns the default `(0, 1)` viewport and requests
only that subset of the measurement. This can remove the outer coordinates
expected by the original full-data plot test.

Visible-range selection now resolves pending auto-range before constructing a
query. A real ViewBox/ImageItem regression deterministically requested the
incorrect subset before the fix. Its companion case verifies that explicit
user zoom remains unchanged. The original variable-array assertions remain
intact. The focused plotting suites passed 130 tests and 10 subtests; another
five real-database heatmap integration tests passed, along with Ruff, project
mypy and the eight-second startup check.
