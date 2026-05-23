"""Конфигурация URL-ов и runtime-настроек через ENV.

P2-8: раньше URL-ы (``https://app.devin.ai/auth/signup``,
``https://www.pinmx.com/ru``, ``https://chkr.cc/``, …) были
захардкожены в 6+ модулях. Это мешало:

* временно переключаться на staging-зеркала сервисов;
* менять домен mail-провайдера (``mail-client.pinmx.com`` →
  ``pingmx.com`` — было в логах истории) без правки кода;
* запускать e2e-тесты против моков без замены реального трафика.

Здесь все внешние URL-ы и runtime-таймауты — единственное место,
где они определены. Каждое значение берётся из ENV-переменной (если
задана), иначе из ``DEFAULTS`` (продакшен-значения, как было в
оригинальном коде).

ENV
===

* ``QQQ_DEVIN_LOGIN_URL`` — overrides ``DEVIN_LOGIN_URL``
* ``QQQ_DEVIN_SIGNUP_URL`` — overrides ``DEVIN_SIGNUP_URL``
* ``QQQ_DEVIN_APP_BASE_URL`` — overrides ``DEVIN_APP_BASE_URL``
* ``QQQ_PINMX_URL`` — overrides ``PINMX_URL`` (страница регистрации почты)
* ``QQQ_MAIL_LOGIN_URL`` — overrides ``MAIL_LOGIN_URL`` (rainloop-клиент)
* ``QQQ_CHKR_URL`` — overrides ``CHKR_URL``
* ``QQQ_STRIPE_CHECKOUT_PREFIX`` — overrides ``STRIPE_CHECKOUT_PREFIX``

.env
====

При наличии файла ``.env`` в корне проекта и установленного пакета
``python-dotenv`` он будет загружен автоматически (без падения, если
пакета нет — окружения это переменные ENV-shell).

Пример ``.env``::

    QQQ_DEVIN_LOGIN_URL=https://staging.devin.ai/auth/login
    QQQ_PINMX_URL=http://localhost:8080/mock-pinmx
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_PREFIX = "QQQ_"
_ROOT = Path(__file__).resolve().parent
_DOTENV_PATH = _ROOT / ".env"


# Дефолтные продакшен-URL-ы — соответствуют тому, что было захардкожено
# в register_devin.py, create_emails.py, check_cards.py, … до P2-8.
_DEFAULTS: dict[str, str] = {
    "DEVIN_LOGIN_URL": "https://app.devin.ai/auth/login",
    "DEVIN_SIGNUP_URL": "https://app.devin.ai/auth/signup",
    "DEVIN_APP_BASE_URL": "https://app.devin.ai",
    "PINMX_URL": "https://www.pinmx.com/ru",
    "MAIL_LOGIN_URL": "https://mail-client.pinmx.com/",
    "CHKR_URL": "https://chkr.cc/",
    "STRIPE_CHECKOUT_PREFIX": "https://checkout.stripe.com/c/pay/",
}


def _load_dotenv_if_available() -> None:
    """Подгрузить переменные из ``.env`` в os.environ, если есть
    python-dotenv и файл существует. Молча no-op в остальных случаях."""
    if not _DOTENV_PATH.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        # python-dotenv опциональный — без него .env просто игнорируется.
        # Пользователь увидит warning при использовании cli, если хочется
        # того жёстче — но сейчас просто молча.
        return
    load_dotenv(_DOTENV_PATH, override=False)


_load_dotenv_if_available()


def _get(name: str) -> str:
    """Получить значение URL/настройки: сначала из ENV, потом из дефолтов."""
    env_name = f"{_ENV_PREFIX}{name}"
    value = os.environ.get(env_name)
    if value:
        return value
    return _DEFAULTS[name]


# Экспортируемые константы. Имена идентичны тому, что было в
# register_devin.py / create_emails.py / check_cards.py / … — это
# позволяет импортирующим модулям просто заменить локальные
# определения на импорт из config.
DEVIN_LOGIN_URL: str = _get("DEVIN_LOGIN_URL")
DEVIN_SIGNUP_URL: str = _get("DEVIN_SIGNUP_URL")
DEVIN_APP_BASE_URL: str = _get("DEVIN_APP_BASE_URL")
PINMX_URL: str = _get("PINMX_URL")
MAIL_LOGIN_URL: str = _get("MAIL_LOGIN_URL")
CHKR_URL: str = _get("CHKR_URL")
STRIPE_CHECKOUT_PREFIX: str = _get("STRIPE_CHECKOUT_PREFIX")


__all__ = [
    "DEVIN_LOGIN_URL",
    "DEVIN_SIGNUP_URL",
    "DEVIN_APP_BASE_URL",
    "PINMX_URL",
    "MAIL_LOGIN_URL",
    "CHKR_URL",
    "STRIPE_CHECKOUT_PREFIX",
]
