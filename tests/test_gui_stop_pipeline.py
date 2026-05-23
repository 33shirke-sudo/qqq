"""Тест P1-7: ``PipelineGUI.stop_pipeline`` взводит ``_step5_stop_event``.

Без поднятия настоящего Tk-рута: используем bound-method прямо на
stub-объекте, как и в ``tests/test_validate_inputs.py``.
"""

from __future__ import annotations

import sys
import threading
import types
import queue as queue_module
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# Заглушка tkinter в headless-CI без python3-tk.
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


def _make_stub_gui(running: bool = True, with_stop_event: bool = True):
    """Stub, повторяющий интерфейс ``PipelineGUI``, нужный ``stop_pipeline``."""

    class _Stub:
        pass

    stub = _Stub()
    stub.running = running
    stub._step5_stop_event = threading.Event() if with_stop_event else None
    stub.processes = {}
    stub.log_queue = queue_module.Queue()
    # P1-4: stop_pipeline стартует daemon-поток на _kill_browser_descendants.
    # В unit-тестах psutil может отсутствовать — делаем no-op, который ещё
    # и фиксирует факт вызова, чтобы assert-ить ниже.
    stub._kill_called = threading.Event()

    def _fake_kill(grace_seconds: float = 0.0) -> int:
        stub._kill_called.set()
        return 0

    stub._kill_browser_descendants = _fake_kill
    return stub


def test_stop_pipeline_sets_step5_stop_event_when_active() -> None:
    """Если Step 5 крутится → stop_pipeline взводит его event."""
    from gui import PipelineGUI

    stub = _make_stub_gui(running=True, with_stop_event=True)
    pre_event = stub._step5_stop_event
    assert not pre_event.is_set()

    PipelineGUI.stop_pipeline(stub)

    assert pre_event.is_set(), "stop_pipeline должен взводить step5_stop_event"
    assert stub.running is False, "stop_pipeline должен снимать running"
    # P1-4: kill_browser_descendants должен быть запущен (асинхронно).
    assert stub._kill_called.wait(timeout=2.0), (
        "stop_pipeline должен звать _kill_browser_descendants в daemon-потоке"
    )


def test_stop_pipeline_safe_when_step5_not_running() -> None:
    """Если Step 5 НЕ крутится (event=None) → stop_pipeline не падает."""
    from gui import PipelineGUI

    stub = _make_stub_gui(running=True, with_stop_event=False)
    assert stub._step5_stop_event is None

    PipelineGUI.stop_pipeline(stub)  # не должно бросать

    assert stub._step5_stop_event is None
    assert stub.running is False


def test_stop_pipeline_noop_when_not_running() -> None:
    """Early return: если ``self.running`` уже False → stop_pipeline ничего не делает."""
    from gui import PipelineGUI

    stub = _make_stub_gui(running=False, with_stop_event=True)
    pre_event = stub._step5_stop_event

    PipelineGUI.stop_pipeline(stub)

    # running остался False, event не должен трогаться.
    assert stub.running is False
    assert not pre_event.is_set(), "уже не running → не лезем в step5_stop_event"
