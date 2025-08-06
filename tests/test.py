from qplot import run
from qplot._repair import _repair

try:
    run()
except Exception as err:
    print("Closing SQL Connect")
    _repair()
    raise err
