# Changelog

All notable user-facing changes are recorded here.

This project currently releases from GitHub source tags rather than PyPI. For
installation commands and release validation, see `docs/distribution.md`.

## Unreleased

### Added

- Add configurable plot image clipboard output with screen-resolution,
  300 dpi, and vector SVG options.
- Add a Preferences option for the default plot image copy format/resolution.
- Document the plot image copy setting in the user guide and configuration
  reference.
- Add tests for plot image copy resolution, SVG clipboard data, and preference
  persistence.

### Changed

- Update plot-window copy shortcut help text to reflect the selected copy
  format/resolution.

## 1.3.2 - 2026-05-16

### Added

- Add in-app help, keyboard shortcut reference, and diagnostic log path copying.
- Add application diagnostics, startup version logging, and `qplot-cfg -version`.
- Add Preferences, including restore defaults and auto-plot controls.
- Add live database load progress feedback and improved cloud-sync status
  reporting.
- Add demo-data notes, screenshot capture tooling, and expanded user
  documentation.
- Add local package build validation in CI and release documentation.
- Add macOS CI coverage alongside Windows.

### Changed

- Move database and cloud-load logic out of the main window into dedicated
  datahandling modules.
- Split plotting, colorbar, run-control, preference, and window-control code
  into smaller modules.
- Improve autoplot behavior and empty-database handling.
- Improve horizontal and vertical cut controls for heatmaps.
- Make `import qplot` lazy for GUI modules so lightweight commands do not import
  the full GUI stack.
- Improve README installation and usage guidance.

### Fixed

- Fix completion status handling for interrupted runs.
- Fix several CI, lint, and packaging issues found during release preparation.
- Fix the "Limit Maximum" operation label typo.

## 1.3.1 - 2026-05-14

### Added

- Add package metadata, project URLs, and release badge updates for the GitHub
  repository.
- Add contributor setup guidance, architecture notes, configuration reference,
  and release hygiene documentation.
- Add development checks for Ruff, mypy, pytest, build, and twine metadata
  validation.
- Add Edward Laird as a package author.

### Changed

- Consolidate and clarify README setup, installation, and usage instructions.
- Tighten generated-file ignores and repository hygiene around local build/test
  artifacts.
- Improve test coverage around configuration, reset behavior, and GUI display.

### Fixed

- Fix package license metadata problems that blocked CI.
- Fix duplicate and inconsistent README notes around plot-window loading.

## Earlier Snapshots

- `Laird-version` - 2026-05-14: interface, plotting, colorbar, marquee, status
  bar, shortcut, and README improvements.
- `Wordsworth-version` - 2025-09-23: earlier project snapshot before the current
  beta release process.
