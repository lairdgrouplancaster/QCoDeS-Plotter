"""Small widget contracts shared by database lifecycle harnesses."""


class PreviewPublicationGate:
    """Expose the RunList gate, including restoration of an already-held gate."""

    preview_publication_suspended = False

    def set_preview_publication_suspended(self, suspended):
        previous = self.preview_publication_suspended
        self.preview_publication_suspended = bool(suspended)
        return previous
