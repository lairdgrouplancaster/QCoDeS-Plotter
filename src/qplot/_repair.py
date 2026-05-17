from os.path import isfile

from qcodes.dataset.sqlite.database import conn_from_dbpath_or_conn, get_DB_location


def repair():
    """
    Attempts to remove SQL lock that can happens on crashes while in IDE

    """
    if isfile(get_DB_location()): #close conn is already open by mistake
        conn_from_dbpath_or_conn(None, get_DB_location(), read_only=True).close()
    
