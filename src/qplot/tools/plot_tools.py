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

def subtract_mean(
        axis : str,
        data_dict : dict
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
    num_axis = 0 if axis == "x" else 1
    
    mean = np.nanmean(dataGrid, axis=num_axis, keepdims=True)
    
    dataGrid = dataGrid - mean
    
    return {"z" : dataGrid}
    

def pass_filter(
        which : str,
        limit : float,
        data_dict : dict
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
    if which == "low":
        limit_arr = (None, limit)
    elif which == "high":
        limit_arr = (limit, None)
    else:
        raise KeyError(f'Invalid value for which: {which}. Must be: "high" or "low"')
    
    new_data = np.clip(data, *limit_arr)
    
    return {axis : new_data}


def differentiate(
        dx : str,
        data_dict : dict
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
    if dx not in ["x", "y"]:
        raise KeyError(f'Invalid value for dx: {dx}, must be "x" or "y".')
    
    # Get y for 1d or z for 2d
    if data_dict["z"] is not None:
        key = "z"
        axis_num = 0 if dx == "x" else 1
        
    else:
        key = "y"
        axis_num = 0
       
    data = data_dict[key]
    new_data = np.gradient(data, data_dict[dx], axis=axis_num)
    
    return {key : new_data}


def fill_heatmap(
        which : str,
        data_dict : dict,
        max_depth : int = 10
        ):
    def set_value_row_major(major, minor, data):
        new_val = data[major - 1, minor]
        if np.isnan(new_val):
            return
        data[major, minor] = new_val
        
    def set_value_col_major(major, minor, data):
        new_val = data[minor, major - 1]
        if np.isnan(new_val):
            return
        data[minor, major] = new_val
        
    data = data_dict["z"].copy()
    if which == "below":
        fetch_row = lambda major: data[major]
        set_item = set_value_row_major
    elif which == "right":
        fetch_row = lambda major: data[:, major]
        set_item = set_value_col_major
    else:
        raise KeyError(f'Invalid value for which: {which}, must be "below" or "right".')
        
    shape = data.shape
    length = shape[0] if which == "below" else shape[1]
        
    
    truth_arr = np.array([np.isnan(fetch_row(0))])
    initial_size = max_depth if length >= max_depth else length
    
    for itr in range(initial_size):
        truth_arr = np.append(
            truth_arr, 
            [np.isnan(fetch_row(itr))],
            axis = 0
            )
    
    for major_itr in range(1, length - 1):
        truth_arr = truth_arr[1:] # remove first item
        
        try:
            truth_arr = np.append(
                truth_arr, 
                [np.isnan(fetch_row(major_itr + max_depth))],
                axis = 0
                )
        except IndexError: # Ignore error for last few data points
            pass
        
        for minor_itr, minor_val in enumerate(truth_arr[0]):
            # print(truth_arr[:, minor_itr].all())
            if minor_val and not truth_arr[:, minor_itr].all():
                # print("updating data", minor_val)
                set_item(major_itr, minor_itr, data)
        
    return {"z" : data}
        

def integrate(
        dx : str,
        data_dict : dict
        ):
    # TO DO
    pass 