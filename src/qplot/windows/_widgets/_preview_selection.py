"""Selection and menu identity checks shared by thumbnail containers."""

from PyQt6 import QtCore, sip
from PyQt6 import QtWidgets as qtw


def preview_key(label):
    return str(getattr(label, "guid", "") or ""), str(label.parameter)


def active_preview_labels(scope):
    """Return current layout-owned thumbnails in visual order."""
    if sip.isdeleted(scope):
        return []
    provider = getattr(scope, "preview_selection_labels", None)
    if callable(provider):
        candidates = provider()
    else:
        candidates = [scope, *scope.findChildren(qtw.QLabel)]
    return [label for label in candidates
            if not sip.isdeleted(label)
            and getattr(label, "_qplot_preview_label", False)
            and not getattr(label, "_preview_retired", False)
            and not (label.isHidden() and label.testAttribute(
                QtCore.Qt.WidgetAttribute.WA_WState_ExplicitShowHide,
            ))
            and label.parameter]


def select_preview(label, *, modifiers=QtCore.Qt.KeyboardModifier.NoModifier,
                   preserve=False):
    scope = label._selection_scope()
    labels = active_preview_labels(scope)
    if label not in labels:
        return
    if preserve and label._selected:
        return
    toggle = bool(modifiers & (QtCore.Qt.KeyboardModifier.ControlModifier
                               | QtCore.Qt.KeyboardModifier.MetaModifier))
    extend = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
    key = preview_key(label)
    anchor = getattr(scope, "_preview_selection_anchor", None)
    keys = [preview_key(candidate) for candidate in labels]
    if extend and anchor in keys:
        start, end = sorted((keys.index(anchor), labels.index(label)))
        selected = set(keys[start:end + 1])
        if toggle:
            selected.update(preview_key(candidate) for candidate in labels if candidate._selected)
    elif toggle:
        selected = {preview_key(candidate) for candidate in labels if candidate._selected}
        selected.symmetric_difference_update((key,))
        scope._preview_selection_anchor = key
    else:
        selected = {key}
        scope._preview_selection_anchor = key
    for candidate in labels:
        candidate.set_selected(preview_key(candidate) in selected)


def selected_preview_targets(scope):
    """Stable identities, deduplicated without losing the displayed order."""
    return tuple(dict.fromkeys(preview_key(label) for label in active_preview_labels(scope)
                               if label._selected and preview_key(label)[0]))


def preview_menu_token(scope):
    """Invalidate menus after source switches, refreshes or selection edits."""
    if sip.isdeleted(scope):
        return None
    window = scope.window()
    state = window.__dict__
    instance = state.get("_loaded_database_instance")
    return (
        state.get("_database_load_generation"),
        getattr(instance, "logical_path", None),
        getattr(instance, "identity", None),
        getattr(scope, "generation", None),
        getattr(scope, "current_guid", None),
        getattr(scope, "_preview_selection_generation", None),
        scope.isVisible(),
        tuple(id(label) for label in active_preview_labels(scope) if label._selected),
    )
