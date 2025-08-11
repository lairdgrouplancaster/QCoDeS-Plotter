"""
Files within here contain code in CSS to format the stylesheet of the windows.
"""

from .light import light
from .dark import dark
from .blank import blank as pyqt

__all__=[
    "light",
    "dark",
    "pyqt",
    ]