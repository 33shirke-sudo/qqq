"""P2-3: единый табличный просмотрщик для всех `show_*`-окон GUI.

Раньше в ``gui.py`` было 6 фактически идентичных методов:
``show_database``, ``show_emails``, ``show_devin_accounts``,
``show_identities``, ``show_live_cards``, ``show_activated_trials``.
Каждый создавал ``Toplevel`` с ``Treeview``, прокруткой, статусной
строкой «Всего: …» и кнопками «Обновить»/«Закрыть». На обновление
данных писался отдельный ``refresh_*_view`` с дубликатом запроса.

Здесь — :class:`TableViewer`, инкапсулирующий весь этот шаблон. ``show_*``
методы в ``gui.py`` сводятся к одному вызову с описанием колонок и
функцией-loader-ом, которая возвращает список строк-кортежей.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Sequence


# Описание одной колонки таблицы: (ключ для tree-`values`, заголовок, ширина).
ColumnSpec = tuple[str, str, int]


class TableViewer:
    """Toplevel-окно с Treeview, скроллом и кнопками Обновить/Закрыть.

    Args:
        parent: родительский Tk-виджет.
        title: заголовок окна.
        columns: список ``(key, header, width)``.
        loader: функция без аргументов, возвращающая список кортежей
            длины ``len(columns)``. Вызывается при открытии и по
            «Обновить». При ошибке показывается ``messagebox.showerror``
            и таблица остаётся пустой.
        status_text: фабрика статусной строки. Принимает int (число
            строк) и возвращает строку для отображения в кнопке-баре.
            ``None`` означает «не показывать статус».
        geometry: размер окна (``"900x600"``).
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        columns: Sequence[ColumnSpec],
        loader: Callable[[], Sequence[tuple]],
        status_text: Callable[[int], str] | None = None,
        geometry: str = "900x600",
    ) -> None:
        self._parent = parent
        self._loader = loader
        self._status_text = status_text
        self._columns = list(columns)

        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.geometry(geometry)

        # Treeview + scrollbar
        tree_frame = ttk.Frame(self.window)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        keys = [c[0] for c in self._columns]
        self.tree = ttk.Treeview(tree_frame, columns=keys, show="headings")
        for key, header, width in self._columns:
            self.tree.heading(key, text=header)
            self.tree.column(key, width=width)

        scrollbar = ttk.Scrollbar(
            tree_frame, orient=tk.VERTICAL, command=self.tree.yview
        )
        self.tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Кнопки внизу
        self._btn_frame = ttk.Frame(self.window)
        self._btn_frame.pack(fill=tk.X, padx=10, pady=5)

        self._status_label: ttk.Label | None = None
        if self._status_text is not None:
            self._status_label = ttk.Label(self._btn_frame, text="")
            self._status_label.pack(side=tk.LEFT, padx=5)

        ttk.Button(self._btn_frame, text="Обновить", command=self.refresh).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(self._btn_frame, text="Закрыть", command=self.window.destroy).pack(
            side=tk.RIGHT, padx=5
        )

        # Первичная загрузка
        self.refresh()

    def refresh(self) -> None:
        """Перезагрузить данные через ``loader`` и перерисовать таблицу."""
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            rows = self._loader()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ошибка", f"Не удалось загрузить данные: {exc}")
            rows = ()
        for row in rows:
            self.tree.insert("", tk.END, values=row)
        if self._status_label is not None and self._status_text is not None:
            self._status_label.config(text=self._status_text(len(self.tree.get_children())))


__all__ = ["TableViewer", "ColumnSpec"]
