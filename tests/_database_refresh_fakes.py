"""Arrange refresh ownership on focused window doubles."""


class RefreshObservations:
    from qplot.windows._database_actions import DatabaseActionsMixin as _Actions

    _database_refresh_generation = _Actions._database_refresh_generation
    _database_refresh_active = _Actions._database_refresh_active
    _database_refresh_pending = _Actions._database_refresh_pending
    _database_refresh_worker = _Actions._database_refresh_worker
    _database_refresh_instance = _Actions._database_refresh_instance
    _database_refresh_staged_new_runs = _Actions._database_refresh_staged_new_runs
    _database_refresh_publication_active = _Actions._database_refresh_publication_active


def seed_refresh(owner, **values):
    from qplot.windows._database_actions import DatabaseActionsMixin
    from qplot.windows._database_refresh_controller import RefreshSession

    if "_database_refresher" not in owner.__dict__:
        DatabaseActionsMixin._init_database_refresher(owner)
    controller = owner._database_refresher
    for name, value in values.items():
        if name == "generation":
            controller._generation = value
            if controller.session:
                controller.session.generation = value
        elif name == "active":
            if not value:
                controller._session = None
            elif controller.session is None:
                field = getattr(owner, "fileTextbox", None)
                path = field.text() if field else ""
                controller._session = RefreshSession(controller.generation, path, None)
        elif name == "publication_active":
            controller._publication_active = value
        else:
            if controller.session is None:
                if value is None or value is False or value == {}:
                    continue
                seed_refresh(owner, active=True)
            session = controller.session
            if name == "instance":
                session.instance = value
                if value is not None:
                    field = getattr(owner, "fileTextbox", None)
                    session.path = field.text() if field else value.logical_path
            elif name == "worker":
                session.worker = value
            elif name == "pending":
                session.pending = value
            elif name == "staged_new_runs":
                session.staged_runs = dict(value)
            else:
                raise AssertionError(f"Unknown refresh state: {name}")
