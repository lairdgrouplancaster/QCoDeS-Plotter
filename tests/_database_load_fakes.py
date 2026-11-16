"""Small widget contracts shared by database lifecycle harnesses."""


class PreviewPublicationGate:
    """Expose the RunList gate, including restoration of an already-held gate."""

    preview_publication_suspended = False

    def set_preview_publication_suspended(self, suspended):
        previous = self.preview_publication_suspended
        self.preview_publication_suspended = bool(suspended)
        return previous


class LoadObservations:
    """Expose the same read-only load observations on focused window doubles."""

    from qplot.windows._database_actions import DatabaseActionsMixin as _Actions

    _database_load_generation = _Actions._database_load_generation
    _database_load_active = _Actions._database_load_active
    _database_load_state = _Actions._database_load_state
    _database_load_worker = _Actions._database_load_worker
    _database_load_publication_active = _Actions._database_load_publication_active
    _pending_trusted_read_services = _Actions._pending_trusted_read_services


def seed_load(owner, **values):
    """Arrange controller state without making production observations writable."""
    from qplot.datahandling.file_identity import database_instance
    from qplot.windows._database_actions import DatabaseActionsMixin
    from qplot.windows._database_load_state import DatabaseLoadSession

    if "_database_loader" not in owner.__dict__:
        DatabaseActionsMixin._init_database_loader(owner)
    loader = owner._database_loader
    for name, value in values.items():
        if name == "generation":
            loader._generation = value
        elif name == "active":
            if value and loader.session is None:
                loader._session = DatabaseLoadSession("", database_instance(""))
            elif not value:
                loader._session = None
        elif name == "state":
            if value is not None and value.worker is None:
                value.worker = loader.worker
            loader._session = value
        elif name == "worker":
            if value is not None and loader.session is None:
                loader._session = DatabaseLoadSession("", database_instance(""))
            if loader.session is not None:
                loader.session.worker = value
        elif name == "publication_active":
            loader._publication_active = value
        elif name == "pending_services":
            loader._pending_services = dict(value)
        else:
            raise AssertionError(f"Unknown load state: {name}")
