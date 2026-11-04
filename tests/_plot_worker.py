"""Construct real plot workers for focused numerical and cancellation tests.

The cache supplies the constructor's QCoDeS metadata contract without opening a
database. Tests that exercise database loading continue to use real datasets.
"""

from pathlib import Path
from types import SimpleNamespace

from qplot.tools.worker import loader


def make_plot_worker(*, param=None, param_dict=None, axes=None, cache=None, database_path=None, **options):
    if param is None:
        param = SimpleNamespace(
            name="signal", label="Signal", unit="A", type="numeric", depends_on_=("x",),
        )
    if param_dict is None:
        param_dict = {
            name: SimpleNamespace(name=name, label=name, unit="V", type="numeric", depends_on_=())
            for name in param.depends_on_
        }
        param_dict[param.name] = param
    if axes is None:
        axes = {"x": "x"}
    if cache is None:
        cache = SimpleNamespace(
            _dataset=SimpleNamespace(
                table_name="results",
                path_to_db=str(database_path or Path(__file__).with_name("_worker_fixture_no_database.db")),
            ),
            rundescriber=SimpleNamespace(shapes=None),
        )
    return loader(cache, param, param_dict, axes, **options)
