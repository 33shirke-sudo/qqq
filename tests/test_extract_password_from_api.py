"""Тест P1-10: ``create_emails.extract_password_from_api`` тащит пароль из
JSON-ответа pinmx ``/random-mail/create-by-device``.

DOM-фоллбэк ``extract_password_from_success_dialog`` остаётся, но новая
функция должна работать на ВСЕХ известных формах payload-а, чтобы DOM-парсер
требовался только в маргинальных случаях.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def extract():
    from create_emails import extract_password_from_api

    return extract_password_from_api


@pytest.mark.parametrize(
    "payload,expected",
    [
        # Самый частый формат: data.mail.password
        (
            {"code": 200, "data": {"mail": {"mail": "x@pingmx.com", "password": "Pass123"}}},
            "Pass123",
        ),
        # Альтернативный ключ pwd внутри mail
        (
            {"code": 200, "data": {"mail": {"mail": "x@pingmx.com", "pwd": "Secret!"}}},
            "Secret!",
        ),
        # Пароль лежит на уровне data, а не data.mail
        ({"code": 200, "data": {"password": "Top"}}, "Top"),
        # Несколько ключей одновременно — берём первый найденный (password в data.mail).
        (
            {
                "code": 200,
                "data": {
                    "password": "Outer",
                    "mail": {"mail": "x@pingmx.com", "password": "Inner"},
                },
            },
            "Outer",  # data перебирается раньше data.mail
        ),
        # Пробелы вокруг — должны триммиться
        (
            {"code": 200, "data": {"mail": {"password": "  trimmed  "}}},
            "trimmed",
        ),
    ],
)
def test_extract_password_from_api_finds_password(extract, payload, expected) -> None:
    assert extract(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        # JWT-only payload: API не отдал пароль → None
        {"code": 200, "data": {"mail": {"mail": "x@pingmx.com"}}},
        # Ошибка регистрации: code != 200, нет data
        {"code": 81002, "msg": "Email Already Exists"},
        # Пустой пароль не считается за валидный
        {"code": 200, "data": {"mail": {"password": ""}}},
        # Только пробелы — тоже невалидно
        {"code": 200, "data": {"mail": {"password": "   "}}},
        # Не-строковое значение игнорируется
        {"code": 200, "data": {"mail": {"password": 12345}}},
        # data — не dict
        {"code": 200, "data": "junk"},
        # payload — вообще не dict
        "not a dict",
        # None
        None,
    ],
)
def test_extract_password_from_api_returns_none(extract, payload) -> None:
    assert extract(payload) is None
