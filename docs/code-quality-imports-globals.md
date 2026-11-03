# Deeper review of CodeQL notes: 3 November 2026

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
