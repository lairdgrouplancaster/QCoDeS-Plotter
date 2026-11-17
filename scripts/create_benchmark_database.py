"""Create a new deterministic QCoDeS workload; never replace an existing file."""

import argparse
from pathlib import Path

import numpy as np

from qplot.testdata import (
    AxisSpecification,
    MeasurementSpecification,
    RunSpecification,
    generate_database,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--heatmap-side", type=int, default=1500)
    parser.add_argument("--runs", type=int, default=30)
    args = parser.parse_args()
    if args.heatmap_side < 2 or args.runs < 2:
        parser.error("heatmap side and run count must both be at least two")
    x = AxisSpecification("x", "Gate voltage", "V", -1, 1, 20_000)
    fast = AxisSpecification("fast", "Fast voltage", "V", -1, 1, args.heatmap_side)
    slow = AxisSpecification("slow", "Slow voltage", "V", -1, 1, args.heatmap_side)
    short = AxisSpecification("x", "Gate voltage", "V", -1, 1, 100)
    runs = [
        RunSpecification((MeasurementSpecification("signal", "Current", "A", (x,)),), "line"),
        RunSpecification((MeasurementSpecification("signal", "Current", "A", (slow, fast)),), "heatmap"),
        *(RunSpecification((MeasurementSpecification("signal", "Current", "A", (short,)),),
                           f"selection_{index}") for index in range(args.runs - 2)),
    ]
    generate_database(runs, args.database, rng=np.random.default_rng(20261117))


if __name__ == "__main__":
    main()
