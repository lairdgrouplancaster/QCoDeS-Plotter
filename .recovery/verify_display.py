"""Read-only acceptance check against an operator-selected QCoDeS database."""
import hashlib
import json
import pathlib
import sys
import tempfile
import time
from unittest.mock import patch

from PyQt6 import QtWidgets
from qplot.windows import main
from qplot.datahandling import trusted_work_coordinator
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache


def protected(path):
    result = {}
    for suffix in ('', '-wal', '-journal'):
        member = pathlib.Path(str(path) + suffix)
        if member.exists():
            with member.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            stat = member.stat()
            result[suffix] = (stat.st_size, stat.st_mtime_ns, digest)
        else:
            result[suffix] = None
    return result


def wait(predicate, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for UI: ' + repr(errors))


def main_check():
    global app, errors
    path = pathlib.Path(sys.argv[1]).resolve(strict=True)
    before = protected(path)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    errors = []
    with tempfile.TemporaryDirectory(prefix='qplot-recovery-check-') as temporary:
        root = pathlib.Path(temporary)
        with (
            patch.object(main.config, 'default_path', str(root)),
            patch.object(main.config, 'default_file', str(root / main.config.config_file_name)),
            patch.object(trusted_work_coordinator, 'TrustedDerivedDiskCache', lambda **kw: TrustedDerivedDiskCache(root / 'cache')),
            patch.object(main.MainWindow, 'show_error', lambda owner, *args, **kw: errors.append(str(args))),
        ):
            window = main.MainWindow()
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config['user_preference']['confirm_close'] = False
            window.config.config['user_preference']['confirm_close_all'] = False
            window.resize(1050, 850)
            window.show()
            try:
                assert window.load_database_path(str(path))
                wait(lambda: not window._database_load_active)
                window.monitor.stop()
                assert window._database_access_mode == 'trusted_live', window._database_access_mode
                selected = next(window.RunList.topLevelItem(i) for i in range(window.RunList.topLevelItemCount()) if window.RunList.topLevelItem(i).text(window.RunList.cols.index('ID')) == '7')
                window.RunList.setCurrentItem(selected)
                selected.setSelected(True)
                guid = selected.guid
                window.infoBox.setCurrentWidget(window.infoBox.preview)
                wait(lambda: window.RunList.run_preview_is_ready(guid) and guid in window.infoBox.preview.cache)
                wait(lambda: all(window.RunList.run_preview_is_ready(window.RunList.topLevelItem(i).guid) for i in range(window.RunList.topLevelItemCount())))
                images = window.RunList.findChildren(QtWidgets.QLabel, 'measurementPreviewImage')
                assert len(images) >= window.RunList.topLevelItemCount()
                assert all(label.pixmap().width() == label.pixmap().height() == 22 for label in images)
                previews = window.infoBox.preview.cache[guid]
                assert previews and all(p.get('image') is not None and not p['image'].isNull() for p in previews)
                assert not errors, errors
                print(json.dumps({'database': path.name, 'runs': window.RunList.topLevelItemCount(), 'square_thumbnails': len(images), 'selected_run': 7, 'preview_images': len(previews)}), flush=True)
                window.grab().save(str(pathlib.Path(__file__).parent / 'verified-display.png'))
            finally:
                window.close_database(status=False)
                wait(lambda: not window._trusted_derived_bridge.background_active() and not window._retired_trusted_read_services, 30)
                window.close()
                app.processEvents()
    assert protected(path) == before, 'Protected database family changed'
    print('Protected main/WAL/journal contents and timestamps unchanged.', flush=True)


if __name__ == "__main__":
    main_check()
