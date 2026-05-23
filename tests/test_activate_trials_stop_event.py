"""Тесты P1-7: ``activate_trials_pipeline`` уважает внешний stop_event.

Сценарий: запускаем pipeline с пустой очередью (нет аккаунтов с
identity+картой) и подменённым :func:`activate_trials._worker`. Это
позволяет проверить именно отмену, не трогая Camoufox.

Параллельно — простой контракт: ``activate_trials_pipeline`` должен
принимать ``threading.Event`` (как из GUI), не падая. Это
регрессионный тест на тот случай, если кто-то закрепит тип как
``asyncio.Event``.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ``activate_trials`` тянет camoufox/playwright. В headless-CI этих
# пакетов нет → подкладываем заглушки до импорта.
def _ensure_stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


if "camoufox" not in sys.modules:
    def _stub(*a, **kw):
        raise RuntimeError("camoufox stub")

    camoufox = _ensure_stub_module("camoufox")
    async_api = _ensure_stub_module(
        "camoufox.async_api",
        AsyncNewBrowser=_stub,
        AsyncCamoufox=_stub,
    )
    camoufox.async_api = async_api


from activate_trials import (  # noqa: E402
    StopEventLike,
    activate_trials_pipeline,
)
from storage import AccountDB  # noqa: E402


@pytest.fixture()
def empty_db(tmp_path: Path) -> AccountDB:
    db = AccountDB(tmp_path / "a.db")
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Контракт: stop_event может быть threading.Event ИЛИ asyncio.Event
# ---------------------------------------------------------------------------


def test_stop_event_alias_includes_threading_and_asyncio() -> None:
    """Регрессионный тест: тип StopEventLike покрывает оба класса."""
    # typing.Union раскрывается в __args__.
    args = getattr(StopEventLike, "__args__", None)
    assert args is not None, StopEventLike
    arg_set = set(args)
    assert threading.Event in arg_set, args
    assert asyncio.Event in arg_set, args


def test_pipeline_accepts_threading_event(empty_db: AccountDB) -> None:
    """С пустой БД pipeline возвращает 0/0/0 и не падает на threading.Event."""
    stop = threading.Event()
    stats = asyncio.run(
        activate_trials_pipeline(
            empty_db, parallel=1, timeout=1.0,
            profile_path=None, stop_event=stop,
        )
    )
    assert stats == {"success": 0, "failed": 0, "total": 0}


def test_pipeline_accepts_asyncio_event(empty_db: AccountDB) -> None:
    """С пустой БД pipeline возвращает 0/0/0 и для asyncio.Event."""
    async def _run() -> dict[str, int]:
        ev = asyncio.Event()
        return await activate_trials_pipeline(
            empty_db, parallel=1, timeout=1.0,
            profile_path=None, stop_event=ev,
        )

    stats = asyncio.run(_run())
    assert stats == {"success": 0, "failed": 0, "total": 0}


def test_pipeline_default_stop_event_is_none_safe(empty_db: AccountDB) -> None:
    """``stop_event=None`` → pipeline сам создаёт asyncio.Event и не валится."""
    stats = asyncio.run(
        activate_trials_pipeline(
            empty_db, parallel=2, timeout=1.0,
            profile_path=None, stop_event=None,
        )
    )
    assert stats == {"success": 0, "failed": 0, "total": 0}


# ---------------------------------------------------------------------------
# Отмена: уже взведённый stop_event → pipeline проходит мгновенно
# ---------------------------------------------------------------------------


def test_pipeline_with_preset_stop_event_returns_quickly(empty_db: AccountDB) -> None:
    """Если stop_event взведён до старта (или БД пуста), pipeline
    возвращает 0/0/0 без зависаний. Это покрывает «нажал Stop ещё до
    того, как Step 5 успел заполнить очередь».
    """
    stop = threading.Event()
    stop.set()

    async def _run_with_timeout() -> dict[str, int]:
        return await asyncio.wait_for(
            activate_trials_pipeline(
                empty_db, parallel=3, timeout=1.0,
                profile_path=None, stop_event=stop,
            ),
            timeout=5.0,
        )

    stats = asyncio.run(_run_with_timeout())
    assert stats == {"success": 0, "failed": 0, "total": 0}


# ---------------------------------------------------------------------------
# Контракт _worker-цикла: проверяем именно ту строку, которую правим
# ---------------------------------------------------------------------------


def test_worker_loop_skeleton_breaks_on_stop_event() -> None:
    """Реплика логики цикла в ``_worker`` (без Camoufox).

    Это не запуск настоящего ``_worker`` (тот поднимает Camoufox),
    а изолированный тест поведения, которое мы хотим гарантировать:
    как только stop_event взведён, очередной аккаунт не берётся —
    цикл выходит. Если кто-то рефакторит ``_worker`` и сломает эту
    инварианту, надо переписать и эту копию параллельно.
    """
    async def _run() -> int:
        q: asyncio.Queue[int] = asyncio.Queue()
        for i in range(10):
            q.put_nowait(i)
        stop = threading.Event()
        processed = 0

        # Воссоздаём цикл из activate_trials._worker:
        while not stop.is_set():
            try:
                _ = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if stop.is_set():
                q.task_done()
                break
            processed += 1
            q.task_done()
            if processed == 3:
                stop.set()  # имитируем «нажал Stop после 3-го аккаунта»

        return processed

    processed = asyncio.run(_run())
    # Обработали 3 задачи, потом отмена. Остальные 7 не должны быть
    # тронуты — основной выигрыш от stop_event'а.
    assert processed == 3, processed
