from os.path import isfile

from qcodes.dataset.sqlite.database import get_DB_location

from qplot.datahandling.readonly import qcodes_read_only_connection


def repair():
    """
    Attempts to remove SQL lock that can happens on crashes while in IDE

    """
    if isfile(get_DB_location()):  # close conn is already open by mistake
        qcodes_read_only_connection(get_DB_location()).close()
