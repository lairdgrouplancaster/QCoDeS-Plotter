__all__ = [
    "get_runs_basic_via_sql",
    "get_runs_via_sql",
    "get_selected_run_setpoint_summaries",
    "get_snapshot_selected_run_detail",
    "find_new_runs",
    "get_run_status",
    "has_finished",
    "iter_run_detail_batches_via_sql",
    "iter_run_shape_batches_via_sql",
    "iter_run_storage_batches_via_sql",
    "load_param_data_from_db_prep",
    "load_param_data_from_db",
    ]

_EXPORT_MODULES = {
    "get_runs_basic_via_sql": ".readSQL",
    "get_runs_via_sql": ".readSQL",
    "get_selected_run_setpoint_summaries": ".readSQL",
    "get_snapshot_selected_run_detail": ".readSQL",
    "find_new_runs": ".readSQL",
    "get_run_status": ".readSQL",
    "has_finished": ".readSQL",
    "iter_run_detail_batches_via_sql": ".readSQL",
    "iter_run_shape_batches_via_sql": ".readSQL",
    "iter_run_storage_batches_via_sql": ".readSQL",
    "load_param_data_from_db_prep": ".LoadFromDB",
    "load_param_data_from_db": ".LoadFromDB",
    }


def __getattr__(name):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(
            f"module 'qplot.datahandling' has no attribute {name!r}"
        )

    from importlib import import_module

    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
