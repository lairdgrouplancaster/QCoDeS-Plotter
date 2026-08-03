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
import numpy as np


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
    mean = np.nanmean(dataGrid, axis=num_axis, keepdims=True)
    _check_cancelled(cancelled_callback)
    dataGrid = dataGrid - mean
    _check_cancelled(cancelled_callback)
    
    return {"z" : dataGrid}
    

def pass_filter(
        which : str,
        limit : float,
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
    data = data_dict[axis]
    
    # Set the bounds
    limit_arr: tuple[float | None, float | None]
    if which == "low":
        limit_arr = (None, limit)
    elif which == "high":
        limit_arr = (limit, None)
    else:
        raise KeyError(f'Invalid value for which: {which}. Must be: "high" or "low"')
    
    _check_cancelled(cancelled_callback)
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
        and an independant to find spacing.

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
       
    data = data_dict[key]
    coordinates = np.asarray(data_dict[dx], dtype=float)
    if coordinates.ndim != 1 or coordinates.size < 2:
        raise ValueError("Differentiation requires at least two axis coordinates.")
    if not np.all(np.isfinite(coordinates)):
        raise ValueError("Differentiation axis coordinates must be finite.")
    if np.any(np.diff(coordinates) == 0):
        raise ValueError("Differentiation axis coordinates must not repeat.")

    _check_cancelled(cancelled_callback)
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
            if not np.isnan(line[position]):
                position += 1
                continue

            gap_start = position
            while position < len(line) and np.isnan(line[position]):
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
