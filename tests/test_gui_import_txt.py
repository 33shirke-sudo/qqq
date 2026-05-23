"""Тест P1-11 (GUI): кнопка «Импортировать .txt в БД» вызывает
``db.import_from_txt_files`` с каноническими путями и корректно показывает
сводку. Также проверяем, что исключение из БД-слоя превращается в
``messagebox.showerror`` и НЕ роняет GUI.
"""

from __future__ import annotations

import queue as queue_module
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# Заглушка tkinter в headless-CI.
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


def _make_stub_gui():
    stub = _Stub()
    stub.log_queue = queue_module.Queue()
    stub._db = MagicMock()
    return stub


def test_import_txt_to_db_calls_db_with_canonical_paths(monkeypatch) -> None:
    from gui import PipelineGUI
    import gui as gui_module

    stub = _make_stub_gui()
    stub._db.import_from_txt_files.return_value = {
        "emails": 3,
        "taken": 1,
        "devin_success": 2,
        "devin_errors": 0,
        "identities": 2,
    }

    showinfo = MagicMock()
    showerror = MagicMock()
    monkeypatch.setattr(gui_module.messagebox, "showinfo", showinfo, raising=False)
    monkeypatch.setattr(gui_module.messagebox, "showerror", showerror, raising=False)

    PipelineGUI.import_txt_to_db(stub)

    # Проверяем, что db.import_from_txt_files позван с правильными путями.
    stub._db.import_from_txt_files.assert_called_once()
    args = stub._db.import_from_txt_files.call_args.args
    # Порядок: emails, taken, devin_accounts, devin_errors, identities.
    names = [p.name for p in args]
    assert names == [
        "имейлы pingmx.txt",
        "taken.txt",
        "аккаунты devin.txt",
        "devin_errors.txt",
        "личности.txt",
    ]

    # Уведомление об успехе показано, об ошибке — нет.
    showinfo.assert_called_once()
    showerror.assert_not_called()

    # В лог-очередь записана сводка.
    msg = stub.log_queue.get_nowait()
    assert "emails=3" in msg
    assert "identities=2" in msg


def test_import_txt_to_db_shows_error_on_exception(monkeypatch) -> None:
    from gui import PipelineGUI
    import gui as gui_module

    stub = _make_stub_gui()
    stub._db.import_from_txt_files.side_effect = RuntimeError("disk full")

    showinfo = MagicMock()
    showerror = MagicMock()
    monkeypatch.setattr(gui_module.messagebox, "showinfo", showinfo, raising=False)
    monkeypatch.setattr(gui_module.messagebox, "showerror", showerror, raising=False)

    # Не должно бросать наружу.
    PipelineGUI.import_txt_to_db(stub)

    showerror.assert_called_once()
    title, text = showerror.call_args.args
    assert "disk full" in text
    showinfo.assert_not_called()

    msg = stub.log_queue.get_nowait()
    assert "FAIL" in msg
