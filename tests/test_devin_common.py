"""P2-2: тесты для общего модуля devin_common.

Покрытие:
- Account / parse_account / load_accounts
- load_done / append_done / append_error
- extract_code (минимум на boundary-кейсы)
- Identity / parse_identity / find_identity_for_email
- Re-export: register_devin импортирует те же объекты
- Re-export: devin_async импортирует те же типы (без поднятия Playwright)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import devin_common as dc  # noqa: E402


def test_account_dataclass_frozen() -> None:
    """Account — frozen dataclass."""
    acc = dc.Account(email="a@b.com", password="pwd")
    assert acc.email == "a@b.com"
    assert acc.password == "pwd"
    try:
        acc.email = "c@d.com"  # type: ignore[misc]
    except Exception:
        pass  # FrozenInstanceError
    else:
        raise AssertionError("Account is supposed to be frozen")


def test_parse_account_basic() -> None:
    assert dc.parse_account("foo@bar.com:secret\n") == dc.Account(
        email="foo@bar.com", password="secret"
    )
    assert dc.parse_account("FOO@BAR.com:secret") == dc.Account(
        email="foo@bar.com", password="secret"
    )


def test_parse_account_with_identity_tail() -> None:
    """`<email>:<pwd> | Имя, Адрес, ...` — identity-хвост отбрасывается."""
    acc = dc.parse_account("a@b.com:hunter2 | Иван, Street 1, 12345, City")
    assert acc == dc.Account(email="a@b.com", password="hunter2")


def test_parse_account_empty_and_invalid() -> None:
    assert dc.parse_account("") is None
    assert dc.parse_account("\n") is None
    assert dc.parse_account("just-a-line-no-colon") is None


def test_load_accounts_dedup_and_missing(tmp_path: Path) -> None:
    """Дубликаты по email игнорируются; нет файла → []."""
    f = tmp_path / "results.txt"
    f.write_text("a@b.com:1\nA@B.com:2\nc@d.com:3\n", encoding="utf-8")
    accounts = dc.load_accounts(f)
    assert accounts == [
        dc.Account(email="a@b.com", password="1"),
        dc.Account(email="c@d.com", password="3"),
    ]

    assert dc.load_accounts(tmp_path / "no-such-file.txt") == []


def test_append_done_and_load_done(tmp_path: Path) -> None:
    f = tmp_path / "done.txt"
    dc.append_done(f, "User@Example.com")
    dc.append_done(f, "second@example.com")
    assert dc.load_done(f) == {"user@example.com", "second@example.com"}


def test_append_error_format(tmp_path: Path) -> None:
    f = tmp_path / "errors.txt"
    dc.append_error(f, "X@Y.com", "Timeout on\tstep\nfoo\r")
    content = f.read_text(encoding="utf-8")
    # email lowercase, переносы и табы в reason — пробелы.
    assert content == "x@y.com\tTimeout on step foo \n"


def test_extract_code_basic() -> None:
    assert dc.extract_code("Your code is 123456 — use within 5 min.") == "123456"
    assert dc.extract_code("Code: 654321\n") == "654321"
    assert dc.extract_code("v123456") == "123456"


def test_extract_code_boundary_rejects_short_long() -> None:
    assert dc.extract_code("only 12345 digits") is None
    assert dc.extract_code("1234567 too many") is None
    assert dc.extract_code("012345678901 mash") is None


def test_identity_parse_valid() -> None:
    res = dc.parse_identity("Ivan Petrov, Lenina 5, 12345, Moscow")
    assert res == dc.Identity(
        full_name="Ivan Petrov", street="Lenina 5", zip_code="12345", city="Moscow"
    )


def test_identity_parse_invalid() -> None:
    assert dc.parse_identity("only-three, fields, here") is None
    assert dc.parse_identity("Empty, , 12345, City") is None
    assert dc.parse_identity("") is None


def test_find_identity_for_email(tmp_path: Path) -> None:
    f = tmp_path / "identities.txt"
    f.write_text(
        "# comment\n"
        "\n"
        "a@b.com\tFoo Bar, Street 1, 12345, City\n"
        "X@Y.COM\tIvan Petrov, Lenina 5, 54321, Moscow\n",
        encoding="utf-8",
    )
    assert dc.find_identity_for_email("a@b.com", identities_path=f) == dc.Identity(
        "Foo Bar", "Street 1", "12345", "City"
    )
    # email lower-case match независимо от регистра в файле
    assert dc.find_identity_for_email("x@y.com", identities_path=f) == dc.Identity(
        "Ivan Petrov", "Lenina 5", "54321", "Moscow"
    )
    assert dc.find_identity_for_email("unknown@example.com", identities_path=f) is None


def test_register_devin_reexports_match_devin_common() -> None:
    """register_devin продолжает экспортировать имена под старыми именами."""
    import register_devin as rd

    assert rd.Account is dc.Account
    assert rd.parse_account is dc.parse_account
    assert rd.load_accounts is dc.load_accounts
    assert rd.load_done is dc.load_done
    assert rd.append_done is dc.append_done
    assert rd.append_error is dc.append_error
    assert rd.extract_code is dc.extract_code
    assert rd.StepError is dc.StepError
    assert rd.InvalidCodeError is dc.InvalidCodeError
    assert rd.RegistrationError is dc.RegistrationError
