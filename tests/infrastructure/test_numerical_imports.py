"""Arithmetic modules must remain usable without the GUI or database stack."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.pure


@pytest.mark.parametrize(
    "module_name",
    [
        "qplot.tools",
        "qplot.tools.general",
        "qplot.tools.heatmap_geometry",
        "qplot.tools.finite_means",
        "qplot.tools.native_transform_math",
        "qplot.tools.sample_statistics",
        "qplot.tools.plot_tools",
        "qplot.tools.plot_result",
        "qplot.tools.plot_processing",
    ],
)
def test_numerical_import_needs_no_gui_or_database_packages(module_name):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib
import importlib.abc
import sys

blocked = {'qcodes', 'PyQt6', 'pyqtgraph', 'pandas', 'matplotlib', 'sqlite3', 'apsw', 'qplotter_native'}

class NoApplicationDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked:
            raise AssertionError(f'Numerical import attempted {fullname}')

sys.meta_path.insert(0, NoApplicationDependencies())
importlib.import_module(sys.argv[1])
if sys.argv[1] == 'qplot.tools.plot_processing':
    # Exercise the boundary as well as its imports: a real pipeline must not
    # lazily construct a reader or a Qt worker when it starts processing.
    from types import SimpleNamespace
    import numpy as np
    from qplot.tools.plot_processing import PlotProcessor, PlotProcessingSpec, PlotProcessingInput

    x = SimpleNamespace(name='x', label='X', unit='V', type='numeric', depends_on_=())
    signal = SimpleNamespace(name='signal', label='Signal', unit='A', type='numeric', depends_on_=('x',))
    processor = PlotProcessor(
        PlotProcessingSpec(signal, {'x': x, 'signal': signal}, {'x': 'x', 'y': 'signal'},
                           [lambda data: {'y': data['y'] * 2}]),
        cancelled=lambda: False,
    )
    output = processor.process(PlotProcessingInput({'x': np.arange(3), 'signal': np.arange(3)}, {}))
    np.testing.assert_array_equal(output.axis_data['y'], [0, 2, 4])
assert 'qplot.tools.worker' not in sys.modules
assert not blocked.intersection(sys.modules)
""",
            module_name,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_public_numerical_exports_are_lazy_and_discoverable():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import qplot.tools as tools
from qplot.tools import data2matrix, unpack_param
from qplot.tools.general import data2matrix as direct_matrix, unpack_param as direct_unpack

assert data2matrix is direct_matrix
assert unpack_param is direct_unpack
assert tools.data2matrix is data2matrix
assert set(tools.__all__).issubset(dir(tools))
try:
    tools.not_an_export
except AttributeError:
    pass
else:
    raise AssertionError('Unknown exports must raise AttributeError')
assert 'qplot.tools.worker' not in sys.modules
assert 'qcodes' not in sys.modules
assert 'PyQt6' not in sys.modules
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
