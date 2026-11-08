"""Native title-bar actions for the expanded macOS plot header."""

import ctypes
import sys
from functools import lru_cache
from typing import cast

from PyQt6 import QtWidgets


class _WindowActions:
    """Use typed Objective-C calls without owning any AppKit objects."""

    def __init__(self):
        library = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        self._library = library
        self._class = library.objc_getClass
        self._class.argtypes = [ctypes.c_char_p]
        self._class.restype = ctypes.c_void_p
        self._selector = library.sel_registerName
        self._selector.argtypes = [ctypes.c_char_p]
        self._selector.restype = ctypes.c_void_p
        pointer = ctypes.c_void_p
        self._get = ctypes.CFUNCTYPE(pointer, pointer, pointer)(("objc_msgSend", library))
        self._get_object = ctypes.CFUNCTYPE(pointer, pointer, pointer, pointer)(
            ("objc_msgSend", library)
        )
        self._string = ctypes.CFUNCTYPE(pointer, pointer, pointer, ctypes.c_char_p)(
            ("objc_msgSend", library)
        )
        self._utf8 = ctypes.CFUNCTYPE(ctypes.c_char_p, pointer, pointer)(
            ("objc_msgSend", library)
        )
        self._action = ctypes.CFUNCTYPE(None, pointer, pointer, pointer)(
            ("objc_msgSend", library)
        )

    def native_window(self, view: int) -> int | None:
        # On Cocoa QWidget.winId() is the NSView, not its containing NSWindow.
        return self._get(view, self._selector(b"window"))

    def double_click_action(self) -> str:
        defaults = self._get(
            self._class(b"NSUserDefaults"), self._selector(b"standardUserDefaults")
        )
        key = self._string(
            self._class(b"NSString"), self._selector(b"stringWithUTF8String:"),
            b"AppleActionOnDoubleClick",
        )
        value = self._get_object(defaults, self._selector(b"stringForKey:"), key)
        text = self._utf8(value, self._selector(b"UTF8String")) if value else None
        # An unset preference uses the normal title-bar zoom action.
        return text.decode("utf-8") if text else "Maximize"

    def perform(self, window: int, selector: bytes) -> None:
        self._action(window, self._selector(selector), None)


@lru_cache(maxsize=1)
def _window_actions() -> _WindowActions:
    return _WindowActions()


def titlebar_double_click(window: QtWidgets.QWidget) -> bool:
    """Apply the macOS preference; return whether native handling succeeded.

    Preferences are read on each double click so changes in System Settings
    take effect without restarting qPlot. The standard zoom action retains
    windowed operation; it never invokes native full screen.
    """
    app = cast(QtWidgets.QApplication | None, QtWidgets.QApplication.instance())
    if sys.platform != "darwin" or app is None or app.platformName() != "cocoa":
        return False
    try:
        actions = _window_actions()
        native_window = actions.native_window(int(window.winId()))
        if not native_window:
            return False
        action = actions.double_click_action()
        if action == "None":
            return True
        selector = {
            "Minimize": b"performMiniaturize:",
            "Maximize": b"performZoom:",
            "Fill": b"performZoom:",
        }.get(action)
        if selector is None:
            return False
        actions.perform(native_window, selector)
        return True
    except (AttributeError, OSError, RuntimeError, UnicodeError, ValueError):
        # Failure to customise a title-bar gesture must not stop plot viewing.
        return False
