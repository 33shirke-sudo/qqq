"""Единый запуск Playwright Chrome в трёх режимах:

* ``clean``     — чистый Playwright-context (default, как раньше).
* ``incognito`` — Chrome с флагом ``--incognito``.
* ``system``    — persistent_context, использующий локальную копию
                  твоего системного Chrome-профиля. Копию готовит
                  ``copy_chrome_profile.py``; она лежит в
                  ``./browser_profile/Default``.

Зачем единый хелпер: чтобы все скрипты конвейера (``create_emails``,
``register_devin``, ``start_devin_trial``) принимали одинаковый флаг
``--browser-mode`` и стартовали браузер ровно одинаково. Между режимами
формы скриптов не отличаются — все три возвращают
``(browser_or_None, context, cleanup)``: ``cleanup()`` корректно закроет
context и browser, если он отдельный.

ВАЖНО: ``system`` НЕ работает, если у тебя одновременно открыт обычный
Chrome с тем же профилем. Поэтому ``system`` использует
**копию** профиля (``./browser_profile/Default``), а не оригинал.
Используй ``copy_chrome_profile.py`` чтобы её обновить.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Playwright,
)


VALID_MODES = ("clean", "incognito", "system", "isolated")
DEFAULT_MODE = "clean"


@dataclass
class LaunchedBrowser:
    """Результат :func:`launch_browser`. Используем как тройку через
    атрибуты — так читабельнее, чем кортеж."""

    browser: Browser | None
    """``None`` для ``system`` (persistent context — там browser не
    выделяется отдельно)."""

    context: BrowserContext
    """Активный контекст. У всех режимов одинаковый интерфейс."""

    cleanup: Callable[[], None]
    """Функция, которую следует вызвать в ``finally`` для корректного
    закрытия context и browser."""


def _project_root() -> Path:
    """Корень проекта = папка, в которой лежит этот файл."""
    return Path(__file__).resolve().parent


def _system_profile_dir() -> Path:
    """Куда копировать / откуда читать копию твоего реального Chrome-профиля."""
    return _project_root() / "browser_profile"


def get_chrome_user_data_default() -> Path | None:
    """Найти стандартный путь к ``User Data\\Default`` для текущей
    системы. Используется в ``copy_chrome_profile.py``.

    Возвращает None, если стандартный путь не существует.
    """
    candidates: list[Path] = []
    # Windows
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(Path(local_app) / "Google" / "Chrome" / "User Data" / "Default")
    # macOS
    home = Path.home()
    candidates.append(home / "Library" / "Application Support" / "Google" / "Chrome" / "Default")
    # Linux
    candidates.append(home / ".config" / "google-chrome" / "Default")

    for c in candidates:
        if c.exists() and c.is_dir():
            return c
    return None


def launch_browser(
    pw: Playwright,
    mode: str,
    *,
    head: bool = True,
) -> LaunchedBrowser:
    """Запустить Chrome в одном из трёх режимов и вернуть (browser, context, cleanup).

    Args:
        pw: активный экземпляр Playwright (``with sync_playwright() as pw``).
        mode: ``clean`` / ``incognito`` / ``system``.
        head: True = видимое окно, False = headless. Для ``system``
            всегда True (persistent context работает только с GUI Chrome).

    Returns:
        :class:`LaunchedBrowser` с заполненными ``browser`` (или None для
        ``system``), ``context`` и ``cleanup``.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown browser mode: {mode!r}. Use one of {VALID_MODES}.")

    if mode == "clean":
        browser = pw.chromium.launch(channel="chrome", headless=not head)
        context = browser.new_context()

        def _cleanup() -> None:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

        return LaunchedBrowser(browser=browser, context=context, cleanup=_cleanup)

    if mode == "incognito":
        browser = pw.chromium.launch(
            channel="chrome",
            headless=not head,
            args=["--incognito"],
        )
        context = browser.new_context()

        def _cleanup_inc() -> None:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

        return LaunchedBrowser(browser=browser, context=context, cleanup=_cleanup_inc)

    # mode == "system"
    profile_dir = _system_profile_dir() / "Default"
    if not profile_dir.exists():
        raise SystemExit(
            f"Профиль для системного режима не найден: {profile_dir}\n"
            "Запусти сначала: .venv\\Scripts\\python.exe copy_chrome_profile.py"
        )

    # launch_persistent_context открывает Chrome с указанным user_data_dir
    # как «единым целым»: возвращает контекст, а не browser+context.
    # Соответственно, browser=None.
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(_system_profile_dir()),
        channel="chrome",
        headless=False,  # persistent_context требует видимое окно
    )

    def _cleanup_sys() -> None:
        try:
            context.close()
        except Exception:
            pass

    return LaunchedBrowser(browser=None, context=context, cleanup=_cleanup_sys)


def add_browser_mode_arg(parser) -> None:
    """Добавить ``--browser-mode`` к argparse-парсеру вызывающего скрипта."""
    parser.add_argument(
        "--browser-mode",
        choices=VALID_MODES,
        default=DEFAULT_MODE,
        help=(
            "Режим запуска Chrome: "
            "'clean' (новый чистый профиль, default), "
            "'incognito' (Chrome --incognito), "
            "'system' (твой реальный профиль из ./browser_profile/, "
            "сначала запусти copy_chrome_profile.py), "
            "'isolated' (изолированная копия профиля для параллельной работы)."
        ),
    )



# ---------------------------------------------------------------------------
# Async-вариант (для check_cards.py с параллельными вкладками)
# ---------------------------------------------------------------------------


async def launch_browser_async(
    pw,
    mode: str,
    *,
    head: bool = True,
    profile_path: Optional[Path] = None,
):
    """Async-аналог :func:`launch_browser`.

    Возвращает кортеж ``(browser_or_None, context, async_cleanup)``,
    где ``async_cleanup`` — корутина, которую следует ``await`` в
    ``finally``.

    Args:
        pw: Playwright instance
        mode: Режим браузера (clean/incognito/system/isolated)
        head: Показывать окно браузера
        profile_path: Путь к профилю для режима 'isolated'
    """
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown browser mode: {mode!r}. Use one of {VALID_MODES}.")

    if mode == "isolated":
        # Создаем изолированную копию профиля
        from profile_manager import ProfileManager

        profile_manager = ProfileManager(source_profile=profile_path)
        temp_profile = profile_manager.create_isolated_profile()

        context = await pw.chromium.launch_persistent_context(
            str(temp_profile),
            channel="chrome",
            headless=not head,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
        )

        async def _cleanup_isolated() -> None:
            try:
                await context.close()
            except Exception:
                pass
            profile_manager.cleanup()

        return None, context, _cleanup_isolated

    if mode == "clean":
        # Анти-throttling флаги: Chrome обычно тормозит фоновые tabs
        # (timer freeze, lazy paint), что ломает наши поллинги.
        chrome_args = [
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion",
        ]
        browser = await pw.chromium.launch(
            channel="chrome",
            headless=not head,
            args=chrome_args,
        )
        context = await browser.new_context()

        async def _cleanup() -> None:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

        return browser, context, _cleanup

    if mode == "incognito":
        browser = await pw.chromium.launch(
            channel="chrome",
            headless=not head,
            args=["--incognito"],
        )
        context = await browser.new_context()

        async def _cleanup_inc() -> None:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

        return browser, context, _cleanup_inc

    # mode == "system"
    profile_dir = _system_profile_dir() / "Default"
    if not profile_dir.exists():
        raise SystemExit(
            f"Профиль для системного режима не найден: {profile_dir}\n"
            "Запусти сначала: .venv\\Scripts\\python.exe copy_chrome_profile.py"
        )
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(_system_profile_dir()),
        channel="chrome",
        headless=False,
    )

    async def _cleanup_sys() -> None:
        try:
            await context.close()
        except Exception:
            pass

    return None, context, _cleanup_sys
