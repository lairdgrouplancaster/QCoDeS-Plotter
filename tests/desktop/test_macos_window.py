"""Native title-bar gestures honour preferences without changing them."""

from types import SimpleNamespace

import pytest

from qplot import _macos_window as macos_window


@pytest.fixture
def native_backend(monkeypatch):
    monkeypatch.setattr(macos_window.sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_window.QtWidgets.QApplication, "instance",
        lambda: SimpleNamespace(platformName=lambda: "cocoa"),
    )
    calls = []
    state = {"action": "Maximize"}
    backend = SimpleNamespace(
        native_window=lambda view: 22 if view == 11 else None,
        double_click_action=lambda: state["action"],
        perform=lambda window, selector: calls.append((window, selector)),
    )
    monkeypatch.setattr(macos_window, "_window_actions", lambda: backend)
    return SimpleNamespace(winId=lambda: 11), state, calls


@pytest.mark.parametrize("preference, expected", [
    ("Minimize", b"performMiniaturize:"),
    ("Maximize", b"performZoom:"),
    ("Fill", b"performZoom:"),
    ("None", None),
])
def test_native_double_click_uses_current_preference(native_backend, preference, expected):
    window, state, calls = native_backend
    state["action"] = preference
    assert macos_window.titlebar_double_click(window)
    assert calls == ([] if expected is None else [(22, expected)])
    # Read-only backend has no preference setter; the choice stays untouched.
    assert state["action"] == preference
    state["action"] = "None"
    assert macos_window.titlebar_double_click(window)
    assert calls == ([] if expected is None else [(22, expected)])


def test_unknown_preference_leaves_gesture_to_caller(native_backend):
    window, state, calls = native_backend
    state["action"] = "Future setting"
    assert not macos_window.titlebar_double_click(window)
    assert calls == []


def test_native_failure_does_not_escape(native_backend, monkeypatch):
    window, _, calls = native_backend

    def unavailable():
        raise OSError("Unavailable Objective-C runtime")

    monkeypatch.setattr(macos_window, "_window_actions", unavailable)
    assert not macos_window.titlebar_double_click(window)
    assert calls == []


def test_offscreen_never_treats_winid_as_native_pointer(native_backend, monkeypatch):
    window, _, calls = native_backend
    monkeypatch.setattr(
        macos_window.QtWidgets.QApplication, "instance",
        lambda: SimpleNamespace(platformName=lambda: "offscreen"),
    )
    monkeypatch.setattr(macos_window, "_window_actions", lambda: pytest.fail("Native calls"))
    assert not macos_window.titlebar_double_click(window)
    assert calls == []
