"""Font sizing shared by themed widgets and plot graphics."""

import re

FONT_SIZE_KEY = "user_preference.font_size"
DEFAULT_FONT_SIZE = 10


def scale_font_stylesheet(stylesheet, font_size):
    """Scale point sizes while retaining smaller secondary text."""
    return re.sub(
        r"font-size:\s*([\d.]+)pt;",
        lambda match: f"font-size: {float(match[1]) * font_size / DEFAULT_FONT_SIZE:g}pt;",
        stylesheet,
    )


def configured_stylesheet(config):
    """Return the selected theme with the saved font size applied."""
    font_size = config.get(FONT_SIZE_KEY)
    stylesheet = config.theme.main
    if not stylesheet:
        return f"QWidget {{ font-size: {font_size}pt; }}"
    return scale_font_stylesheet(stylesheet, font_size)
