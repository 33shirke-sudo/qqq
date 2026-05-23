"""Утилиты для структурированного логирования и диагностики.

Предоставляет:
- Настройку логирования с timestamp и уровнями
- Декоратор @log_timing для замера времени выполнения функций
- Логирование исключений с полным контекстом
- Сохранение логов в logs/qqq.log с ротацией (RotatingFileHandler,
  10 МБ × 5 бэкапов)
"""

from __future__ import annotations

import functools
import logging
import sys
import time
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, TypeVar

# Корень проекта
ROOT = Path(__file__).parent
LOGS_DIR = ROOT / "logs"

# Глобальный флаг debug-режима
_DEBUG_MODE = False

# P2-6: параметры ротации. 10 МБ на файл × 5 бэкапов = до 60 МБ
# логов на весь проект; раньше каждый запуск создавал новый
# `debug_YYYY-MM-DD_HH-MM-SS.log`, и после сотен запусков папка
# логов разбухала. cleanup_old_logs() оставлен для уборки
# старых timestamped-файлов (всё ещё лежат на диске у старых
# инсталляций).
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5
_LOG_FILENAME = "qqq.log"

# Типы для декораторов
F = TypeVar("F", bound=Callable[..., Any])


def set_debug_mode(enabled: bool) -> None:
    """Включить или выключить debug-режим."""
    global _DEBUG_MODE
    _DEBUG_MODE = enabled


def is_debug_mode() -> bool:
    """Проверить, включён ли debug-режим."""
    return _DEBUG_MODE


def setup_logging(debug: bool = False, log_to_file: bool = True) -> logging.Logger:
    """Настроить логирование для проекта.

    Args:
        debug: Если True — уровень DEBUG, иначе INFO
        log_to_file: Если True — сохранять логи в файл

    Returns:
        Настроенный logger
    """
    set_debug_mode(debug)

    # Создать папку для логов
    if log_to_file:
        LOGS_DIR.mkdir(exist_ok=True)

    # Формат логов
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Уровень логирования
    level = logging.DEBUG if debug else logging.INFO

    # Настроить root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Очистить существующие handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_formatter = logging.Formatter(log_format, date_format)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # File handler
    if log_to_file:
        # P2-6: один файл `qqq.log` с ротацией по размеру (10 МБ × 5 бэкапов).
        # При переполнении RotatingFileHandler переименовывает
        # `qqq.log` → `qqq.log.1`, `qqq.log.1` → `qqq.log.2`, и так до .5.
        # Для поиска по времени ранее были timestamped файлы, но это
        # вело к сотням логов (PLAN.md → P2-6) — при необходимости
        # берите timestamp из самой первой строки файла (все записи
        # начинаются с ISO-таймстампа).
        log_file = LOGS_DIR / _LOG_FILENAME
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)  # В файл всегда пишем всё
        file_formatter = logging.Formatter(log_format, date_format)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

        # При старте пишем явный session-маркер — чтобы в слитом
        # лог-файле можно было отыскать границы конкретного запуска.
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        root_logger.info(f"=== Сессия {timestamp} | лог в: {log_file} ===")

    # Отключить verbose логи от playwright
    logging.getLogger("playwright").setLevel(logging.WARNING)

    return root_logger


def log_timing(func: F) -> F:
    """Декоратор для замера времени выполнения функции.

    Логирует время выполнения на уровне DEBUG.
    Работает как с синхронными, так и с асинхронными функциями.

    Usage:
        @log_timing
        def my_function():
            ...

        @log_timing
        async def my_async_function():
            ...
    """
    logger = logging.getLogger(func.__module__)

    # Проверить, асинхронная ли функция
    import asyncio

    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            func_name = func.__qualname__
            start_time = time.perf_counter()
            logger.debug(f"[TIMING] {func_name} started")

            try:
                result = await func(*args, **kwargs)
                elapsed = time.perf_counter() - start_time
                logger.debug(f"[TIMING] {func_name} completed in {elapsed:.3f}s")
                return result
            except Exception as e:
                elapsed = time.perf_counter() - start_time
                logger.debug(f"[TIMING] {func_name} failed after {elapsed:.3f}s: {e}")
                raise

        return async_wrapper  # type: ignore
    else:
        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            func_name = func.__qualname__
            start_time = time.perf_counter()
            logger.debug(f"[TIMING] {func_name} started")

            try:
                result = func(*args, **kwargs)
                elapsed = time.perf_counter() - start_time
                logger.debug(f"[TIMING] {func_name} completed in {elapsed:.3f}s")
                return result
            except Exception as e:
                elapsed = time.perf_counter() - start_time
                logger.debug(f"[TIMING] {func_name} failed after {elapsed:.3f}s: {e}")
                raise

        return sync_wrapper  # type: ignore


def log_exception(logger: logging.Logger, exc: Exception, context: str = "") -> None:
    """Логировать исключение с полным контекстом.

    Args:
        logger: Logger для записи
        exc: Исключение для логирования
        context: Дополнительный контекст (например, "processing account X")
    """
    exc_type = type(exc).__name__
    exc_msg = str(exc)
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    if context:
        logger.error(f"[EXCEPTION] {context}: {exc_type}: {exc_msg}")
    else:
        logger.error(f"[EXCEPTION] {exc_type}: {exc_msg}")

    logger.debug(f"[TRACEBACK]\n{tb}")


def log_step(logger: logging.Logger, step: str, details: str = "") -> None:
    """Логировать шаг выполнения (для debug-режима).

    Args:
        logger: Logger для записи
        step: Название шага (например, "mail-login", "wait-email")
        details: Дополнительные детали
    """
    if details:
        logger.debug(f"[STEP] {step}: {details}")
    else:
        logger.debug(f"[STEP] {step}")


def log_metric(logger: logging.Logger, metric: str, value: Any, unit: str = "") -> None:
    """Логировать метрику (для сбора статистики).

    Args:
        logger: Logger для записи
        metric: Название метрики (например, "emails_found", "captcha_attempts")
        value: Значение метрики
        unit: Единица измерения (например, "ms", "count")
    """
    if unit:
        logger.info(f"[METRIC] {metric}={value} {unit}")
    else:
        logger.info(f"[METRIC] {metric}={value}")


async def save_screenshot_on_error(
    page,
    error: Exception,
    prefix: str = "error",
) -> Path | None:
    """Сохранить скриншот страницы при ошибке (только в debug-режиме).

    Args:
        page: Playwright Page
        error: Исключение, вызвавшее ошибку
        prefix: Префикс имени файла

    Returns:
        Path к сохранённому скриншоту или None
    """
    if not is_debug_mode():
        return None

    try:
        screenshots_dir = ROOT / "screenshots"
        screenshots_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        error_type = type(error).__name__
        filename = f"{prefix}_{error_type}_{timestamp}.png"
        screenshot_path = screenshots_dir / filename

        await page.screenshot(path=str(screenshot_path), full_page=True)

        logger = logging.getLogger(__name__)
        logger.info(f"Скриншот сохранён: {screenshot_path}")

        return screenshot_path
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.warning(f"Не удалось сохранить скриншот: {e}")
        return None


def cleanup_old_logs(days: int = 30) -> int:
    """Удалить логи старше указанного количества дней.

    Args:
        days: Возраст логов в днях

    Returns:
        Количество удалённых файлов
    """
    if not LOGS_DIR.exists():
        return 0

    cutoff_time = time.time() - (days * 24 * 60 * 60)
    deleted = 0

    # P2-6: чистим и старые `debug_*.log` (перед RotatingFileHandler-ом),
    # и ротационные бэкапы `qqq.log.*`. Активный `qqq.log` не
    # трогаем — в него пишет текущий handler.
    patterns = ("debug_*.log", f"{_LOG_FILENAME}.*")
    for pattern in patterns:
        for log_file in LOGS_DIR.glob(pattern):
            try:
                if log_file.stat().st_mtime < cutoff_time:
                    log_file.unlink()
                    deleted += 1
            except Exception:
                pass

    return deleted


def cleanup_old_screenshots(days: int = 7) -> int:
    """Удалить скриншоты старше указанного количества дней.

    Args:
        days: Возраст скриншотов в днях

    Returns:
        Количество удалённых файлов
    """
    screenshots_dir = ROOT / "screenshots"
    if not screenshots_dir.exists():
        return 0

    cutoff_time = time.time() - (days * 24 * 60 * 60)
    deleted = 0

    for screenshot in screenshots_dir.glob("*.png"):
        try:
            if screenshot.stat().st_mtime < cutoff_time:
                screenshot.unlink()
                deleted += 1
        except Exception:
            pass

    return deleted
