"""P2-2: общий код, шарящийся между register_devin.py (sync) и
devin_async.py (async).

Раньше один и тот же набор сущностей был продублирован:

  * dataclass-ы и парсеры (``Account``, ``parse_account``, ``load_accounts``,
    ``load_done``, ``Identity``, ``parse_identity``, ``find_identity_for_email``);
  * helper'ы файловой системы (``_flush_to_disk``, ``append_done``,
    ``append_error``);
  * исключения (``RegistrationError``, ``StepError``, ``InvalidCodeError``);
  * вспомогательная регулярка / ``extract_code``;
  * ленивый синглтон OCR-модели ``_get_ocr``.

Всё это НЕ зависит от sync/async API Playwright, поэтому собрано здесь в
один модуль. ``register_devin.py`` и ``devin_async.py`` импортируют этих
имён, а собственно sync- и async-обвязки браузера остаются в исходных
модулях. Это сокращает дубль кода без рискованной радикальной правки
рантайма (Шаг 2 пайплайна).

Перенос обратной совместимости: ``register_devin`` ре-экспортирует все
имена (см. модуль), чтобы существующие импорты ``from register_devin
import Account, …`` продолжали работать.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import ddddocr

from paths import IDENTITIES_FILE


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


# ---------------------------------------------------------------------------
# Account: учётная запись для Devin
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Запись прогресса на диск (append_done / append_error)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Извлечение 6-значного кода подтверждения
# ---------------------------------------------------------------------------


# Регулярка для поиска 6-значного кода подтверждения в теле письма.
# Negative lookarounds ``(?<!\d)`` и ``(?!\d)`` гарантируют, что найденная
# последовательность из ровно 6 цифр НЕ примыкает к другим цифрам:
# - 5-значные числа не подходят (короче);
# - 7-значные числа не подходят (соседняя цифра справа ломает ``(?!\d)``);
# - 12 цифр подряд тоже не дают совпадения (любая позиция внутри окружена
#   цифрами с одной из сторон, поэтому lookaround всегда проваливается).
# Берём именно первое совпадение — это самый ранний 6-значный «остров».
_SIX_DIGIT_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


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
# OCR singleton (ленивая инициализация ddddocr с цифровым алфавитом)
# ---------------------------------------------------------------------------


_ocr: ddddocr.DdddOcr | None = None


def get_ocr() -> ddddocr.DdddOcr:
    """Вернуть инициализированную модель ddddocr (с алфавитом ``0-9``).

    Первый вызов грузит ONNX-модель и фиксирует алфавит цифрами; последующие
    отдают тот же объект. Делаем это лениво, чтобы импорт модуля оставался
    дешёвым (без побочных эффектов на уровне модуля).
    """
    global _ocr
    if _ocr is None:
        instance = ddddocr.DdddOcr(show_ad=False)
        instance.set_ranges("0123456789")
        _ocr = instance
    return _ocr


# ---------------------------------------------------------------------------
# Identity: имя + адрес для подстановки в Stripe Checkout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """Личность для заполнения адреса в Stripe Checkout.

    Парсится из строки ``личности.txt`` после ``\\t``-разделителя:
    ``email<TAB>Имя Фамилия, Улица + номер, Индекс, Город``.
    """

    full_name: str
    street: str
    zip_code: str
    city: str


def parse_identity(identity_str: str) -> Identity | None:
    """Извлечь :class:`Identity` из identity-строки.

    Формат: ``Имя Фамилия, Улица 12, 12345, Город``.
    Возвращает ``None``, если формат не подходит.
    """
    parts = [p.strip() for p in identity_str.split(",")]
    if len(parts) != 4:
        return None
    full_name, street, zip_code, city = parts
    if not (full_name and street and zip_code and city):
        return None
    return Identity(full_name=full_name, street=street, zip_code=zip_code, city=city)


def find_identity_for_email(email: str, identities_path: Path | None = None) -> Identity | None:
    """Найти identity по email в ``личности.txt`` (TSV-формат).

    Сравнение по lower-case email. Возвращает ``None`` если не нашли.
    Параметр ``identities_path`` позволяет переопределить путь в тестах;
    по умолчанию используется :data:`paths.IDENTITIES_FILE`.
    """
    path = identities_path if identities_path is not None else IDENTITIES_FILE
    if not path.exists():
        return None
    target = email.lower()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#") or "\t" not in line:
            continue
        e, identity_str = line.split("\t", 1)
        if e.strip().lower() != target:
            continue
        return parse_identity(identity_str.strip())
    return None


__all__ = [
    "Account",
    "Identity",
    "InvalidCodeError",
    "RegistrationError",
    "StepError",
    "_SIX_DIGIT_CODE_RE",
    "_flush_to_disk",
    "append_done",
    "append_error",
    "extract_code",
    "find_identity_for_email",
    "get_ocr",
    "load_accounts",
    "load_done",
    "parse_account",
    "parse_identity",
]
