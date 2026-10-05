"""Measure bounded trusted reads and native validation/I/O costs; never write the source."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from qplot.datahandling.trusted_live import TrustedLiveReader
from qplot.datahandling.trusted_plot import identifier


class FullPathReader(TrustedLiveReader):
    """Diagnostic reference: retain full pathname checks on every page read."""

    def _validate_native_source(self, *, begin_reads=False):
        super()._validate_native_source(begin_reads=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("run_id", type=int)
    parser.add_argument("--pages", type=int, default=16)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--full-path-checks", action="store_true",
                        help="Disable pathname-proof reuse for a controlled reference measurement")
    args = parser.parse_args()
    if args.pages < 1 or args.repeats < 1:
        parser.error("--pages and --repeats must be positive")

    def protected_state():
        return {suffix: (path.stat().st_size, path.stat().st_mtime_ns)
                for suffix in ("", "-wal", "-journal")
                if (path := Path(str(args.database) + suffix)).exists()}

    before = protected_state()
    results = []
    reader_type = FullPathReader if args.full_path_checks else TrustedLiveReader
    with reader_type.open(args.database) as reader:
        table, = reader.query("SELECT result_table_name FROM runs WHERE run_id=?",
                              (args.run_id,)).rows[0]
        table = identifier(table)
        columns = reader.query(f"SELECT * FROM {table} WHERE 0").columns
        numeric = [identifier(name) for name in columns if name != "id"]
        # Read all numeric fields, with only a small aggregate crossing the
        # helper boundary. Match the plot's finite primary-key intervals.
        expressions = ", ".join(f"MIN({name}), MAX({name})" for name in numeric)
        for repeat in range(args.repeats):
            audit_before = dict(reader.audit().counters)
            started = time.perf_counter()
            checksum = hashlib.sha256()
            first_page = None
            for page in range(args.pages):
                rows = reader.query(
                    f"SELECT COUNT(*), {expressions} FROM {table} WHERE id>? AND id<=?",
                    (page * 65_536, (page + 1) * 65_536), timeout=5,
                ).rows
                checksum.update(json.dumps(rows, separators=(",", ":")).encode("utf-8"))
                if first_page is None:
                    first_page = rows
            elapsed = time.perf_counter() - started
            audit = {key: value - audit_before[key]
                     for key, value in reader.audit().counters.items()}
            results.append({"repeat": repeat + 1, "elapsed_seconds": elapsed,
                            "audit_delta": audit, "first_page": first_page,
                            "result_sha256": checksum.hexdigest()})
    after = protected_state()
    if before != after:
        raise AssertionError("Protected source file sizes/timestamps changed")
    print(json.dumps({"run_id": args.run_id, "pages": args.pages, "results": results,
                      "full_path_checks": args.full_path_checks,
                      "protected_file_stats_unchanged": True}, indent=2))


if __name__ == "__main__":
    main()
