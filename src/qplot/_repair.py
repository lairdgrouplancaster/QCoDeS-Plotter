from qcodes.dataset.sqlite.database import (
    get_DB_location,
    conn_from_dbpath_or_conn
    )

from os.path import isfile

def _repair():
    """
    Attempts to remove SQL lock that can happens on crashes while in IDE

    """
    if isfile(get_DB_location()): #close conn is already open by mistake
        conn_from_dbpath_or_conn(None, get_DB_location()).close()
    