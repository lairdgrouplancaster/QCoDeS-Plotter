"""Arrange owned jobs without making widget observations writable."""

from qplot.windows._widgets._preview_controller import PreviewJob


def seed_preview_job(tab, guid, worker, priority=0):
    job = PreviewJob(tab.generation, guid, priority, worker, starting=False)
    tab._preview_controller._jobs[job.key] = job
    return job
