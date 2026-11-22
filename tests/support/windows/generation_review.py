"""Drive the generation review using the test's injected native file pickers."""

from qplot.windows._generation_dialog import GenerationReview


def choose_custom_generation(monkeypatch):
    def review(dialog):
        dialog.source.setCurrentIndex(1)
        dialog.choose_csv()
        if dialog.browse_output.isEnabled():
            dialog.choose_destination()
        if dialog.generate.isEnabled():
            dialog.generate.click()
        return dialog.result()

    monkeypatch.setattr(GenerationReview, "exec", review)
