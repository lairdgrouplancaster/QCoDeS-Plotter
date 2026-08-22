# Trusted live QCoDeS reader

`qplot.datahandling.trusted_live` is the completed Stage 2 reader boundary for
inspecting an actively written QCoDeS WAL database without copying it. It sees
the latest committed transaction, including pages that exist only in the WAL,
and never exposes an APSW connection or cursor to its caller.

The boundary is deliberately not connected to qPlot's application loading,
workers, previews, plots, refresh scheduler, or thumbnail pipeline. Those paths
continue to use `readonly.py` and its private snapshots. A persistent helper
process, UI scheduling, and hard process-level recovery remain later stages.

## Source-file boundary

The reader uses pinned APSW 3.53.4.0 and its SQLite 3.53.4 build. A small native
VFS shim is loaded into that SQLite instance and delegates locking and WAL
coordination to the platform's base VFS. SQLite therefore uses the real,
colocated WAL index and the writer's native operating-system lock domain; the
reader does not use `immutable=1`, `nolock`, raw WAL parsing, a copied snapshot,
or a private SHM mirror.

The operating-system handles used for the main database and WAL are physically
read-only and non-creating. The same protection applies to any rollback journal:
qPlot never creates, modifies, truncates, renames, deletes, or recovers it. The
native boundary rejects source writes, truncation, deletion, writable mapping,
mutating file controls, journal creation, and any fallback to a read/write
connection. The SQL layer additionally enables `query_only`, defensive mode and
`trusted_schema=OFF`, disables checkpoint-on-close, keeps temporary state in
memory or a private temporary directory, and uses a deny-by-default authorizer.
Checkpoints, journal-mode changes, migrations, `VACUUM`, `ATTACH`, and other
mutating SQL are rejected.

qPlot does not checkpoint the selected database and does not write experimental
data. The main database, `-wal`, and `-journal` remain read-only for the lifetime
of the reader.

SQLite normally asks a WAL VFS to open `-wal` with create permission even when
the connection itself is read-only. If the real WAL is initially absent, the
native boundary instead creates a zero-length placeholder in qPlot's private
temporary directory and gives SQLite a read-only handle to that private file.
Immediately before each finite transaction it checks the real WAL pathname. If
a writer has created the WAL, the same native file object is promoted to a
proved, physically read-only handle for that exact WAL before the transaction
can begin. The real `-wal` is never created by qPlot, and the placeholder is not
a database or WAL copy.

## Sole SHM exception

`<database>-shm` is transient SQLite coordination state, not experimental data.
It is the only source-side file the trusted reader permits SQLite to mutate. For
that exact regular, colocated path, the pinned SQLite VFS may:

- create or open the file when a WAL transaction needs it;
- read and write WAL-index structures and reader marks;
- extend, resize, or truncate it as SQLite requires;
- create shared read/write mappings;
- take and release SQLite SHM locks and issue memory barriers; and
- recover or reconstruct WAL-index headers and hashes.

Consequently, reading a live database may change the SHM file's contents, size,
timestamps, permission mode, or other metadata. The pinned SQLite Unix VFS may,
for example, use `fchmod()` to normalise the exact SHM file's mode while opening
it. Those changes are expected and are not treated as source replacement. The
SHM pathname and filesystem object must still match the selected database
family. The native VFS never extends this permission to the main database, WAL,
rollback journal, or an arbitrary path.

Application code never writes or changes the SHM directly. Any permission-mode
normalisation is performed only inside the pinned SQLite VFS. Elevated POSIX
execution is rejected, preventing SQLite's root-only ownership normalisation.
Elevated Windows execution is rejected as well, so every trusted live session
runs in an unprivileged process.
qPlot does not rename or delete the SHM, and native `xShmUnmap` delegation
always uses `deleteFlag=0`, so closing the trusted reader does not request SHM
deletion. A writer or another ordinary SQLite owner may independently manage
its sidecars.

## File identity and accepted sources

The reader binds the expected source identity, a retained proof handle, and the
actual handle used by SQLite. Repeated pathname `stat()` results are not used as
a substitute for that proof:

- On POSIX, device and inode values come from `fstat()` on the retained proof
  descriptor and SQLite's real descriptor.
- On Windows, volume identity and file ID come from the retained proof handle
  and SQLite's real operating-system handle.

The binding is checked at open and transaction boundaries. A sidecar may
legitimately appear between operations, but an operation never silently changes
to another inode or file ID. Replacing the main database, WAL, or SHM invalidates
the reader and requires an explicit reopen. SHM content, size, `mtime`, and
`ctime` changes alone do not invalidate it.

Only regular files with correctly named, colocated sidecars on supported
same-host local filesystems are accepted. Symbolic links, Windows reparse-point
escapes or alternate data streams, non-regular files, multiply linked SHM
objects, malformed or non-colocated sidecars, UNC/network paths, unsupported
platforms or filesystems, incompatible APSW/SQLite builds, and elevated POSIX
root or Windows execution fail closed before a trusted session is established.

The current filesystem allowlist is deliberately explicit: local APFS and HFS
on macOS; NTFS on Windows; and ext2/3/4, XFS, btrfs, tmpfs, ramfs, overlayfs,
and OpenZFS on Linux. A filesystem being mounted locally is not by itself
sufficient. Expanding this list requires platform-specific evidence for file
identity, shared mapping, and SQLite locking semantics.

## Finite operations and errors

`TrustedLiveReader.open()` returns an owner-thread-bound reader with a finite
five-second operation default. `query()` materialises one read-only statement
inside a promptly ended transaction; callers can supply `bindings`, `timeout`,
`deadline`, and `cancel_event`. `query_batch()` accepts an already bounded
sequence of immutable
`TrustedQuery` specifications and materialises them in one repeatable-read
transaction; it accepts no callback or cursor that could hold the transaction
idle. Transactions are intentionally short so they do not indefinitely delay
writer checkpoints or WAL resets.

Relative timeouts, absolute monotonic deadlines, cancellation events, and the
cross-thread `interrupt()` method cooperatively interrupt SQLite work. Busy
handling is separately bounded and cannot outlive the operation deadline. On
every success, policy rejection, timeout, cancellation, query error, source
change, close, and partial-open failure, the reader attempts rollback and checks
every wrapper-visible cleanup result. If complete lock and proof-state release
cannot be established, it force-closes what it can, raises
`TrustedLiveCleanupError`, and permanently quarantines the in-process reader
singleton. No later trusted session is then allowed in that process; process
exit is the final recovery boundary for any state whose release was uncertain.

The public surface does not return connections, cursors, writable row objects,
or an indefinitely idle transaction. Its error taxonomy distinguishes:

- policy rejection (`TrustedLiveSqlRejectedError`);
- a proven identity replacement (`TrustedLiveSourceChangedError`);
- bounded lock expiry (`TrustedLiveBusyTimeoutError`);
- cancellation and deadline expiry (`TrustedLiveCancelledError` and
  `TrustedLiveDeadlineExceededError`);
- unsupported sources and ordinary source I/O
  (`TrustedLiveUnsupportedSourceError` and `TrustedLiveSourceIOError`);
- an unavailable pinned runtime or native boundary
  (`TrustedLiveReaderUnavailableError`);
- corrupt or invalid databases (`TrustedLiveInvalidDatabaseError`);
- ordinary allowed-query failures (`TrustedLiveQueryError`); and
- cleanup that cannot be proved complete (`TrustedLiveCleanupError`).

A source-change error is reported only after an identity check proves a change.

SQLite interruption can stop SQLite virtual-machine work and unwind the reader's
transaction. In-process Python cannot forcibly pre-empt arbitrary caller code
that has stalled outside SQLite. The API is therefore designed for finite jobs;
the later helper-process stage will provide the hard process-level failure
boundary.

The reader token and captured source identity are available for future callback
binding. Normally, `close()` or the context manager releases the session
cleanly; the quarantine rule above applies if that release cannot be proved.
The native audit counters separate prohibited main/WAL/journal operations from
the allowed SHM coordination operations used by tests and diagnostics.

## Deliberate limits

- The native control channel permits one trusted reader per process. Close that
  reader before opening another; the later helper-process architecture will
  provide concurrency by assigning finite jobs to isolated reader processes.
- The reader is a trusted same-host viewer, not a sandbox against a hostile
  process continuously replacing files or redirecting parent path components.
  Preflight, retained-proof, actual-handle, and operation-boundary checks detect
  observed replacements; they are not a security boundary against adversarial
  namespace substitution. The selected local path hierarchy is assumed trusted.
- A writer may legitimately remove and recreate WAL sidecars between
  transactions. A bound reader fails closed on an identity change; callers must
  close it and explicitly open the new source incarnation.
- Windows and POSIX native behavior must pass platform CI before a source
  revision is accepted across platforms. The workflow exercises Linux, ARM64
  macOS, Intel macOS, and unprivileged Windows, but coverage is not considered
  passed until all four hosted jobs succeed for that exact revision. Windows
  and macOS remain qPlot's supported GUI desktop platforms; the Linux wheel is
  a reader and packaging reference rather than a declaration of Linux GUI
  support.
- Changing the APSW/SQLite pin requires reviewing the platform VFS integration
  and rerunning native audit, replacement-race, concurrent-writer, and installed
  wheel tests on every supported platform.
