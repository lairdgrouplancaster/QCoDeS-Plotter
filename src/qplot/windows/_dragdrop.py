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


def make_run_previews_mime(previews):
    """Carry an ordered thumbnail selection as one copy operation."""
    previews = [_normalise_preview_payload(preview) for preview in previews]
    if not previews or any(preview is None for preview in previews):
        raise ValueError("A preview drag requires valid measurement identities.")
    if len(previews) == 1:
        return make_run_preview_mime(**previews[0])
    mime_data = QtCore.QMimeData()
    mime_data.setData(RUN_PREVIEW_MIME, json.dumps({"previews": previews}).encode("utf-8"))
    mime_data.setText(f"{len(previews)} selected measurements")
    return mime_data


def run_preview_payloads_from_mime(mime_data):
    if mime_data is None or not mime_data.hasFormat(RUN_PREVIEW_MIME):
        return None

    try:
        payload = json.loads(bytes(mime_data.data(RUN_PREVIEW_MIME)).decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    previews = payload.get("previews", [payload])
    if not isinstance(previews, list) or not previews:
        return None
    result = []
    seen = set()
    for preview in previews:
        normalised = _normalise_preview_payload(preview)
        if normalised is None:
            return None
        key = (normalised.get("database_path"), normalised["guid"], normalised["parameter"])
        if key not in seen:
            seen.add(key)
            result.append(normalised)
    return tuple(result)


def run_preview_payload_from_mime(mime_data):
    previews = run_preview_payloads_from_mime(mime_data)
    return previews[0] if previews is not None and len(previews) == 1 else None


def _normalise_preview_payload(payload):
    if not isinstance(payload, dict):
        return None
    guid = payload.get("guid")
    parameter = payload.get("parameter")
    if not isinstance(guid, str) or not guid or not isinstance(parameter, str) or not parameter:
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
        return len(target_axes) in (1, 2, 3)
    return len(target_axes) in (1, 2, 3) and len(axes) in (1, 2, 3) and len(set(axes)) == len(axes)
