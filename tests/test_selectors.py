"""P2-7: тесты для selectors_.find_any / find_any_async / wait_for_any_async."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402  — sys.path insertion above

from selectors_ import find_any, find_any_async, wait_for_any_async  # noqa: E402


class _FakeLocator:
    def __init__(self, *, visible: bool, raises: bool = False) -> None:
        self._visible = visible
        self._raises = raises
        self.is_visible_calls = 0

    def is_visible(self, timeout: int = 0) -> bool:
        self.is_visible_calls += 1
        if self._raises:
            raise RuntimeError("boom")
        return self._visible


class _FakeAsyncLocator:
    def __init__(self, *, visible: bool, raises: bool = False) -> None:
        self._visible = visible
        self._raises = raises

    async def is_visible(self, timeout: int = 0) -> bool:
        if self._raises:
            raise RuntimeError("boom")
        return self._visible


class _FakeLocatorChain:
    """Имитация page.locator(sel).first."""
    def __init__(self, locator: object) -> None:
        self.first = locator


class _FakePage:
    def __init__(self, mapping: dict[str, _FakeLocator | _FakeAsyncLocator]) -> None:
        self._mapping = mapping
        self.locator_calls: list[str] = []

    def locator(self, sel: str) -> _FakeLocatorChain:
        self.locator_calls.append(sel)
        loc = self._mapping[sel]
        return _FakeLocatorChain(loc)


def test_find_any_returns_first_visible() -> None:
    """find_any должен вернуть первый видимый локатор, не трогая остальные."""
    page = _FakePage({
        "a": _FakeLocator(visible=False),
        "b": _FakeLocator(visible=True),
        "c": _FakeLocator(visible=True),  # не дойдёт
    })
    result = find_any(page, ["a", "b", "c"])
    assert result is page._mapping["b"].is_visible.__self__  # noqa
    # Проверяем что после первого "видимого" не пытались дальше.
    assert page.locator_calls == ["a", "b"]


def test_find_any_returns_none_when_nothing_visible() -> None:
    page = _FakePage({
        "x": _FakeLocator(visible=False),
        "y": _FakeLocator(visible=False),
    })
    assert find_any(page, ["x", "y"]) is None


def test_find_any_skips_exceptions() -> None:
    """find_any не должен ронять весь поиск, если один локатор кидает."""
    page = _FakePage({
        "broken": _FakeLocator(visible=False, raises=True),
        "ok": _FakeLocator(visible=True),
    })
    result = find_any(page, ["broken", "ok"])
    assert result is not None
    assert page.locator_calls == ["broken", "ok"]


def test_find_any_async_returns_first_visible() -> None:
    """Аналог find_any_async — должен раздавать первый async-видимый."""
    page = _FakePage({
        "a": _FakeAsyncLocator(visible=False),
        "b": _FakeAsyncLocator(visible=True),
    })
    result = asyncio.run(find_any_async(page, ["a", "b"]))
    assert result is not None
    assert page.locator_calls == ["a", "b"]


def test_find_any_async_returns_none_when_all_hidden() -> None:
    page = _FakePage({
        "a": _FakeAsyncLocator(visible=False),
        "b": _FakeAsyncLocator(visible=False),
    })
    result = asyncio.run(find_any_async(page, ["a", "b"]))
    assert result is None


def test_wait_for_any_async_returns_when_appears() -> None:
    """wait_for_any_async должен возвращаться немедленно, если локатор уже виден."""
    page = _FakePage({
        "a": _FakeAsyncLocator(visible=True),
    })
    result = asyncio.run(
        wait_for_any_async(page, ["a"], timeout_s=2.0, poll_interval_s=0.1)
    )
    assert result is not None


def test_wait_for_any_async_returns_none_on_timeout() -> None:
    page = _FakePage({
        "a": _FakeAsyncLocator(visible=False),
    })
    result = asyncio.run(
        wait_for_any_async(page, ["a"], timeout_s=0.3, poll_interval_s=0.1)
    )
    assert result is None


@pytest.mark.parametrize("constant", [
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
])
def test_selector_tuples_are_nonempty(constant: str) -> None:
    """У каждой логической точки должен быть хотя бы один селектор."""
    import selectors_
    value = getattr(selectors_, constant)
    assert isinstance(value, tuple), f"{constant} должен быть tuple"
    assert len(value) >= 1, f"{constant} пуст — без fallback'ов теряется смысл"
    assert all(isinstance(s, str) and s for s in value), f"{constant} содержит не-строки"
