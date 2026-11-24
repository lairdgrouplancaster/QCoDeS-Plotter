"""Stable, session-local names for visible plot windows."""

from PyQt6 import QtWidgets as qtw


def update_plot_title(window: qtw.QWidget) -> None:
    """Describe every attached source, including layers temporarily hidden."""
    state = window.__dict__
    number = state.get("_plot_window_number")
    if number is None:
        return
    sources = [window]
    sources.extend(line.from_win for line in state.get("lines", {}).values()
                   if line is not None and line is not state.get("line"))
    sources.extend(layer.from_win for layer in state.get("heatmaps", {}).values()
                   if layer is not window)
    info = [s.__dict__["_plot_source_info"] for s in sources if "_plot_source_info" in s.__dict__]
    title = f"Plot {number}"
    if info:
        runs = list(dict.fromkeys((db, run) for db, run, _ in info))
        labels = [label for _, _, label in info]
        if len(runs) == 1 and len(info) <= 2:
            summary = f"Run {runs[0][1]}, {' + '.join(labels)}"
        elif len(info) == 2 and labels[0] == labels[1] and runs[0][1] != runs[1][1]:
            summary = f"{labels[0]}, runs {runs[0][1]} and {runs[1][1]}"
        else:
            unit = "run" if len(runs) == 1 else "runs"
            summary = f"{len(info)} measurements from {len(runs)} {unit}"
        title += f" — {summary}"
        if len(info) == 1:
            if getattr(window, "cursor_has_depth", False):
                title += " (3D slice)"
            elif getattr(window, "operation_kind", None) == "plot2d":
                title += " (2D heatmap)"
            elif "sweep_id" in state:
                title += f" (cut {state['sweep_id'] + 1})"
    state["_plot_sources_tooltip"] = "\n".join(f"{db} — Run {run}, {label}" for db, run, label in info)
    if title != window.windowTitle():
        window.setWindowTitle(title)


def assign_plot_title(window: qtw.QWidget) -> int | None:
    """Number a visible plot once; closing other plots never changes its name."""
    # Hidden renderers load overlay data without creating another user window.
    if not window.__dict__.get("visible", True):
        return None
    number = window.__dict__.get("_plot_window_number")
    if number is None:
        application = qtw.QApplication.instance()
        if application is None:
            raise RuntimeError("A QApplication is required to name plot windows.")
        number = application.__dict__.get("_qplot_next_plot_number", 1)
        application.__dict__["_qplot_next_plot_number"] = number + 1
        window.__dict__["_plot_window_number"] = number
    update_plot_title(window)
    return number


def inherit_plot_title(source: qtw.QWidget, replacement: qtw.QWidget) -> None:
    """Retain the visible window's identity when its primary source changes."""
    number = source.__dict__.get("_plot_window_number")
    if number is not None:
        replacement.__dict__["_plot_window_number"] = number
        update_plot_title(replacement)
