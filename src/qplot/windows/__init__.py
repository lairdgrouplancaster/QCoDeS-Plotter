# -*- coding: utf-8 -*-
"""
Created on Sun Jul  6 16:31:54 2025

@author: Benjamin Wordsworth
"""

from .plot1d import plot1d
from .plot2d import plot2d
from . import widgets
from .main import MainWindow

__all__ = [
    "plot1d",
    "plot2d",
    "MainWindow",
    "widgets"
    ]
