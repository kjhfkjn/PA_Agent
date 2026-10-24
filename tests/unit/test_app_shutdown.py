"""Application shutdown must release resources on every quit path."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6")

from pa_agent.app_context import AppContext


def test_main_window_shutdown_is_idempotent(qtbot) -> None:
    from pa_agent.gui.main_window import MainWindow

    data_source = MagicMock()
    window = MainWindow(ctx=AppContext(data_source=data_source))
    qtbot.addWidget(window)

    window._cancel_analysis_worker = MagicMock()
    window._cancel_snapshot_fetch_worker = MagicMock()
    window._stop_refresh_loop = MagicMock()

    window.shutdown()
    window.shutdown()

    window._cancel_analysis_worker.assert_called_once_with()
    window._cancel_snapshot_fetch_worker.assert_called_once_with()
    window._stop_refresh_loop.assert_called_once_with()
    data_source.unsubscribe.assert_called_once_with()
    data_source.disconnect.assert_called_once_with()


def test_hidden_window_watchdog_requests_quit(qtbot, monkeypatch) -> None:
    from pa_agent.gui import main_window as main_window_module
    from pa_agent.gui.main_window import MainWindow

    window = MainWindow(ctx=AppContext())
    qtbot.addWidget(window)
    window.shutdown = MagicMock()

    app = MagicMock()
    monkeypatch.setattr(main_window_module.QApplication, "instance", lambda: app)

    window.hide()
    window._quit_if_main_window_hidden()

    window.shutdown.assert_called_once_with()
    app.quit.assert_called_once_with()
