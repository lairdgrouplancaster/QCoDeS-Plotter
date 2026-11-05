"""Insertion boundaries shared by trace, heatmap and mixed appearance lists."""

import pytest

from qplot.windows._plot_appearance import reordered_rows


@pytest.mark.parametrize(
    ("source_rows", "destination", "expected"),
    [
        ([], 3, "abcd"),
        ([-1, 4], 2, "abcd"),
        ([1], 0, "bacd"),
        ([1], 1, "abcd"),
        ([1], 2, "abcd"),
        ([1], 4, "acdb"),
        ([0, 2], 4, "bdac"),
        ([2, 0, 2, -1, 8], 4, "bdac"),
        ([1, 3], 2, "abdc"),
        ([1, 3], -10, "bdac"),
        ([0, 2], 10, "bdac"),
        ([0, 1, 2, 3], 2, "abcd"),
    ],
)
def test_selected_rows_move_to_original_insertion_boundary(source_rows, destination, expected):
    entries = [("trace" if index % 2 else "heatmap", key)
               for index, key in enumerate("abcd")]

    result = reordered_rows(entries, source_rows, destination)

    assert [key for _kind, key in result] == list(expected)
    assert [key for _kind, key in entries] == list("abcd")
    assert all(any(item is original for original in entries) for item in result)


def test_reordering_an_empty_registry_is_empty():
    assert reordered_rows([], [0, -1], 4) == []
