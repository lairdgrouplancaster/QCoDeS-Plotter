from types import SimpleNamespace

from qplot.windows._dataset_handle import DatasetHandle, DatasetKey
from qplot.windows._dataset_registry import DatasetRegistry


def dataset():
    closed = []
    return SimpleNamespace(conn=SimpleNamespace(close=lambda: closed.append(True))), closed


def test_eviction_observes_selection_restored_after_release(tmp_path, qapplication):
    first, closed = dataset()
    key = DatasetKey(str(tmp_path / "source.db"), "run")
    selected = [None]
    handles = {key: DatasetHandle(first)}
    registry = DatasetRegistry(handles, lambda: selected[0])

    registry.release(key, lambda: 60)
    timer = handles[key].delete_timer
    selected[0] = first  # A database view rollback restores this selection.
    timer.timeout.emit()

    assert not handles
    assert not closed
    selected[0] = None
    assert registry.close_unowned(first, context="test cleanup")
    assert closed == [True]


def test_retain_cancels_delayed_eviction(tmp_path, qapplication):
    first, closed = dataset()
    key = DatasetKey(str(tmp_path / "source.db"), "run")
    handle = DatasetHandle(first)
    registry = DatasetRegistry({key: handle}, lambda: None)
    registry.release(key, lambda: 60)
    timer = handle.delete_timer

    handle.retain()

    assert handle.users == 1
    assert handle.delete_timer is None
    assert not timer.isActive()
    registry.close_all()
    assert closed == [True]


def test_stale_discard_closes_even_selected_dataset(tmp_path):
    first, closed = dataset()
    key = DatasetKey(str(tmp_path / "source.db"), "run")
    handle = DatasetHandle(first)
    registry = DatasetRegistry({key: handle}, lambda: first)

    registry.discard(key, handle)

    assert not registry.handles
    assert closed == [True]


def test_temporary_action_cannot_close_retained_plot_dataset(tmp_path):
    first, closed = dataset()
    key = DatasetKey(str(tmp_path / "source.db"), "run")
    registry = DatasetRegistry({key: DatasetHandle(first)}, lambda: None)

    assert not registry.close_unowned(first, context="action cleanup")
    assert not closed
    registry.release(key, lambda: 0)
    assert closed == [True]


def test_remaining_plot_user_does_not_read_eviction_preferences(tmp_path):
    first, closed = dataset()
    key = DatasetKey(str(tmp_path / "source.db"), "run")
    handle = DatasetHandle(first, users=2)
    registry = DatasetRegistry({key: handle}, lambda: None)

    def unexpected_preference_read():
        raise AssertionError("The dataset is still retained")

    registry.release(key, unexpected_preference_read)
    assert handle.users == 1
    assert not closed
    registry.close_all()
