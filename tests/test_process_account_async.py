"""P2-2 ч.2: тесты для async-pipeline process_account_async.

Тестируем оркестрацию без живого Playwright: подменяем
``login_to_mailclient_async``, ``start_devin_signup_async``,
``wait_for_devin_email_code_async``, ``submit_devin_code_async``
на async-моки и проверяем, что:

- успешный путь вызывает шаги в правильном порядке;
- InvalidCodeError на первой попытке → ретрай;
- 3 InvalidCodeError подряд → StepError с пометкой «verification failed»;
- StepError из любого шага улетает наружу без ретрая.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import devin_async  # noqa: E402
from devin_common import Account, InvalidCodeError, StepError  # noqa: E402


def _make_page_mock(count: int = 0) -> MagicMock:
    """Mock Playwright Page: только .locator(sel).count() для baseline."""
    page = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=count)
    page.locator = MagicMock(return_value=locator)
    return page


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def patched_steps(monkeypatch):
    """Заменить шаги пайплайна на async-моки.

    Возвращает словарь моков, чтобы тест мог утверждать вызовы
    и подменять side-effect.
    """
    login = AsyncMock(return_value=None)
    signup = AsyncMock(return_value=None)
    wait = AsyncMock(return_value="123456")
    submit = AsyncMock(return_value=None)

    monkeypatch.setattr(devin_async, "login_to_mailclient_async", login)
    monkeypatch.setattr(devin_async, "start_devin_signup_async", signup)
    monkeypatch.setattr(devin_async, "wait_for_devin_email_code_async", wait)
    monkeypatch.setattr(devin_async, "submit_devin_code_async", submit)
    # asyncio.sleep — чтобы тесты не висели на бэкоффах 1/2/4 секунды
    monkeypatch.setattr(devin_async.asyncio, "sleep", AsyncMock())

    return {"login": login, "signup": signup, "wait": wait, "submit": submit}


def test_happy_path_runs_steps_in_order(patched_steps):
    """Успешный flow: login → signup → wait → submit."""
    account = Account(email="a@b.com", password="pwd")
    mail = _make_page_mock(count=5)
    devin = _make_page_mock()

    _run(devin_async.process_account_async(
        account, mail_page=mail, devin_page=devin
    ))

    patched_steps["login"].assert_awaited_once_with(mail, account)
    patched_steps["signup"].assert_awaited_once_with(devin, account.email)
    # baseline_count = 5 (что вернул mail.locator().count())
    patched_steps["wait"].assert_awaited_once_with(mail, baseline_count=5)
    patched_steps["submit"].assert_awaited_once_with(devin, "123456")


def test_invalid_code_triggers_retry(patched_steps):
    """Первая попытка submit бросает InvalidCodeError → ретрай → успех."""
    patched_steps["submit"].side_effect = [
        InvalidCodeError("wrong code 1"),
        None,
    ]
    patched_steps["wait"].side_effect = ["111111", "222222"]

    _run(devin_async.process_account_async(
        Account(email="x@y.com", password="p"),
        mail_page=_make_page_mock(), devin_page=_make_page_mock(),
    ))

    assert patched_steps["wait"].await_count == 2
    assert patched_steps["submit"].await_count == 2
    # asyncio.sleep с бэкоффом — должен быть вызван хотя бы 1 раз
    assert devin_async.asyncio.sleep.await_count >= 1


def test_three_invalid_codes_raises_step_error(patched_steps):
    """3 InvalidCodeError подряд → StepError 'verification failed after 3 attempts'."""
    patched_steps["submit"].side_effect = [
        InvalidCodeError("wrong 1"),
        InvalidCodeError("wrong 2"),
        InvalidCodeError("wrong 3"),
    ]
    patched_steps["wait"].side_effect = ["111", "222", "333"]

    with pytest.raises(StepError, match="verification failed after 3 attempts"):
        _run(devin_async.process_account_async(
            Account(email="x@y.com", password="p"),
            mail_page=_make_page_mock(), devin_page=_make_page_mock(),
        ))


def test_step_error_from_wait_propagates(patched_steps):
    """StepError из wait_for_devin_email_code_async — без ретрая, наружу."""
    patched_steps["wait"].side_effect = StepError("timeout reading email")

    with pytest.raises(StepError, match="timeout reading email"):
        _run(devin_async.process_account_async(
            Account(email="x@y.com", password="p"),
            mail_page=_make_page_mock(), devin_page=_make_page_mock(),
        ))

    # submit не вызывался — wait свалился раньше
    patched_steps["submit"].assert_not_awaited()


def test_step_error_from_login_propagates(patched_steps):
    """StepError из login_to_mailclient_async — наружу, остальные шаги не вызывались."""
    patched_steps["login"].side_effect = StepError("mail login captcha failed")

    with pytest.raises(StepError, match="mail login captcha failed"):
        _run(devin_async.process_account_async(
            Account(email="x@y.com", password="p"),
            mail_page=_make_page_mock(), devin_page=_make_page_mock(),
        ))

    patched_steps["signup"].assert_not_awaited()
    patched_steps["wait"].assert_not_awaited()
    patched_steps["submit"].assert_not_awaited()


def test_step_error_from_signup_propagates(patched_steps):
    """StepError из start_devin_signup_async — после успешного login,
    но до wait/submit."""
    patched_steps["signup"].side_effect = StepError("SAML route")

    with pytest.raises(StepError, match="SAML route"):
        _run(devin_async.process_account_async(
            Account(email="x@y.com", password="p"),
            mail_page=_make_page_mock(), devin_page=_make_page_mock(),
        ))

    patched_steps["login"].assert_awaited_once()
    patched_steps["wait"].assert_not_awaited()
    patched_steps["submit"].assert_not_awaited()
