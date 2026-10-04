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
import math
from decimal import Decimal
from fractions import Fraction

import numpy as np

from qplot.datahandling.parameter_data import numeric_isfinite, numeric_isnan


def _integer_differences(values, axis=0, step=1):
    """Subtract before conversion; int64 and uint64 gaps can exceed int64."""
    exact_values = np.moveaxis(values.astype(object), axis, -1)
    try:
        differences = exact_values[..., step:] - exact_values[..., :-step]
    except TypeError:
        # A limit can follow object data containing both binary floats and
        # decimal clipped cells; Fraction makes their subtraction compatible.
        # Missing cells and infinities cannot be converted to Fraction. Keep
        # those as floats so they propagate through only their own stencils.
        fractions = exact_values.copy()
        finite = numeric_isfinite(exact_values)
        fractions[finite] = np.frompyfunc(Fraction, 1, 1)(exact_values[finite])
        fractions[~finite] = exact_values[~finite].astype(float)
        differences = fractions[..., step:] - fractions[..., :-step]
    return np.moveaxis(differences.astype(np.float64), -1, axis)


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


def _gradient_with_integer_samples(
        data, coordinates, spacing, axis, integer_data, *, cancelled_callback=None):
    """Use gradient's first-order edges and three-point interior stencil."""
    samples = np.moveaxis(np.asarray(data), axis, -1)
    dtype = np.float64 if integer_data else np.result_type(samples.dtype, np.float64)
    result = np.empty(samples.shape, dtype=dtype)
    if integer_data:
        differences = _integer_differences(samples, axis=-1)
    else:
        samples = samples.astype(dtype, copy=False)
        with np.errstate(over="ignore", invalid="ignore"):
            differences = np.diff(samples, axis=-1)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        slopes = differences / spacing
    if not integer_data:
        # A finite sample jump can exceed float's range before division by a
        # large, finite spacing restores a representable secant slope.
        repair = (~np.isfinite(differences) & np.isfinite(samples[..., 1:])
                  & np.isfinite(samples[..., :-1]))
        if np.any(repair):
            distances = np.broadcast_to(spacing, differences.shape)
            slopes[repair] = (
                samples[..., 1:][repair] / 2. - samples[..., :-1][repair] / 2.
            ) / (distances[repair] / 2.)

    if np.all(spacing == spacing[0]):
        dx = spacing[0]
        if integer_data:
            # The uniform central stencil uses only its two outer samples.
            # Summing adjacent differences would involve a missing centre and
            # erase a valid derivative (or subtract opposite infinities).
            outer_difference = _integer_differences(samples, axis=-1, step=2)
            if abs(dx) > np.finfo(float).max / 2:
                result[..., 1:-1] = (outer_difference / 2.) / dx
            else:
                result[..., 1:-1] = outer_difference / (2. * dx)
        else:
            with np.errstate(over="ignore", invalid="ignore"):
                outer_difference = samples[..., 2:] - samples[..., :-2]
                denominator = 2. * dx
                central = outer_difference / denominator
            # Both the two-step difference and twice the spacing can exceed
            # float's range even when their quotient is small. Halve the
            # operands first only for those stencils; ordinary small values
            # retain the direct subtraction's precision and missingness.
            repair = (np.isfinite(samples[..., 2:])
                      & np.isfinite(samples[..., :-2])
                      & (~np.isfinite(outer_difference) | ~np.isfinite(denominator)))
            if np.any(repair):
                central[repair] = (
                    samples[..., 2:][repair] / 2. - samples[..., :-2][repair] / 2.
                ) / dx
            result[..., 1:-1] = central
    else:
        dx1, dx2 = spacing[:-1], spacing[1:]
        # Blend the adjacent secant slopes. Normalized spacings avoid
        # under/overflow in the products used by the equivalent three-point
        # coefficients, and subtraction still precedes weighting so stored
        # offsets cannot erase representable differences.
        scale = np.maximum(np.abs(dx1), np.abs(dx2))
        before, after = dx1 / scale, dx2 / scale
        total = before + after
        with np.errstate(over="ignore", invalid="ignore"):
            left_term = (after / total) * slopes[..., :-1]
            right_term = (before / total) * slopes[..., 1:]
            central = left_term + right_term
        if samples.dtype.kind != "c":
            # Individual secants can overflow while their weighted stencil
            # remains finite. Recover only fully finite source stencils;
            # acquired NaN/Inf samples must retain their usual propagation.
            # Finite opposing terms can also cancel after their individual
            # differences or weights have rounded. Recover small residuals
            # from the recorded stencil, not its already rounded secants.
            cancellation = ((np.signbit(left_term) != np.signbit(right_term))
                            & (np.abs(central) <= np.sqrt(np.finfo(float).eps)
                               * np.maximum(np.abs(left_term), np.abs(right_term))))
            repair = ((~np.isfinite(central) | cancellation)
                      & numeric_isfinite(samples[..., :-2])
                      & numeric_isfinite(samples[..., 1:-1])
                      & numeric_isfinite(samples[..., 2:]))
            for index, position in enumerate(zip(*np.nonzero(repair), strict=True)):
                if index % 1024 == 0:
                    _check_cancelled(cancelled_callback)
                centre = position[-1] + 1
                row = position[:-1]

                def exact(value):
                    return Fraction(value.item() if isinstance(value, np.generic) else value)

                # Subtract the original coordinates exactly. Rounded spacing
                # loses the small residual when opposing large slopes cancel.
                left, middle, right = map(exact, coordinates[centre - 1:centre + 2])
                before_step, after_step = middle - left, right - middle
                before_value, middle_value, after_value = map(
                    exact, samples[row][centre - 1:centre + 2],
                )
                derivative = (
                    after_step * (middle_value - before_value) / before_step
                    + before_step * (after_value - middle_value) / after_step
                ) / (before_step + after_step)
                try:
                    central[position] = float(derivative)
                except OverflowError:
                    central[position] = np.inf if derivative > 0 else -np.inf
        result[..., 1:-1] = central

    result[..., 0] = slopes[..., 0]
    result[..., -1] = slopes[..., -1]
    return np.moveaxis(result, -1, axis)


def _check_cancelled(cancelled_callback):
    if cancelled_callback is not None and cancelled_callback():
        raise InterruptedError("Plot operation cancelled.")


def _center_float_samples(values, axis=-1, *, ignore_nan=False, cancelled_callback=None):
    """Remove a float mean without rounding a large offset into the result.

    Subtract a recorded anchor before accumulation. Rows whose range exceeds
    float64 use a scaled mean instead, retaining the usual NaN/Inf semantics.
    Process groups of complete rows, checking cancellation between groups.
    Temporary arrays contain at most 65536 cells or one complete row.
    """
    values = np.asarray(values)
    dtype = np.result_type(values.dtype, np.float64)
    samples = np.moveaxis(values.astype(dtype, copy=False), axis, -1)
    if samples.shape[-1] == 0:
        return values.astype(dtype, copy=True)
    rows = samples.reshape(-1, samples.shape[-1])
    centered = np.empty(rows.shape, dtype=dtype)
    mean_func = np.nanmean if ignore_nan else np.mean
    chunk_size = max(1, 65536 // samples.shape[-1])
    for start in range(0, len(rows), chunk_size):
        _check_cancelled(cancelled_callback)
        chunk = rows[start:start + chunk_size]
        finite = np.isfinite(chunk)
        indices = np.argmax(finite, axis=1)
        anchors = chunk[np.arange(len(chunk)), indices, None]
        with np.errstate(over="ignore", invalid="ignore"):
            shifted = chunk - anchors
            offset_mean = mean_func(shifted, axis=1, keepdims=True)
            result = shifted - offset_mean
            overflow = (np.any(np.isinf(shifted) & finite, axis=1)
                        | ~np.isfinite(offset_mean[:, 0]))
            infinite_source = np.any(np.isinf(chunk), axis=1)
            expected_finite_mean = (np.any(finite, axis=1) if ignore_nan
                                    else np.all(finite, axis=1))
            fallback = overflow & expected_finite_mean & ~infinite_source
            if np.any(fallback):
                large = chunk[fallback]
                scale = np.nanmax(np.abs(large), axis=1, keepdims=True)
                mean = mean_func(large / scale, axis=1, keepdims=True) * scale
                result[fallback] = large - mean
            # Anchoring around a large positive/negative sample can round
            # away small source cells across zero before the mean is formed.
            # Sum those original values accurately; nearby same-sign offsets
            # keep the anchor path so fractional offset means remain intact.
            mixed_sign = (np.any(chunk < 0, axis=1) & np.any(chunk > 0, axis=1)
                          & expected_finite_mean & ~infinite_source)
            for row_index in np.flatnonzero(mixed_sign):
                _check_cancelled(cancelled_callback)
                original = chunk[row_index][finite[row_index]]

                def values_for_sum(values):
                    for index, value in enumerate(values):
                        if index % 1024 == 0:
                            _check_cancelled(cancelled_callback)
                        yield float(value)

                nonzero = np.abs(original[original != 0])
                # With more than a mantissa's range between samples, rounding
                # the mean itself can change the small residual by one ULP.
                # Subtract exact means in just these sensitive rows.
                exact_required = np.max(nonzero) / (2. ** 53) > np.min(nonzero)
                if not exact_required:
                    try:
                        mean = math.fsum(values_for_sum(original)) / len(original)
                    except OverflowError:
                        # The exact total can overflow despite a finite mean.
                        exact_required = True
                if exact_required:
                    total = Fraction()
                    exact_values = []
                    for value in values_for_sum(original):
                        exact_value = Fraction(value)
                        exact_values.append(exact_value)
                        total += exact_value
                    exact_mean = total / len(original)
                    residuals = np.empty(len(original))
                    for index, value in enumerate(exact_values):
                        if index % 1024 == 0:
                            _check_cancelled(cancelled_callback)
                        difference = value - exact_mean
                        try:
                            residuals[index] = float(difference)
                        except OverflowError:
                            residuals[index] = np.inf if difference > 0 else -np.inf
                    result[row_index, finite[row_index]] = residuals
                else:
                    result[row_index] = chunk[row_index] - mean
            if np.any(infinite_source):
                special = chunk[infinite_source]
                result[infinite_source] = special - mean_func(special, axis=1, keepdims=True)
        centered[start:start + len(chunk)] = result
    _check_cancelled(cancelled_callback)
    return np.moveaxis(centered.reshape(samples.shape), -1, axis)


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
    elif dataGrid.dtype.kind == "f":
        dataGrid = _center_float_samples(
            dataGrid, num_axis, ignore_nan=True,
            cancelled_callback=cancelled_callback,
        )
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
        # Decimal comparisons with NaN raise InvalidOperation. Missing cells
        # are never clipped, including holes in an integer heatmap.
        valid = ~numeric_isnan(exact_data)
        clipped = np.zeros(data.shape, dtype=bool)
        clipped[valid] = (exact_data[valid] > exact_limit if which == "low"
                          else exact_data[valid] < exact_limit)
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
    new_data = _gradient_with_integer_samples(
        data, coordinates, spacing, axis_num, integer_data,
        cancelled_callback=cancelled_callback,
    )
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
