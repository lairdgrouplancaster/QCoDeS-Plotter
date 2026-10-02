from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike
from qcodes import dataset


def data2matrix(indep1 : ArrayLike,
                indep2 : ArrayLike,
                depvar : ArrayLike,
                *,
                check_cancelled=lambda: None,
                ):
    """
    Converts 3 numpy.ndarry into a datagrid via pandas.DataFrame.
    This is used for producing a data grid to be passed to a heatmap, also 
    handles numpy.Nan values in depvar and duplicates

    Parameters
    ----------
    indep1 : np.array
        Array to be placed in the pandas.DataFrame index column.
    indep2 : np.array
        Array to be placed in the pandas.DataFrame header row.
    depvar : np.array
        Array to take up the .

    Returns
    -------
    matrix : pandas.DataFrame
        The data frame containing all 3 inputted arrays as a dataframe.

    """
    values = np.asarray(depvar)
    if values.dtype.kind in "iuO":
        # A floating pivot mean rounds even a cell containing a single large
        # integer. Retain those cells, including holes; repeated coordinates
        # still mean-average their acquired samples, using exact arithmetic.
        rows, row_indices = np.unique(indep1, return_inverse=True)
        columns, column_indices = np.unique(indep2, return_inverse=True)
        grid = np.full((len(rows), len(columns)), np.nan, dtype=object)
        cells: dict[tuple[int, int], list[Any]] = {}
        for index, (row, column, value) in enumerate(
            zip(row_indices, column_indices, values, strict=True)
        ):
            if index % 1024 == 0:
                check_cancelled()
            if isinstance(value, np.generic):
                value = value.item()
            if not np.isfinite(float(value)):
                continue
            cells.setdefault((row, column), []).append(value)
        for index, (position, samples) in enumerate(cells.items()):
            if index % 1024 == 0:
                check_cancelled()
            if len(samples) == 1:
                grid[position] = samples[0]
            else:
                mean = sum((Fraction(value) for value in samples), Fraction()) / len(samples)
                if mean.denominator == 1:
                    grid[position] = int(mean)
                else:
                    # Decimal keeps the offset and fractional detail while
                    # remaining a numeric CSV cell (Fraction writes "a/b").
                    with localcontext() as context:
                        context.prec = max(34, len(str(abs(mean.numerator))) + 17)
                        grid[position] = Decimal(mean.numerator) / Decimal(mean.denominator)
        check_cancelled()
        return pd.DataFrame(grid, index=rows, columns=columns)

    # convert to 3 column dataframe
    df = pd.DataFrame({
        'indep1': indep1,
        'indep2': indep2,
        'depvar': depvar
    })
    # convert dataframe to grid
    matrix = df.pivot_table(index='indep1', columns='indep2', values='depvar', fill_value=np.nan)
    return matrix


def unpack_param(dataset : dataset.data_set.DataSet, paramName : str):
    """
    Gets specified parameter from list all parameters in a dataset.

    Parameters
    ----------
    dataset : qcodes.dataset.data_set.DataSet
        Dataset to ook through.
    paramName : str
        Name of parameter.

    Returns
    -------
    ParamSpec : qcodes.dataset.descriptions.param_spec.ParamSpec
        The desired parameter data.

    """
    for ParamSpec in dataset.get_parameters():
        if ParamSpec.name == paramName:
            return ParamSpec
