# Code quality review: 1 November 2026

For the current review and refactoring, see the
[focused follow-up to PR #121](maintainability-followup-121.md), the
[5 November follow-up to PR #120](maintainability-followup-120.md) and the
[4 November maintainability review](#maintainability-review-4-november-2026).
The earlier scan counts below are historical.

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

## Follow-up: 2 November 2026

The fresh `qplot-code-quality-9lqtf8bk.json` export contains 447 findings. This
follow-up re-evaluates retained findings for readability and targets 48 more:

| Rule | Findings | Change |
| --- | ---: | --- |
| Unused local variable | 35 | Select needed fixture fields; remove unused state/result assignments; mark the ignored eviction key conventionally. |
| Unused global variable | 1 | Remove the testdata provenance-prefix alias and import its defining constant in the test. |
| Implicit string concatenation | 6 | Make deliberate concatenation explicit in help text, messages and subprocess programs. |
| Mixed returns | 1 | Make the operation lookup's failing path an explicit unittest failure exception. |
| Variable defined multiple times | 1 | Remove a redundant cancellation-test result sentinel; the expected-exception assertion already rejects normal returns. |
| Unreachable code | 2 | Return generator results through a shared exhaustion helper. |
| Conflicting attributes | 2 | Explicitly select concrete test-harness implementations. |

The follow-up entry in the JSON records the export hash, base commit, exact
finding IDs and grouped rationale. The fixture parameters retain their owner
objects throughout each test. Existing publication, cancellation, numerical and
cleanup assertions remain in place.

Validation: 685 tests and 10 subtests passed across all affected test files and
the coordinator, plot and command suites; 33 Windows timestamp cases were skipped
locally. Ruff, project mypy (91 source files), targeted mypy and the offscreen
startup check passed. The four
files changed only for explicit string concatenation produce identical Python
bytecode and constants. Explicit addition follows the
[CodeQL recommendation](https://codeql.github.com/codeql-query-help/python/py-implicit-string-concatenation-in-list/).

A fresh scan of the merged default branch is required to confirm the reduction.

The [error and warning review](code-quality-errors-warnings.md) covers all 28
higher-severity findings individually: 11 code fixes and 17 reviewed exceptions
with concrete reasons and supporting evidence. The JSON records the same
dispositions and runtime mixin method owners. The additional review passed 438
tests, including new malformed-comparison tests that demonstrate why a flagged
exception handler must remain.


## Follow-up to the 399-finding export

The `qplot-code-quality-k6zxb1vu.json` export confirms all 48 targets from PR #104
have cleared. This follow-up targets another **29 existing findings: both
remaining errors, two warnings and 25 notes**. The derived-work service contract
removes partly initialized concrete-service inheritance from test doubles. The
appearance base constructor no longer calls into incomplete subclass controls.
Reader/supervisor test lifetimes use context managers while retaining cleanup
faults, close timing and protected-database assertions.

The latest [error and warning review](code-quality-errors-warnings.md) records
all 17 high-severity findings and the specific reasons for retaining 13 warnings.
The JSON records the new export hash, base commit and exact targets. New request
Protocol declarations may add informational findings; net counts require the
next scan. No findings were dismissed or suppressed.

Validation: 642 tests and 29 subtests passed, with one Windows-only skip. Ruff,
project mypy (91 files), and an eight-second offscreen qPlot startup passed.


## Structural-warning follow-up to the 374-finding export

The export `qplot-code-quality-_acaoijs.json` confirms all 29 targets from PR #105
cleared: 374 findings remain, comprising zero errors, 13 warnings and 361 notes.
This follow-up addresses the nine constructor-hook and mixin-conflict warnings.
It separates construction, control creation and initial read submission, shares
host requirements across features, and makes the four deliberate Qt handler
choices explicit. The latest [dispositions](code-quality-errors-warnings.md)
explain the four retained warnings and the lifecycle/MRO evidence. No findings
were dismissed or suppressed; the next merged-main scan must confirm clearance.


Validation for the structural follow-up: the full window/widget suites passed
2,965 tests and 82 subtests, with 55 platform/feature skips. The final focused
recheck passed 11 tests. Ruff, project mypy (92 files), the runtime owner
comparison and an eight-second offscreen startup check passed. Three additional
modules outside the configured mypy set retain the same 187 existing diagnostics;
none were added.


## Assertion correction: 3 November 2026

The fresh `qplot-code-quality-jn_sae33.json` export confirms all nine structural
warnings cleared and six fewer typing-declaration notes. It contains 360 findings:
one new test error, the four reviewed warnings and 355 notes.

The new error is `assert window.close()` in the lifecycle regression. The close
now runs before its return value is asserted; the fixture's database load follows
the same rule. All ten lifecycle tests and Ruff passed. Optimized bytecode omits
both original calls and retains both corrected calls. The JSON records finding
1403, the export hash and the exact base commit. Clearance requires the next
merged-main scan; no findings were dismissed or suppressed.

## Maintainability review: 4 November 2026

Reviewed the export `qplot-code-quality-gsvmji1o.json`, produced at
`2026-11-04T11:19:05.814753+00:00`, against checkout
`04d86a14e501868523f4f446a1ce69781311c99b`. It contains **318 findings: three
warnings and 315 notes**. This pass focuses on understandable ownership and
module dependencies, building on the earlier fault-path reviews.

### Findings addressed

1. **Layout verification had no separate state owner.** The 6,194-line
   `trusted_live_queries.py` combined metadata caching, six maps of partial and
   completed layout evidence, numeric verification, preview sampling, and shared
   value types. Adding a query operation required understanding all of them.
   `ResultLayoutQueries` now owns layout evidence, its bounded SQL, and the
   decision sequence that advances it. Executor rebinding, bootstrap reset, and
   per-run invalidation are explicit operations. The adapter is now 2,148 lines
   and concentrates on metadata/query orchestration and publication.
2. **Consumers depended on adapter internals for unrelated work.** The snapshot
   reader called the adapter's private planned-step method; cache, rendering,
   broker, and UI modules imported the adapter module for its value types.
   Shared parameter normalization now lives in `_trusted_query_metadata.py`,
   immutable contracts in `trusted_query_types.py`, and pure preview sampling in
   `_trusted_grid_sampling.py`. Internal consumers import their defining
   modules. Existing public imports through `trusted_live_queries` still work.
3. **Obsolete hooks made tests look stronger than they were.** The aggregate
   compatibility switch always returned `False`, and the planned-layout helper
   always returned `None`. They were no longer alternative implementations.
   Both are removed, along with two unused private helpers. Their tests now
   inspect the actual bounded query plan or rejection of unproven layouts by
   the adapter/renderer. The real-supervisor test no longer patches a switch
   that production never calls.
4. **Settings behavior contradicted the documented compatibility policy.**
   Startup still repaired selected obsolete preference formats through
   `_migrate_config`, including a removed axis setting. That special path is
   removed. Incompatible settings now consistently use the existing validated
   defaults, original-file backup, and atomic replacement path. Current valid
   settings retain their values. Backup/write failures still allow startup with
   defaults in memory and leave the original file intact.

The [architecture map](architecture.md#data-loading) describes where future
changes belong. The change introduces no additional mixin hierarchy or dynamic
method forwarding. Verification snapshots exposed to the adapter are immutable;
mutable verification maps stay with their owner.

The extraction preserves the bodies of 107 existing definitions after
accounting for moved calls and names. A syntax-tree comparison also verified the
ordering of the extracted layout-decision statements. Query budgets, numeric
algorithms, transaction boundaries, and source-revision fingerprint payloads
remain unchanged. The low-level reader, VFS, and process-containment code are
unchanged.

### GitHub findings retained deliberately

| Findings | Disposition |
| --- | --- |
| Warnings 605 and 606: incomplete ordering | Qt uses `__lt__` as its item-sorting hook. These are widget identities, not generally ordered value objects. Adding `__le__`, `__ge__`, or value equality would invent unrelated semantics. |
| Warning 78: unreachable handler | `_safe_equal` calls arbitrary equality implementations. Existing regressions exercise `RuntimeError`, `KeyboardInterrupt`, `SystemExit`, and non-boolean returns. The rejection handler is reachable and necessary. |
| 233 notes: `BaseException` handlers | Preserve the reviewed cancellation, cleanup, failure-publication, and process-containment boundaries. Replacing these with `Exception` can bypass ownership retention on interruption. The prior resource review documents concrete failures of that kind. |
| 70 notes: ineffectual statements | These are typing-only, Protocol, or overload declarations. Keeping explicit type contracts is more useful than replacing conventional ellipses to satisfy this rule. Four executor declarations move with their contract. |
| Five notes: lambdas | One callback intentionally looks up the current autoscale method; two Qt test adapters bridge plain harness methods; two request factories accept the service's `deadline` keyword. Replacing them with extra forwarding functions would add indirection. |
| Two notes: complex destructors | Fallback cleanup handles partially initialized or abandoned reader/process owners. Explicit close remains the normal API. Moving the same statements to another helper would hide the lifetime policy. |
| Four notes: special-method exceptions | Test sentinels reject forbidden truthiness or over-budget binding access. Their assertion failures are intentional. |
| One note: unused `Generic` | `TrustedReadRequest(Generic[_ResultT])` uses this import. Removing it would break the generic request declaration. |

No finding has been dismissed or suppressed, and this refactor does not promise
an alert-count reduction. These remaining categories express useful contracts
or safety boundaries; the substantive maintenance improvement is independent of
the scan score. A fresh GitHub scan is needed to establish its resulting alert
state.

### Validation

The full macOS regression run completed with **6,519 tests and 130 subtests
passing**, 138 platform/feature skips, and one test-adapter failure. That test
patched the source-revision randomness through the old query module; it now
imports the defining value-type module. The final focused run passed **251
tests and 30 subtests**, covering the complete scheduler and query test files,
the settings changes, recovery failures, and preferences. No observed failure
remains unresolved.

New query regressions verify that an executor replacement resumes at the next
4,096-row page, bootstrap discards both pending and completed evidence, and a
changed run identity restarts only that run's verification. The rewritten
settings tests verify backup fidelity, validated defaults, failed writes,
read-only directories, and preservation of current settings without rewriting
them on startup.

Repository-wide Ruff, project mypy (96 source files), and `git diff --check`
pass. qPlot remained running through an eight-second offscreen startup check;
its launcher exited after the check requested termination. The full suite
required local socket access for the shutdown-supervisor tests. Native Windows
and platform-specific numeric cases remain subject to their normal CI jobs.
