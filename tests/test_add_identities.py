"""Unit-тесты для модуля :mod:`add_identities` (Шаг 3 пайплайна).

Покрытие:
* :func:`add_identities.generate_identity` отдаёт ``(name, "street, zip, city")``,
  где address-часть всегда содержит ровно две ``", "``-запятых
  (требование downstream-парсеров в ``devin_async`` / ``storage``).
* :func:`add_identities.process` идемпотентен (повторный запуск отдаёт
  ``(0, 0)``).
* :func:`add_identities.process` возвращает ``(created, total)`` —
  фикс P1-5 из PLAN.md (раньше было ``(created, created)``).
* :func:`add_identities._make_faker` fallback'ает на ``DEFAULT_LOCALE``,
  если переданная локаль недоступна в Faker (например ``en_SG``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from faker import Faker  # noqa: E402

from add_identities import (  # noqa: E402
    DEFAULT_LOCALE,
    _make_faker,
    generate_identity,
    process,
)
from storage import AccountDB  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> AccountDB:
    """Свежая БД на каждый тест (изолированная, без артефактов между тестами)."""
    instance = AccountDB(tmp_path / "test_accounts.db")
    yield instance
    instance.close()


def _seed_devin_success_accounts(db: AccountDB, n: int) -> list[str]:
    """Создать ``n`` Devin-аккаунтов в статусе ``success`` (готовых под Шаг 3)."""
    emails = []
    for i in range(n):
        email = f"user{i}@pingmx.com"
        db.add_email(email, f"pw{i}", f"user{i}")
        db.mark_devin_success(email)
        emails.append(email)
    return emails


# ---------------------------------------------------------------------------
# generate_identity / _format_address
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "locale", ["el_GR", "en_US", "de_DE", "ko_KR", "en_GB"],
)
def test_generate_identity_format(locale: str) -> None:
    """Address-часть содержит ровно ``", "``-запятых между street/zip/city."""
    name, address = generate_identity(Faker(locale))
    assert name, f"name is empty for {locale}"
    parts = address.split(", ")
    assert len(parts) == 3, f"locale={locale}: expected 3 parts, got {parts!r}"
    street, postcode, city = parts
    assert street.strip(), f"empty street for {locale}"
    assert postcode.strip(), f"empty postcode for {locale}"
    assert city.strip(), f"empty city for {locale}"


def test_generate_identity_strips_comma_in_street() -> None:
    """Если у Faker в street есть запятая, она удаляется (иначе разломает split)."""
    class _StreetWithComma:
        def street_address(self) -> str:
            return "Acacia Ave, Apt 5"
        def postcode(self) -> str:
            return "12345"
        def city(self) -> str:
            return "Springfield"
        def name(self) -> str:
            return "Homer Simpson"
    name, address = generate_identity(_StreetWithComma())
    assert name == "Homer Simpson"
    # Главное — не больше двух разделителей-запятых на 3 поля,
    # иначе downstream `.split(", ")` разъедет.
    assert address.count(", ") == 2, address
    # И часть от street-адреса всё ещё узнаваема (comma убрана, но
    # символы остались).
    assert "Acacia Ave" in address
    assert "Apt 5" in address


# ---------------------------------------------------------------------------
# _make_faker fallback
# ---------------------------------------------------------------------------


def test_make_faker_fallback_on_unknown_locale(capsys: pytest.CaptureFixture[str]) -> None:
    """``en_SG`` сейчас отсутствует в Faker — должен быть fallback на el_GR."""
    faker = _make_faker("en_SG")
    captured = capsys.readouterr().out
    assert "fallback" in captured.lower()
    # И сам объект Faker всё-таки создан.
    assert faker.name()


def test_make_faker_known_locale_no_warning(capsys: pytest.CaptureFixture[str]) -> None:
    faker = _make_faker("en_US")
    captured = capsys.readouterr().out
    assert "fallback" not in captured.lower()
    assert faker.name()


# ---------------------------------------------------------------------------
# process()
# ---------------------------------------------------------------------------


def test_process_returns_created_and_total_not_created_twice(db: AccountDB) -> None:
    """P1-5: ``process`` возвращает ``(created, total)``, не ``(created, created)``.

    Если бы баг был жив, для 3-х входов мы бы видели ``(3, 3)``,
    что выглядит ОК — но при сценарии «5 pending, 2 успешно» вернулось
    бы ``(2, 2)``, потеряв информацию о 3 неуспешных. Поэтому проверим
    именно случай с разными значениями: подсунем faker, у которого
    `name()` raise'ит для каждого второго аккаунта.
    """
    _seed_devin_success_accounts(db, 4)

    # Patch _make_faker через monkeypatch не удобно — используем кастомный
    # generate_identity путём подмены модульной функции.
    import add_identities as mod
    real_gen = mod.generate_identity
    counter = {"n": 0}

    def flaky_generate(_faker):
        counter["n"] += 1
        if counter["n"] % 2 == 0:
            raise RuntimeError("simulated faker glitch")
        return real_gen(_faker)

    mod.generate_identity = flaky_generate
    try:
        created, total = process(db, locale="el_GR")
    finally:
        mod.generate_identity = real_gen

    assert total == 4, total
    assert created == 2, created
    assert created != total, "P1-5: created должен отличаться от total при ошибках"


def test_process_idempotent(db: AccountDB) -> None:
    """Повторный запуск ничего не делает (`get_pending_identities` фильтрует)."""
    _seed_devin_success_accounts(db, 2)

    c1, t1 = process(db, locale="el_GR")
    assert (c1, t1) == (2, 2)

    c2, t2 = process(db, locale="el_GR")
    assert (c2, t2) == (0, 0)


def test_process_no_pending_returns_zero(db: AccountDB) -> None:
    """Пустой pending → ``(0, 0)``."""
    created, total = process(db, locale=DEFAULT_LOCALE)
    assert created == 0
    assert total == 0


def test_process_writes_back_to_db_in_correct_format(db: AccountDB) -> None:
    """После Шага 3 ``identity_address`` парсится `get_identity_by_email`."""
    emails = _seed_devin_success_accounts(db, 1)
    created, total = process(db, locale="el_GR")
    assert (created, total) == (1, 1)

    row = db.get_identity_by_email(emails[0])
    assert row is not None
    assert row["full_name"]
    assert row["street"]
    assert row["zip_code"]
    assert row["city"]


def test_process_respects_limit(db: AccountDB) -> None:
    _seed_devin_success_accounts(db, 5)
    created, total = process(db, limit=2, locale="el_GR")
    assert total == 2
    assert created == 2

    # И оставшиеся 3 видны при следующем запуске.
    c2, t2 = process(db, locale="el_GR")
    assert (c2, t2) == (3, 3)
