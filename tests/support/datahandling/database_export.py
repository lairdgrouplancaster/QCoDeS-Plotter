"""A real export child stalled after reporting startup to its parent."""

import threading


def blocked_backup(channel, *_args):
    channel.send(("progress", 0, 0))
    # The parent must terminate this child. The upper bound also contains a
    # broken test without involving an application database or writer.
    threading.Event().wait(30)
