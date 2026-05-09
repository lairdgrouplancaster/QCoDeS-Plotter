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
