"""Тест P1-4: ``PipelineGUI._kill_browser_descendants``.

Основная проверка: метод-наследие безопасно работает, когда ``psutil`` не
установлен (возвращает 0, не падает). А когда мок-``psutil`` есть — метод
вызывает ``terminate``/``kill`` на потомках, чьё имя матчится со списком
браузерных процессов, и игнорирует остальных.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# Заглушка tkinter в headless-CI без python3-tk (как в test_gui_stop_pipeline).
if "tkinter" not in sys.modules:
    try:
        import tkinter  # noqa: F401
    except Exception:
        _tk = types.ModuleType("tkinter")
        _ttk = types.ModuleType("tkinter.ttk")
        _filedialog = types.ModuleType("tkinter.filedialog")
        _messagebox = types.ModuleType("tkinter.messagebox")
        _scrolledtext = types.ModuleType("tkinter.scrolledtext")
        _tk.ttk = _ttk
        _tk.filedialog = _filedialog
        _tk.messagebox = _messagebox
        _tk.scrolledtext = _scrolledtext
        for k in ("StringVar", "IntVar", "BooleanVar"):
            setattr(_tk, k, lambda *a, **kw: None)
        for name in (
            "END W E N S X Y BOTH LEFT RIGHT TOP BOTTOM NSEW "
            "HORIZONTAL VERTICAL DISABLED NORMAL"
        ).split():
            setattr(_tk, name, name.lower())
        sys.modules["tkinter"] = _tk
        sys.modules["tkinter.ttk"] = _ttk
        sys.modules["tkinter.filedialog"] = _filedialog
        sys.modules["tkinter.messagebox"] = _messagebox
        sys.modules["tkinter.scrolledtext"] = _scrolledtext


class _Stub:
    pass


def _make_stub():
    return _Stub()


def test_kill_browser_descendants_returns_zero_without_psutil(monkeypatch) -> None:
    import gui as gui_module

    monkeypatch.setattr(gui_module, "psutil", None)

    stub = _make_stub()
    n = gui_module.PipelineGUI._kill_browser_descendants(stub)

    assert n == 0


def test_kill_browser_descendants_targets_only_browser_children(monkeypatch) -> None:
    """psutil моки: ``children()`` отдаёт смесь firefox + системных
    процессов; убиваться должны только браузерные."""
    import gui as gui_module

    firefox = MagicMock()
    firefox.name.return_value = "firefox"
    firefox.terminate = MagicMock()
    firefox.kill = MagicMock()

    chrome = MagicMock()
    chrome.name.return_value = "chrome.exe"
    chrome.terminate = MagicMock()
    chrome.kill = MagicMock()

    other = MagicMock()
    other.name.return_value = "bash"
    other.terminate = MagicMock()
    other.kill = MagicMock()

    me = MagicMock()
    me.children.return_value = [firefox, chrome, other]

    fake_psutil = types.SimpleNamespace(
        Process=MagicMock(return_value=me),
        # wait_procs: возвращаем «никто не выжил» — kill() звать не нужно.
        wait_procs=MagicMock(return_value=([firefox, chrome], [])),
    )
    monkeypatch.setattr(gui_module, "psutil", fake_psutil)

    stub = _make_stub()
    n = gui_module.PipelineGUI._kill_browser_descendants(stub, grace_seconds=0.0)

    assert n == 2, "должны быть выбраны только firefox и chrome.exe"
    firefox.terminate.assert_called_once()
    chrome.terminate.assert_called_once()
    other.terminate.assert_not_called()
    other.kill.assert_not_called()


def test_kill_browser_descendants_falls_back_to_kill_for_survivors(monkeypatch) -> None:
    """Если ``wait_procs`` сообщил, что процессы выжили после SIGTERM,
    ``kill()`` должен быть позван явно."""
    import gui as gui_module

    firefox = MagicMock()
    firefox.name.return_value = "firefox-bin"
    firefox.terminate = MagicMock()
    firefox.kill = MagicMock()

    me = MagicMock()
    me.children.return_value = [firefox]

    fake_psutil = types.SimpleNamespace(
        Process=MagicMock(return_value=me),
        # gone=[], alive=[firefox] — выжил.
        wait_procs=MagicMock(side_effect=[([], [firefox]), ([firefox], [])]),
    )
    monkeypatch.setattr(gui_module, "psutil", fake_psutil)

    stub = _make_stub()
    n = gui_module.PipelineGUI._kill_browser_descendants(stub, grace_seconds=0.0)

    assert n == 1
    firefox.terminate.assert_called_once()
    firefox.kill.assert_called_once()
