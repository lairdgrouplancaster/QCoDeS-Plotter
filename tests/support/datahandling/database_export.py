"""Real export children stopped at observable failure boundaries."""

import os
import threading
from pathlib import Path


def _private_files(output):
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(output + suffix).write_bytes(b"private export scratch")


def blocked_backup(channel, _instance, _live, output, *_args):
    _private_files(output)
    channel.send(("progress", 0, 0))
    # The parent must terminate this child. The upper bound also contains a
    # broken test without involving an application database or writer.
    threading.Event().wait(30)


def exiting_backup(channel, _instance, _live, output, *_args, released, outcome):
    _private_files(output)
    channel.send(("progress", 0, 0))
    if not released.wait(10):
        raise RuntimeError("The parent did not observe the export child")
    if outcome == "missing-result":
        channel.close()
        return
    if outcome == "unsuccessful-exit":
        channel.send(("done",))
        channel.close()
        os._exit(7)
    raise AssertionError(outcome)
