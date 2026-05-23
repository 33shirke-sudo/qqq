"""Property-based тесты для учёта прогресса (``devin_done.txt``).

Покрывают свойства 1 и 6 из ``design.md`` (раздел Correctness Properties):

- **Property 1 (Идемпотентность по email)** — после ``append_done`` запрос
  «обработан ли email» (моделируется как ``e.lower() in load_done(p)``)
  отвечает True независимо от того, в каком регистре email был передан
  в ``append_done`` и в каком регистре проверяется.
- **Property 6 (Нормализация email при сравнении)** — сравнение
  «обработан / не обработан» происходит по lower-case email: ``"Foo@Pingmx.com"``
  и ``"foo@pingmx.com"`` неотличимы.

Дополнительно фиксируем заявленное в задаче 5 поведение: ``append_done``
не дедуплицирует строки в файле (мы пишем один email — одна строка),
а ``load_done`` дедуплицирует их при чтении (отдаёт set).

**Validates: Property 1, Property 6**
"""

from __future__ import annotations

import sys
from pathlib import Path

# Чтобы импортировать ``register_devin`` без установки пакета, добавляем
# корень pinmx-mailer/pinmx-mailer/ в sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypothesis import assume, given, settings, strategies as st

from register_devin import append_done, load_done


# ---------------------------------------------------------------------------
# Профиль hypothesis для I/O-bound тестов
# ---------------------------------------------------------------------------

# В тестах ниже `append_done` делает `flush + os.fsync` после каждой
# записи, поэтому при N=20 на холодной Windows-FS один прогон может
# занять 300–400 мс. Дефолтный hypothesis-deadline 200 мс провоцирует
# Flaky-падения. Отключаем deadline точечно — корректность от этого
# не страдает, мы по-прежнему генерируем десятки кейсов.
_IO_BOUND = settings(deadline=None)


# ---------------------------------------------------------------------------
# Стратегия генерации валидных email
# ---------------------------------------------------------------------------

# Локальная часть: ASCII-буквы, цифры и безопасные символы. Без пробелов
# и переводов строк, чтобы ни ``append_done`` (write `email + "\n"`), ни
# ``load_done`` (``raw_line.strip().lower()``) не «съели» часть значения.
_LOCAL_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+"
_DOMAIN_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-"


@st.composite
def emails(draw: st.DrawFn) -> str:
    local = draw(st.text(alphabet=_LOCAL_ALPHABET, min_size=1, max_size=20))
    domain = draw(st.text(alphabet=_DOMAIN_ALPHABET, min_size=1, max_size=15))
    tld = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=2, max_size=6))
    # Доменная метка не может начинаться/заканчиваться дефисом.
    assume(not domain.startswith("-") and not domain.endswith("-"))
    return f"{local}@{domain}.{tld}"


# ---------------------------------------------------------------------------
# Property 1 — идемпотентность по email
# ---------------------------------------------------------------------------


@_IO_BOUND
@given(email=emails())
def test_append_done_then_load_done_contains_email(
    tmp_path_factory, email: str
) -> None:
    """После ``append_done(p, e)`` множество ``load_done(p)`` содержит
    ``e.lower()``. Validates: Property 1."""
    tmp_dir = tmp_path_factory.mktemp("done")
    p = tmp_dir / "devin_done.txt"

    append_done(p, email)

    assert email.lower() in load_done(p)


@_IO_BOUND
@given(email=emails())
def test_append_done_normalizes_case_on_write(
    tmp_path_factory, email: str
) -> None:
    """Запись с любым регистром даёт тот же результат при чтении: после
    ``append_done(p, email.swapcase())`` множество всё равно содержит
    ``email.lower()``. Validates: Property 6."""
    tmp_dir = tmp_path_factory.mktemp("done")
    p = tmp_dir / "devin_done.txt"

    append_done(p, email.swapcase())

    done = load_done(p)
    # И исходный, и swapcase сравниваются с одним и тем же lower-case ключом.
    assert email.lower() in done
    assert email.swapcase().lower() in done
    # А значит «есть ли email в done» инвариантно к регистру входа.
    assert (email.lower() in done) == (email.swapcase().lower() in done)


# ---------------------------------------------------------------------------
# Property 6 — конкретный пример из задачи 5
# ---------------------------------------------------------------------------


def test_case_insensitive_concrete_example(tmp_path: Path) -> None:
    """Конкретный пример из tasks.md: ``Foo@Pingmx.com`` и ``foo@pingmx.com``
    дают одинаковый ответ. Validates: Property 6."""
    p = tmp_path / "devin_done.txt"
    append_done(p, "Foo@Pingmx.com")

    done = load_done(p)
    assert "foo@pingmx.com" in done
    assert "Foo@Pingmx.com".lower() in done
    # Любой регистр запроса — один и тот же ответ.
    assert ("foo@pingmx.com" in done) == ("Foo@Pingmx.com".lower() in done)


@_IO_BOUND
@given(email=emails())
def test_lookup_invariant_to_query_case(
    tmp_path_factory, email: str
) -> None:
    """Запрос «есть ли email в done» инвариантен к регистру: для любого
    написания запроса ``q`` его принадлежность к ``load_done(p)`` зависит
    только от ``q.lower()``. Validates: Property 6."""
    tmp_dir = tmp_path_factory.mktemp("done")
    p = tmp_dir / "devin_done.txt"

    append_done(p, email)
    done = load_done(p)

    # Любая комбинация регистров запроса даёт один и тот же булев ответ.
    in_lower = email.lower() in done
    in_upper = email.upper().lower() in done
    in_swapped = email.swapcase().lower() in done
    assert in_lower == in_upper == in_swapped is True


# ---------------------------------------------------------------------------
# Property: запись не дедуплицирует, чтение — дедуплицирует
# ---------------------------------------------------------------------------


@_IO_BOUND
@given(
    email=emails(),
    n=st.integers(min_value=1, max_value=20),
)
def test_n_appends_yield_n_file_lines_but_one_set_element(
    tmp_path_factory, email: str, n: int
) -> None:
    """После ``n`` вызовов ``append_done(p, email)`` файл содержит ровно
    ``n`` строк, но ``load_done(p) == {email.lower()}``.

    Так формулируется идемпотентность учёта: write-side не делает уникальность,
    read-side её обеспечивает. Validates: Property 1."""
    tmp_dir = tmp_path_factory.mktemp("done")
    p = tmp_dir / "devin_done.txt"

    for _ in range(n):
        append_done(p, email)

    # ``splitlines()`` корректно разбирает и ``\n``, и Windows-овский ``\r\n``.
    file_lines = p.read_text(encoding="utf-8").splitlines()
    assert len(file_lines) == n
    # Каждая строка — это lower-case email.
    assert all(line.strip() == email.lower() for line in file_lines)

    assert load_done(p) == {email.lower()}


@_IO_BOUND
@given(
    email_a=emails(),
    email_b=emails(),
    repeats_a=st.integers(min_value=1, max_value=5),
    repeats_b=st.integers(min_value=1, max_value=5),
)
def test_load_done_returns_unique_lowercased_set(
    tmp_path_factory,
    email_a: str,
    email_b: str,
    repeats_a: int,
    repeats_b: int,
) -> None:
    """При смешанной записи двух разных email-ов с дублями и в разных
    регистрах ``load_done`` возвращает ровно их lower-case множество.
    Validates: Property 1, Property 6."""
    assume(email_a.lower() != email_b.lower())

    tmp_dir = tmp_path_factory.mktemp("done")
    p = tmp_dir / "devin_done.txt"

    # Чередуем записи с разной формой регистра, чтобы исключить зависимость
    # от порядка/регистра при дедупликации.
    for i in range(max(repeats_a, repeats_b)):
        if i < repeats_a:
            email_form = email_a if i % 2 == 0 else email_a.swapcase()
            append_done(p, email_form)
        if i < repeats_b:
            email_form = email_b.swapcase() if i % 2 == 0 else email_b
            append_done(p, email_form)

    assert load_done(p) == {email_a.lower(), email_b.lower()}


# ---------------------------------------------------------------------------
# Базовые edge-cases (unit-тесты, дополняют property-тесты)
# ---------------------------------------------------------------------------


def test_load_done_missing_file_returns_empty_set(tmp_path: Path) -> None:
    """Если файла нет, ``load_done`` возвращает пустое множество — это
    нормальный путь для первого запуска."""
    assert load_done(tmp_path / "absent.txt") == set()


def test_load_done_skips_blank_and_whitespace_lines(tmp_path: Path) -> None:
    """Пустые строки и строки из одних пробелов игнорируются."""
    p = tmp_path / "devin_done.txt"
    p.write_text(
        "alice@pingmx.com\n\n   \n\tbob@pingmx.com\t\n",
        encoding="utf-8",
    )

    assert load_done(p) == {"alice@pingmx.com", "bob@pingmx.com"}


def test_load_done_lowercases_existing_entries(tmp_path: Path) -> None:
    """Если файл уже содержит email в смешанном регистре (например, ручная
    правка), ``load_done`` всё равно нормализует их к lower-case."""
    p = tmp_path / "devin_done.txt"
    p.write_text("Alice@Pingmx.COM\nBOB@PINGMX.com\n", encoding="utf-8")

    assert load_done(p) == {"alice@pingmx.com", "bob@pingmx.com"}
