from math import trunc
import numpy as np
import pandas as pd
from qcodes import dataset

def progressBar(curItr, totItr, length : int = 50):
    
    ratio = curItr/totItr #percent complete of the simulation
    filled = trunc(ratio * length)
    #uses a bar for completed blocks and a dash for uncompleted blocks, the dash is about half the length of the blocks
    bar = filled*"█" + "-"*(length-filled)
    print(f"Progress: |{bar}| {(ratio*100):.2f}% Complete", end="\r")
    
def data2matrix(indep1 : np.array,
                indep2 : np.array,
                depvar : np.array,
                ):
    
    
    df = pd.DataFrame({
        'indep1': indep1,
        'indep2': indep2,
        'depvar': depvar
    })
    
    
    # df_sorted = df.sort_values(['indep1', 'indep2'], ascending=True)
    
    matrix = df.pivot_table(index='indep1', columns='indep2', values='depvar', fill_value=np.nan)
    return matrix

def unpack_param(dataset : dataset.data_set.DataSet, paramName : str):
    
    for ParamSpec in dataset.get_parameters():
        if ParamSpec.name == paramName:
            return ParamSpec
        
        
def find_indep(dataset : dataset.data_set.DataSet, 
               param : dataset.ParamSpec
               ):
    depenancies = param.depends_on.split(",")
    
    
    return {name: dataset.get_parameter_data(name) for name in depenancies}
    