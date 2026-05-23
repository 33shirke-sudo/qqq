"""P2-3: тесты для gui_table_viewer.TableViewer без поднятия Tk.

Tk-классы (Toplevel, Frame, Treeview, Scrollbar, Label, Button)
имитируются легковесными stub-ами, повторяющими использованный API.
Это позволяет проверить:
  - что loader вызывается при создании и при refresh;
  - что данные передаются в tree.insert();
  - что status_text получает правильный счётчик;
  - что messagebox.showerror вызывается при exception в loader.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# Stub tkinter, если в окружении нет python-tk (CI).
# Безусловно ставим свои _StubWidget/_StubTree, даже если другой тест
# (например test_gui_stop_pipeline) уже положил лёгкие stubs в sys.modules —
# нам нужны более развитые версии с .destroy/.set/.heading/.insert.
def _setup_tk_stubs() -> None:
    # P2-3: проверка `import tkinter` тут вредна — если другой тест
    # (test_gui_stop_pipeline) уже положил lightweight-stub в sys.modules,
    # `import tkinter` отработает, но stub не будет иметь .Toplevel.
    # Поэтому всегда дополняем имена ниже — это безопасно и для реального
    # tkinter (мы только аугментируем модуль, а не подменяем).
    tk = sys.modules.get("tkinter") or types.ModuleType("tkinter")
    ttk = sys.modules.get("tkinter.ttk") or types.ModuleType("tkinter.ttk")
    messagebox = sys.modules.get("tkinter.messagebox") or types.ModuleType("tkinter.messagebox")
    tk.ttk = ttk
    tk.messagebox = messagebox

    for name in ("END", "BOTH", "LEFT", "RIGHT", "TOP", "X", "Y", "VERTICAL", "HORIZONTAL"):
        setattr(tk, name, name.lower())

    class _StubWidget:
        def __init__(self, *args, **kwargs):
            self._kw = kwargs
            self._children = []
            self._text = kwargs.get("text", "")

        def pack(self, *args, **kwargs): pass
        def configure(self, **kw): self._kw.update(kw)
        config = configure
        def winfo_children(self): return self._children
        def title(self, t): self._title = t
        def geometry(self, g): self._geom = g
        def set(self, *args, **kwargs): pass  # Scrollbar.set
        def destroy(self): pass  # Toplevel.destroy

    class _StubTree(_StubWidget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._rows: list[tuple] = []
            self._headings: dict[str, str] = {}
            self._columns: dict[str, int] = {}

        def heading(self, key, text=""):
            self._headings[key] = text

        def column(self, key, width=0):
            self._columns[key] = width

        def insert(self, parent, index, values=()):
            self._rows.append(values)

        def delete(self, item):
            # упрощённо — get_children отдаёт индексы строк, delete их удаляет
            try:
                idx = int(item)
                self._rows.pop(idx)
            except Exception:
                pass

        def get_children(self):
            return [str(i) for i in range(len(self._rows))]

        def yview(self, *args, **kwargs): pass

    tk.Toplevel = _StubWidget
    ttk.Frame = _StubWidget
    ttk.Treeview = _StubTree
    ttk.Scrollbar = _StubWidget
    ttk.Label = _StubWidget
    ttk.Button = _StubWidget
    tk.Misc = _StubWidget

    showerror_mock = MagicMock()
    messagebox.showerror = showerror_mock
    messagebox.showinfo = MagicMock()
    messagebox.showwarning = MagicMock()

    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.messagebox"] = messagebox


_setup_tk_stubs()

from gui_table_viewer import TableViewer  # noqa: E402


def test_table_viewer_loads_on_init() -> None:
    """При создании TableViewer вызывает loader и заполняет tree."""
    rows = [("a@b.com", "pass", "nick", "ok", "ivan")]
    loader = MagicMock(return_value=rows)

    viewer = TableViewer(
        None,
        title="X",
        columns=[("c1", "C1", 100), ("c2", "C2", 100), ("c3", "C3", 100), ("c4", "C4", 100), ("c5", "C5", 100)],
        loader=loader,
    )

    assert loader.call_count == 1
    assert viewer.tree._rows == rows


def test_table_viewer_refresh_reloads() -> None:
    """refresh() очищает и перечитывает данные."""
    rows_v1 = [("a", "b")]
    rows_v2 = [("c", "d"), ("e", "f")]
    loader = MagicMock(side_effect=[rows_v1, rows_v2])

    viewer = TableViewer(
        None,
        title="X",
        columns=[("a", "A", 100), ("b", "B", 100)],
        loader=loader,
    )
    assert viewer.tree._rows == rows_v1

    viewer.refresh()
    assert viewer.tree._rows == rows_v2
    assert loader.call_count == 2


def test_table_viewer_status_text_receives_count() -> None:
    """status_text получает количество загруженных строк."""
    rows = [("a", "b"), ("c", "d"), ("e", "f")]
    status_text = MagicMock(return_value="Всего: 3")
    loader = MagicMock(return_value=rows)

    TableViewer(
        None,
        title="X",
        columns=[("a", "A", 100), ("b", "B", 100)],
        loader=loader,
        status_text=status_text,
    )
    status_text.assert_called_with(3)


def test_table_viewer_handles_loader_exception() -> None:
    """Если loader падает — TableViewer не валится, показывает messagebox."""
    import tkinter.messagebox as mb

    loader = MagicMock(side_effect=RuntimeError("db connection lost"))

    viewer = TableViewer(
        None,
        title="X",
        columns=[("a", "A", 100)],
        loader=loader,
    )
    # Tree должен быть пустой, error показан.
    assert viewer.tree._rows == []
    assert mb.showerror.called


def test_table_viewer_no_status_when_callback_none() -> None:
    """status_text=None — статусная Label не создаётся."""
    viewer = TableViewer(
        None,
        title="X",
        columns=[("a", "A", 100)],
        loader=lambda: [],
        status_text=None,
    )
    assert viewer._status_label is None
