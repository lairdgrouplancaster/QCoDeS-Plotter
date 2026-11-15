"""Shared fixtures and assertions for live acquired point counts."""

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_live_service import TrustedLiveReadService

__all__ = [
    '_open_reader',
]


def _open_reader(path):
    service = TrustedLiveReadService(
        path,
        expected_database_instance=database_instance(path),
        request_timeout_seconds=30,
    )
    try:
        bootstrap = service.submit_bootstrap().wait(30)
        service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30)
    except BaseException:
        service.close(timeout=30)
        raise
    return service
