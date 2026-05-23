"""register_devin: автоматическая регистрация аккаунтов на app.devin.ai.

Модуль импортируется без сайд-эффектов: вся CLI-логика — под
``if __name__ == "__main__"``. Сейчас здесь парсинг ``results.txt``,
учёт прогресса в ``devin_done.txt`` / ``devin_errors.txt``, извлечение
6-значного кода из тела письма и защитный капча-солвер; остальные шаги
пайплайна (логин в mail-client, sign up на Devin и т.д.) добавляются в
последующих задачах спеки ``devin-ai-registration``.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import ddddocr
from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from browser_modes import add_browser_mode_arg
from logging_utils import setup_logging, log_exception
from storage import AccountDB


# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------

# Все рантайм-файлы лежат рядом со скриптом, чтобы CLI работал одинаково
# и из ``.\register_devin.py``, и из ``python -m register_devin``.
ROOT = Path(__file__).parent
RESULTS_PATH = ROOT / "имейлы pingmx.txt"
DEVIN_DONE_PATH = ROOT / "аккаунты devin.txt"
DEVIN_ERRORS_PATH = ROOT / "devin_errors.txt"
IDENTITIES_PATH = ROOT / "личности.txt"
DB_PATH = ROOT / "accounts.db"


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

# URL входа в почтовый клиент. После успешного логина mail-client редиректит
# на отдельную страницу ``/mail/index.html`` (это rainloop, не SPA с хешем).
# Хеш-маршрут после входа отсутствует, поэтому ``MAIL_INBOX_URL_HASH`` —
# фактически часть URL-пути, по которой мы понимаем, что вход успешен.
MAIL_LOGIN_URL = "https://mail-client.pinmx.com/"
MAIL_INBOX_URL_HASH = "/mail/"

# URL страницы регистрации Devin. Берём именно ``/auth/signup``, а не корень
# ``app.devin.ai`` — корень редиректит на лендинг и просит залогиниться,
# тогда как форма «Email address + Sign up» живёт по этому пути напрямую
# (см. design.md → Investigation → ``app.devin.ai/auth/signup``).
DEVIN_SIGNUP_URL = "https://app.devin.ai/auth/signup"

# Кнопка «обновить список писем». В rainloop она помечена двумя стабильными
# признаками: классом ``buttonReload`` и привязкой ``command: reloadCommand``.
# Берём первое — селектор короче и стабильнее. Внутри кнопки — `<i>`,
# поэтому кликать нужно по самому ``<a>``.
MAIL_REFRESH_BUTTON_SELECTOR = "a.buttonReload"

# Элемент списка писем. Каждое письмо в rainloop живёт в ``.messageListItem``
# внутри контейнера ``.messageListPlace``. Раньше использовался селектор
# с родителем ``.messageList``, но в актуальной разметке такого родителя
# может не быть — оставляем только обязательную часть.
MAIL_LIST_ITEM_SELECTOR = ".messageListPlace .messageListItem"

# Тело открытого письма. Когда письмо открыто в правой панели (preview),
# его содержимое лежит внутри ``.messageView .messageItem.fixIndex .content``.
# Это селектор, по которому ``inner_text()`` отдаёт читаемый текст письма.
# Fallback-цепочка: если разметка изменится — пробовать
# ``.b-message-view-wrapper`` (более общий контейнер), затем ``iframe``
# в правой панели (rainloop иногда рендерит HTML-письма в iframe).
MAIL_DETAIL_BODY_SELECTOR = ".messageView .messageItem.fixIndex .content"
MAIL_DETAIL_BODY_FALLBACK_SELECTORS: tuple[str, ...] = (
    ".messageView .b-message-view-wrapper",
    ".messageView iframe",
)


# ---------------------------------------------------------------------------
# Иерархия исключений пайплайна регистрации
# ---------------------------------------------------------------------------


class RegistrationError(Exception):
    """Базовый класс для всех ожидаемых ошибок пайплайна регистрации."""


class StepError(RegistrationError):
    """Шаг провалился, аккаунт пропускаем (фиксируется в ``devin_errors.txt``).

    Используется для любой неустранимой ошибки конкретного шага: таймаута,
    невидимого элемента, явной ошибки сервера, неверных учётных данных и т.п.
    """


class InvalidCodeError(RegistrationError):
    """Devin отклонил введённый код подтверждения как неверный/просроченный.

    Внешний код может попытаться перечитать письмо и ввести более свежий код;
    лимит ретраев фиксируется в задаче ``submit_devin_code``.
    """


# Регулярка для поиска 6-значного кода подтверждения в теле письма.
# Negative lookarounds ``(?<!\d)`` и ``(?!\d)`` гарантируют, что найденная
# последовательность из ровно 6 цифр НЕ примыкает к другим цифрам:
# - 5-значные числа не подходят (короче);
# - 7-значные числа не подходят (соседняя цифра справа ломает ``(?!\d)``);
# - 12 цифр подряд тоже не дают совпадения (любая позиция внутри окружена
#   цифрами с одной из сторон, поэтому lookaround всегда проваливается).
# Берём именно первое совпадение — это самый ранний 6-значный «остров».
_SIX_DIGIT_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


@dataclass(frozen=True)
class Account:
    """Учётная запись из ``results.txt``.

    ``email`` хранится в нижнем регистре (нормализуется при парсинге),
    ``password`` — как есть, без изменения регистра и пробелов.
    """

    email: str
    password: str


def parse_account(line: str) -> Account | None:
    """Распарсить одну строку ``results.txt`` в :class:`Account`.

    Правила:

    - пустая строка (после удаления завершающего перевода строки) → ``None``;
    - строка без двоеточия → ``None``;
    - режем по первому ``":"``: слева — email, справа — всё остальное;
    - если в правой части есть разделитель ``" | "`` (его добавляет
      ``add_identities.py``), то всё, начиная с него, отбрасывается;
    - email приводится к нижнему регистру; пароль остаётся как есть.
    """
    stripped = line.rstrip("\r\n")
    if not stripped:
        return None
    if ":" not in stripped:
        return None

    email_part, _, rest = stripped.partition(":")
    if " | " in rest:
        password = rest.split(" | ", 1)[0]
    else:
        password = rest

    return Account(email=email_part.lower(), password=password)


def load_accounts(path: Path) -> list[Account]:
    """Загрузить список аккаунтов из ``results.txt``.

    Дубликаты по email отбрасываются — первый встреченный выигрывает.
    Если файла нет, возвращается пустой список; решение о ненулевом коде
    возврата принимает ``main`` (см. задачу CLI).
    """
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    # ``str.splitlines`` режет ещё и по управляющим символам вроде ``\x1c``,
    # ``\x1e``, ``\x85`` и т.п., из-за чего пароль с такими байтами «рвался»
    # на две строки. Делим только по ``\n`` (и срезаем хвостовой ``\r``,
    # чтобы поддержать CRLF), а одиночный завершающий ``\n`` не превращаем
    # в лишнюю пустую запись.
    if text.endswith("\n"):
        text = text[:-1]

    accounts: list[Account] = []
    seen: set[str] = set()
    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r")
        account = parse_account(line)
        if account is None:
            continue
        if account.email in seen:
            continue
        seen.add(account.email)
        accounts.append(account)
    return accounts


def load_done(path: Path) -> set[str]:
    """Загрузить множество email-ов уже обработанных аккаунтов.

    Email-ы приводятся к нижнему регистру, пустые строки пропускаются.
    Если файла нет — возвращается пустое множество (это нормальный путь
    для первого запуска).
    """
    if not path.exists():
        return set()

    done: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            email = raw_line.strip().lower()
            if not email:
                continue
            done.add(email)
    return done


def _flush_to_disk(fh) -> None:
    """Сбросить буфер и заставить ОС записать данные на диск.

    После ``flush()`` данные доходят до ОС, после ``os.fsync`` — до диска.
    Это нужно, чтобы Ctrl+C / падение процесса не теряли уже записанный
    прогресс (Requirement 2.3).
    """
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except (OSError, AttributeError):
        # На некоторых файловых системах / редиректах stdio fsync может
        # быть недоступен — это не повод падать; flush мы уже сделали.
        pass


def append_done(path: Path, email: str) -> None:
    """Дописать email в ``devin_done.txt`` (по одному в строке, lower-case).

    Открывается в режиме ``"a"`` с UTF-8, после записи — flush + fsync,
    чтобы прогресс гарантированно оказался на диске до возврата управления.
    """
    normalized = email.lower()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(normalized + "\n")
        _flush_to_disk(fh)


def append_error(path: Path, email: str, reason: str) -> None:
    """Дописать запись об ошибке в ``devin_errors.txt``.

    Формат строки — TSV: ``email<TAB>reason``. Чтобы формат не «ломался»
    переносами строк или лишними табами в причине, ``\\t``, ``\\n`` и ``\\r``
    в ``reason`` заменяются на пробел перед записью. Email пишется в
    нижнем регистре — для консистентности с ``devin_done.txt``.
    """
    normalized_email = email.lower()
    safe_reason = (
        reason.replace("\t", " ")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{normalized_email}\t{safe_reason}\n")
        _flush_to_disk(fh)


def extract_code(body: str) -> str | None:
    """Извлечь 6-значный код подтверждения из тела письма.

    Возвращает строку из ровно 6 цифр (первое подходящее совпадение в
    тексте) либо ``None``, если такой последовательности нет.

    За счёт negative lookarounds в регулярке (см. ``_SIX_DIGIT_CODE_RE``)
    последовательности, примыкающие к другим цифрам, НЕ считаются кодом:

    - 5 цифр подряд — не совпадают (короче 6);
    - 7 цифр подряд — не совпадают (справа ещё цифра, ``(?!\\d)`` валится);
    - 12 цифр подряд — тоже не совпадают (с любой стороны соседняя цифра);
    - ``"v123456"``, ``"abc 123456 def"``, ``"Code: 654321\\n"`` —
      совпадают, потому что границы — буквы / пробелы / переводы строк,
      но не цифры.

    Если в тексте несколько подходящих 6-значных «островов», возвращается
    первый из них (так Devin кладёт код в начало письма / отдельной строкой).
    """
    match = _SIX_DIGIT_CODE_RE.search(body)
    if match is None:
        return None
    return match.group(1)


# ---------------------------------------------------------------------------
# Капча-солвер (защитный путь)
# ---------------------------------------------------------------------------

# Сколько раз обновляем капчу, прежде чем сдаться. На странице
# pinmx.com/ru хватало 15, на login-форме mail-client (тот же стиль
# капчи, но картинка крупнее и шумнее) практика показала, что 15 иногда
# не хватает — поднято до 30. На реально сильных промахах ddddocr и
# 30 итераций укладываются в ~12 секунд.
CAPTCHA_REFRESHES = 30

# Пауза после клика «обновить» — даёт сайту время отрисовать новую картинку
# и поменять ``src``. То же значение используется в ``create_emails.py``.
_CAPTCHA_REFRESH_PAUSE_S = 0.35

# Кэш модели ddddocr. Инициализируем лениво — загрузка ONNX-модели стоит
# заметного времени и памяти, а сам модуль должен импортироваться дёшево
# (это требование тестов и Requirement 11). Модель потокобезопасно использовать
# из одного потока, что нам и нужно.
_ocr: ddddocr.DdddOcr | None = None


def _get_ocr() -> ddddocr.DdddOcr:
    """Вернуть инициализированную модель ddddocr (с алфавитом ``0-9``).

    Первый вызов грузит ONNX-модель и фиксирует алфавит цифрами; последующие
    отдают тот же объект. Делаем это лениво, чтобы ``import register_devin``
    оставался дешёвым (без побочных эффектов на уровне модуля).
    """
    global _ocr
    if _ocr is None:
        instance = ddddocr.DdddOcr(show_ad=False)
        instance.set_ranges("0123456789")
        _ocr = instance
    return _ocr


def solve_digit_captcha(
    page,
    img_selector: str,
    refresh_callable: Callable[[], None],
    expected_len: int,
) -> str | None:
    """Решить цифровую капчу на странице ``page``.

    Защитный путь — по образцу ``create_emails.py``. На сегодняшний день
    ни в логине mail-client, ни в sign up Devin капчи нет, но если она
    однажды появится — публичный API уже готов.

    Алгоритм одной попытки:

    1. Берём ``src`` у ``<img>`` по селектору ``img_selector``. Если его
       ещё нет — обновляем капчу и пробуем снова.
    2. Скачиваем картинку через ``page.request.get`` (так Playwright
       автоматически переиспользует cookies контекста, и мы не тащим
       отдельный HTTP-клиент).
    3. Прогоняем байты через ddddocr, оставляем только цифры.
    4. Если получилось ровно ``expected_len`` цифр — возвращаем строку.
       Иначе — обновляем капчу и идём на следующий круг.

    Лимит — :data:`CAPTCHA_REFRESHES` итераций. После исчерпания возвращаем
    ``None``: вызывающий код решает, считать это ошибкой или нет.

    Args:
        page: объект Playwright ``Page`` (или совместимый — нужен ``evaluate``
            и ``request.get``).
        img_selector: CSS-селектор тэга ``<img>`` с капчей.
        refresh_callable: функция без аргументов, которая обновляет капчу
            на странице (например, кликает по кнопке «обновить» или дергает
            JS-функцию). Вызывается, когда текущая попытка не удалась.
        expected_len: ожидаемая длина кода в цифрах (для Devin-сигнала
            подтверждения это было бы 6, но капчи обычно 4–6).

    Returns:
        Строка из ``expected_len`` цифр или ``None``, если после
        :data:`CAPTCHA_REFRESHES` обновлений подходящего ответа не нашлось.
    """
    ocr = _get_ocr()

    last_src = ""
    empty_src_in_a_row = 0
    for _ in range(CAPTCHA_REFRESHES):
        # ``selector`` передаём через аргумент ``evaluate`` — так не
        # нужно экранировать кавычки в самом селекторе и нет соблазна
        # JS-инъекций.
        src = page.evaluate(
            "(sel) => { const el = document.querySelector(sel); "
            "return el ? el.src : ''; }",
            img_selector,
        )
        if not src:
            # Несколько подряд пустых ``src`` означают, что капча-модалка
            # закрылась (например, сервер сам её снял после неудачной
            # попытки). Дальше крутиться внутри ``solve_digit_captcha``
            # бессмысленно — отдаём управление наверх (None), пусть
            # внешний цикл решает, что делать.
            empty_src_in_a_row += 1
            if empty_src_in_a_row >= 3:
                return None
            refresh_callable()
            time.sleep(_CAPTCHA_REFRESH_PAUSE_S)
            continue
        empty_src_in_a_row = 0

        if src == last_src:
            # ``refresh_callable`` ещё не успел сменить картинку —
            # подождём чуть-чуть и повторим итерацию (счётчик идёт).
            time.sleep(_CAPTCHA_REFRESH_PAUSE_S)
            continue
        last_src = src

        response = page.request.get(src)
        if response.status != 200:
            refresh_callable()
            time.sleep(_CAPTCHA_REFRESH_PAUSE_S)
            continue

        raw = ocr.classification(response.body())
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) == expected_len:
            return digits

        refresh_callable()
        time.sleep(_CAPTCHA_REFRESH_PAUSE_S)

    return None


# ---------------------------------------------------------------------------
# Вход в mail-client.pinmx.com
# ---------------------------------------------------------------------------

# Селекторы формы логина mail-client. Они зафиксированы исследованием через
# Playwright MCP (см. design.md → Investigation): сайт локализован, плейс-
# холдеры приходят на русском прямо из DOM, поэтому самый стабильный путь —
# матчить по ним. Если когда-нибудь сайт сменит плейсхолдеры, эти константы
# легко заменить, не трогая остальную логику.
_MAIL_LOGIN_EMAIL_SELECTOR = 'input[placeholder="Введите свой адрес электронной почты"]'
_MAIL_LOGIN_PASSWORD_SELECTOR = 'input[placeholder="Введите пароль"]'
_MAIL_LOGIN_SUBMIT_NAME = "Авторизоваться"

# Сколько секунд после клика «Авторизоваться» ждём перехода в залогиненный
# UI. Спека требует фиксировать неудачу, если мы всё ещё на форме логина
# через 10 секунд — этот лимит ровно про неё.
_MAIL_LOGIN_POST_SUBMIT_TIMEOUT_S = 90.0
_MAIL_LOGIN_POLL_INTERVAL_S = 0.25

# Селекторы модалки капчи на форме логина. Mail-client стабильно показывает
# Element-UI ``el-message-box`` с заголовком «Введите капчу» и шестью
# цифрами на картинке. Селекторы зафиксированы исследованием в
# ``tests/_explore_mailclient.py`` (см. оригиналы там же — мы держим их
# здесь, чтобы не дублировать литералы и иметь один источник правды).
_MAIL_LOGIN_CAPTCHA_DIALOG_SELECTOR = (
    'div[role="dialog"][aria-label="Введите капчу"]'
)
_MAIL_LOGIN_CAPTCHA_IMG_SELECTOR = "#weiqu_captcha_img_url"
_MAIL_LOGIN_CAPTCHA_REFRESH_SELECTOR = "#weiqu_captcha_img_change"
_MAIL_LOGIN_CAPTCHA_INPUT_SELECTOR = ".el-message-box__input input"
_MAIL_LOGIN_CAPTCHA_INPUT_FALLBACKS = [
    'div[role="dialog"] input[type="text"]',
    '.el-message-box input.el-input__inner',
    'input[placeholder*="капч"]',
]
_MAIL_LOGIN_CAPTCHA_SUBMIT_SELECTOR = (
    '.el-message-box__btns button:has-text("проверять")'
)

# Сколько ждём появления модалки после клика «Авторизоваться»: если за
# это время её нет — значит, в этот раз капчи не будет. 3 секунд хватает
# с запасом, в исследовательском скрипте используется тот же бюджет.
_MAIL_LOGIN_CAPTCHA_DIALOG_TIMEOUT_MS = 3_000

# Таймаут точечных кликов / fill внутри модалки капчи. ``fill`` остаётся
# на 3с (без анимации), а click ``проверять`` мы делаем с более щедрым
# бюджетом, потому что Element-UI ``el-message-box`` рендерится с
# fade-in-анимацией.
_MAIL_LOGIN_CAPTCHA_CLICK_TIMEOUT_MS = 3_000
_MAIL_LOGIN_CAPTCHA_SUBMIT_CLICK_TIMEOUT_MS = 8_000

# Пауза после нажатия «проверять»: даём серверу проверить код и закрыть
# (или перерисовать) модалку, прежде чем мы решим, удался ли вход.
_MAIL_LOGIN_CAPTCHA_POST_SUBMIT_S = 1.5

# Сколько раз пробуем решить капчу подряд. На практике одной-двух попыток
# хватает; верхний лимит в 5 совпадает с Requirement 9.3.
_MAIL_LOGIN_CAPTCHA_MAX_ATTEMPTS = 5

# Сколько раз внутри одной открытой модалки пробуем разный код. Если
# ddddocr ошибся, сервер возвращает «Invalid Captcha» — модалка остаётся
# и перерисовывается с новой картинкой. Перерисовку обрабатываем
# **внутри** ``_solve_mailclient_login_captcha``, не выходя в внешний
# цикл (тот может вообще не дождаться повторной модалки за свои 3с).
_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS = 5

# Длина капчи на mail-client.pinmx.com — 6 цифр (см. tests/_explore_mailclient.py).
_MAIL_LOGIN_CAPTCHA_LENGTH = 6


def _is_post_login_url(url: str) -> bool:
    """Проверка, что mail-client перешёл с формы входа в кабинет.

    На текущей версии сайта rainloop редиректит после успешного логина
    на отдельную страницу ``/mail/index.html`` (см. design.md →
    Investigation). Hash-маршрута после входа нет, но мы для совместимости
    с возможной будущей SPA-версией оставляем и hash-вариант.

    Считаем, что мы «после логина», если выполняется любое из:

    1. URL содержит :data:`MAIL_INBOX_URL_HASH` (``/mail/``) — это
       основной случай, наблюдаемый сегодня.
    2. В URL есть hash-фрагмент с непустым маршрутом, отличным от
       ``/`` и ``login`` — на случай возврата SPA-роутинга в будущем.
    """
    # Основной случай: rainloop редиректит на /mail/index.html.
    if MAIL_INBOX_URL_HASH in url:
        return True
    # Запасной путь — старый hash-маршрут.
    if "#" not in url:
        return False
    fragment = url.rsplit("#", 1)[-1].strip("/")
    return fragment not in ("", "login")


def _solve_mailclient_login_captcha(page: Page) -> bool:
    """Решить модалку капчи на форме логина mail-client (если она появилась).

    На текущей версии ``mail-client.pinmx.com`` после клика
    «Авторизоваться» сервер обычно показывает Element-UI ``el-message-box``
    с шестизначной цифровой капчей. Без её решения логин не проходит —
    форма просто остаётся на экране, и внешний поллинг падает по таймауту
    «still on login page after 10s».

    Алгоритм:

    1. Ждём до :data:`_MAIL_LOGIN_CAPTCHA_DIALOG_TIMEOUT_MS` мс появления
       селектора :data:`_MAIL_LOGIN_CAPTCHA_DIALOG_SELECTOR`. Если его
       нет — капча в этой попытке не нужна, возвращаем ``False``.
    2. **Внутри** одной модалки делаем до
       :data:`_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS` попыток ввода кода:
       ddddocr иногда даёт неверные 6 цифр, и сервер отвечает
       «Invalid Captcha» — модалка остаётся на экране и просто
       перерисовывается с новой картинкой. В таком случае повторяем
       цикл solve→fill→submit без выхода наверх.
    3. Если после submit модалка пропала (``aria-hidden="true"`` /
       элемент удалён) — считаем, что код принят, возвращаем ``True``.
    4. Если все
       :data:`_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS` попыток исчерпаны и
       модалка всё ещё на экране — отдаём управление внешнему циклу
       через :class:`StepError`.

    Returns:
        ``True``, если модалка появилась и была закрыта (значит,
        введённый код принят сервером).

        ``False``, если модалка вообще не появилась за отведённое время.

    Raises:
        StepError: если модалка появилась, но мы не смогли её закрыть
            за :data:`_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS` попыток.
    """
    from logging_utils import setup_logging
    logger = setup_logging()

    try:
        page.wait_for_selector(
            _MAIL_LOGIN_CAPTCHA_DIALOG_SELECTOR,
            timeout=_MAIL_LOGIN_CAPTCHA_DIALOG_TIMEOUT_MS,
        )
        logger.debug("[captcha] модалка капчи найдена")
    except PWTimeout:
        logger.debug("[captcha] модалка не появилась за 3s")
        return False
    except Exception:
        logger.debug("[captcha] ошибка при ожидании модалки")
        return False

    def _refresh() -> None:
        try:
            page.locator(_MAIL_LOGIN_CAPTCHA_REFRESH_SELECTOR).click(
                timeout=_MAIL_LOGIN_CAPTCHA_CLICK_TIMEOUT_MS
            )
        except Exception:
            pass

    for inner in range(_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS):
        # Если модалки больше нет — кто-то уже её закрыл (например,
        # предыдущая успешная попытка), значит мы внутри loop-а
        # отработали и можно выходить.
        if not _captcha_dialog_visible(page):
            logger.debug("[captcha] модалка закрылась")
            return True

        # Небольшая пауза перед повторной попыткой (после неудачного кода
        # модалка перерисовывается и поле может быть временно недоступно)
        if inner > 0:
            time.sleep(0.5)
            logger.debug(f"[captcha] попытка {inner + 1}/{_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS}")

        code = solve_digit_captcha(
            page,
            _MAIL_LOGIN_CAPTCHA_IMG_SELECTOR,
            _refresh,
            expected_len=_MAIL_LOGIN_CAPTCHA_LENGTH,
        )
        if code is None:
            logger.warning("[captcha] ddddocr не смог распознать код")
            # Картинка перестала отдавать `src` (модалка ушла) или
            # ddddocr так и не дал 6 цифр за CAPTCHA_REFRESHES попыток.
            # Если модалка ушла — значит, она закрылась сама, и логин
            # либо прошёл, либо нет — пусть решает внешний цикл.
            if not _captcha_dialog_visible(page):
                return True
            # Модалка ещё на экране, но картинка не отдаётся / OCR
            # не справляется — пробуем дальше во внешнем цикле.
            continue

        logger.debug(f"[captcha] ddddocr распознал код: {code}")

        # Найти поле ввода с fallback-селекторами
        input_locator = None
        selectors_to_try = [_MAIL_LOGIN_CAPTCHA_INPUT_SELECTOR] + _MAIL_LOGIN_CAPTCHA_INPUT_FALLBACKS

        for selector in selectors_to_try:
            try:
                loc = page.locator(selector).first
                # Проверяем не только count, но и видимость
                if loc.count() > 0:
                    # Даём время на отрисовку после перерисовки модалки
                    loc.wait_for(state="visible", timeout=5_000)
                    input_locator = loc
                    if selector == _MAIL_LOGIN_CAPTCHA_INPUT_SELECTOR:
                        logger.debug("[captcha] поле ввода найдено основным селектором")
                    else:
                        logger.debug(f"[captcha] поле ввода найдено fallback: {selector}")
                    break
            except Exception as e:
                logger.debug(f"[captcha] селектор {selector} не сработал: {e}")
                continue

        if input_locator is None:
            logger.error("[captcha] ни один селектор не нашёл видимое поле ввода")
            raise StepError("login failed: captcha input field not found or not visible")

        try:
            # Кликнуть на поле для установки фокуса
            try:
                input_locator.click(timeout=2_000)
                logger.debug("[captcha] кликнул на поле для фокуса")
            except Exception as e:
                logger.debug(f"[captcha] клик на поле не удался (не критично): {e}")

            # Ввести код
            logger.debug(f"[captcha] пытаюсь ввести код {code}")
            input_locator.fill(code, timeout=5_000)
            logger.debug(f"[captcha] код {code} введён")

            # Нажать кнопку "проверять"
            page.locator(_MAIL_LOGIN_CAPTCHA_SUBMIT_SELECTOR).click(
                timeout=_MAIL_LOGIN_CAPTCHA_SUBMIT_CLICK_TIMEOUT_MS
            )
            logger.debug("[captcha] кнопка 'проверять' нажата")
        except PWTimeout as exc:
            logger.error(f"[captcha] таймаут при вводе/отправке: {exc}")
            raise StepError(f"login failed: cannot submit captcha: {exc}") from exc
        except Exception as exc:
            logger.error(f"[captcha] ошибка при вводе/отправке: {exc}")
            raise StepError(f"login failed: cannot submit captcha: {exc}") from exc

        # Дадим серверу проверить код. Если код принят — Element-UI
        # закроет модалку (анимация ~300мс). Если не принят — обычно
        # модалка не закрывается, но картинка перерисовывается.
        time.sleep(_MAIL_LOGIN_CAPTCHA_POST_SUBMIT_S)

        if not _captcha_dialog_visible(page):
            logger.info(f"[captcha] SUCCESS: код {code} принят")
            return True
        # Модалка ещё на экране — значит код был неверный. Идём на
        # следующий inner-attempt с новой картинкой.
        logger.warning(f"[captcha] код {code} отклонён, попытка {inner + 1}/{_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS}")

    # Все inner-попытки исчерпаны, модалка всё ещё на экране.
    logger.error(f"[captcha] исчерпаны все {_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS} попытки")
    raise StepError(
        "login failed: captcha unsolvable after "
        f"{_MAIL_LOGIN_CAPTCHA_INNER_ATTEMPTS} inner attempts"
    )


def _captcha_dialog_visible(page: Page) -> bool:
    """Проверить, отрисована ли ещё модалка капчи на странице.

    Возвращает ``True``, если хотя бы один элемент по селектору
    :data:`_MAIL_LOGIN_CAPTCHA_DIALOG_SELECTOR` найден и видим. Любая
    транзиентная ошибка Playwright (страница в процессе навигации) —
    «не видно».
    """
    try:
        loc = page.locator(_MAIL_LOGIN_CAPTCHA_DIALOG_SELECTOR).first
        return bool(loc.is_visible())
    except Exception:
        return False


def login_to_mailclient(
    page: Page,
    account: "Account",
    *,
    timeout: int = 30_000,
) -> None:
    """Войти в ``mail-client.pinmx.com`` под учётной записью ``account``.

    Шаги (см. Requirements 3.1–3.6):

    1. Открыть ``MAIL_LOGIN_URL`` (``wait_until="domcontentloaded"`` —
       SPA отрисует форму как только подгрузится JS).
    2. Дождаться появления поля ввода email.
    3. Заполнить email и пароль.
    4. Кликнуть кнопку «Авторизоваться».
    5. Подождать до 10 секунд, пока URL не переключится с формы входа
       на залогиненный маршрут (``#/inbox`` и т.п.) **либо** пока на
       странице не появится текст «Входящие» — индикатор того, что
       сайдбар уже отрисован. Любого из условий достаточно.

    Args:
        page: страница Playwright (sync API), на которой выполняется вход.
        account: учётная запись (``email`` / ``password``).
        timeout: таймаут ожидания формы (мс) на шаге 2. По умолчанию 30 000.

    Raises:
        StepError: если страница не загрузилась, форма не появилась за
            ``timeout`` мс, либо после клика «Авторизоваться» страница
            всё ещё на форме логина по истечении 10 секунд.
    """
    # --- Шаг 1: открыть страницу логина -------------------------------------
    try:
        page.goto(MAIL_LOGIN_URL, wait_until="domcontentloaded")
    except Exception as exc:  # pragma: no cover - сетевые ошибки
        # Любая ошибка перехода (DNS, TLS, отказ браузера) — это
        # неустранимая для текущего аккаунта проблема.
        raise StepError(f"login failed: cannot open {MAIL_LOGIN_URL}: {exc}") from exc

    # --- Шаг 2: дождаться формы ---------------------------------------------
    try:
        page.wait_for_selector(_MAIL_LOGIN_EMAIL_SELECTOR, timeout=timeout)
    except PWTimeout as exc:
        raise StepError("login failed: email field not visible") from exc

    # --- Шаги 3–4: заполнить форму и отправить ------------------------------
    try:
        page.locator(_MAIL_LOGIN_EMAIL_SELECTOR).fill(account.email)
        page.locator(_MAIL_LOGIN_PASSWORD_SELECTOR).fill(account.password)
        page.get_by_role("button", name=_MAIL_LOGIN_SUBMIT_NAME).click()
    except PWTimeout as exc:
        raise StepError(f"login failed: cannot submit form: {exc}") from exc

    # --- Шаг 4.5: решить модалку капчи, если появилась ----------------------
    # mail-client.pinmx.com стабильно показывает 6-значную цифровую капчу
    # сразу после клика «Авторизоваться». Без её решения форма просто
    # остаётся на экране и пост-логин-поллинг падает по таймауту.
    # На случай, если ddddocr ошибётся в первом распознавании, делаем до
    # ``_MAIL_LOGIN_CAPTCHA_MAX_ATTEMPTS`` попыток подряд: между ними
    # сервер сам перерисовывает модалку с новой картинкой.
    captcha_attempts = 0
    while captcha_attempts < _MAIL_LOGIN_CAPTCHA_MAX_ATTEMPTS:
        try:
            had_captcha = _solve_mailclient_login_captcha(page)
        except StepError as exc:
            # ``_solve_mailclient_login_captcha`` уже подготовил человеко-
            # читаемое сообщение про неустранимую капчу — пробрасываем как
            # есть, оборачивать ещё раз не нужно.
            raise exc

        if not had_captcha:
            # Модалки нет — либо вход прошёл сразу, либо она никогда и не
            # появится для этого аккаунта. Идём дальше к пост-логин-поллингу.
            break

        # Если капча была введена и URL уже сменился — повторно крутить
        # цикл нет смысла.
        if _is_post_login_url(page.url):
            break

        captcha_attempts += 1
    else:
        raise StepError(
            "login failed: captcha unsolvable after "
            f"{_MAIL_LOGIN_CAPTCHA_MAX_ATTEMPTS} attempts"
        )

    # --- Шаг 5: дождаться смены маршрута либо появления списка писем --------
    # Поллим до 90 секунд: либо URL ушёл с формы входа, либо в DOM появился
    # видимый текст «Входящие» / «Inbox» (название стандартной папки —
    # rainloop переключает локаль по языку браузера). Любое из этих
    # условий — достаточный признак успешного входа.
    from logging_utils import setup_logging
    logger = setup_logging()

    deadline = time.monotonic() + _MAIL_LOGIN_POST_SUBMIT_TIMEOUT_S
    post_login_start = time.monotonic()
    check_iteration = 0

    while time.monotonic() < deadline:
        check_iteration += 1
        if _is_post_login_url(page.url):
            elapsed = time.monotonic() - post_login_start
            logger.info(f"[mail-login] SUCCESS: пост-логин URL достигнут за {elapsed:.1f}s")
            return
        for marker_text in ("Входящие", "Inbox"):
            try:
                marker = page.locator(f"text={marker_text}").first
                if marker.is_visible():
                    elapsed = time.monotonic() - post_login_start
                    logger.info(f"[mail-login] SUCCESS: маркер '{marker_text}' найден за {elapsed:.1f}s")
                    return
            except PWTimeout:
                # ``is_visible`` сам по себе не должен бросать PWTimeout, но
                # защитимся от внутренних ожиданий локатора.
                pass
            except Exception:
                pass

        # Логировать каждые 10 секунд
        if check_iteration % 50 == 1:  # 50 * 0.2s = 10s
            elapsed = time.monotonic() - post_login_start
            remaining = deadline - time.monotonic()
            logger.debug(f"[mail-login] ожидание {elapsed:.1f}s, осталось {remaining:.1f}s, URL: {page.url[:60]}")

        time.sleep(_MAIL_LOGIN_POLL_INTERVAL_S)

    # 90 секунд истекли, мы всё ещё на форме входа.
    elapsed = time.monotonic() - post_login_start
    logger.error(f"[mail-login] TIMEOUT: всё ещё на login page после {elapsed:.1f}s, URL: {page.url}")
    raise StepError(f"login failed: still on login page after {int(_MAIL_LOGIN_POST_SUBMIT_TIMEOUT_S)}s")


# ---------------------------------------------------------------------------
# Sign up на app.devin.ai
# ---------------------------------------------------------------------------

# Имена ролей/элементов на странице регистрации Devin. Берём именно так, как
# они зафиксированы при исследовании MCP-снэпшотом (см. design.md →
# Investigation → ``app.devin.ai/auth/signup``): локали у Devin нет, всё
# по-английски. Если интерфейс однажды изменится, эти константы — единые
# точки правки.
_DEVIN_EMAIL_ROLE = "textbox"
_DEVIN_EMAIL_NAME = "Email address"
_DEVIN_SIGNUP_BUTTON_NAME = "Sign up"
_DEVIN_SUCCESS_HEADING_NAME = "Verify your identity"
_DEVIN_SAML_HEADING_NAME = "Choose your login method"

# Пауза между опросами «появилось ли уже что-то из success / SAML / toast».
# 200 мс — компромисс: не нагружаем браузер опросами и при этом реагируем
# почти сразу же, как только Devin отрисует ответ сервера.
_DEVIN_SIGNUP_POLL_INTERVAL_S = 0.2


def _devin_locator_visible(locator) -> bool:
    """Безопасно проверить ``is_visible()`` у локатора Devin.

    ``is_visible()`` сам по себе по контракту не должен бросать
    ``PWTimeout`` (в отличие от ``wait_for``), но Playwright иногда
    возвращает разные ошибки на гонках с навигацией / закрытием страницы.
    Любая такая ошибка для нас — «пока не видно», а не падение всего шага.
    """
    try:
        return bool(locator.is_visible())
    except Exception:
        return False


def _read_devin_alert_text(devin_page: Page) -> str:
    """Попытаться прочитать текст видимого toast/алерта на странице Devin.

    Returns:
        Непустую строку с текстом первого видимого алерта/toast,
        либо пустую строку, если ни один из источников не виден.

    Источники по приоритету (см. design.md → шаг 5/7 для signup):

    1. ``[role="region"][aria-label*="Notification" i]`` — описанный в
       дизайне регион ``Notifications``, в который Devin кладёт toast-ы.
    2. ``role=alert`` — типовая роль для одиночных всплывающих ошибок
       (как inline-баннер).
    """
    for locator in (
        devin_page.locator('[role="region"][aria-label*="Notification" i]').first,
        devin_page.get_by_role("alert").first,
    ):
        try:
            if not locator.is_visible():
                continue
            text = locator.inner_text(timeout=1_000).strip()
        except Exception:
            continue
        if text:
            return text
    return ""


def start_devin_signup(
    devin_page: Page,
    email: str,
    *,
    timeout: int = 30_000,
) -> None:
    """Открыть Devin signup, ввести email и дождаться исхода Sign up.

    Шаги (см. Requirements 4.1–4.3 и 5.1–5.6, design.md →
    ``start_devin_signup``):

    1. ``devin_page.goto(DEVIN_SIGNUP_URL, wait_until="domcontentloaded")``.
    2. Дождаться поля ``getByRole("textbox", name="Email address")``.
    3. Заполнить поле email-ом.
    4. Кликнуть ``getByRole("button", name="Sign up", exact=True)``.
    5. Поллить страницу, пока не появится ОДНО из:
       - заголовок ``Verify your identity`` — успех, выходим;
       - заголовок ``Choose your login method`` — SAML-маршрут, бросаем
         :class:`StepError` (это значит, email не на ``@pingmx.com``,
         см. Investigation в design.md);
       - видимый toast/алерт (region ``Notifications`` или
         ``role="alert"``) — Devin сообщил об ошибке (например,
         «email уже зарегистрирован»), бросаем :class:`StepError`
         с текстом из toast-а.
    6. Если за ``timeout`` мс ни одно из условий не выполнилось — бросаем
       :class:`StepError` с понятной формулировкой про неоднозначный исход.

    Args:
        devin_page: страница Playwright (sync API), на которой выполняется
            регистрация. Обычно — отдельная вкладка того же контекста,
            что и mail-tab.
        email: адрес для регистрации (ожидается ``@pingmx.com``; если
            домен другой — Devin показывает SAML, и мы это явно
            детектируем как ошибку шага).
        timeout: таймаут в миллисекундах на:
            - ожидание появления поля email после ``goto``;
            - общее ожидание исхода Sign up (success / SAML / toast).
            По умолчанию 30 000 мс — соответствует Requirement 4.3 и 5.4.

    Raises:
        StepError: если страница не открылась, поле email не появилось,
            появился SAML-блок («Choose your login method»), Devin показал
            toast/алерт об ошибке, либо за ``timeout`` мс не появилось
            ни одного из ожидаемых исходов.
    """
    # --- Шаг 1: открыть страницу регистрации --------------------------------
    try:
        devin_page.goto(DEVIN_SIGNUP_URL, wait_until="domcontentloaded")
    except Exception as exc:  # pragma: no cover - сетевые ошибки
        raise StepError(
            f"signup failed: cannot open {DEVIN_SIGNUP_URL}: {exc}"
        ) from exc

    # --- Шаг 2: дождаться поля email ----------------------------------------
    email_field = devin_page.get_by_role(_DEVIN_EMAIL_ROLE, name=_DEVIN_EMAIL_NAME)
    try:
        email_field.wait_for(state="visible", timeout=timeout)
    except PWTimeout as exc:
        raise StepError("signup failed: email field not visible") from exc

    # --- Шаги 3–4: заполнить и отправить ------------------------------------
    try:
        email_field.fill(email)
        devin_page.get_by_role(
            "button", name=_DEVIN_SIGNUP_BUTTON_NAME, exact=True
        ).click()
    except PWTimeout as exc:
        raise StepError(f"signup failed: cannot submit form: {exc}") from exc
    except Exception as exc:
        raise StepError(f"signup failed: cannot submit form: {exc}") from exc

    # --- Шаг 5: дождаться одного из исходов ---------------------------------
    success_heading = devin_page.get_by_role(
        "heading", name=_DEVIN_SUCCESS_HEADING_NAME
    )
    saml_heading = devin_page.get_by_role(
        "heading", name=_DEVIN_SAML_HEADING_NAME
    )

    deadline = time.monotonic() + timeout / 1000.0
    while time.monotonic() < deadline:
        # Успех имеет приоритет: если параллельно Devin успел показать
        # и заголовок «Verify your identity», и какой-нибудь
        # информационный toast — мы трактуем это как успех.
        if _devin_locator_visible(success_heading):
            return

        if _devin_locator_visible(saml_heading):
            raise StepError("non-pingmx or signup error: SAML route")

        toast_text = _read_devin_alert_text(devin_page)
        if toast_text:
            raise StepError(f"non-pingmx or signup error: {toast_text}")

        time.sleep(_DEVIN_SIGNUP_POLL_INTERVAL_S)

    # Ни одно из условий не выполнилось за timeout мс. Не считаем это
    # «успехом по таймауту»: нам нужен явный сигнал от Devin.
    raise StepError("signup outcome unclear (timeout)")


# ---------------------------------------------------------------------------
# Ожидание письма от Devin и извлечение кода подтверждения
# ---------------------------------------------------------------------------

# Ключевые слова, по которым мы качественно отбираем «то самое» письмо в
# списке inbox. ``inner_text`` строки списка в rainloop включает имя
# отправителя (``Devin``), его адрес (``no-reply@cognition.ai``) и тему
# письма, поэтому достаточно лёгкого regex-в-строку через ``in``. Все
# проверки выполняются над lower-case-текстом, поэтому ключи тоже
# в нижнем регистре.
_DEVIN_EMAIL_KEYWORDS: tuple[str, ...] = (
    "devin",
    "cognition",
    "verification code",
    "verify your",
    "your code",
)

# Короткий таймаут на отдельные UI-операции внутри одного цикла поллинга:
# клик по кнопке «обновить», клик по строке списка, чтение тела письма.
# Если оно не уложилось — это не повод падать, мы просто дождёмся
# следующей итерации внешнего цикла.
_MAIL_CLICK_TIMEOUT_MS = 2_000
_MAIL_LIST_WAIT_TIMEOUT_MS = 5_000
_MAIL_BODY_READ_TIMEOUT_MS = 3_000


def wait_for_devin_email_code(
    mail_page: Page,
    target_email: str,
    *,
    total_timeout_ms: int = 300_000,
    poll_ms: int = 3_000,  # Уменьшено с 5s до 3s для быстрого обнаружения
) -> str:
    """Дождаться письма от Devin в открытой вкладке mail-client и вернуть код.

    Шаги одной итерации (см. Requirements 6.1–6.4 и 6.6):

    1. Пытаемся нажать «обновить список писем» по
       :data:`MAIL_REFRESH_BUTTON_SELECTOR`. Если кнопка не найдена /
       клик упал — делаем ``mail_page.reload(wait_until="domcontentloaded")``
       как фолбэк (Requirement 6.2).
    2. Коротко (``5s``) ждём появления хотя бы одного элемента списка по
       :data:`MAIL_LIST_ITEM_SELECTOR`; ``PWTimeout`` глотаем — может быть,
       список ещё не подгрузился, попробуем на следующем круге.
    3. Перебираем все элементы списка и берём ``inner_text().lower()``;
       строка квалифицируется, если содержит любое из ключевых слов
       (``devin``, ``cognition``, ``verification code``, ``verify your``,
       ``your code``) — этого хватает, чтобы отличить письмо Devin от
       прочей почты, потому что rainloop кладёт в текст строки и имя
       отправителя ``Devin``, и адрес ``no-reply@cognition.ai``, и тему
       (Requirement 6.3).
    4. Кликаем по первой подходящей строке и читаем тело письма.
       Сначала — :data:`MAIL_DETAIL_BODY_SELECTOR`; если оно пустое или
       упало по таймауту — пробуем по очереди селекторы из
       :data:`MAIL_DETAIL_BODY_FALLBACK_SELECTORS`. Для
       ``.messageView iframe`` используем ``frame_locator(sel).first``
       и читаем ``body`` внутри фрейма (rainloop иногда рендерит HTML-
       письма в iframe).
    5. Конкатенируем ``inner_text`` строки списка и ``inner_text`` тела,
       и прогоняем через :func:`extract_code`. Если код найден —
       возвращаем (Requirement 6.4). Иначе спим ``poll_ms / 1000``
       секунд и идём на следующую итерацию (письмо могло быть ещё не
       догружено).

    Оптимизации:
    - Локаторы Playwright кэшируются перед циклом (экономия ~50-100ms/итерацию)
    - Интервал поллинга уменьшен с 5s до 3s (быстрее обнаружение письма)
    - Проверка наличия элементов через .count() вместо try/except (быстрее)

    Отдельные операции Playwright (клик, чтение текста, ожидание элемента)
    обёрнуты в защитные ``try / except`` (``PWTimeout`` и общий
    ``Exception``), чтобы транзиентная ошибка одного шага не убивала
    цикл — мы просто переходим к следующей итерации поллинга.

    Args:
        mail_page: страница Playwright с уже открытым inbox-ом mail-client.
        target_email: email текущего аккаунта (логирование / диагностика
            могут пригодиться позже; на саму логику отбора письма он сейчас
            не влияет — отбор идёт по ключевым словам в строке списка).
        total_timeout_ms: общий бюджет ожидания в миллисекундах (по
            умолчанию 5 минут, см. Requirement 6.6).
        poll_ms: пауза между итерациями поллинга (по умолчанию 3 секунды,
            уменьшено с 5s для быстрого обнаружения, см. Requirement 6.2).

    Returns:
        Шесть цифр кода подтверждения, извлечённые из тела письма.

    Raises:
        StepError: если за ``total_timeout_ms`` мс письмо с подходящим
            кодом так и не пришло (Requirement 6.6).
    """
    # ``target_email`` пока используется только как аргумент для совместимого
    # с дизайном API. Когда селектор инбокса станет точнее (например, начнёт
    # выделять адрес получателя), здесь можно будет добавить более жёсткую
    # фильтрацию. Сейчас отбор идёт по ключевым словам, описанным в дизайне.
    del target_email

    deadline = time.monotonic() + total_timeout_ms / 1000.0

    # Кэшировать локаторы для переиспользования (экономия ~50-100ms на итерацию)
    refresh_button_loc = mail_page.locator(MAIL_REFRESH_BUTTON_SELECTOR)
    list_items_loc = mail_page.locator(MAIL_LIST_ITEM_SELECTOR)
    detail_body_loc = mail_page.locator(MAIL_DETAIL_BODY_SELECTOR)

    while time.monotonic() < deadline:
        # --- Шаг 1: обновить список писем -----------------------------------
        try:
            refresh_button_loc.click(timeout=_MAIL_CLICK_TIMEOUT_MS)
        except (PWTimeout, Exception):
            # Кнопка не найдена / клик упал — пробуем «жёсткий» reload.
            # Если и он упал — игнорируем: следующая итерация попробует ещё
            # раз, дедлайн нас в любом случае рассудит.
            try:
                mail_page.reload(wait_until="domcontentloaded")
            except Exception:
                pass

        # --- Шаг 2: дождаться появления элементов списка --------------------
        try:
            mail_page.wait_for_selector(
                MAIL_LIST_ITEM_SELECTOR,
                timeout=_MAIL_LIST_WAIT_TIMEOUT_MS,
            )
        except PWTimeout:
            # Список ещё не отрисован / в ящике пока пусто — переждём poll
            # и попробуем снова.
            pass
        except Exception:
            pass

        # --- Шаг 3: перебрать строки списка ---------------------------------
        try:
            items = list_items_loc.all()
        except Exception:
            items = []

        for item in items:
            try:
                row_text = item.inner_text(timeout=_MAIL_CLICK_TIMEOUT_MS)
            except (PWTimeout, Exception):
                continue

            row_lower = row_text.lower()
            if not any(keyword in row_lower for keyword in _DEVIN_EMAIL_KEYWORDS):
                continue

            # --- Шаг 4: открыть письмо и прочитать тело ---------------------
            # Кликаем по строке списка с retry: rainloop иногда
            # перерисовывает строки после refresh, и локатор может на
            # секунду «detach». 3 попытки с короткими паузами почти всегда
            # достаточно. Если все три не пройдут — берём следующее
            # подходящее письмо (но это плохой знак: первое — самое
            # свежее, а старые могут уже не быть валидными).
            click_ok = False
            for _click_attempt in range(3):
                try:
                    item.click(timeout=_MAIL_CLICK_TIMEOUT_MS)
                    click_ok = True
                    break
                except (PWTimeout, Exception):
                    time.sleep(0.4)

            if not click_ok:
                continue

            # --- Шаг 4.5: прочитать тело письма (оптимизировано) ------------
            body_text = ""

            # Проверить наличие основного селектора через .count() (быстрее try/except)
            if detail_body_loc.count() > 0:
                try:
                    body_text = detail_body_loc.first.inner_text(timeout=_MAIL_BODY_READ_TIMEOUT_MS)
                except (PWTimeout, Exception):
                    pass

            # Если основной селектор не сработал, пробуем fallback-ы
            if not body_text:
                for fallback_selector in MAIL_DETAIL_BODY_FALLBACK_SELECTORS:
                    fallback_loc = mail_page.locator(fallback_selector)
                    if fallback_loc.count() > 0:
                        try:
                            if fallback_selector == ".messageView iframe":
                                body_text = (
                                    mail_page.frame_locator(fallback_selector)
                                    .first.locator("body")
                                    .inner_text(timeout=_MAIL_BODY_READ_TIMEOUT_MS)
                                )
                            else:
                                body_text = fallback_loc.first.inner_text(timeout=_MAIL_BODY_READ_TIMEOUT_MS)
                            if body_text:
                                break
                        except (PWTimeout, Exception):
                            pass

            # --- Шаг 5: попытаться извлечь код ------------------------------
            combined = f"{row_text}\n{body_text}"
            code = extract_code(combined)
            if code is not None:
                return code

            # Тело письма могло ещё не догрузиться — ждём poll и крутимся
            # дальше. Прерываем перебор оставшихся строк списка: нет смысла
            # открывать ещё одну, пока эта не догрузилась.
            break

        # --- Пауза между итерациями ----------------------------------------
        time.sleep(poll_ms / 1000.0)

    raise StepError("verification email timeout")


# ---------------------------------------------------------------------------
# Ввод кода подтверждения в Devin
# ---------------------------------------------------------------------------

# Поле ввода 6-значного кода. У Devin к нему привязан атрибут
# ``autocomplete="one-time-code"`` — самый стабильный сигнал, не зависящий
# от текущей вёрстки и текста плейсхолдера. Страница содержит ровно одно
# такое поле, поэтому ``locator(...)`` без ``.first`` корректен.
_DEVIN_CODE_INPUT_SELECTOR = 'input[autocomplete="one-time-code"]'

# Кнопка «Continue» под полем кода. Devin держит её ``disabled``, пока в
# поле меньше шести цифр; как только последний символ введён — атрибут
# снимается. Это поведение мы и используем как сигнал «код принят клиентом
# и можно отправлять».
#
# ВАЖНО: на странице ``/auth/signup`` параллельно с verify-блоком видны
# кнопки ``Continue with GitHub`` / ``Continue with Google`` /
# ``Continue with Windsurf`` (см. MCP-снэпшот в design.md). Поэтому
# нельзя использовать ``:has-text("Continue")`` — он матчит первую же
# кнопку из этого OAuth-блока, и клик по ней проваливается с
# «pointer events intercepted». Берём ``role="button" name="Continue"``
# с ``exact=True`` — это строгое равенство видимого имени, под него
# попадает ровно нужная кнопка.
_DEVIN_CONTINUE_BUTTON_NAME = "Continue"

# Подстроки, по которым мы определяем, что Devin отклонил введённый код.
# Сравнение идёт против ``inner_text("body").lower()``, поэтому
# ключевые слова — в нижнем регистре. Список совпадает со словами,
# зафиксированными в дизайне (см. design.md → submit_devin_code).
_DEVIN_INVALID_CODE_PHRASES: tuple[str, ...] = (
    "invalid",
    "incorrect",
    "expired",
)

# Сколько секунд ждём, пока Continue станет enabled после ввода кода.
# 2 секунды — компромисс из спеки (Requirement 7.2): за это время Devin
# успевает прогнать клиентскую валидацию, и если кнопка не «ожила» — это
# уже сигнал, что ``fill`` не дошёл до React-стейта (см. fallback на
# ``type(delay=50)``).
_DEVIN_CONTINUE_ENABLE_TIMEOUT_S = 2.0
_DEVIN_CODE_POLL_INTERVAL_S = 0.1

# Сколько секунд после клика Continue ждём перехода с ``/auth/...``
# или появления текста ошибки. 30 секунд — Requirement 7.4.
_DEVIN_POST_SUBMIT_TIMEOUT_S = 30.0
_DEVIN_POST_SUBMIT_POLL_INTERVAL_S = 0.5

# Если за это время после ``fill(code)`` URL не сменился И сообщения
# об ошибке нет — в качестве защитного fallback кликнем Continue
# (на случай UI-edge, когда Devin не делает auto-submit). На сегодняшнем
# UI auto-submit срабатывает за <1 сек, поэтому 5 секунд — комфортный
# буфер.
_DEVIN_AUTO_SUBMIT_FALLBACK_DELAY_S = 5.0

# Таймаут чтения текста ``body`` на каждой итерации поллинга. Берём
# короткий, чтобы не «съедать» сразу весь бюджет опроса в случае, если
# страница в процессе навигации и body временно недоступен.
_DEVIN_BODY_READ_TIMEOUT_MS = 1_000

# Таймаут самого клика по Continue. Кнопка к этому моменту уже enabled,
# поэтому 5 секунд с запасом перекрывают любую микро-анимацию.
_DEVIN_CONTINUE_CLICK_TIMEOUT_MS = 5_000


def _wait_for_devin_continue_enabled(continue_btn, timeout_s: float) -> bool:
    """Подождать до ``timeout_s`` секунд, пока кнопка Continue станет enabled.

    Возвращает ``True``, как только ``is_enabled()`` отдал True хотя бы
    один раз; ``False`` — если по истечении таймаута кнопка так и
    осталась disabled. Любые исключения от Playwright (гонка с навигацией,
    локатор не нашёл элемент за тик опроса) трактуются как «пока не
    enabled» и заставляют идти на следующую итерацию опроса.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if continue_btn.first.is_enabled():
                return True
        except Exception:
            pass
        time.sleep(_DEVIN_CODE_POLL_INTERVAL_S)
    return False


def submit_devin_code(
    devin_page: Page,
    code: str,
    *,
    retries: int = 2,
) -> None:
    """Ввести 6-значный код подтверждения в Devin и дождаться перехода.

    Шаги (см. Requirements 7.1–7.5, design.md → ``submit_devin_code``):

    1. Заполнить поле :data:`_DEVIN_CODE_INPUT_SELECTOR` через ``fill(code)``.
    2. Devin **сам** делает auto-submit, когда в поле набирается 6-я
       цифра (POST на ``/api/auth1/email/complete``). Кнопка Continue в
       этой форме — резервная: при удачном auto-submit она нам не нужна,
       и более того, Devin сразу очищает поле после ответа сервера, так
       что Continue остаётся disabled — ждать ``is_enabled()`` не имеет
       смысла. Поэтому сразу после ``fill`` уходим в поллинг шага 3.
    3. Поллим страницу до 30 секунд (Requirement 7.4):

       - если URL ушёл с ``/auth/...`` — успех, выходим;
       - если на странице появился текст «invalid», «incorrect» или
         «expired» — Devin отклонил код, бросаем :class:`InvalidCodeError`
         (Requirement 7.5); внешний цикл в ``process_account`` поймает
         его и попробует получить более свежий код.
       - если за первые ~5 секунд URL не сменился И сообщения об
         ошибке нет (значит Devin почему-то не сделал auto-submit), —
         защитный fallback: пробуем кликнуть Continue
         (она может быть включена в каких-то редких сценариях).
    4. Если за 30 секунд ничего не случилось — :class:`StepError` про
       неоднозначный исход.

    Параметр ``retries`` зарезервирован под внешний цикл повторов
    (``process_account`` пере-фетчит код из почты при
    :class:`InvalidCodeError`); внутри самого ``submit_devin_code`` мы
    делаем одну попытку ``fill``. Параметр оставлен в сигнатуре, чтобы
    сохранить контракт из спеки.

    Args:
        devin_page: страница Playwright (sync API), на которой уже отрисован
            экран ввода кода (после успешного ``start_devin_signup``).
        code: ровно 6 цифр кода подтверждения, как его вернул
            :func:`extract_code`.
        retries: зарезервированный параметр (см. описание выше).

    Raises:
        InvalidCodeError: Devin отклонил код (на странице появилось одно
            из ключевых слов «invalid» / «incorrect» / «expired»).
        StepError: не удалось заполнить поле, либо за 30 секунд после
            ввода не произошло ни перехода с ``/auth/...``, ни появления
            сообщения об ошибке.
    """
    from logging_utils import setup_logging
    logger = setup_logging()

    # ``retries`` используется внешним циклом ``process_account`` для
    # перечитывания кода из почты при ``InvalidCodeError``. Сигнатуру
    # мы храним для совместимости с дизайном.
    del retries

    code_input = devin_page.locator(_DEVIN_CODE_INPUT_SELECTOR)
    continue_btn = devin_page.get_by_role(
        "button", name=_DEVIN_CONTINUE_BUTTON_NAME, exact=True
    )

    # --- Шаг 1: заполнить поле кода через fill ------------------------------
    # ``fill`` для React-controlled input обычно вызывает оба события
    # ``input`` и ``change``, что эквивалентно «настоящему» вводу.
    # Devin отслеживает ввод и при 6 цифрах сам отправляет код на бэкенд.
    logger.debug(f"[submit_devin_code] заполняю код {code}")
    try:
        code_input.fill(code)
    except PWTimeout as exc:
        raise StepError(f"submit code failed: cannot fill input: {exc}") from exc
    except Exception as exc:
        raise StepError(f"submit code failed: cannot fill input: {exc}") from exc

    # --- Шаг 2/3: дождаться перехода с /auth/... или сообщения об ошибке ---
    deadline = time.monotonic() + _DEVIN_POST_SUBMIT_TIMEOUT_S
    fallback_continue_clicked = False
    fallback_deadline = (
        time.monotonic() + _DEVIN_AUTO_SUBMIT_FALLBACK_DELAY_S
    )

    last_log_time = time.monotonic()
    iterations = 0

    while time.monotonic() < deadline:
        iterations += 1
        elapsed = time.monotonic() - (deadline - _DEVIN_POST_SUBMIT_TIMEOUT_S)

        # Логировать каждые 5 секунд
        if time.monotonic() - last_log_time >= 5.0:
            logger.debug(f"[submit_devin_code] ожидание редиректа: {elapsed:.1f}s / {_DEVIN_POST_SUBMIT_TIMEOUT_S}s")
            last_log_time = time.monotonic()

        # Если URL ушёл с маршрутов авторизации — Devin принял код и
        # роутер увёл нас в основной кабинет.
        # Devin редиректит на https://app.devin.ai/org/{nickname} после успешной регистрации.
        try:
            url = devin_page.url
        except Exception:
            url = ""
        if url and "/auth/" not in url:
            # Дополнительная проверка: убедиться, что мы на /org/ или dashboard
            if "/org/" in url or "/dashboard" in url or url.endswith("devin.ai/"):
                logger.info(f"[submit_devin_code] SUCCESS: редирект на {url} за {elapsed:.1f}s")
                return
            else:
                # Неожиданный URL — логируем, но считаем успехом (может быть новый UI)
                logger.warning(f"[submit_devin_code] неожиданный URL после /auth/: {url}")
                return

        # Иначе ищем явное сообщение об ошибке. ``inner_text("body")``
        # отдаёт нам весь видимый текст страницы; если по какой-то
        # причине body временно недоступен (страница в процессе
        # навигации) — глотаем ошибку и идём на следующую итерацию.
        try:
            body_text = (
                devin_page.locator("body")
                .inner_text(timeout=_DEVIN_BODY_READ_TIMEOUT_MS)
                .lower()
            )
        except PWTimeout:
            body_text = ""
        except Exception:
            body_text = ""

        for phrase in _DEVIN_INVALID_CODE_PHRASES:
            if phrase in body_text:
                logger.warning(f"[submit_devin_code] код отклонён: '{phrase}' на странице")
                raise InvalidCodeError(
                    f"Devin rejected code: phrase '{phrase}' on page"
                )

        # Защитный fallback: если auto-submit не случился за ~5 секунд,
        # пробуем кликнуть Continue, если она вдруг доступна (UI-edge).
        # Делаем это ровно один раз — и не падаем, если кнопка disabled.
        if (
            not fallback_continue_clicked
            and time.monotonic() >= fallback_deadline
        ):
            fallback_continue_clicked = True
            logger.debug("[submit_devin_code] пробую fallback: клик на Continue")
            try:
                if continue_btn.first.is_enabled():
                    continue_btn.first.click(
                        timeout=_DEVIN_CONTINUE_CLICK_TIMEOUT_MS
                    )
                    logger.debug("[submit_devin_code] Continue кликнут")
            except Exception:
                # Кнопка не enabled или не отрисована — это норма для
                # auto-submit-сценария, идём дальше по поллингу.
                pass

        time.sleep(_DEVIN_POST_SUBMIT_POLL_INTERVAL_S)

    # 30 секунд истекли, мы всё ещё на ``/auth/...`` и явной ошибки нет.
    # Это не ``InvalidCodeError`` (внешний ретрай тут не поможет) — это
    # неустранимая для текущего аккаунта неоднозначность.
    logger.error(f"[submit_devin_code] таймаут: всё ещё на /auth/* после {_DEVIN_POST_SUBMIT_TIMEOUT_S}s, URL={devin_page.url}")
    raise StepError("submit code failed: still on /auth/* after 30s")


# ---------------------------------------------------------------------------
# Оркестрация одного аккаунта: process_account
# ---------------------------------------------------------------------------

# Экспоненциальные паузы между ретраями кода подтверждения
# (Requirement 9.1). Длина списка задаёт число попыток; сама пауза
# применяется ПОСЛЕ неудачной попытки и ПЕРЕД следующей. На последней
# попытке пауза уже не нужна — мы либо успешно вышли, либо бросили
# ``StepError("verification failed after 3 attempts")``.
_PROCESS_ACCOUNT_RETRY_BACKOFFS_S: tuple[float, ...] = (1.0, 2.0, 4.0)


def process_account(
    context: BrowserContext,
    account: Account,
    *,
    mail_tab: Page,
    devin_tab: Page,
) -> None:
    """Полный пайплайн регистрации одного аккаунта (Requirements 8.4, 8.5, 9.1, 9.4).

    Композиция шагов с одним внешним ретраем по
    :class:`InvalidCodeError`. Любая другая ошибка шага
    (:class:`StepError`) считается неустранимой для текущего аккаунта и
    пробрасывается наружу — внешний main-цикл запишет её в
    ``devin_errors.txt`` (Requirement 8.2) и продолжит со следующего
    аккаунта (Requirement 9.4).

    Алгоритм:

    1. :func:`login_to_mailclient` на ``mail_tab``.
    2. :func:`start_devin_signup` на ``devin_tab``.
    3. До 3 попыток подряд получить и подтвердить код (Requirement 9.1):

       - получаем код через :func:`wait_for_devin_email_code`;
       - отправляем его через :func:`submit_devin_code`;
       - при :class:`InvalidCodeError` — ждём
         :data:`_PROCESS_ACCOUNT_RETRY_BACKOFFS_S` секунд (1с, 2с, 4с)
         и идём на следующую итерацию: новое письмо могло прийти позже,
         попробуем достать свежий код;
       - при :class:`StepError` от любой подфункции — пробрасываем без
         попыток ретрая (это уже не «неверный код», а инфраструктурная
         ошибка).
    4. После того как :func:`submit_devin_code` отработал без исключений —
       аккаунт зарегистрирован, возвращаемся.

    Если все 3 попытки кода исчерпаны — поднимаем :class:`StepError`
    с пометкой ``verification failed after 3 attempts`` и оригинальным
    текстом последней :class:`InvalidCodeError` в ``__cause__``.

    Args:
        context: общий ``BrowserContext`` Playwright. Для каждого аккаунта
            создаётся новый context в main() для полной изоляции сессий.
        account: учётка из ``results.txt``.
        mail_tab: вкладка для mail-client.
        devin_tab: вкладка для app.devin.ai.

    Raises:
        StepError: любой неустранимый сбой шага логина / signup / получения
            письма / подтверждения, либо исчерпаны все 3 попытки кода.
    """
    # --- Шаг 1: логин в почту -----------------------------------------------
    login_to_mailclient(mail_tab, account)

    # --- Шаг 2: открыть Devin signup и отправить email ----------------------
    start_devin_signup(devin_tab, account.email)

    # --- Шаг 3: цикл до 3 попыток с экспоненциальной паузой -----------------
    last_invalid: InvalidCodeError | None = None
    total_attempts = len(_PROCESS_ACCOUNT_RETRY_BACKOFFS_S)
    for attempt, backoff in enumerate(
        _PROCESS_ACCOUNT_RETRY_BACKOFFS_S, start=1
    ):
        try:
            code = wait_for_devin_email_code(mail_tab, account.email)
            submit_devin_code(devin_tab, code)
        except InvalidCodeError as exc:
            last_invalid = exc
            if attempt < total_attempts:
                # Письмо со свежим кодом могло прийти позже — подождём
                # экспоненциальную паузу и попробуем ещё раз.
                time.sleep(backoff)
                continue
            # Попытки исчерпаны — это уже не транзиентная ошибка кода,
            # а неустранимый отказ верификации для аккаунта.
            raise StepError(
                f"verification failed after {total_attempts} attempts: {exc}"
            ) from exc
        # Любой другой ``StepError`` от шагов внутри блока ``try`` улетает
        # наружу без обработки (нам это и нужно — это инфраструктурная
        # ошибка, ретрай по InvalidCodeError её не лечит).
        # --- Шаг 5: успех -------------------------------------------------
        return

    # До этой точки мы доходим только если цикл выше как-то завершился без
    # ``return`` и без ``raise`` (теоретически — если кортеж пауз пуст).
    # Это страховка, чтобы в любом исходе функция возвращалась через
    # явный путь.
    raise StepError(
        f"verification failed after {total_attempts} attempts: {last_invalid}"
    )


# ---------------------------------------------------------------------------
# Worker function for parallel processing
# ---------------------------------------------------------------------------


def process_account_worker(
    account: Account,
    worker_id: int,
    headless: bool,
    db_path: Path,
) -> tuple[str, bool, str | None]:
    """Обработать один аккаунт в отдельном воркере (для ThreadPoolExecutor).

    Каждый воркер запускает свой браузер и BrowserContext для полной изоляции.
    Результат записывается в БД через thread-safe AccountDB.

    Args:
        account: Account для обработки
        worker_id: ID воркера (для логирования)
        headless: Режим браузера (True = headless)
        db_path: Путь к БД SQLite

    Returns:
        (email, success, error_message) - результат обработки
    """
    # Создать логгер с префиксом воркера
    logger = logging.getLogger(f"register_devin.worker{worker_id}")
    logger.info(f"[Worker-{worker_id}] Начало обработки {account.email}")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=headless)
            try:
                context = browser.new_context()
                mail_tab = context.new_page()
                devin_tab = context.new_page()

                try:
                    # Вызвать основную функцию обработки
                    process_account(
                        context,
                        account,
                        mail_tab=mail_tab,
                        devin_tab=devin_tab,
                    )
                    logger.info(f"[Worker-{worker_id}] {account.email} - УСПЕХ")
                    return (account.email, True, None)

                except StepError as exc:
                    error_msg = str(exc)
                    logger.error(f"[Worker-{worker_id}] {account.email} - ОШИБКА: {error_msg}")
                    return (account.email, False, error_msg)

                finally:
                    try:
                        mail_tab.close()
                    except Exception:
                        pass
                    try:
                        devin_tab.close()
                    except Exception:
                        pass
                    try:
                        context.close()
                    except Exception:
                        pass

            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    except Exception as exc:
        error_msg = f"Worker exception: {exc}"
        logger.exception(f"[Worker-{worker_id}] {account.email} - КРИТИЧЕСКАЯ ОШИБКА")
        return (account.email, False, error_msg)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# Пауза по умолчанию между аккаунтами в секундах. Маленькая по меркам
# антифрод-эвристик, но и не нулевая — чтобы давать серверам Devin /
# mail-client передохнуть между регистрациями (Requirement 10.4).
_DEFAULT_DELAY_S = 2.0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Распарсить флаги CLI (Requirement 10.1).

    Поддерживаемые флаги:

    - ``--head`` — показать окно браузера (по умолчанию headless);
    - ``--limit N`` — обработать только первые ``N`` аккаунтов из очереди;
    - ``--no-skip-done`` — не пропускать email-ы из ``devin_done.txt``;
    - ``--delay SEC`` — пауза между аккаунтами (по умолчанию
      :data:`_DEFAULT_DELAY_S`);
    - ``--help`` / ``-h`` — добавляется ``argparse`` автоматически.
    """
    parser = argparse.ArgumentParser(
        prog="register_devin",
        description=(
            "Автоматическая регистрация аккаунтов на app.devin.ai по "
            "email-адресам из results.txt."
        ),
    )
    parser.add_argument(
        "--head",
        action="store_true",
        help="показать окно браузера (по умолчанию headless)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="обработать только первые N аккаунтов из очереди",
    )
    parser.add_argument(
        "--no-skip-done",
        action="store_true",
        help="не пропускать email-ы из 'аккаунты devin.txt'",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=_DEFAULT_DELAY_S,
        metavar="SEC",
        help=(
            "пауза в секундах между аккаунтами "
            f"(по умолчанию {_DEFAULT_DELAY_S})"
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="количество параллельных воркеров (по умолчанию 1 - последовательно)",
    )
    parser.add_argument(
        "--worker-delay",
        type=float,
        default=5.0,
        metavar="SEC",
        help="задержка между запуском воркеров в секундах (rate limiting, по умолчанию 5.0)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="включить детальное логирование и сохранение скриншотов при ошибках",
    )
    add_browser_mode_arg(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI (Validates: Requirements 10.1–10.6).

    Поведение:

    1. Загрузить аккаунты из БД. Если список пуст — напечатать сообщение
       в stderr и вернуть ``1`` (Requirement 10.2).
    2. Загрузить уже обработанные (если ``--no-skip-done`` не задан)
       и отфильтровать. Применить ``--limit`` (Requirement 10.3).
    3. Поднять Playwright с ``channel="chrome"`` и
       ``headless = not args.head``, создать один ``BrowserContext`` и
       две вкладки — для mail-client и Devin (Requirement 10.4 / 8.4).
    4. Для каждого аккаунта:

       - вызвать :func:`process_account`;
       - при успехе — записать в БД;
       - при :class:`StepError` — записать ошибку в БД и продолжить;
       - при ``KeyboardInterrupt`` — корректно прервать цикл, прогресс
         к этому моменту уже в БД (Requirement 10.5);
       - после каждого аккаунта — ``time.sleep(args.delay)``.
    5. В ``finally`` закрыть браузер и контекст.
    6. Напечатать итоговую сводку: успешно / ошибок / пропущено
       (Requirement 10.6).
    """
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # Настроить логирование
    logger = setup_logging(debug=args.debug, log_to_file=True)
    logger.info(f"register_devin запущен с флагами: {args}")

    # Initialize database
    db = AccountDB(DB_PATH)

    # Import existing data if needed
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        logger.info("Импорт существующих данных из .txt файлов...")
        counts = db.import_from_txt_files(
            RESULTS_PATH, ROOT / "taken.txt",
            DEVIN_DONE_PATH, DEVIN_ERRORS_PATH, IDENTITIES_PATH
        )
        logger.info(f"Импортировано: {counts}")
        print(f"Импортировано из .txt файлов: {counts}")

    # Get pending accounts from database
    pending_data = db.get_pending_devin(limit=args.limit)
    if not pending_data:
        error_msg = (
            "Не найдено аккаунтов для регистрации на Devin.\n\n"
            "Это шаг 2 пайплайна. Сначала нужно создать email-аккаунты:\n"
            "  .venv\\Scripts\\python.exe create_emails.py --debug --limit 5 --head\n\n"
            "После этого БД будет содержать созданные аккаунты."
        )
        logger.error(error_msg)
        print(error_msg, file=sys.stderr)
        return 1

    # Convert to Account objects
    pending = [Account(email=d['email'], password=d['password']) for d in pending_data]

    done_emails = db.get_devin_done_emails()
    skipped_done = len(done_emails)

    print(
        f"Всего аккаунтов в БД: {len(done_emails) + len(pending)}; "
        f"уже сделано: {skipped_done}; "
        f"в очереди: {len(pending)}"
    )

    if not pending:
        print("Нечего делать.")
        return 0

    ok = 0
    errors = 0
    interrupted = False

    # Выбор режима: последовательный (workers=1) или параллельный (workers>1)
    if args.workers == 1:
        # ===== ПОСЛЕДОВАТЕЛЬНЫЙ РЕЖИМ (старая логика) =====
        logger.info("Режим: последовательная обработка (1 воркер)")

        with sync_playwright() as pw:
            # Создаём браузер один раз, но для каждого аккаунта будем создавать
            # новый BrowserContext для полной изоляции сессий (cookies, localStorage, sessionStorage).
            browser = pw.chromium.launch(channel="chrome", headless=not args.head)
            context = None

            try:
                for i, account in enumerate(pending, 1):
                    logger.info(f"[{i}/{len(pending)}] >>> {account.email}")

                    # Создать новый BrowserContext для каждого аккаунта.
                    # Это гарантирует полную изоляцию: cookies, localStorage, sessionStorage, cache.
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            pass

                    context = browser.new_context()
                    logger.debug(f"[{account.email}] создан новый BrowserContext для изоляции")

                    mail_tab = context.new_page()
                    devin_tab = context.new_page()

                    try:
                        process_account(
                            context,
                            account,
                            mail_tab=mail_tab,
                            devin_tab=devin_tab,
                        )
                    except StepError as exc:
                        db.mark_devin_error(account.email, str(exc))
                        errors += 1
                        logger.error(f"[{account.email}] ОШИБКА: {exc}")
                        log_exception(logger, exc, f"processing {account.email}")
                    except KeyboardInterrupt:
                        print(
                            "\nОстановлено пользователем (Ctrl+C). "
                            "Прогресс сохранён."
                        )
                        interrupted = True
                        break
                    else:
                        db.mark_devin_success(account.email)
                        ok += 1
                        logger.info(f"[{account.email}] OK — Devin зарегистрирован")
                    finally:
                        # Закрыть вкладки после каждого аккаунта
                        try:
                            mail_tab.close()
                        except Exception:
                            pass
                        try:
                            devin_tab.close()
                        except Exception:
                            pass

                    if args.delay > 0:
                        time.sleep(args.delay)
            finally:
                # Закрыть последний context и browser
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                try:
                    browser.close()
                except Exception:
                    pass

    else:
        # ===== ПАРАЛЛЕЛЬНЫЙ РЕЖИМ (ThreadPoolExecutor) =====
        logger.info(f"Режим: параллельная обработка ({args.workers} воркеров)")
        logger.info(f"Rate limiting: задержка {args.worker_delay}s между запуском воркеров")

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {}

            try:
                # Запустить воркеры с rate limiting
                for i, account in enumerate(pending):
                    # Rate limiting: задержка между запуском воркеров
                    if i > 0 and args.worker_delay > 0:
                        time.sleep(args.worker_delay)

                    worker_id = i % args.workers
                    future = executor.submit(
                        process_account_worker,
                        account,
                        worker_id=worker_id,
                        headless=not args.head,
                        db_path=DB_PATH,
                    )
                    futures[future] = account
                    logger.info(f"[{i+1}/{len(pending)}] Запущен воркер для {account.email}")

                # Собрать результаты по мере завершения
                for future in as_completed(futures):
                    account = futures[future]
                    try:
                        email, success, error = future.result()

                        if success:
                            db.mark_devin_success(email)
                            ok += 1
                            print(f"[OK] {email} - УСПЕХ")
                        else:
                            db.mark_devin_error(email, error or "Unknown error")
                            errors += 1
                            print(f"[FAIL] {email} - ОШИБКА: {error}")

                    except Exception as exc:
                        db.mark_devin_error(account.email, f"Future exception: {exc}")
                        errors += 1
                        logger.exception(f"Ошибка при обработке future для {account.email}")
                        print(f"[FAIL] {account.email} - КРИТИЧЕСКАЯ ОШИБКА: {exc}")

            except KeyboardInterrupt:
                print("\nОстановка... Ждём завершения активных воркеров...")
                logger.info("Получен Ctrl+C, останавливаем воркеры...")
                executor.shutdown(wait=True, cancel_futures=True)
                interrupted = True
                print("Воркеры остановлены. Прогресс сохранён.")

    # Export to .txt files for backward compatibility
    logger.info("Экспорт в .txt файлы...")
    db.export_devin_accounts_txt(DEVIN_DONE_PATH)
    db.export_devin_errors_txt(DEVIN_ERRORS_PATH)

    processed = ok + errors
    not_attempted = len(pending) - processed if interrupted else 0
    print(
        f"\nИтого: успешно {ok}, ошибок {errors}, "
        f"пропущено (уже сделано) {skipped_done}, "
        f"не дошли до них {not_attempted}"
    )

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
