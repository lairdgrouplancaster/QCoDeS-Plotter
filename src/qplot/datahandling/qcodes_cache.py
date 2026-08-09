"""
Compatibility helpers for QCoDeS dataset cache internals.

qPlot needs per-parameter refreshes that QCoDeS does not expose as a stable
public API. Keep those private-attribute touches in this module so future
QCoDeS upgrades have one place to adapt.
"""
from threading import Lock, RLock

_CACHE_LOCK_CREATION = Lock()


def cache_lock(cache):
    """Return qPlot's per-cache lock, creating it safely on first use."""

    with _CACHE_LOCK_CREATION:
        lock = getattr(cache, "_qplot_lock", None)
        if lock is None:
            lock = RLock()
            cache._qplot_lock = lock
        return lock


def cache_dataset(cache):
    return cache._dataset


def cache_table_name(cache):
    return cache_dataset(cache).table_name


def cache_database_path(cache):
    return cache_dataset(cache).path_to_db


def cache_rundescriber(cache):
    return cache.rundescriber


def cache_read_status(cache):
    return cache._read_status


def cache_write_status(cache):
    return cache._write_status


def cache_data(cache):
    return cache._data


def cache_parameter_data(cache, parameter_name):
    return cache_data(cache)[parameter_name]


def cache_is_live(cache):
    return cache.live


def cache_dataset_connection(cache):
    return cache_dataset(cache).conn


def cache_dataset_run_id(cache):
    return cache_dataset(cache).run_id


def cache_dataset_completed(cache):
    return cache_dataset(cache).completed


def set_cache_dataset_completed(cache, completed):
    """Update qPlot's cached QCoDeS completion state without database writes.

    QCoDeS 0.58's public ``DataSet.completed`` setter calls
    ``mark_run_complete`` after changing ``_completed``. Viewer datasets are
    loaded from databases that qPlot must never write, and their connections
    may belong to another thread, so completion observations from SQLite must
    only update the viewer-owned in-memory flag.
    """

    with cache_lock(cache):
        cache_dataset(cache)._completed = bool(completed)


def cache_parameter_is_synchronized(cache, parameter_name):
    """Return whether qPlot has committed this parameter's final cache rows."""

    with cache_lock(cache):
        synchronized = getattr(cache, "_qplot_synchronized_parameters", ())
        return parameter_name in synchronized


def set_cache_parameter_synchronized(cache, parameter_name, synchronized=True):
    """Publish viewer-owned, cache-wide final synchronization state."""

    with cache_lock(cache):
        completed_parameters = getattr(
            cache,
            "_qplot_synchronized_parameters",
            None,
            )
        if completed_parameters is None:
            completed_parameters = set()
            cache._qplot_synchronized_parameters = completed_parameters

        if synchronized:
            completed_parameters.add(parameter_name)
        else:
            completed_parameters.discard(parameter_name)


def parameter_is_complete(param):
    """Return the legacy ParamSpec-local completion mirror.

    QCoDeS can reconstruct ParamSpec instances, so this is not authoritative;
    use :func:`cache_parameter_is_synchronized` for refresh decisions.
    """

    return param._complete is True


def set_parameter_complete(param, complete=True):
    param._complete = complete


def prepare_cache_if_empty(cache):
    with cache_lock(cache):
        if cache_data(cache) == {}:
            cache.prepare()


def snapshot_cache_parameter_state(cache, parameter_name):
    """Take a consistent shallow snapshot for one parameter-tree read."""

    with cache_lock(cache):
        parameter_data = dict(cache_parameter_data(cache, parameter_name))
        return (
            dict(cache_write_status(cache)),
            dict(cache_read_status(cache)),
            {parameter_name: parameter_data},
            )


def update_cache_parameter_data(
        cache,
        parameter_name,
        updated_read_status,
        updated_write_status,
        updated_data,
        dataset_completed=None,
        ):
    """Commit a parameter refresh unless a newer worker already won the race.

    ``_read_status`` is QCoDeS' database cursor and is therefore monotonic for
    data read from SQLite.  ``_write_status`` is instead an in-memory array
    insertion offset.  In particular, QCoDeS reports ``0`` after appending to
    an unshaped parameter tree, so it cannot safely be used to order refresh
    workers. Parameter synchronization and database completion are monotonic
    and are published only after the full parameter cache commit, so another
    plot can distinguish global source completion from its own outstanding
    final read.
    """

    with cache_lock(cache):
        next_read_status = updated_read_status[parameter_name]
        next_write_status = updated_write_status[parameter_name]
        current_read_status = cache_read_status(cache).get(parameter_name, -1)

        # QCoDeS represents an untouched shaped parameter with ``None``. Treat
        # it as older than any concrete row count when comparing results.
        comparable = lambda status: -1 if status is None else status
        if comparable(current_read_status) > comparable(next_read_status):
            return False

        cache_read_status(cache)[parameter_name] = next_read_status
        cache_write_status(cache)[parameter_name] = next_write_status
        cache_data(cache)[parameter_name] = updated_data[parameter_name]
        if dataset_completed is True:
            set_cache_parameter_synchronized(cache, parameter_name, True)
            # Source completion is global, but individual plots still track
            # whether their final display commit succeeded.
            set_cache_dataset_completed(cache, dataset_completed)
        return True


def cache_has_no_written_data(cache):
    return all(
        status is None or status == 0
        for status in cache_write_status(cache).values()
        )
