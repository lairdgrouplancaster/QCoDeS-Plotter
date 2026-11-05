"""Isolated, standard-library-only reader for cloud placeholders.

Run with ``python -m qplot.datahandling._cloud_prefetch <path>``. Progress and
the final byte count go to stdout; failures go to stderr. The parent process
enforces cancellation and the overall deadline, including stalled OS reads.
"""

import argparse
import errno
import os
import time

DATABASE_CLOUD_SYNC_CHUNK_BYTES = 4 * 1024 * 1024
DATABASE_CLOUD_SYNC_STATUS_INTERVAL = 1.0
DATABASE_CLOUD_SYNC_RETRY_INTERVAL = 0.25
DATABASE_PREFETCH_STATUS_PREFIX = "QPLOT_PREFETCH_PROGRESS:"

_TRANSIENT_ERRORS = {errno.ETIMEDOUT, errno.ECANCELED}


def prefetch_file(path: str) -> int:
    """Read an existing file, retrying transient provider errors in place."""

    total = os.path.getsize(path)
    bytes_read = 0
    last_status = 0.0

    def report(force: bool = False) -> None:
        nonlocal last_status
        percent = min(100.0, bytes_read / total * 100.0) if total > 0 else 100.0
        now = time.perf_counter()
        if force or now - last_status >= DATABASE_CLOUD_SYNC_STATUS_INTERVAL:
            print(f"{DATABASE_PREFETCH_STATUS_PREFIX}{percent:.0f}", flush=True)
            last_status = now

    report(True)
    while True:
        try:
            handle = open(path, "rb")
            break
        except OSError as error:
            if error.errno not in _TRANSIENT_ERRORS:
                raise
            report(True)
            time.sleep(DATABASE_CLOUD_SYNC_RETRY_INTERVAL)

    with handle:
        while True:
            try:
                chunk = handle.read(DATABASE_CLOUD_SYNC_CHUNK_BYTES)
            except OSError as error:
                if error.errno not in _TRANSIENT_ERRORS:
                    raise
                report(True)
                time.sleep(DATABASE_CLOUD_SYNC_RETRY_INTERVAL)
                continue
            if not chunk:
                break
            bytes_read += len(chunk)
            report()

    report(True)
    return bytes_read


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Existing cloud-backed database to read")
    args = parser.parse_args(argv)
    print(prefetch_file(args.path), flush=True)


if __name__ == "__main__":
    main()
