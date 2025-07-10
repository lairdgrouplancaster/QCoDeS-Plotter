# -*- coding: utf-8 -*-
"""
Created on Thu Jul 10 10:57:06 2025

@author: Benjamin Wordsworth
"""
from itertools import chain
from operator import attrgetter
from qcodes.dataset.experiment_container import experiments

def get_runs_from_db(start: int = 0,
                     stop: int = None,
                     ) -> list:
    datasets = sorted(
        chain.from_iterable(exp.data_sets() for exp in experiments()),
        key=attrgetter('run_id')
    )

    # There is no need for checking whether ``stop`` is ``None`` because if
    # it is the following is simply equivalent to ``datasets[start:]``
    datasets = datasets[start:stop]

    overview = [ds.run_id for ds in datasets]
    
    return overview