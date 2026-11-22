"""Exercise numeric input through the heatmap's actual Color Scale dialog."""

import pytest

from tests.support.windows.color_scale_locale import (
    enter_colour_value,
)
from tests.support.windows.color_scale_locale import (
    heatmap as heatmap,
)


@pytest.mark.parametrize("texts, expected", [
    (("1.5", "2.5"), (1.5, 2.5)),
    (("1.234", "2.345"), (1.234, 2.345)),
    (("1.5e-3", "2.5E+3"), (0.0015, 2500.0)),
    (("-2.5e2", "+1.5e2"), (-250.0, 150.0)),
])
def test_manual_input_uses_decimal_point_across_locales(heatmap, texts, expected):
    for field, text in zip(("min", "max"), texts, strict=True):
        enter_colour_value(heatmap, field, text)
    assert heatmap._colorbar_manual_levels == expected
    assert tuple(heatmap.bar.levels()) == expected
    assert heatmap.colorbar_manual_radio.isChecked()


@pytest.mark.parametrize("text", [
    "1,5", "1,234", "1.234,5", "1,234.5", "1 234", "1_234",
    "nan", "inf", "-inf", "1e999", "1e", "",
])
@pytest.mark.parametrize("field_name", ["min", "max"])
def test_invalid_input_cannot_apply_through_manual(heatmap, text, field_name):
    assert heatmap.setColorbarManualRange(0.0, 3.0)
    field = heatmap.colorbar_range_entry.fields[field_name]
    field.setText(text)
    field.accepted.emit()
    assert heatmap.colorbar_range_entry.error_label.text()
    assert heatmap._colorbar_manual_levels == (0.0, 3.0)
    assert tuple(heatmap.bar.levels()) == (0.0, 3.0)


@pytest.mark.parametrize("limits", [(2.0, 1.0), (2.0, 2.0)])
def test_manual_requires_increasing_limits(heatmap, limits):
    enter_colour_value(heatmap, "min", limits[0])
    enter_colour_value(heatmap, "max", limits[1])
    assert heatmap._colorbar_manual_levels == (limits[0], 3.0)
    assert heatmap.colorbar_range_entry.error_label.text()


@pytest.mark.parametrize("limits", [
    (1.234, 2.345),
    (1.2345678901234567, 1.2345678901234569),
    (-1.2345678901234567e-12, 2.345678901234567e12),
])
def test_reopening_manual_limits_preserves_values(heatmap, limits, qapplication):
    assert heatmap.setColorbarManualRange(*limits)
    heatmap.colorbar_scale_dialog.close()
    heatmap.open_colorbar_scale_dialog()
    qapplication.processEvents()
    for field, value in zip(
        (heatmap.colorbar_min_text, heatmap.colorbar_max_text), limits, strict=True
    ):
        assert field.hasAcceptableInput()
        assert float(field.text()) == value
    heatmap.colorbar_manual_radio.click()
    assert heatmap._colorbar_manual_levels == limits
    assert tuple(heatmap.bar.levels()) == limits
