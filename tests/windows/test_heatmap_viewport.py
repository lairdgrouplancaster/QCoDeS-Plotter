"""Viewport requests and publication outcomes without windows or workers."""

from dataclasses import replace

import pytest

from qplot.windows._heatmap_viewport import (
    AxisRanges,
    HeatmapViewportController,
    HeatmapViewportState,
)
from qplot.windows._plot_publication import PlotPublicationOwner

FULL = AxisRanges((0., 10.), (0., 20.))
ZOOM = AxisRanges((2., 4.), (3., 7.))
NEXT = AxisRanges((5., 8.), (8., 12.))


class Host:
    def __init__(self):
        self.callbacks = []
        self.loads = []
        self.running = False
        self.active = True
        self.ranges = ZOOM
        self.stops = 0
        self.accept_load = True

    def available(self):
        return self.active

    def busy(self):
        return self.running

    def visible_ranges(self):
        return self.ranges

    def schedule(self, callback):
        self.callbacks.append(callback)

    def stop_timer(self):
        self.stops += 1

    def load(self, request):
        self.loads.append(request)
        return self.accept_load


@pytest.fixture
def viewport():
    host = Host()
    controller = HeatmapViewportController(host)
    controller._accepted = HeatmapViewportState(True, FULL, FULL)
    return controller, host, PlotPublicationOwner()


def stage(controller, owner, displayed):
    worker = object()
    record = owner.begin(worker, {}, cache=None, parameter_name="signal")
    controller.stage(record, replace(controller.accepted, displayed=displayed))
    return record


def test_rapid_zoom_changes_only_load_latest_range(viewport):
    controller, host, _ = viewport
    for ranges in (ZOOM, FULL, NEXT):
        host.ranges = ranges
        controller.schedule_visible()
    for callback in host.callbacks[:]:
        callback()
    assert len(host.loads) == 1
    assert host.loads[0].ranges == NEXT
    assert controller.accepted.displayed is None


def test_full_request_survives_busy_worker_and_partial_publication(viewport):
    controller, host, owner = viewport
    host.running = True
    assert not controller.reload(full=True)
    host.callbacks[-1]()
    assert not host.loads
    record = stage(controller, owner, ZOOM)
    assert owner.commit(record, record.worker)
    controller.finish(record)
    host.running = False
    host.callbacks[-1]()
    assert len(host.loads) == 1
    assert host.loads[0].ranges is None
    assert controller.accepted.displayed == ZOOM


@pytest.mark.parametrize("outcome", ["render_failed", "rollback"])
def test_failed_render_keeps_accepted_range_and_allows_same_range_retry(viewport, outcome):
    controller, host, owner = viewport
    controller._accepted = replace(controller.accepted, displayed=ZOOM)
    previous = controller.accepted
    record = stage(controller, owner, NEXT)
    assert controller.accepted is previous
    assert getattr(owner, outcome)(record.worker, record.worker, {})
    controller.finish(record)
    assert controller.accepted is previous
    assert controller.reload()
    assert host.loads[-1].ranges == ZOOM


def test_only_committed_publication_advances_accepted_viewport(viewport):
    controller, _, owner = viewport
    record = stage(controller, owner, ZOOM)
    assert controller.accepted.displayed is None
    assert owner.commit(record, record.worker)
    # A preview consumer can see the commit before renderer finalisation.
    assert controller.accepted.displayed == ZOOM
    controller.finish(record)
    assert controller.accepted.displayed == ZOOM


def test_nested_newer_publication_cannot_be_finished_by_outer_renderer(viewport):
    controller, _, owner = viewport
    older = stage(controller, owner, ZOOM)
    newer = stage(controller, owner, NEXT)
    controller.finish(older)
    assert controller.accepted.displayed is None
    assert owner.commit(newer, newer.worker)
    controller.finish(newer)
    controller.finish(older)
    assert controller.accepted.displayed == NEXT


def test_cancelled_timer_cannot_consume_a_new_request(viewport):
    controller, host, _ = viewport
    controller.schedule_visible()
    old = host.callbacks[-1]
    controller.cancel_pending()
    host.ranges = NEXT
    controller.schedule_visible()
    old()
    assert not host.loads
    assert controller.pending
    host.callbacks[-1]()
    assert host.loads[-1].ranges == NEXT


@pytest.mark.parametrize("action", ["reset", "cancel_pending"])
def test_cancellation_inside_range_read_prevents_loading(viewport, action):
    controller, host, _ = viewport
    def ranges():
        getattr(controller, action)()
        return ZOOM
    host.visible_ranges = ranges
    assert not controller.reload()
    assert not host.loads


def test_rejected_start_does_not_mark_range_as_displayed(viewport):
    controller, host, _ = viewport
    host.accept_load = False
    assert not controller.reload()
    assert not controller.reload()
    assert len(host.loads) == 2
    assert controller.accepted.displayed is None


def test_worker_started_inside_range_read_is_not_replaced(viewport):
    controller, host, _ = viewport
    def ranges():
        host.running = True
        return ZOOM
    host.visible_ranges = ranges
    assert not controller.reload()
    assert controller.pending
    assert not host.loads


@pytest.mark.parametrize("observe_before_finish", [False, True])
def test_non_sql_commit_disables_pending_detail_reads(viewport, observe_before_finish):
    controller, host, owner = viewport
    controller.schedule_visible()
    record = stage(controller, owner, None)
    controller.stage(record, HeatmapViewportState())
    assert owner.commit(record, record.worker)
    if observe_before_finish:
        assert not controller.accepted.enabled
    controller.finish(record)
    assert not controller.pending
    host.callbacks[-1]()
    assert not controller.pending
    assert not controller.accepted.enabled
    assert not host.loads


def test_reset_retires_staged_publication_and_queued_callback(viewport):
    controller, host, owner = viewport
    controller.schedule_visible()
    record = stage(controller, owner, ZOOM)
    controller.reset()
    assert owner.commit(record, record.worker)
    controller.finish(record)
    host.callbacks[-1]()
    assert not host.loads
    assert controller.accepted == HeatmapViewportState()


def test_range_clamping_full_view_and_existing_tolerance():
    assert AxisRanges((-2., 4.), (3., 22.)).visible_within(FULL) == AxisRanges((0., 4.), (3., 20.))
    assert FULL.visible_within(FULL) is None
    assert AxisRanges((20., 30.), (0., 10.)).visible_within(FULL) is None
    assert ZOOM.matches(AxisRanges((2.001, 4.), (3., 7.)))
    assert not ZOOM.matches(NEXT)


@pytest.mark.parametrize("ranges", [None, {}, {"x": (1., 1.), "y": (0., 1.)},
                                   {"x": (0., float("inf")), "y": (0., 1.)}])
def test_invalid_ranges_are_rejected(ranges):
    assert AxisRanges.normalise(ranges) is None
