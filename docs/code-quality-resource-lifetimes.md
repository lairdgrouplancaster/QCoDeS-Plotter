# Resource and callback review: 3 November 2026

This follow-up examines `qplot-code-quality-go2fimqj.json` (323 findings: four
warnings and 319 notes), against merged main `2e7d73c`. It supersedes the
earlier retention reasons for the five findings below. Clearance still requires
a successful scan of the merged changes; no alert is suppressed or dismissed.

## Header descriptor: finding 456

The ordinary read path already closed its descriptor in `finally`, through a
helper used only there. The close is now directly visible next to the read, and
the single-use helper is removed. Read-only flags and the bounded header read
are preserved. The application never opens this source handle for writing.

Closer fault testing found a real gap in the old failure path. The helper
translated `OSError` into uncertain preflight cleanup, but allowed
`KeyboardInterrupt` and `SystemExit` to escape that classification. An
interruption before the descriptor closed could therefore leave a descriptor
open while releasing the process's trusted-reader session ownership.

Any failure of the close attempt now produces the existing uncertain-cleanup
signal, with the original exception as its cause. The reader constructor blocks
further trusted reads in that process until it exits. It never retries the
close: an interrupted close may already have consumed the descriptor number,
which could subsequently belong to another file. Python makes this same
distinction for close in [PEP 475](https://peps.python.org/pep-0475/#modified-functions).

Two disposable-process regressions failed before the fix: the first error was
`KeyboardInterrupt` or `SystemExit`, instead of the cleanup error that prevents
process reuse. Both now verify the cleanup error and rejection of a subsequent
reader. Additional boundary tests exercise fragmented reads, ordinary read
errors, interrupted reads, exactly one close attempt and uncertain close
outcomes. The disposable fault processes intentionally model an unclosed handle;
process exit releases it.

This fixes a failure-path ownership defect. It does not promise that an OS close
can never fail; such a promise would be impossible to enforce safely.

## Supervisor lifetime: finding 1378

The buffered-reply close test now owns its supervisor in a `with` block. It uses
the existing `shutdown_timeout_seconds=2.0` setting, so both its explicit close
and context cleanup retain the original two-second shutdown deadline. The test
still proves that an already-buffered reply completes without a cancellation
race, checks the result, and verifies the helper stopped. The context manager
also preserves a body failure if cleanup reports a second error.

## Qt callback ownership: findings 712, 1449 and 1451

Database completion connects directly to the existing five-argument Qt slot.
The refresh button and action connect directly to the existing zero-argument Qt
slot. Their declared signatures already handle the signal arguments; the
forwarding lambdas are unnecessary.

The direct connection also gives Qt an explicit receiver lifetime. A regression
destroys the QObject receiver while retaining the worker's signal object and
checks that its completion connection is removed. With the original lambda,
that test fails because one connection remains; with the direct slot it passes.
Another regression emits `clicked(True)` through the real button and verifies
that it invokes the source refresh correctly. The supporting test harnesses now
provide the required slot and QObject ownership, matching real windows.

Qt documents removal of connections when their
[sender or receiver is destroyed](https://doc.qt.io/qt-6/signalsandslots.html#automatic-connection-management).

## Other remaining categories

| Category in this export | Closer check and disposition |
| --- | --- |
| 70 statements with no effect | Each flagged ellipsis was matched to its syntax-tree context: 46 are inside `TYPE_CHECKING`, 22 are Protocol declarations, and two are overload declarations. These describe method requirements or types. None is a missing runtime implementation. |
| 233 `BaseException` handlers | Revisited launcher/Windows-job containment, reader transaction cleanup, service failure publication and Qt shutdown handlers. Their failure bodies retain ownership, restore state, reject work, report a fatal service failure or reap processes. Existing individual records remain in `code-quality-review.json`. The new header-close defect illustrates why interruptions must also reach cleanup and containment. The new close handler may add a justified informational finding. |
| Five remaining lambda notes | One axis callback deliberately looks up the current autoscale implementation; an existing monkeypatch regression exercises that behavior. Two preview-test callbacks adapt decorated methods on a plain test harness to Qt signals. Two fallback factories accept the service's keyword-only `deadline` while constructing a request sentinel. |
| Two complex destructors | The finalizers are fallback cleanup for abandoned native readers and helper processes. Explicit `close` and context managers remain the normal ownership APIs. Existing abandoned-reader, abandoned-supervisor and partial-job regressions verify release of sessions, temporary directories, helpers and receiver threads. Moving statements into another helper merely to reduce the destructor's apparent complexity would not improve ownership. |
| Four special-method exception notes | These test sentinels raise assertion failures if forbidden dataset truthiness or oversized binding access occurs. Replacing those failures with ordinary lookup/type exceptions would weaken the tests' diagnostics. |
| One unused-import note | `Generic` is used explicitly in `TrustedReadRequest(Generic[_ResultT])`; removing it would break that declaration. |

The two Qt-ordering warnings and defensive-comparison warning retain their
specific explanations in `code-quality-errors-warnings.md`. This review has not
proved that every possible resource failure is covered; the regression failures
above provide concrete evidence for the changes in this batch.

## Validation

The final affected reader, supervisor, partial-frame, callback, main-window and
plot integration suites passed 542 tests and 21 subtests, with one NTFS-only
skip. Ruff, project mypy (92 source files), and an eight-second offscreen qPlot
startup check passed. Both interrupted-close regressions and the receiver
destruction regression were demonstrated failing with their earlier behavior.
Native Windows behavior still requires CI validation.
