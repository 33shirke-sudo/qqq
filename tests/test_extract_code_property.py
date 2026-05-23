"""Property-based тесты для извлечения 6-значного кода из тела письма.

Покрывают свойство 5 из ``design.md`` (раздел Correctness Properties):

    Для любого тела письма, содержащего ровно одну непрерывную
    последовательность из 6 цифр, не примыкающую к другим цифрам,
    ``extract_code(body)`` возвращает её.
    Формально: ∀ before, after — строки без цифр на стыке с кодом:
    ``extract_code(f"{before}{code6}{after}") == code6``.

Чтобы свойство было well-defined (в ``before``/``after`` не должно быть
других 6-значных «островов», иначе вернётся первый из них), мы
генерируем ``before`` и ``after`` из символов, которые ``\\d`` точно
**не** матчит: это исключает любые ASCII-цифры и Unicode-категории
``Nd`` / ``Nl`` / ``No``. Тогда единственный кандидат на код —
сгенерированный нами ``code``, и регулярка ``(?<!\\d)(\\d{6})(?!\\d)``
обязана вернуть именно его.

**Validates: Property 5 (Извлечение кода из письма)**
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Чтобы импортировать ``register_devin`` без установки пакета, добавляем
# корень pinmx-mailer/pinmx-mailer/ в sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypothesis import given, strategies as st

from register_devin import extract_code


# ---------------------------------------------------------------------------
# Стратегия: текст без символов, которые ``\d`` мог бы счесть цифрой
# ---------------------------------------------------------------------------

# ``\d`` в Python по умолчанию матчит не только ASCII-цифры, но и любые
# Unicode-символы из категорий ``Nd`` (Decimal_Number, например арабская
# ١٢٣), ``Nl`` (Letter_Number, например римские Ⅰ Ⅱ Ⅲ — на самом деле
# Nl, ``\d`` их НЕ матчит, но ``\d`` зависит от флага re.ASCII; чтобы
# избежать любых сюрпризов на разных версиях/локалях, исключаем все три
# числовые категории).
#
# Также исключаем суррогаты (``Cs``) — они нарушают валидность строк.
_NON_DIGIT_CHARS = st.characters(
    blacklist_categories=("Cs", "Nd", "Nl", "No"),
    blacklist_characters="0123456789",
)

# Строки контекста до/после кода: любой длины (включая пустую), но
# гарантированно без символов, которые могут «прилипнуть» к коду как цифры.
_non_digit_text = st.text(alphabet=_NON_DIGIT_CHARS, max_size=40)


# Само значение кода — целое число от 100000 до 999999. Так мы избегаем
# ведущих нулей (с ними строка из 6 символов всё равно бы матчилась, но
# спецификация требует именно диапазон 100000..999999) и фиксируем длину 6.
_code_strings = st.integers(min_value=100_000, max_value=999_999).map(str)


# Дополнительная самопроверка стратегии: сгенерированные ``before``/``after``
# действительно не содержат символов, которые ``\d`` мог бы матчить. Если
# Hypothesis когда-то начнёт порождать неожиданный символ, тест явно
# упадёт здесь, а не на загадочном ассерте о значении кода.
def _assert_no_digit_chars(s: str) -> None:
    assert re.search(r"\d", s) is None, (
        f"строка контекста содержит символ, матчимый \\d: {s!r}"
    )


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------


@given(before=_non_digit_text, code=_code_strings, after=_non_digit_text)
def test_extract_code_returns_isolated_six_digit_run(
    before: str, code: str, after: str
) -> None:
    """Property 5: для любых ``before``/``after`` без цифр (значит и без
    «прилипания» к коду) и любого 6-значного ``code`` из 100000..999999
    функция возвращает ровно ``code``."""
    _assert_no_digit_chars(before)
    _assert_no_digit_chars(after)

    assert extract_code(before + code + after) == code


# ---------------------------------------------------------------------------
# Edge cases — 5, 7 и 12 цифр подряд
# ---------------------------------------------------------------------------


def test_extract_code_five_digits_returns_none() -> None:
    """5-значное число — короче кода, не должно матчиться."""
    assert extract_code("Hello 12345 world") is None


def test_extract_code_seven_digits_returns_none() -> None:
    """7 цифр подряд: внутри нет изолированной 6-значной подстроки —
    с какой стороны её ни взять, рядом окажется седьмая цифра, и
    lookaround в регулярке проваливается."""
    assert extract_code("Hello 1234567 world") is None


def test_extract_code_twelve_digits_returns_none() -> None:
    """12 цифр подряд — тот же случай, что и 7: каждая 6-значная
    подстрока окружена цифрой минимум с одной стороны."""
    assert extract_code("123456789012") is None


@given(
    before=_non_digit_text,
    digits=st.text(alphabet="0123456789", min_size=7, max_size=20),
    after=_non_digit_text,
)
def test_extract_code_long_digit_run_never_matches(
    before: str, digits: str, after: str
) -> None:
    """Усиленный property-вариант edge-case: любая непрерывная цепочка
    из 7+ цифр, окружённая нецифровым контекстом, не даёт кода."""
    _assert_no_digit_chars(before)
    _assert_no_digit_chars(after)

    assert extract_code(before + digits + after) is None


@given(
    before=_non_digit_text,
    digits=st.text(alphabet="0123456789", min_size=1, max_size=5),
    after=_non_digit_text,
)
def test_extract_code_short_digit_run_never_matches(
    before: str, digits: str, after: str
) -> None:
    """Любая цепочка из 1..5 цифр, окружённая нецифровым контекстом, —
    не код, потому что её длина меньше 6."""
    _assert_no_digit_chars(before)
    _assert_no_digit_chars(after)

    assert extract_code(before + digits + after) is None


# ---------------------------------------------------------------------------
# Конкретные примеры из реальных писем
# ---------------------------------------------------------------------------


def test_extract_code_real_example_verification_phrase() -> None:
    """Реальный пример: «Your verification code is 123456»."""
    assert extract_code("Your verification code is 123456") == "123456"


def test_extract_code_real_example_code_label_with_newline() -> None:
    """Реальный пример: «Code: 654321\\n»."""
    assert extract_code("Code: 654321\n") == "654321"


def test_extract_code_returns_first_match_when_multiple_isolated_codes() -> None:
    """Если в тексте несколько изолированных 6-значных «островов», берём
    первый — Devin кладёт код в начало письма / отдельной строкой."""
    body = "old code was 111111, new one is 222222"
    assert extract_code(body) == "111111"


def test_extract_code_no_digits_returns_none() -> None:
    """В тексте без цифр кода нет."""
    assert extract_code("nothing here at all") is None
