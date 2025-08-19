

def subtract_mean(axis : str,
                   x : "np.array",
                   y : "np.array",
                   z : "np.ndarray",
                   ):
    """
    Subtracts the mean from the dataGrid based on the axis.
    
    
    Parameters
    ----------
    axis : str
        Which axis to caculate the mean on.
        run through rows (axis="y")
        run through cols (axis="x")
    x : np.array
        Unused by this function
    y : np.array
        Unused by this function
    z : np.ndrray
        Entered via kargs, 
        the 2d numpy array dataGrid of the plot to opperate on
        
    Returns
    -------
    dataGrid : dict{str: np.ndarray}
        returns the updated dictionary in the the form:
            {"z": dataGrid}
    
    """
    dataGrid = z
    num_axis = 0 if axis == "x" else 1
    
    mean = dataGrid.nanmean(axis=num_axis, keepdims=True)
    
    dataGrid = dataGrid - mean
    
    return {"z" : dataGrid}
    