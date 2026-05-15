from importlib.metadata import PackageNotFoundError, version


def package_version():
    """
    Return the installed qPlot package version.

    """
    try:
        return version("qplot")
    except PackageNotFoundError:
        return "0+unknown"
