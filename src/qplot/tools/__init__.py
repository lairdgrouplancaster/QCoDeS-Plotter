"""Numerical tools; the Qt loading worker is imported only on request."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .general import data2matrix, unpack_param
    from .worker import loader

__all__ = [
    "data2matrix",
    "unpack_param",
    "loader",
    ]

_EXPORT_MODULES = {
    "data2matrix": ".general",
    "unpack_param": ".general",
    "loader": ".worker",
}


def __getattr__(name):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'qplot.tools' has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
