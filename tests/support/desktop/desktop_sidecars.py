"""Shared fixtures and assertions for desktop sidecars."""

from copy import deepcopy

from qplot import _desktop_sidecars as sidecars

__all__ = [
    'FakeRegistry',
]


class FakeKey:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.keys = {sidecars._CLASSES: {}}
        self.fail_write = None

    def OpenKey(self, root, path, *_args):
        assert root is self.HKEY_CURRENT_USER
        if path not in self.keys:
            raise FileNotFoundError(path)
        return FakeKey(path)

    def CreateKey(self, root, path):
        assert root is self.HKEY_CURRENT_USER
        parts = path.split("\\")
        for count in range(2, len(parts) + 1):
            self.keys.setdefault("\\".join(parts[:count]), {})
        return FakeKey(path)

    def QueryValueEx(self, key, name):
        try:
            return deepcopy(self.keys[key.path][name])
        except KeyError:
            raise FileNotFoundError(name) from None

    def SetValueEx(self, key, name, _reserved, kind, value):
        if key.path == self.fail_write:
            raise PermissionError("injected registry write failure")
        self.keys[key.path][name] = (deepcopy(value), kind)

    def DeleteValue(self, key, name):
        try:
            del self.keys[key.path][name]
        except KeyError:
            raise FileNotFoundError(name) from None

    def QueryInfoKey(self, key):
        prefix = key.path + "\\"
        children = sum(path.startswith(prefix) and "\\" not in path[len(prefix):]
                       for path in self.keys)
        return children, len(self.keys[key.path]), 0

    def DeleteKey(self, root, path):
        assert root is self.HKEY_CURRENT_USER
        if path not in self.keys:
            raise FileNotFoundError(path)
        if any(other.startswith(path + "\\") for other in self.keys):
            raise OSError("key has subkeys")
        del self.keys[path]
