"""pipeline_runner: in-process запуск шагов пайплайна.

Раньше GUI запускал ``create_emails.py``, ``register_devin.py``,
``add_identities.py`` и ``check_cards.py`` через ``subprocess.Popen([
sys.executable, "script.py", ...])``. В сборке PyInstaller ``sys.executable``
указывает на сам exe, а ``.venv\\Scripts\\python.exe`` в дистрибутиве
отсутствует — поэтому при клике «Запустить» открывалась ещё одна копия
GUI, а шаги не выполнялись.

Здесь мы вызываем ``main(argv)`` каждого модуля напрямую. Это:

* даёт работоспособный exe (нет внешнего интерпретатора);
* убирает накладные расходы на повторное открытие БД, импорт ddddocr
  и т.п.;
* позволяет ловить ``print()`` и ``logging`` сразу в очередь логов GUI
  через :class:`LogQueueStream`;
* открывает дорогу к реальной отмене (через :class:`threading.Event`)
  в следующих PR-ах.

``run_step`` — синхронный, ожидается вызов из background-потока GUI.
Перенаправление ``sys.stdout`` и ``sys.stderr`` устанавливается на время
вызова и обязательно снимается в ``finally``; GUI ставит общий захват
один раз на запуск нескольких шагов через :func:`capture_stdio`.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import logging
import queue
import sys
import threading
import traceback
from types import ModuleType
from typing import Iterable


# Каноничные имена модулей шагов 1..4. Шаг 5 (activate_trials) GUI
# запускает напрямую — там async-pipeline, не argparse main().
STEP_MODULES: dict[str, str] = {
    "create_emails.py": "create_emails",
    "register_devin.py": "register_devin",
    "add_identities.py": "add_identities",
    "check_cards.py": "check_cards",
}


class LogQueueStream(io.TextIOBase):
    """File-like объект, пушащий написанное в ``queue.Queue`` построчно.

    Используется как замена ``sys.stdout`` / ``sys.stderr`` на время
    запуска шагов. Сохраняет буфер до перевода строки, чтобы в очередь
    попадали целые строки (а не куски форматированного вывода
    Playwright/ddddocr).

    Параллельно (если задан ``mirror``) дублирует вывод в исходный
    stdout — удобно для запуска ``python gui.py`` из терминала.
    В windowed exe-сборке ``sys.stdout`` может быть ``None``;
    ``mirror=None`` обрабатывается корректно.
    """

    def __init__(
        self,
        log_queue: "queue.Queue[str]",
        mirror=None,
        prefix: str = "",
    ) -> None:
        super().__init__()
        self._queue = log_queue
        self._mirror = mirror
        # P1-6: префикс клеится к каждой строке, уходящей в GUI. Нужен,
        # чтобы отличать stderr-сообщения (traceback'и, Playwright warnings)
        # от обычных print()-ов; в mirror (реальный stdout/stderr) уходит без
        # префикса, чтобы не засорять файловые логи logging.
        self._prefix = prefix
        self._buf: list[str] = []
        self._lock = threading.Lock()

    # io.TextIOBase API ------------------------------------------------------

    def writable(self) -> bool:  # noqa: D401 — fileobj API
        return True

    def write(self, s: str) -> int:  # noqa: D401 — fileobj API
        if not s:
            return 0
        if self._mirror is not None:
            try:
                self._mirror.write(s)
            except Exception:
                # mirror может быть закрыт (windowed exe). Молча игнорируем.
                pass
        with self._lock:
            self._buf.append(s)
            joined = "".join(self._buf)
            if "\n" in joined:
                lines = joined.split("\n")
                self._buf = [lines[-1]]
                for line in lines[:-1]:
                    self._queue.put(self._prefix + line if self._prefix else line)
        return len(s)

    def flush(self) -> None:  # noqa: D401 — fileobj API
        if self._mirror is not None:
            try:
                self._mirror.flush()
            except Exception:
                pass
        with self._lock:
            if self._buf:
                rest = "".join(self._buf)
                self._buf.clear()
                if rest:
                    self._queue.put(self._prefix + rest if self._prefix else rest)


@contextlib.contextmanager
def capture_stdio(log_queue: "queue.Queue[str]"):
    """Контекст: перенаправить ``sys.stdout`` и ``sys.stderr`` в очередь.

    Также добавляет :class:`logging.StreamHandler` на ту же очередь,
    чтобы ``logging.getLogger(...).info(...)`` (используется в
    ``register_devin``) тоже попадал в GUI.
    """
    # P1-6: stdout идёт без префикса (это типичный трафик print()),
    # stderr помечаем маркером — чтобы traceback'и/warning'и сразу было
    # видно в окне логов GUI.
    sink = LogQueueStream(log_queue, mirror=sys.__stdout__)
    err_sink = LogQueueStream(log_queue, mirror=sys.__stderr__, prefix="[stderr] ")

    handler = logging.StreamHandler(sink)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))

    root_logger = logging.getLogger()
    prev_level = root_logger.level
    root_logger.addHandler(handler)
    if root_logger.level > logging.INFO or root_logger.level == logging.NOTSET:
        root_logger.setLevel(logging.INFO)

    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = sink
    sys.stderr = err_sink
    try:
        yield
    finally:
        try:
            sink.flush()
            err_sink.flush()
        except Exception:
            pass
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        root_logger.removeHandler(handler)
        root_logger.setLevel(prev_level)


def _resolve_module(script_name: str) -> ModuleType:
    """Получить модуль шага по имени файла (``"create_emails.py"``)."""
    module_name = STEP_MODULES.get(script_name)
    if module_name is None:
        raise ValueError(f"Unknown pipeline step: {script_name!r}")
    return importlib.import_module(module_name)


def run_step(
    script_name: str,
    argv: Iterable[str],
    log_queue: "queue.Queue[str]",
) -> int:
    """Синхронно выполнить ``module.main(argv)`` указанного шага.

    Args:
        script_name: имя ``.py``-файла шага (см. :data:`STEP_MODULES`).
        argv: argparse-аргументы для ``main()`` шага.
        log_queue: очередь, в которую попадает stdout/stderr/logging.

    Returns:
        Код возврата ``main()`` шага. ``0`` — успех, ``>0`` — ошибка,
        ``-1`` — необработанное исключение.
    """
    argv = list(argv)
    log_queue.put(f"=== Запуск {script_name} (in-process) ===")
    try:
        module = _resolve_module(script_name)
    except Exception as exc:
        log_queue.put(f"!!! Не удалось импортировать модуль шага {script_name}: {exc}")
        return -1

    main = getattr(module, "main", None)
    if main is None:
        log_queue.put(f"!!! У модуля {module.__name__} нет main(argv)")
        return -1

    with capture_stdio(log_queue):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
        except Exception as exc:
            traceback.print_exc()
            log_queue.put(f"!!! Исключение в {script_name}: {exc}")
            code = -1

    if code == 0:
        log_queue.put(f"=== {script_name} завершён успешно ===")
    else:
        log_queue.put(f"=== {script_name} завершён с кодом {code} ===")
    return code
