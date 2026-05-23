"""Property-based тесты для парсинга ``results.txt``.

Покрывают свойство 4 из ``design.md`` (раздел Correctness Properties):
``parse_account`` и ``load_accounts`` корректно работают с разными формами
строк, а ``load_accounts`` дедуплицирует email-ы.

**Validates: Property 4 (Парсинг results.txt)**
"""

from __future__ import annotations

import sys
from pathlib import Path

# Добавляем корень pinmx-mailer/pinmx-mailer/ в sys.path, чтобы можно было
# импортировать ``register_devin`` без установки пакета.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypothesis import assume, given, strategies as st

from register_devin import Account, load_accounts, parse_account


# ---------------------------------------------------------------------------
# Стратегии генерации
# ---------------------------------------------------------------------------

# Локальная часть email: ASCII-буквы, цифры и безопасные символы, исключая
# любые разделители, которые могут попасть в формат ``results.txt``.
_LOCAL_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+"
# Доменная часть: только нижний регистр и цифры (так домены и приходят).
_DOMAIN_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-"


@st.composite
def emails(draw: st.DrawFn) -> str:
    local = draw(st.text(alphabet=_LOCAL_ALPHABET, min_size=1, max_size=20))
    domain = draw(st.text(alphabet=_DOMAIN_ALPHABET, min_size=1, max_size=15))
    tld = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=2, max_size=6))
    # Домен не может начинаться или заканчиваться на ``-``.
    assume(not domain.startswith("-") and not domain.endswith("-"))
    return f"{local}@{domain}.{tld}"


# Пароль: любые printable-символы без двоеточия и без переводов строк.
# Также избегаем подстроки ``" | "`` — в формате ``results.txt`` это
# разделитель identity, и его наличие в самом пароле сделало бы строку
# принципиально неоднозначной.
_password_chars = st.characters(
    blacklist_characters=":\r\n",
    blacklist_categories=("Cs",),
)


@st.composite
def passwords(draw: st.DrawFn) -> str:
    pwd = draw(st.text(alphabet=_password_chars, min_size=1, max_size=30))
    assume(" | " not in pwd)
    return pwd


# Identity: любые символы без переводов строк (двоеточие тут уже допустимо —
# на парсинг не влияет, потому что отсекается раньше).
_identity_chars = st.characters(
    blacklist_characters="\r\n",
    blacklist_categories=("Cs",),
)
identities = st.text(alphabet=_identity_chars, max_size=30)


# ---------------------------------------------------------------------------
# Property-тесты
# ---------------------------------------------------------------------------


@given(email=emails(), password=passwords(), identity=identities)
def test_parse_account_with_identity(email: str, password: str, identity: str) -> None:
    """Строка ``email:password | identity`` парсится корректно: identity
    отбрасывается, email приводится к lower-case."""
    line = f"{email}:{password} | {identity}"
    assert parse_account(line) == Account(email=email.lower(), password=password)


@given(email=emails(), password=passwords())
def test_parse_account_without_identity(email: str, password: str) -> None:
    """Строка ``email:password`` без хвоста identity также парсится."""
    line = f"{email}:{password}"
    assert parse_account(line) == Account(email=email.lower(), password=password)


@given(email=emails(), password=passwords(), identity=identities)
def test_parse_account_idempotent_on_identity(
    email: str, password: str, identity: str
) -> None:
    """Наличие или отсутствие identity не меняет результат."""
    with_identity = parse_account(f"{email}:{password} | {identity}")
    without_identity = parse_account(f"{email}:{password}")
    assert with_identity == without_identity


# --- Негативные примеры -----------------------------------------------------


def test_parse_account_empty_string() -> None:
    assert parse_account("") is None


def test_parse_account_comment_line() -> None:
    # ``# comment`` не содержит двоеточия → None.
    assert parse_account("# comment") is None


def test_parse_account_no_colon() -> None:
    assert parse_account("nopassword") is None


@given(
    text=st.text(
        alphabet=st.characters(blacklist_characters=":\r\n", blacklist_categories=("Cs",)),
        max_size=40,
    )
)
def test_parse_account_no_colon_returns_none(text: str) -> None:
    """Любая строка без двоеточия и без переводов строк парсится в None."""
    # rstrip("\r\n") в parse_account уберёт возможный хвост, но для чистоты
    # стратегия и так не порождает таких символов.
    assume(":" not in text)
    assert parse_account(text) is None


# --- Дедупликация в load_accounts ------------------------------------------


@given(
    email=emails(),
    password=passwords(),
    repeats=st.integers(min_value=2, max_value=10),
)
def test_load_accounts_deduplicates_email(
    tmp_path_factory, email: str, password: str, repeats: int
) -> None:
    """Если в ``results.txt`` один и тот же email встречается N раз,
    ``load_accounts`` возвращает ровно один экземпляр Account."""
    tmp_dir = tmp_path_factory.mktemp("results")
    results_path = tmp_dir / "results.txt"
    line = f"{email}:{password}"
    results_path.write_text("\n".join([line] * repeats), encoding="utf-8")

    accounts = load_accounts(results_path)
    assert len(accounts) == 1
    assert accounts[0] == Account(email=email.lower(), password=password)


@given(
    email_a=emails(),
    email_b=emails(),
    password_a=passwords(),
    password_b=passwords(),
)
def test_load_accounts_keeps_first_for_duplicate(
    tmp_path_factory,
    email_a: str,
    email_b: str,
    password_a: str,
    password_b: str,
) -> None:
    """При коллизии по email (lower-case) выигрывает первая встреченная
    запись — последующие с тем же email отбрасываются."""
    assume(email_a.lower() != email_b.lower())

    tmp_dir = tmp_path_factory.mktemp("results")
    results_path = tmp_dir / "results.txt"
    # Дублируем первый email с другим регистром и другим паролем.
    duplicate_with_diff_case = email_a.swapcase()
    lines = [
        f"{email_a}:{password_a}",
        f"{duplicate_with_diff_case}:{password_b}",
        f"{email_b}:{password_b}",
    ]
    results_path.write_text("\n".join(lines), encoding="utf-8")

    accounts = load_accounts(results_path)
    emails_loaded = [a.email for a in accounts]

    assert emails_loaded == [email_a.lower(), email_b.lower()]
    # Пароль у первой записи — тот, что был в первой строке.
    assert accounts[0].password == password_a


def test_load_accounts_missing_file(tmp_path: Path) -> None:
    """Если файла нет, возвращается пустой список (CLI решает, что делать)."""
    assert load_accounts(tmp_path / "absent.txt") == []


def test_load_accounts_skips_blank_and_comment_lines(tmp_path: Path) -> None:
    """Пустые строки и комментарии не попадают в результат."""
    results_path = tmp_path / "results.txt"
    results_path.write_text(
        "\n".join(
            [
                "",
                "# this is a comment",
                "alice@pingmx.com:secret123",
                "",
                "# another comment",
                "bob@pingmx.com:hunter2 | bob_identity",
            ]
        ),
        encoding="utf-8",
    )

    accounts = load_accounts(results_path)
    assert accounts == [
        Account(email="alice@pingmx.com", password="secret123"),
        Account(email="bob@pingmx.com", password="hunter2"),
    ]
