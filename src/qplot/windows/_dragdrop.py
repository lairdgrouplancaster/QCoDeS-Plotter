import json

from PyQt6 import QtCore


RUN_PREVIEW_MIME = "application/x-qplot-run-preview"


def make_run_preview_mime(guid, parameter, axes=None):
    payload = {
        "guid": str(guid or ""),
        "parameter": str(parameter or ""),
        "axes": [str(axis) for axis in (axes or [])],
        }
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

    return {
        "guid": guid,
        "parameter": parameter,
        "axes": [str(axis) for axis in axes],
        }


def preview_drop_is_compatible(target_axes, payload):
    axes = payload.get("axes") or []
    return len(axes) == 1 and tuple(axes) == tuple(str(axis) for axis in target_axes)
