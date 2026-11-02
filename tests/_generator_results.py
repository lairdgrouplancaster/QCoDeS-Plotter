"""Collect the final result of a short, cooperatively stepped test operation."""

from collections.abc import Generator


def finish_steps[Result](steps: Generator[object, None, Result]) -> Result:
    """Exhaust yielded steps and return the generator's terminal value."""
    while True:
        try:
            next(steps)
        except StopIteration as finished:
            return finished.value
