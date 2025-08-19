"""
FUNCTIONS TO BE PASSED TO THE WORKER VIA OPERATIONS

Function .worker.loader.do_operations() will give each operation
data_dict{x : np.array, y : np.array, z : np.array | None} as only arguemnt.
To pass other arguments, please use lambda functions, i.e.:
    operations["subtract_mean_x"] = lambda data_dict: subtract_mean("x", data_dict)

.worker.loader.do_operations() expects a dictionary to be returned which is 
used to find which properties to update the keyed value.
"""
import numpy as np

def subtract_mean(axis : str,
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
    