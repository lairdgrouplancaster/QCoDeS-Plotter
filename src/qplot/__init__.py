from ._metadata import package_version

__version__ = package_version()

__all__ = [
    "datahandling",
    "windows",
    "tools",
    "run",
    "config",
    "__version__",
]


def run(*args, **kwargs):
    """
    Start qPlot through its dedicated public-API launcher.

    Plain package import still avoids importing GUI modules. Passing
    ``return_objects=True`` remains the explicit caller-owned exception.

    """
    from .__main__ import run_public as _run

    return _run(*args, **kwargs)


def __getattr__(name):
    if name == "config":
        from qplot.configuration.config import config

        globals()[name] = config
        return config

    if name in {"datahandling", "tools", "windows"}:
        import importlib

        module = importlib.import_module(f"qplot.{name}")
        globals()[name] = module
        return module

    raise AttributeError(f"module 'qplot' has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
