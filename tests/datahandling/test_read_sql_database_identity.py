from qplot.datahandling import readSQL

_READ_APIS = (
    "get_runs_via_sql",
    "get_runs_basic_via_sql",
    "iter_run_detail_batches_via_sql",
    "iter_run_shape_batches_via_sql",
    "iter_run_storage_batches_via_sql",
    "find_new_runs",
    "get_run_status",
    "has_finished",
)


class _Cursor:
    def execute(self, *_args, **_kwargs):
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _Connection:
    def __init__(self):
        self.closed = False

    def cursor(self):
        return _Cursor()

    def close(self):
        self.closed = True


def _install_read_doubles(monkeypatch):
    calls = []
    connections = []

    def open_connection(*args, **kwargs):
        calls.append((args, kwargs))
        connection = _Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(readSQL, "qcodes_read_only_connection", open_connection)
    monkeypatch.setattr(readSQL, "get_DB_location", lambda: "configured.db")
    monkeypatch.setattr(readSQL, "_fetch_run_rows", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(readSQL, "_run_storage_tables", lambda *_args: {})
    monkeypatch.setattr(
        readSQL,
        "_table_storage_bytes_by_name",
        lambda *_args: {},
    )
    return calls, connections


def _invoke_read_api(api_name, **identity_kwargs):
    if api_name == "get_runs_via_sql":
        readSQL.get_runs_via_sql("contract.db", **identity_kwargs)
    elif api_name == "get_runs_basic_via_sql":
        readSQL.get_runs_basic_via_sql("contract.db", **identity_kwargs)
    elif api_name == "iter_run_detail_batches_via_sql":
        list(readSQL.iter_run_detail_batches_via_sql(
            "contract.db",
            [1],
            **identity_kwargs,
        ))
    elif api_name == "iter_run_shape_batches_via_sql":
        list(readSQL.iter_run_shape_batches_via_sql(
            "contract.db",
            [1],
            **identity_kwargs,
        ))
    elif api_name == "iter_run_storage_batches_via_sql":
        list(readSQL.iter_run_storage_batches_via_sql(
            "contract.db",
            [1],
            **identity_kwargs,
        ))
    elif api_name == "find_new_runs":
        readSQL.find_new_runs(0, database_path="contract.db", **identity_kwargs)
    elif api_name == "get_run_status":
        readSQL.get_run_status(
            "run-guid",
            database_path="contract.db",
            **identity_kwargs,
        )
    elif api_name == "has_finished":
        readSQL.has_finished("run-guid", **identity_kwargs)
    else:
        raise AssertionError(f"Unhandled read API: {api_name}")


def test_public_read_sql_apis_forward_expected_database_identity(monkeypatch):
    expected_identity = (7, 11)

    for api_name in _READ_APIS:
        calls, connections = _install_read_doubles(monkeypatch)
        _invoke_read_api(
            api_name,
            expected_database_identity=expected_identity,
        )

        expected_path = "configured.db" if api_name == "has_finished" else "contract.db"
        assert calls == [(
            (expected_path,),
            {"expected_database_identity": expected_identity},
        )]
        assert len(connections) == 1
        assert connections[0].closed


def test_public_read_sql_api_defaults_omit_identity_keyword(monkeypatch):
    for api_name in _READ_APIS:
        calls, connections = _install_read_doubles(monkeypatch)
        _invoke_read_api(api_name)

        expected_path = "configured.db" if api_name == "has_finished" else "contract.db"
        assert calls == [((expected_path,), {})]
        assert len(connections) == 1
        assert connections[0].closed
