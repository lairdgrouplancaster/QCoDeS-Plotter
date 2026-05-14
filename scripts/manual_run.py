"""Start qPlot for a manual GUI smoke check.

Use this after automated checks when a change affects runtime behavior or the
GUI. If startup fails, the script runs the local SQL repair helper before
re-raising the original exception.
"""

from qplot import run
from qplot._repair import repair


def main():
    try:
        run()
    except Exception as err:
        print("Closing SQL Connect")
        repair()
        raise err


if __name__ == "__main__":
    main()
