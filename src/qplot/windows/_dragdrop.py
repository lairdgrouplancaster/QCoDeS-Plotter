import json

from PyQt6 import QtCore

RUN_PREVIEW_MIME = "application/x-qplot-run-preview"


def make_run_preview_mime(
        guid, parameter, axes=None, database_path=None, *, axes_pending=False):
    payload: dict[str, str | list[str] | bool] = {
        "guid": str(guid or ""),
        "parameter": str(parameter or ""),
        "axes": [str(axis) for axis in (axes or [])],
        }
    if database_path:
        payload["database_path"] = str(database_path)
    if axes_pending:
        payload["axes_pending"] = True
    mime_data = QtCore.QMimeData()
    mime_data.setData(
        RUN_PREVIEW_MIME,
        json.dumps(payload).encode("utf-8")
        )
    mime_data.setText(f"{payload['parameter']} from {payload['guid']}")
    return mime_data


def run_preview_payload_from_mime(mime_data):
    if mime_data is None or not mime_data.hasFormat(RUN_PREVIEW_MIME):
        return None

    try:
        payload = json.loads(bytes(mime_data.data(RUN_PREVIEW_MIME)).decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    guid = str(payload.get("guid") or "")
    parameter = str(payload.get("parameter") or "")
    if not guid or not parameter:
        return None

    axes = payload.get("axes") or []
    if isinstance(axes, str):
        axes = [axes]
    elif not isinstance(axes, (list, tuple)):
        axes = []

    normalised: dict[str, str | list[str] | bool] = {
        "guid": guid,
        "parameter": parameter,
        "axes": [str(axis) for axis in axes],
        }
    database_path = str(payload.get("database_path") or "")
    if database_path:
        normalised["database_path"] = database_path
    if payload.get("axes_pending") is True:
        normalised["axes_pending"] = True
    return normalised


def preview_drop_is_compatible(target_axes, payload):
    """Check plot dimensionality; physical-axis compatibility is handled on drop."""
    axes = tuple(str(axis) for axis in (payload.get("axes") or []))
    target_axes = tuple(str(axis) for axis in target_axes)
    if not axes and payload.get("axes_pending") is True:
        # Pending thumbnails have a run/parameter identity but no rendered
        # axes yet. The add action loads the actual parameter before choosing
        # axes and requesting approval for any remaining incompatibility.
        return len(target_axes) in (1, 2)
    return len(target_axes) in (1, 2) and len(axes) in (1, 2) and len(set(axes)) == len(axes)
