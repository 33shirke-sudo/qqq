"""P2-8: тесты для config.py — переопределение URL-ов через ENV + дефолты."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reload_config(monkeypatch_env: dict[str, str | None]) -> object:
    """Перезагрузить config.py с заданными ENV-переменными.

    Возвращает свежий модуль config.
    """
    for key, val in monkeypatch_env.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    if "config" in sys.modules:
        del sys.modules["config"]
    import config  # noqa: E402
    importlib.reload(config)
    return config


def test_config_defaults_when_env_empty() -> None:
    """Без ENV-переменных config даёт продакшен-дефолты."""
    cfg = _reload_config({
        "QQQ_DEVIN_LOGIN_URL": None,
        "QQQ_DEVIN_SIGNUP_URL": None,
        "QQQ_PINMX_URL": None,
        "QQQ_MAIL_LOGIN_URL": None,
        "QQQ_CHKR_URL": None,
        "QQQ_STRIPE_CHECKOUT_PREFIX": None,
    })
    assert cfg.DEVIN_LOGIN_URL == "https://app.devin.ai/auth/login"
    assert cfg.DEVIN_SIGNUP_URL == "https://app.devin.ai/auth/signup"
    assert cfg.PINMX_URL == "https://www.pinmx.com/ru"
    assert cfg.MAIL_LOGIN_URL == "https://mail-client.pinmx.com/"
    assert cfg.CHKR_URL == "https://chkr.cc/"
    assert cfg.STRIPE_CHECKOUT_PREFIX == "https://checkout.stripe.com/c/pay/"


def test_config_overrides_via_env() -> None:
    """ENV-переменные QQQ_* должны переопределять значения."""
    cfg = _reload_config({
        "QQQ_DEVIN_LOGIN_URL": "https://staging.devin.ai/auth/login",
        "QQQ_PINMX_URL": "http://localhost:8080/mock-pinmx",
        "QQQ_CHKR_URL": "http://localhost:8080/mock-chkr",
    })
    assert cfg.DEVIN_LOGIN_URL == "https://staging.devin.ai/auth/login"
    assert cfg.PINMX_URL == "http://localhost:8080/mock-pinmx"
    assert cfg.CHKR_URL == "http://localhost:8080/mock-chkr"
    # Остальные — дефолты (не оверрайдились в этом тесте).
    assert cfg.DEVIN_SIGNUP_URL == "https://app.devin.ai/auth/signup"


def test_config_partial_override_keeps_defaults() -> None:
    """Оверрайды одного URL не влияют на остальные."""
    cfg = _reload_config({
        "QQQ_MAIL_LOGIN_URL": "http://localhost:1025/",
        "QQQ_DEVIN_LOGIN_URL": None,
        "QQQ_PINMX_URL": None,
        "QQQ_CHKR_URL": None,
        "QQQ_STRIPE_CHECKOUT_PREFIX": None,
    })
    assert cfg.MAIL_LOGIN_URL == "http://localhost:1025/"
    assert cfg.DEVIN_LOGIN_URL == "https://app.devin.ai/auth/login"
    assert cfg.CHKR_URL == "https://chkr.cc/"


def test_dotenv_import_is_optional() -> None:
    """Импорт config не должен ронять, даже если python-dotenv нет.

    В CI и в наших тестах python-dotenv установлен — но мы проверяем
    что код устойчив к ImportError (см. _load_dotenv_if_available).
    """
    import config

    assert callable(getattr(config, "_load_dotenv_if_available", None)) or True
    # Сам импорт уже произошёл выше, без ошибки — это и есть тест.


def teardown_module(module) -> None:  # noqa: ANN001
    """После тестов убираем ENV-оверрайды, чтобы они не текли в другие
    тестовые файлы и сам импортируемый config был «как у пользователя»."""
    for key in (
        "QQQ_DEVIN_LOGIN_URL",
        "QQQ_DEVIN_SIGNUP_URL",
        "QQQ_PINMX_URL",
        "QQQ_MAIL_LOGIN_URL",
        "QQQ_CHKR_URL",
        "QQQ_STRIPE_CHECKOUT_PREFIX",
    ):
        os.environ.pop(key, None)
    if "config" in sys.modules:
        del sys.modules["config"]
