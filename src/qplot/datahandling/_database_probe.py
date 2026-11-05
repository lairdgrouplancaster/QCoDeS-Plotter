"""Isolated access probe for an existing database, using the read-only reader.

The parent supplies path, quarantined-WAL flag, timeout, and JSON file identity
as four positional arguments. It owns the process deadline and cancellation;
the child also enforces a read deadline and reports typed failures on stderr.
"""

import json
import sys
from time import monotonic

from qplot.datahandling.readonly import probe_read_only_database

DATABASE_ACCESS_ERROR_PREFIX = "QPLOT_DATABASE_ACCESS_ERROR:"


def main(argv: list[str] | None = None) -> int:
    """Run the parent-supplied probe, retaining its structured error format."""
    args = sys.argv[1:] if argv is None else argv
    try:
        expected_database_identity = json.loads(args[3])
        if expected_database_identity is not None:
            expected_database_identity = tuple(expected_database_identity)
        deadline = monotonic() + max(0.0, float(args[2]))
        probe_read_only_database(
            args[0],
            ignore_unpaired_wal=(args[1] == "1"),
            expected_database_identity=expected_database_identity,
            deadline=deadline,
            _check_sqlite_lock_bytes=True,
        )
    except Exception as err:
        payload = {"type": type(err).__name__, "message": str(err)}
        print(DATABASE_ACCESS_ERROR_PREFIX + json.dumps(payload), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
