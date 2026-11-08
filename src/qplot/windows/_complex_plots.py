"""Complex display preferences and cursor presentation shared by plot windows."""

import math
from typing import Any

from qplot.datahandling.complex_data import COMPLEX_1D_KEY, COMPLEX_ND_KEY


def configured_complex_mode(window: Any) -> str:
    key = COMPLEX_1D_KEY if len(window.param.depends_on_) == 1 else COMPLEX_ND_KEY
    return window.config.get(key)


def apply_complex_preference(window: Any, *, include_sources: bool = True) -> None:
    if include_sources:
        seen = {id(window)}
        for registry in ("lines", "heatmaps"):
            for item in window.__dict__.get(registry, {}).values():
                source = getattr(item, "from_win", None)
                if source is not None and id(source) not in seen:
                    seen.add(id(source))
                    apply_complex_preference(source, include_sources=False)
    if "_frozen_result" in window.__dict__ or getattr(window, "param", None) is None:
        return  # Snapshots retain the display captured when they were frozen.
    mode = configured_complex_mode(window)
    previous = window.__dict__.get("_requested_complex_mode", mode)
    if mode != previous:
        window._requested_complex_mode = mode
        if (getattr(window.param, "type", None) not in ("complex", "array")
                and window.__dict__.get("complex_samples") is None):
            return
        invalidate = getattr(window, "_invalidate_volume_range", None)
        if callable(invalidate):
            invalidate()
        window.refreshWindow(force=True)


def format_complex_cursor(window: Any, value: complex) -> str:
    real = window.format_cursor_number(value.real).strip()
    imaginary = window.format_cursor_number(abs(value.imag)).strip()
    sign = "−" if math.copysign(1., value.imag) < 0 else "+"
    return f"{real} {sign} {imaginary}i"


def sync_complex_axes(window: Any) -> None:
    samples = window.__dict__.get("complex_samples")
    if samples is None or len(samples.coordinates) != 1:
        return
    fixed = samples.mode == "imag_vs_real"
    for dropdown in window.__dict__.get("axis_dropdown", {}).values():
        dropdown.setEnabled(not fixed)
        dropdown.setToolTip(
            "Im vs Re uses Re horizontally and Im vertically. Change the "
            "complex plot preference to select a setpoint axis." if fixed else ""
        )
    dialog = window.__dict__.get("_trace_appearance_dialog")
    if dialog is not None:
        dialog.sync_swap_axes_control()


def show_complex_line_readout(window: Any, line: Any, index: int) -> bool:
    source = getattr(line, "from_win", window)
    samples = source.__dict__.get("complex_samples")
    if samples is None:
        return False
    if len(samples.coordinates) == 2:
        if getattr(source, "operation_kind", None) != "sweeper":
            return False
        index = samples.cell_index(index, source.fixed_index)
        if index is None:
            window.pos_labels["x"].setText("—")
            window.pos_labels["y"].setText("—")
            return True
    # An FFT bin has no unique recorded complex sample. Never present a
    # frequency-domain value as if it were a measurement from the source.
    if getattr(line, "opts", {}).get("fftMode") or not 0 <= index < samples.values.size:
        window.pos_labels["x"].setText("—")
        window.pos_labels["y"].setText("—")
    else:
        window.pos_labels["x"].setText(window.format_cursor_number(samples.coordinates[0][index]))
        window.pos_labels["y"].setText(format_complex_cursor(window, samples.values[index]))
    window.cursor_readout.setToolTip(
        f"Original {samples.parameters[0].label} and recorded complex value.\n"
        "The real and imaginary components are shown regardless of the display projection."
    )
    return True
