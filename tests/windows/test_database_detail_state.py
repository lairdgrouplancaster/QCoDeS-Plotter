from types import SimpleNamespace

from qplot.windows._database_actions import DatabaseActionsMixin
from qplot.windows._database_detail_state import detail_channel
from qplot.windows._shutdown_coordinator import ShutdownCoordinator


def test_shutdown_invalidates_both_channels_before_late_callbacks():
    window = SimpleNamespace(
        _database_load_generation=1,
        show_status=lambda *_args: (_ for _ in ()).throw(AssertionError("late status")),
    )
    basic = detail_channel(window)
    expensive = detail_channel(window, True)
    basic.generation, expensive.generation = 3, 7
    basic.active = expensive.active = True
    basic.worker = expensive.worker = object()

    ShutdownCoordinator._invalidate_database_publications(SimpleNamespace(window=window))

    assert (basic.generation, expensive.generation) == (4, 8)
    assert not basic.active and not expensive.active
    assert basic.worker is None and expensive.worker is None
    DatabaseActionsMixin.database_detail_status(window, 3, "late basic")
    DatabaseActionsMixin.database_expensive_detail_status(window, 7, "late expensive")
