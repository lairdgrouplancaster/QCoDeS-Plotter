"""Optional Windows sidecar icons, without registering an application opener."""

from __future__ import annotations

import sys
from pathlib import Path

_CLASSES = r"Software\Classes"
_SIDECARS = {".db-wal": "sqlite-wal.ico", ".db-shm": "sqlite-shm.ico"}


def _registry():
    if sys.platform != "win32":
        raise ValueError("Windows sidecar icon registration requires Windows.")
    import winreg

    return winreg


def _key_exists(registry, path):
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, path):
            return True
    except FileNotFoundError:
        return False


def _read_icon(registry, path):
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, path) as key:
            return registry.QueryValueEx(key, "")
    except FileNotFoundError:
        return None


def _encode_previous(value):
    if value is None:
        return None
    data, kind = value
    return {"type": kind, "data": data.hex() if isinstance(data, bytes) else data,
            "binary": isinstance(data, bytes)}


def _decode_previous(value):
    """Validate the JSON backup before any registry changes are attempted."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"type", "data", "binary"}:
        raise ValueError("Invalid sidecar icon backup.")
    kind, data, binary = value["type"], value["data"], value["binary"]
    if type(kind) is not int or kind not in range(12) or type(binary) is not bool:
        raise ValueError("Invalid sidecar icon registry type.")
    if binary:
        if kind in {1, 2, 4, 7, 11} or not isinstance(data, str):
            raise ValueError("Invalid binary sidecar icon backup.")
        return bytes.fromhex(data), kind
    valid = (
        kind in {1, 2} and isinstance(data, str)
        or kind in {4, 11} and type(data) is int and 0 <= data < 2 ** (32 if kind == 4 else 64)
        or kind == 7 and isinstance(data, list) and all(isinstance(item, str) for item in data)
    )
    if not valid:
        raise ValueError("Invalid sidecar icon backup value.")
    return data, kind


def _validated_records(records):
    if not isinstance(records, list):
        raise ValueError("Invalid sidecar icon registration record.")
    checked = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Invalid sidecar icon registration record.")
        extension = record.get("extension")
        if not isinstance(extension, str) or extension not in _SIDECARS or extension in seen:
            raise ValueError("Invalid sidecar icon extension.")
        seen.add(extension)
        icon = record.get("icon")
        if not isinstance(icon, str) or not icon.startswith('"') or not icon.endswith(
            _SIDECARS[extension] + '\",0'
        ):
            raise ValueError("Invalid sidecar icon location.")
        for flag in ("created_extension", "created_icon_key"):
            if type(record.get(flag)) is not bool:
                raise ValueError("Invalid sidecar icon key record.")
        if "previous" not in record:
            raise ValueError("Missing sidecar icon backup.")
        previous = _decode_previous(record["previous"])
        if (record["created_extension"] and not record["created_icon_key"]
                or record["created_icon_key"] and previous is not None):
            raise ValueError("Inconsistent sidecar icon backup.")
        checked.append((record, previous))
    return checked


def _delete_empty_key(registry, path):
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, path) as key:
            children, values, _modified = registry.QueryInfoKey(key)
        if children == 0 and values == 0:
            registry.DeleteKey(registry.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def prepare_sidecar_icons(destination: Path) -> list[dict]:
    """Read and validate reversible JSON state without changing the registry."""
    registry = _registry()
    records = []
    for extension, filename in _SIDECARS.items():
        extension_key = _CLASSES + "\\" + extension
        icon_key = extension_key + r"\DefaultIcon"
        records.append({
            "extension": extension,
            "icon": f'"{destination / filename}",0',
            "previous": _encode_previous(_read_icon(registry, icon_key)),
            "created_extension": not _key_exists(registry, extension_key),
            "created_icon_key": not _key_exists(registry, icon_key),
        })
    _validated_records(records)
    return records


def install_sidecar_icons(destination: Path, *, records=None) -> list[dict]:
    """Set only extension DefaultIcon values, using an optional saved backup.

    Setup saves prepared records before calling this function so a restoration
    failure cannot discard the original icons. FriendlyTypeName belongs to a
    ProgID: bindings and all verbs remain untouched. The caller notifies the
    Shell after completing its other registrations.
    """
    if records is None:
        records = prepare_sidecar_icons(destination)
    checked = _validated_records(records)
    if len(checked) != len(_SIDECARS) or any(
        record["icon"] != f'"{destination / _SIDECARS[record["extension"]]}",0'
        for record, _previous in checked
    ):
        raise ValueError("Sidecar icon backup does not match the setup destination.")
    registry = _registry()
    for record, previous in checked:
        extension_key = _CLASSES + "\\" + record["extension"]
        icon_key = extension_key + r"\DefaultIcon"
        if (_read_icon(registry, icon_key) != previous
                or _key_exists(registry, icon_key) == record["created_icon_key"]
                or _key_exists(registry, extension_key) == record["created_extension"]):
            raise ValueError("Sidecar icon registration changed after preparing its backup.")
    attempted = []
    try:
        for record in records:
            attempted.append(record)
            path = _CLASSES + "\\" + record["extension"] + r"\DefaultIcon"
            with registry.CreateKey(registry.HKEY_CURRENT_USER, path) as key:
                registry.SetValueEx(key, "", 0, registry.REG_SZ, record["icon"])
    except OSError:
        remove_sidecar_icons(attempted)
        raise
    return records


def remove_sidecar_icons(records) -> None:
    """Restore prior icons only while our exact installed values still own them."""
    checked = _validated_records(records)
    registry = _registry()
    errors = []
    for record, previous in reversed(checked):
        extension_key = _CLASSES + "\\" + record["extension"]
        icon_key = extension_key + r"\DefaultIcon"
        try:
            if _read_icon(registry, icon_key) == (record["icon"], registry.REG_SZ):
                with registry.OpenKey(registry.HKEY_CURRENT_USER, icon_key, 0,
                                      registry.KEY_SET_VALUE) as key:
                    if previous is None:
                        registry.DeleteValue(key, "")
                    else:
                        data, kind = previous
                        registry.SetValueEx(key, "", 0, kind, data)
            if record["created_icon_key"]:
                _delete_empty_key(registry, icon_key)
            if record["created_extension"]:
                _delete_empty_key(registry, extension_key)
        except OSError as error:
            # A failure on one extension must not prevent the other's rollback.
            errors.append(error)
    if errors:
        raise errors[0]
