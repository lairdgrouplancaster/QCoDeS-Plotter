import numpy as np
import os
import qcodes as qc

## Multidimensional scanning module
from qcodes.dataset import (
    LinSweep,
    Measurement,
    dond,
    do1d,
    experiments,
    initialise_or_create_database_at,
    load_by_run_spec,
    load_or_create_experiment,
    plot_dataset,
)

## Dummy instruments for generating synthetic data
from qcodes.instrument_drivers.mock_instruments import (
    DummyInstrument,
    DummyInstrumentWithMeasurement,
)


qc.Instrument.close_all()
initialise_or_create_database_at(os.path.join(os.getcwd(), "tests", "data", "experiments_for_15_mins.db"))


# A dummy signal generator with two parameters ch1 and ch2
dac = DummyInstrument("dac", gates=["ch1", "ch2"])

# A dummy digital multimeter that generates a synthetic data depending
# on the values set on the setter_instr, in this case the dummy dac
dmm = DummyInstrumentWithMeasurement("dmm", setter_instr=dac)


dac.ch1(1.1)
dmm.v1()

station = qc.Station()

station.add_component(dac)
station.add_component(dmm)

tutorial_exp = load_or_create_experiment(
    experiment_name="tutorial_exp", sample_name="synthetic data"
)


context_meas = Measurement(exp=tutorial_exp, station=station, name="context_example")


# Register the independent parameter...
context_meas.register_parameter(dac.ch1)
# ...then register the dependent parameter
context_meas.register_parameter(dmm.v1, setpoints=(dac.ch1,))

# Setting up a doNd measurement
sweep_1 = LinSweep(dac.ch1, -1, 1, 100, 0.01)
sweep_2 = LinSweep(dac.ch2, -1, 1, 100, 0.01)


dond(
    sweep_1,  # 1st independent parameter
    sweep_2,  # 2nd independent parameter
    dmm.v1,  # 1st dependent parameter
    dmm.v2,  # 2nd dependent parameter
    measurement_name="dond_example",  # Set the measurement name
    exp=tutorial_exp,  # Set the experiment to save data to.
    show_progress=True,  # Optional progress bar
    write_period=0.1,
)

do1d(dac.ch1, 0, 25, 1000, 0.01, dmm.v1, dmm.v2, write_period=0.1)
