from qplot import run
from qplot._repair import repair

try:
    run()
except Exception as err:
    print("Closing SQL Connect")
    repair()
    raise err
