import numpy as np
import pandas as pd
from qcodes import dataset
    
def data2matrix(indep1 : np.array,
                indep2 : np.array,
                depvar : np.array,
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
    