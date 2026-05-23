"""add_identities: Шаг 3 пайплайна — сгенерировать identity (имя +
адрес) для аккаунтов, успешно зарегистрированных в Devin.

Из БД берём `accounts` со ``devin_status='success' AND identity_name IS NULL``,
прогоняем через :class:`faker.Faker` с указанной локалью и пишем
обратно в БД (`accounts.identity_name`, `accounts.identity_address`).
Параллельно дописываем в legacy-файл ``личности.txt`` (формат
``email\\tName, Street, ZIP, City``) — это нужно для совместимости со
старыми сценариями (`find_and_pay.py`, `test_hcaptcha.py`,
``find_identity_for_email`` в ``devin_async.py``), которые читают
``личности.txt`` напрямую.

Запуск (из корня проекта)::

    .venv\\Scripts\\python.exe add_identities.py [--limit N] [--locale CODE]

Опции
-----
* ``--limit N`` — максимум аккаунтов за один запуск (по умолчанию все
  pending).
* ``--locale CODE`` — Faker-локаль (по умолчанию ``el_GR``; через GUI
  выбирается из списка ``LOCALES`` — Корея/США/Германия/Сингапур/Англия/
  Греция).

Идемпотентность
---------------
:meth:`AccountDB.get_pending_identities` уже отдаёт только аккаунты без
identity. Дополнительно перед записью вызывается :meth:`AccountDB.has_identity` —
гарантирует, что параллельный запуск Шага 3 не перепишет существующий
identity.

Возврат
-------
``main()`` возвращает 0 если всё прошло без падений (даже если делать было
нечего); 2 — если был хотя бы один аккаунт, но identity для него
сгенерировать не удалось.

:func:`process` возвращает ``(created, total)``: сколько identity создали
из общего количества pending-аккаунтов. Это **исправляет P1-5** из
PLAN.md, где было ошибочно ``return created, created``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable

from faker import Faker

from logging_utils import setup_logging
from paths import DB_FILE, IDENTITIES_FILE, ROOT  # noqa: F401 — ROOT исторически экспортируется отсюда
from storage import AccountDB

# P2-1: алиасы для совместимости. Раньше IDENTITIES_PATH импортировался
# из register_devin — теперь все модули берут одно и то же из paths.
IDENTITIES_PATH = IDENTITIES_FILE
DB_PATH = DB_FILE
DEFAULT_LOCALE = "el_GR"


# ---------------------------------------------------------------------------
# Identity generation
# ---------------------------------------------------------------------------


def _format_address(faker: Faker) -> str:
    """Собрать ``street, zip, city`` строкой через ``", "``.

    Этот формат ожидает downstream-код:
    * ``devin_async.find_identity_for_email`` парсит ``личности.txt`` по
      ``split(",")`` и требует ровно 4 поля: name, street, zip, city
      (имя — отдельным TAB-сегментом, остальные три через запятую).
    * ``storage.AccountDB.get_identity_by_email`` делает
      ``identity_address.split(", ")`` и берёт первые три элемента.

    Внутри ``street`` мы убираем запятые/переводы строк — иначе
    `, `-split на нижестоящей стороне поедет вкось.
    """
    street = faker.street_address().replace(",", " ").replace("\n", " ").strip()
    postcode = faker.postcode().strip()
    city = faker.city().replace(",", " ").strip()
    return f"{street}, {postcode}, {city}"


def generate_identity(faker: Faker) -> tuple[str, str]:
    """Сгенерировать ``(full_name, address_str)`` через Faker.

    ``full_name`` — ``faker.name()`` (в выбранной локали).
    ``address_str`` — см. :func:`_format_address`.
    """
    name = faker.name().strip()
    address = _format_address(faker)
    return name, address


# ---------------------------------------------------------------------------
# Legacy-файл ``личности.txt``
# ---------------------------------------------------------------------------


def _append_identity_to_txt(email: str, name: str, address: str) -> None:
    """Дописать строку ``email\\tName, Street, ZIP, City`` в ``личности.txt``.

    Создаёт файл при первом вызове. Никакой дедупликации — если запись
    дублируется, это просто значит, что Шаг 3 запускали повторно для
    аккаунта, у которого identity уже была (что отсекается через
    ``has_identity`` ещё до вызова).
    """
    IDENTITIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with IDENTITIES_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{email}\t{name}, {address}\n")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _make_faker(locale: str) -> Faker:
    """Создать ``Faker(locale)`` с fallback на :data:`DEFAULT_LOCALE`.

    GUI выставляет ``en_SG`` для «Сингапур», но в Faker такой локали
    нет (по состоянию на 30.x); см. P3 в PLAN.md «расширить LOCALES».
    Вместо падения с ``AttributeError`` мы предупреждаем и берём
    дефолтную локаль — Шаг 3 завершится, а identity-формат останется
    валидным для downstream.
    """
    try:
        return Faker(locale)
    except (AttributeError, ImportError) as exc:
        print(
            f"[identities] предупреждение: локаль {locale!r} недоступна в Faker "
            f"({exc}); fallback на {DEFAULT_LOCALE}"
        )
        return Faker(DEFAULT_LOCALE)


def process(
    db: AccountDB,
    *,
    limit: int | None = None,
    locale: str = DEFAULT_LOCALE,
) -> tuple[int, int]:
    """Сгенерировать identity для всех pending-аккаунтов.

    Args:
        db: открытое подключение к БД.
        limit: максимум аккаунтов за один запуск (``None`` — все).
        locale: код Faker-локали (``el_GR``, ``en_US``, …). Если
            недоступна в Faker — fallback на :data:`DEFAULT_LOCALE`
            (см. :func:`_make_faker`).

    Returns:
        ``(created, total)`` — сколько identity успешно создано из
        общего количества pending-аккаунтов. **Не путать** с ``(created,
        created)`` — это P1-5 из PLAN.md, было багом старой версии.
    """
    faker = _make_faker(locale)

    pending = db.get_pending_identities(limit=limit)
    total = len(pending)

    if total == 0:
        print("[identities] нет аккаунтов, требующих identity")
        return 0, 0

    print(f"[identities] нужно создать identity для {total} аккаунт(ов) (locale={locale})")

    created = 0
    for email in pending:
        # Защита от гонок: вдруг параллельный запуск уже записал.
        if db.has_identity(email):
            print(f"[identities] {email}: уже есть identity — пропускаю")
            continue

        try:
            name, address = generate_identity(faker)
            db.add_identity(email, name, address)
            _append_identity_to_txt(email, name, address)
            created += 1
            print(f"[identities] {email}: {name} | {address}")
        except Exception as exc:
            print(f"[identities] {email}: ошибка {exc!r} — пропускаю")
            continue

    print(f"[identities] готово: создано {created} из {total}")
    return created, total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Генерация identity для зарегистрированных Devin-аккаунтов (Шаг 3)"
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="максимум аккаунтов за запуск (default — все pending)",
    )
    p.add_argument(
        "--locale", default=DEFAULT_LOCALE,
        help=f"код Faker-локали (default {DEFAULT_LOCALE})",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="детальные debug-логи",
    )
    return p.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logger = setup_logging(debug=args.debug, log_to_file=True)
    logger.info(f"add_identities запущен с {args}")

    db = AccountDB(DB_PATH)
    try:
        created, total = process(db, limit=args.limit, locale=args.locale)
    finally:
        db.close()

    if total > 0 and created == 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
