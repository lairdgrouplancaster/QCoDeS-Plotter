# Code quality review: 1 November 2026

This review covers all **596 open findings across 27 rules** in the export made
at `2026-11-01T17:53:20.029278+00:00`. It supersedes the earlier preliminary
screening. The [individual review record](code-quality-review.json) contains each
finding's number, rule, original location, enclosing scope, source excerpt,
disposition and rationale. Exception findings also include their protected and
failure actions, so the reasons can be checked against the actual code.

The export does not contain scan commit metadata. Before review, the successful
main scan was separately checked against
`4d18f13391b16bf9c20bc11a1a60e59895de9805`; source excerpts and line numbers refer
to that commit. The export's SHA-256 is recorded in the JSON. Locations in newer
commits may have moved.

| Disposition | Findings | Result |
| --- | ---: | --- |
| Cleanup in this PR | 24 | Remove redundant code, clarify return contracts and test adapters, move operations outside assertions, simplify fixture generation. |
| Intent clarified in this PR | 125 | Explain 111 deliberate empty handlers and related broad catches; document Qt comparison hooks and variable-length rectangle collections. |
| Cleanup in [PR #101](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/101) | 12 | Unused imports/re-exports and explicit NaN checks. |
| Defects fixed in [PR #102](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/102) | 4 | Retain helper ownership through failed startup, interrupted retirement and failed closes of parent-held startup pipe copies. Four findings lead to three related resource-ownership defects. |
| Retained with rationale | 431 | Intentional interfaces, test sentinels, explicit cleanup ownership, signal argument adaptation and shutdown containment. |
| **Total** | **596** | No findings left unclassified in this export. |

The 73 ineffectual-statement findings are ellipsis declarations in
`TYPE_CHECKING` interfaces, `Protocol` methods or `@overload` signatures. They are
not unfinished production implementations. Broad exception handlers were traced
through failure publication, cleanup, quarantine and process containment rather
than narrowed mechanically. The review found the additional ownership defects
above while doing that tracing.

The original NumPy expression reported as a non-iterable is a valid ndarray.
The fixture now generates its rows with `range(64)` and scalar arithmetic, making
the iteration contract explicit. The retained self-contained test sentinels and
typing declarations are explained individually in the JSON; no new query
suppression or GitHub dismissal was added.

Validation for this PR: 1,008 automated tests passed across the focused database,
UI, numerical and launcher suites, plus 41 subtests. Ruff passed on every changed
Python file, and qPlot stayed running through an eight-second offscreen startup
check. Targeted mypy checking found the same 11 existing errors in `database.py`
as a check using the pre-change file; the other three checked files passed.
PR #102 separately passed all 81 supervisor tests, Ruff, targeted mypy and the
startup smoke check, with regression failures demonstrated before its fixes.

Native Windows behavior still needs Windows CI. These dispositions are a source
review, not proof that the program has no other defects. After merging the PRs,
wait for a successful scan of the merged `main` commit and export a fresh report.
Only that report can establish which GitHub findings have cleared or changed.
GitHub alert state was not modified by this review.
