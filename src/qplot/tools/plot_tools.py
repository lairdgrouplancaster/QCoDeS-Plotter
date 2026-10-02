"""
FUNCTIONS TO BE PASSED TO THE WORKER VIA OPERATIONS
PLEASE IMPORT INTO qplot.windows._widget.operations AND ADD TO RESPECTIVE CLASS
AT BOTTOM OF FILE. 
The code should handle the rest.

Function .worker.loader.do_operations() will give each operation
data_dict{x : np.array, y : np.array, z : np.array | None} as only arguemnt.
The operations tab can pass 1 user defined input of type: int, float, str or a
list of options.
To pass other arguments, please use lambda functions, i.e.:
    "func" : lambda data_dict: subtract_mean("x", data_dict)

.worker.loader.do_operations() expects a dictionary to be returned which is 
used to find which properties to update the keyed value.
"""
from decimal import Decimal
from fractions import Fraction

import numpy as np

from qplot.datahandling.parameter_data import numeric_isfinite, numeric_isnan


def _integer_differences(values, axis=0):
    """Subtract before conversion; int64 and uint64 gaps can exceed int64."""
    exact_values = values.astype(object)
    try:
        differences = np.diff(exact_values, axis=axis)
    except TypeError:
        # A limit can follow object data containing both binary floats and
        # decimal clipped cells; Fraction makes their subtraction compatible.
        fractions = np.frompyfunc(Fraction, 1, 1)(exact_values)
        differences = np.diff(fractions, axis=axis)
    return differences.astype(np.float64)


def _integer_samples(values):
    if values.dtype.kind in "iu":
        return True
    # Joined QCoDeS records can use object cells to retain mixed int64/uint64.
    return values.dtype.kind == "O" and all(
        isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_))
        for value in values.flat
    )


def _numeric_object_samples(values):
    return values.dtype.kind == "O" and all(
        isinstance(value, (int, float, Decimal, Fraction, np.integer, np.floating))
        and not isinstance(value, (bool, np.bool_))
        for value in values.flat
    )


def _gradient_with_integer_samples(data, spacing, axis, integer_data):
    """Use gradient's first-order edges and three-point interior stencil."""
    samples = np.moveaxis(np.asarray(data), axis, -1)
    result = np.empty(samples.shape, dtype=np.float64)
    if integer_data:
        differences = _integer_differences(samples, axis=-1)
    else:
        samples = samples.astype(np.float64, copy=False)

    if np.all(spacing == spacing[0]):
        dx = spacing[0]
        if integer_data:
            result[..., 1:-1] = (differences[..., :-1] + differences[..., 1:]) / (2. * dx)
        else:
            result[..., 1:-1] = (samples[..., 2:] - samples[..., :-2]) / (2. * dx)
    else:
        dx1, dx2 = spacing[:-1], spacing[1:]
        a = -dx2 / (dx1 * (dx1 + dx2))
        c = dx1 / (dx2 * (dx1 + dx2))
        if integer_data:
            result[..., 1:-1] = -a * differences[..., :-1] + c * differences[..., 1:]
        else:
            b = (dx2 - dx1) / (dx1 * dx2)
            result[..., 1:-1] = (
                a * samples[..., :-2] + b * samples[..., 1:-1]
                + c * samples[..., 2:]
            )

    if integer_data:
        result[..., 0] = differences[..., 0] / spacing[0]
        result[..., -1] = differences[..., -1] / spacing[-1]
    else:
        result[..., 0] = (samples[..., 1] - samples[..., 0]) / spacing[0]
        result[..., -1] = (samples[..., -1] - samples[..., -2]) / spacing[-1]
    return np.moveaxis(result, -1, axis)


def _check_cancelled(cancelled_callback):
    if cancelled_callback is not None and cancelled_callback():
        raise InterruptedError("Plot operation cancelled.")


def subtract_mean(
        axis : str,
        data_dict : dict,
        cancelled_callback=None,
        ):
    """
    Subtracts the mean from the dataGrid based on the axis.
    
    Parameters
    ----------
    axis : str
        Which axis to caculate the mean on.
        run through rows (axis="y")
        run through cols (axis="x")
    data_dict : dict{str, np.ndarry}
        This function only uses data_dict["z"] : 
        the 2d numpy array dataGrid of the plot to opperate on
        
    Returns
    -------
    dataGrid : dict{str: np.ndarray}
        returns the updated dictionary in the the form:
            {"z": dataGrid}
    
    """
    dataGrid = data_dict["z"]
    num_axis = 1 if axis == "x" else 0

    _check_cancelled(cancelled_callback)
    if dataGrid.dtype.kind in "iuO":
        # Center exact samples before conversion; NaN holes remain missing.
        # Work one row/column at a time so this path stays cancellable.
        samples = np.moveaxis(dataGrid, num_axis, -1)
        centered = np.full(samples.shape, np.nan, dtype=float)
        for position in np.ndindex(samples.shape[:-1]):
            _check_cancelled(cancelled_callback)
            row = samples[position]
            valid = ~numeric_isnan(row)
            values = row[valid]
            if not values.size:
                continue
            if np.all(numeric_isfinite(values)):
                exact = [Fraction(value.item() if isinstance(value, np.generic) else value)
                         for value in values]
                mean = sum(exact) / len(exact)
                centered[position][valid] = [float(value - mean) for value in exact]
            else:
                numeric = values.astype(float)
                centered[position][valid] = numeric - np.mean(numeric)
        dataGrid = np.moveaxis(centered, -1, num_axis)
    else:
        # Narrow floats must be widened before accumulation and subtraction.
        dataGrid = dataGrid.astype(np.result_type(dataGrid.dtype, np.float64), copy=False)
        mean = np.nanmean(dataGrid, axis=num_axis, keepdims=True)
        _check_cancelled(cancelled_callback)
        dataGrid = dataGrid - mean
    _check_cancelled(cancelled_callback)
    
    return {"z" : dataGrid}
    

def pass_filter(
        which : str,
        limit : float | Decimal,
        data_dict : dict,
        cancelled_callback=None,
        ):
    """
    Filters dependant parameter data to set values outside the limit to the 
    limit
    
    Parameters
    ----------
    which : str
        Whether to do a low or high pass filter.
        low - sets maximum allowed value
        high - sets minimum allowed value
    limit : float
        The boundary value.
    data_dict : dict{str, np.ndarry}
        The data array to operate on.
        This uses the dependant parameter data. (data_dict["y"] or data_dict["z"])
        Integer samples that remain inside the bound retain their exact value.
        Partial fractional clipping may return mixed integer/decimal object cells.
        
    Returns
    -------
    data : dict{str: np.ndarray}
        returns the updated dictionary in the the form:
            {"z": new_data} for 2d
            or 
            {"y": new_data} for 1d
    
    """
    # Get y for 1d or z for 2d
    axis = "z" if data_dict["z"] is not None else "y"
    data = np.asarray(data_dict[axis])
    
    # The controls retain the user's decimal text; direct float callers keep
    # their float value. Compare integers with a decimal bound exactly.
    exact_limit = limit if isinstance(limit, Decimal) else Decimal(str(limit))
    float_limit = float(limit)

    # Set the bounds for arrays that already contain floating-point samples.
    limit_arr: tuple[float | None, float | None]
    if which == "low":
        limit_arr = (None, float_limit)
    elif which == "high":
        limit_arr = (float_limit, None)
    else:
        raise KeyError(f'Invalid value for which: {which}. Must be: "high" or "low"')
    
    _check_cancelled(cancelled_callback)
    if data.dtype.kind in "iuO" and not exact_limit.is_nan():
        # NumPy promotes an integer array to float before clipping against a
        # float bound, even when no element changes. Compare Python integers
        # with the bound first, so adjacent int64/uint64 values stay distinct.
        exact_data = data.astype(object)
        clipped = exact_data > exact_limit if which == "low" else exact_data < exact_limit
        if not np.any(clipped):
            new_data = data.copy()
        elif (data.dtype.kind in "iu" and exact_limit.is_finite()
              and exact_limit == exact_limit.to_integral_value()
              and int(np.iinfo(data.dtype).min) <= exact_limit
              <= int(np.iinfo(data.dtype).max)):
            new_data = data.copy()
            new_data[clipped] = int(exact_limit)
        elif np.all(clipped):
            new_data = np.full(data.shape, exact_limit, dtype=object)
        else:
            # A fractional bound and unchanged large integers need different
            # scalar types. Object cells retain both exactly for later ops and
            # the original-data CSV exporter.
            new_data = exact_data
            new_data[clipped] = exact_limit
    else:
        new_data = np.clip(data, *limit_arr)
    _check_cancelled(cancelled_callback)
    
    return {axis : new_data}


def differentiate(
        dx : str,
        data_dict : dict,
        cancelled_callback=None,
        ):
    """
    Differentiates the dependant parameter data with respect to the input dx    

    Parameters
    ----------
    dx : str
        The axis to perform the differentiation against.
    data_dict : dict{str : np.ndarry}
        The data array to operate on.
        This uses the dependant parameter data. (data_dict["y"] or data_dict["z"])
        and an independant to find spacing. Coordinates must be finite and
        strictly increasing or decreasing in their supplied order.

    Returns
    -------
    data : dict{str: np.ndarray}
        returns the updated dictionary in the the form:
            {"z": new_data} for 2d
            or 
            {"y": new_data} for 1d

    """
    _check_cancelled(cancelled_callback)
    if dx not in ["x", "y"]:
        raise KeyError(f'Invalid value for dx: {dx}, must be "x" or "y".')
    
    # Get y for 1d or z for 2d
    if data_dict["z"] is not None:
        key = "z"
        axis_num = 1 if dx == "x" else 0
        
    else:
        key = "y"
        axis_num = 0
       
    data = np.asarray(data_dict[key])
    coordinates = np.asarray(data_dict[dx])
    if coordinates.ndim != 1 or coordinates.size < 2:
        raise ValueError("Differentiation requires at least two axis coordinates.")
    integer_coordinates = _integer_samples(coordinates)
    if integer_coordinates:
        spacing = _integer_differences(coordinates)
    else:
        coordinates = coordinates.astype(float)
        spacing = np.diff(coordinates)
    if ((not integer_coordinates and not np.all(np.isfinite(coordinates)))
            or not np.all(np.isfinite(spacing))):
        raise ValueError("Differentiation axis coordinates must be finite.")
    if np.any(spacing == 0):
        raise ValueError("Differentiation axis coordinates must not repeat.")
    # Reversals can make gradient's three-point stencils singular. Keep the
    # acquisition order: sorting or merging samples would erase hysteresis.
    if not (np.all(spacing > 0) or np.all(spacing < 0)):
        raise ValueError(
            "Differentiation axis coordinates must be strictly increasing or "
            "decreasing; reversing sweeps are not supported."
        )

    _check_cancelled(cancelled_callback)
    integer_data = _integer_samples(data) or _numeric_object_samples(data)
    if integer_coordinates or integer_data:
        new_data = _gradient_with_integer_samples(
            data, spacing, axis_num, integer_data,
        )
    else:
        data = data.astype(np.result_type(data.dtype, np.float64), copy=False)
        new_data = np.gradient(data, coordinates, axis=axis_num)
    _check_cancelled(cancelled_callback)
    
    return {key : new_data}


def fill_heatmap(
        which : str,
        data_dict : dict,
        max_depth : int = 10,
        cancelled_callback=None,
        ):
    _check_cancelled(cancelled_callback)
    data = data_dict["z"].copy()
    if which == "below":
        lines = (data[:, column] for column in range(data.shape[1]))
    elif which == "right":
        lines = (data[row, :] for row in range(data.shape[0]))
    else:
        raise KeyError(f'Invalid value for which: {which}, must be "below" or "right".')

    if max_depth <= 0:
        return {"z": data}

    for line in lines:
        _check_cancelled(cancelled_callback)
        position = 0
        while position < len(line):
            if not numeric_isnan(line[position]):
                position += 1
                continue

            gap_start = position
            while position < len(line) and numeric_isnan(line[position]):
                if position % 1024 == 0:
                    _check_cancelled(cancelled_callback)
                position += 1

            gap_length = position - gap_start
            bounded = gap_start > 0 and position < len(line)
            if bounded and gap_length <= max_depth:
                line[gap_start:position] = line[gap_start - 1]

    _check_cancelled(cancelled_callback)
    return {"z" : data}
        

def integrate(
        dx : str,
        data_dict : dict
        ):
    # TO DO
    pass
