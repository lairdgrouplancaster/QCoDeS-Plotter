"""Publish resolution explanations with the heatmap image they describe."""

from qplot.tools.heatmap_resolution import HeatmapResolutionReport

from ._plot_publication import PublicationOutcome, RefreshPublication


class HeatmapResolutionState:
    def __init__(self) -> None:
        self._accepted: HeatmapResolutionReport | None = None
        self._staged: tuple[RefreshPublication, HeatmapResolutionReport | None] | None = None

    @property
    def accepted(self) -> HeatmapResolutionReport | None:
        # Preview callbacks can observe a commit before renderer finalisation.
        self._settle()
        return self._accepted

    def _settle(self) -> None:
        if self._staged is not None:
            publication, report = self._staged
            if publication.outcome is not PublicationOutcome.PENDING:
                self._staged = None
                if publication.outcome is PublicationOutcome.COMMITTED:
                    self._accepted = report
                elif publication.outcome in (PublicationOutcome.RENDER_FAILED, PublicationOutcome.ROLLED_BACK):
                    # These outcomes clear the image: there is no displayed
                    # measurement for a resolution warning to describe.
                    self._accepted = None

    def stage(self, publication: RefreshPublication, report: HeatmapResolutionReport | None) -> None:
        # Settle a previous commit before a nested publication takes ownership.
        self._settle()
        self._staged = (publication, report)

    def reset(self) -> None:
        self._accepted = None
        self._staged = None
