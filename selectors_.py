"""Реестр стабильных CSS-селекторов и helper для fallback-поиска.

P2-7: раньше селекторы DOM/iframe были захардкожены без альтернатив
(см. ``check_cards.py`` — ``#bin``, ``a#gen``, ``button#start``…). При
любом изменении разметки сайта пайплайн молча ломался — найти, что
именно отвалилось, можно было только запустив и поймав timeout.

Здесь — единственное место с CSS-селекторами по сервисам (chkr.cc,
Stripe checkout, Devin app, rainloop mail). Для каждой логической
точки фиксируем **кортеж** селекторов от самого специфичного к самому
общему. Helper :func:`find_any` (sync) / :func:`find_any_async` (async)
обходит кортеж и возвращает первый видимый локатор — или ``None``,
если ни один не нашёлся за указанный таймаут.

Шаблон применения для существующих модулей:

.. code-block:: python

    from selectors_ import START_BUTTON_SELECTORS, find_any_async

    loc = await find_any_async(page, START_BUTTON_SELECTORS, timeout_s=10)
    if loc is None:
        raise StepError("chkr.cc 'Start' button не найден")
    await loc.click()
"""

from __future__ import annotations

import asyncio
from typing import Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# chkr.cc — Шаг 4 (check_cards.py)
# ---------------------------------------------------------------------------

# «Открыть генератор по BIN». Кнопка-триггер модалки с инпутами BIN+quantity.
CHKR_OPEN_GENERATOR_SELECTORS: tuple[str, ...] = (
    'button[data-bs-target="#bin-generator"]',
    'button[data-target="#bin-generator"]',  # bootstrap 4 fallback
    'button:has-text("BIN Generator")',
)

# Текстовое поле для BIN внутри модалки генератора.
CHKR_BIN_INPUT_SELECTORS: tuple[str, ...] = (
    "#bin-generator #bin",
    "#bin",
    'input[name="bin"]',
)

# Сколько карт сгенерировать.
CHKR_QUANTITY_INPUT_SELECTORS: tuple[str, ...] = (
    "#bin-generator #quantity",
    "#quantity",
    'input[name="quantity"]',
)

# Кнопка «Сгенерировать» внутри модалки.
CHKR_GENERATE_BUTTON_SELECTORS: tuple[str, ...] = (
    "a#gen",
    "#bin-generator a#gen",
    "#bin-generator button#gen",
)

# Кнопка «Старт» — главная кнопка чекинга на странице после генерации.
CHKR_START_BUTTON_SELECTORS: tuple[str, ...] = (
    "button#start",
    'button[type="submit"]:has-text("Start")',
)

# Кнопка-стоп внутри модалки прогресса (показывается во время чекинга).
CHKR_PROGRESS_STOP_SELECTORS: tuple[str, ...] = (
    "button#modal-stop",
    '#progress-modal button[data-action="stop"]',
)

# Контейнер с живыми результатами (text-area, обновляется по мере чекинга).
CHKR_LIVE_RESULTS_SELECTORS: tuple[str, ...] = (
    "#liveResults",
    ".live-results",
)

# Textarea для ввода списка карт (если используется ручной режим).
CHKR_CC_TEXTAREA_SELECTORS: tuple[str, ...] = (
    "textarea#cc",
    'textarea[name="cc"]',
)


# ---------------------------------------------------------------------------
# Rainloop / pinmx mail-клиент — Шаг 2 (register_devin.py)
# ---------------------------------------------------------------------------

# Кнопка «обновить список писем». В rainloop помечена классом buttonReload
# и привязкой ``command: reloadCommand``.
MAIL_REFRESH_BUTTON_SELECTORS: tuple[str, ...] = (
    "a.buttonReload",
    'a[data-bind*="reloadCommand"]',
)

# Элемент списка писем.
MAIL_LIST_ITEM_SELECTORS: tuple[str, ...] = (
    ".messageListPlace .messageListItem",
    ".messageList .messageListItem",
    ".messageListItem",
)

# Тело открытого письма (preview-панель). От самого специфичного к общему.
MAIL_DETAIL_BODY_SELECTORS: tuple[str, ...] = (
    ".messageView .messageItem.fixIndex .content",
    ".messageView .b-message-view-wrapper",
    ".messageView iframe",
)


# ---------------------------------------------------------------------------
# Stripe Checkout — Шаг 5 (activate_accounts.py)
# ---------------------------------------------------------------------------

# Все четыре поля Stripe — в новом ElementsApp 2024+ и в старом «legacy».
STRIPE_CARD_NUMBER_SELECTORS: tuple[str, ...] = (
    'input[name="cardNumber"]',
    'input[autocomplete="cc-number"]',
    'input[name="number"]',
    'input[placeholder*="1234 1234" i]',
)
STRIPE_CARD_EXPIRY_SELECTORS: tuple[str, ...] = (
    'input[name="cardExpiry"]',
    'input[autocomplete="cc-exp"]',
    'input[name="expiry"]',
    'input[placeholder*="MM" i][placeholder*="YY" i]',
)
STRIPE_CARD_CVC_SELECTORS: tuple[str, ...] = (
    'input[name="cardCvc"]',
    'input[autocomplete="cc-csc"]',
    'input[name="cvc"]',
    'input[placeholder*="CVC" i]',
)
STRIPE_CARD_NAME_SELECTORS: tuple[str, ...] = (
    'input[name="billingName"]',
    'input[autocomplete="cc-name"]',
    'input[name="cardholderName"]',
    'input[name="name"]',
    'input[placeholder*="имя" i]',
    'input[placeholder*="name on card" i]',
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_any(
    page_or_frame,
    selectors: Iterable[str],
    *,
    timeout_ms: int = 5_000,
):
    """Sync-версия. Найти первый видимый локатор из ``selectors``.

    Args:
        page_or_frame: Playwright ``Page`` или ``Frame`` (sync API).
        selectors: набор CSS-селекторов в порядке предпочтения.
        timeout_ms: сколько ждать каждый селектор перед переходом к
            следующему. Общее ожидание =
            ``len(selectors) * timeout_ms``.

    Returns:
        Locator (первый, у которого ``is_visible`` вернул True) или
        ``None`` если ни один не сработал.
    """
    for sel in selectors:
        try:
            loc = page_or_frame.locator(sel).first
            if loc.is_visible(timeout=timeout_ms):
                return loc
        except Exception:
            continue
    return None


async def find_any_async(
    page_or_frame,
    selectors: Iterable[str],
    *,
    timeout_ms: int = 5_000,
):
    """Async-версия :func:`find_any`. Все условия идентичны, но локатор
    проверяется через ``await loc.is_visible(...)``.

    Используется в async-pipeline (``activate_trials.py``,
    ``check_cards.py``, ``devin_async.py``).
    """
    for sel in selectors:
        try:
            loc = page_or_frame.locator(sel).first
            if await loc.is_visible(timeout=timeout_ms):
                return loc
        except Exception:
            continue
    return None


async def wait_for_any_async(
    page_or_frame,
    selectors: Iterable[str],
    *,
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.5,
) -> Optional[object]:
    """Подождать, пока ХОТЯ БЫ ОДИН селектор станет видимым.

    Аналог :func:`find_any_async`, но с polling-логикой: каждый
    ``poll_interval_s`` пробует найти любой видимый локатор;
    суммарно — не больше ``timeout_s`` секунд.

    Удобно для UI-переходов: после клика по «Submit» страница может
    показать либо «Success», либо «Captcha required» — обе кнопки
    зеркально валидны.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    sels: Sequence[str] = tuple(selectors)
    while True:
        loc = await find_any_async(page_or_frame, sels, timeout_ms=200)
        if loc is not None:
            return loc
        if asyncio.get_event_loop().time() > deadline:
            return None
        await asyncio.sleep(poll_interval_s)


__all__ = [
    "CHKR_OPEN_GENERATOR_SELECTORS",
    "CHKR_BIN_INPUT_SELECTORS",
    "CHKR_QUANTITY_INPUT_SELECTORS",
    "CHKR_GENERATE_BUTTON_SELECTORS",
    "CHKR_START_BUTTON_SELECTORS",
    "CHKR_PROGRESS_STOP_SELECTORS",
    "CHKR_LIVE_RESULTS_SELECTORS",
    "CHKR_CC_TEXTAREA_SELECTORS",
    "MAIL_REFRESH_BUTTON_SELECTORS",
    "MAIL_LIST_ITEM_SELECTORS",
    "MAIL_DETAIL_BODY_SELECTORS",
    "STRIPE_CARD_NUMBER_SELECTORS",
    "STRIPE_CARD_EXPIRY_SELECTORS",
    "STRIPE_CARD_CVC_SELECTORS",
    "STRIPE_CARD_NAME_SELECTORS",
    "find_any",
    "find_any_async",
    "wait_for_any_async",
]
