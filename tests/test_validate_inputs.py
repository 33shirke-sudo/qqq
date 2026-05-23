"""Тест guard'а P1-3 (параллельный запуск Шага 3 при включённых Шагах 1/2).

Напрямую инстанцировать ``PipelineGUI`` в headless-CI не получится
(нет ``$DISPLAY``), а Tk-варсы привязаны к корню. Поэтому мы
проверяем поведение через лёгкий stub-класс, который повторяет тот
ровно тот же атрибутный контракт, что читает ``_validate_inputs``:
``stepN_enabled``, ``stepN_custom``, ``stepN_parallel``,
``global_workers``, ``global_limit``, ``global_delay``,
``step1_nicks_file``, ``step4_bins_file`` — всё с .get()-like API.

Сам ``_validate_inputs`` остаётся в ``gui.PipelineGUI`` без
изменений (рефакторинг не входит в P1-3). Мы вызываем его как
``PipelineGUI._validate_inputs(self=stub, ...)``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# В headless-CI без python3-tk импорт ``gui`` падает на ``import
# tkinter``. Для тестов чистой логики ``_validate_inputs`` Tk не
# нужен — подставляем заглушку до импорта ``gui``.
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
        _tk.StringVar = _tk.IntVar = _tk.BooleanVar = lambda *a, **kw: None
        _tk.END = "end"
        _tk.W = "w"
        _tk.E = "e"
        _tk.N = "n"
        _tk.S = "s"
        _tk.X = "x"
        _tk.Y = "y"
        _tk.BOTH = "both"
        _tk.LEFT = "left"
        _tk.RIGHT = "right"
        _tk.TOP = "top"
        _tk.BOTTOM = "bottom"
        _tk.NSEW = "nsew"
        _tk.HORIZONTAL = "horizontal"
        _tk.VERTICAL = "vertical"
        _tk.DISABLED = "disabled"
        _tk.NORMAL = "normal"
        sys.modules["tkinter"] = _tk
        sys.modules["tkinter.ttk"] = _ttk
        sys.modules["tkinter.filedialog"] = _filedialog
        sys.modules["tkinter.messagebox"] = _messagebox
        sys.modules["tkinter.scrolledtext"] = _scrolledtext


class _StubVar:
    """Минимальная замена ``tk.StringVar``/``BooleanVar`` для тестов."""

    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


def _make_stub(**overrides):
    """Собрать stub-объект с дефолтами, перекрытыми ``overrides``.

    Дефолт: ничего не включено, все custom-настройки выключены,
    глобальные числовые поля валидны, файлы существуют (берём
    реальные пути в репо, так как они закоммичены).
    """
    bins_path = ROOT / "бины.txt"
    nicks_path = ROOT / "имена для имейлов.txt"

    defaults = dict(
        # numeric globals
        global_workers="3", global_limit="10", global_delay="1",
        # step toggles
        step1_enabled=False, step2_enabled=False,
        step3_enabled=False, step4_enabled=False,
        # custom (off → не валидируем числа шага)
        step1_custom=False, step2_custom=False,
        step3_custom=False, step4_custom=False, step5_custom=False,
        # parallel
        step3_parallel=False, step4_parallel=False,
        # files
        step1_nicks_file=str(nicks_path), step4_bins_file=str(bins_path),
    )
    defaults.update(overrides)

    from gui import PipelineGUI

    class _Stub:
        # Подключаем настоящий staticmethod _parse_positive_int.
        # ``PipelineGUI._parse_positive_int`` уже разворачивает дескриптор
        # и отдаёт чистую функцию двух аргументов; оборачиваем обратно
        # в staticmethod, чтобы вызов ``self._parse_positive_int(...)``
        # не подмешивал ``self`` первым параметром.
        _parse_positive_int = staticmethod(PipelineGUI._parse_positive_int)

    stub = _Stub()
    for k, v in defaults.items():
        setattr(stub, k, _StubVar(v))
    return stub


@pytest.fixture()
def validate():
    """Возвращает функцию, дублирующую сигнатуру ``_validate_inputs``."""
    from gui import PipelineGUI

    def _run(stub, *, sequential_steps=None, parallel_steps=None, step5_enabled=False):
        return PipelineGUI._validate_inputs(
            stub,
            sequential_steps or [("dummy.py", [])],
            parallel_steps or [],
            step5_enabled,
        )

    return _run


# ---------------------------------------------------------------------------
# P1-3 — happy paths
# ---------------------------------------------------------------------------


def test_step3_sequential_allowed_with_steps_1_2(validate):
    """Шаг 3 *последовательно* + Шаги 1/2 включены → OK (это нормальный сценарий)."""
    stub = _make_stub(
        step1_enabled=True, step2_enabled=True, step3_enabled=True,
        step3_parallel=False,
    )
    assert validate(stub) is None


def test_step3_parallel_allowed_without_steps_1_2(validate):
    """Шаг 3 *параллельно* + Шаги 1/2 ВЫКЛЮЧЕНЫ → OK."""
    stub = _make_stub(
        step1_enabled=False, step2_enabled=False, step3_enabled=True,
        step3_parallel=True,
    )
    assert validate(stub) is None


def test_step3_disabled_step3_parallel_is_irrelevant(validate):
    """Если Шаг 3 выключен, его step3_parallel не должен ничего ломать."""
    stub = _make_stub(
        step1_enabled=True, step3_enabled=False,
        step3_parallel=True,  # шумовой бит
    )
    assert validate(stub) is None


# ---------------------------------------------------------------------------
# P1-3 — error paths
# ---------------------------------------------------------------------------


def test_step3_parallel_with_step1_returns_error(validate):
    stub = _make_stub(
        step1_enabled=True, step3_enabled=True, step3_parallel=True,
    )
    err = validate(stub)
    assert err is not None
    assert "Шаг 3" in err
    assert "параллельно" in err.lower()


def test_step3_parallel_with_step2_returns_error(validate):
    stub = _make_stub(
        step2_enabled=True, step3_enabled=True, step3_parallel=True,
    )
    err = validate(stub)
    assert err is not None
    assert "Шаг 3" in err


def test_step3_parallel_with_both_step1_and_step2_returns_error(validate):
    stub = _make_stub(
        step1_enabled=True, step2_enabled=True,
        step3_enabled=True, step3_parallel=True,
    )
    err = validate(stub)
    assert err is not None
    assert "Шаг 3" in err


# ---------------------------------------------------------------------------
# Step 4 parallel — PLAN.md явно говорит: оно ОК (Step 4 independent).
# Регресс-тест на случай, если кто-то «исправит» по аналогии с Шагом 3.
# ---------------------------------------------------------------------------


def test_step4_parallel_with_steps_1_2_is_allowed(validate):
    """Шаг 4 параллельно при включённых 1/2 — допустимо (см. PLAN.md P1-3)."""
    stub = _make_stub(
        step1_enabled=True, step2_enabled=True,
        step4_enabled=True, step4_parallel=True,
    )
    assert validate(stub) is None, (
        "Step 4 должен оставаться независимым — он читает .txt-бины, "
        "Step 1/2 на него не влияют. См. PLAN.md P1-3."
    )
