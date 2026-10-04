"""Filesystem evidence of acquisition activity, without opening SQLite files."""

import os
import stat


def source_activity_timestamp(database_path):
    """Return the latest main/WAL modification time; never use SHM or the clock.

    This is a database-wide estimate for unfinished runs, not a recorded run
    completion time. Merely viewing an idle database cannot advance it.
    """
    timestamps = []
    for path in (os.fspath(database_path), f"{os.fspath(database_path)}-wal"):
        try:
            observation = os.stat(path)
        except OSError:
            continue
        if stat.S_ISREG(observation.st_mode) and observation.st_size:
            timestamps.append(observation.st_mtime)
    return max(timestamps, default=None)
