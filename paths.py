"""Каноничные пути к runtime-файлам проекта.

P2-1: раньше пути с кириллицей (``имейлы pingmx.txt``, ``аккаунты devin.txt``,
``бины.txt``, ``личности.txt``, …) были рассыпаны в 30+ местах по
``gui.py``, ``create_emails.py``, ``register_devin.py``, ``check_cards.py``.
На Windows ``cmd``-кодировка cp1251 ломала CLI-аргументы; в Git Bash /
WSL встречались разные кодировки локалей. Любая опечатка в кириллице
приводила к молчаливо неработающему пайплайну (Шаг 4 искал
``bins.txt`` вместо ``бины.txt`` — это и был P0-3).

Здесь — единственное место, где определены имена. Импортируйте отсюда:

.. code-block:: python

    from paths import NICKS_FILE, EMAILS_FILE, DEVIN_OK_FILE

Сами файлы на диске остаются с прежними кириллическими именами —
переименование сломало бы пайплайн пользователей. При необходимости
можно перейти на латинские имена файлов, поменяв константы здесь.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Входные файлы (источники для пайплайна)
NICKS_FILE: Path = ROOT / "имена для имейлов.txt"
BINS_FILE: Path = ROOT / "бины.txt"

# Накопительные результаты Шага 1 (create_emails)
EMAILS_FILE: Path = ROOT / "имейлы pingmx.txt"
TAKEN_FILE: Path = ROOT / "taken.txt"

# Накопительные результаты Шага 2 (register_devin)
DEVIN_OK_FILE: Path = ROOT / "аккаунты devin.txt"
DEVIN_ERRORS_FILE: Path = ROOT / "devin_errors.txt"

# Шаг 3 (add_identities) пишет identity по этим email-ам
IDENTITIES_FILE: Path = ROOT / "личности.txt"

# Шаг 4 (check_cards) накапливает пул карт
LIVE_CARDS_FILE: Path = ROOT / "живые карты.txt"
CONFIRMED_LIVE_CARDS_FILE: Path = ROOT / "подтверждённые живые карты.txt"

# Артефакты и runtime
DB_FILE: Path = ROOT / "accounts.db"
LOGS_DIR: Path = ROOT / "logs"
SCREENSHOTS_DIR: Path = ROOT / "screenshots"
CAPTCHA_DEBUG_DIR: Path = ROOT / "captcha_debug"
TEMP_PROFILES_DIR: Path = ROOT / "temp_profiles"
BROWSER_PROFILE_DIR: Path = ROOT / "browser_profile"

__all__ = [
    "ROOT",
    "NICKS_FILE",
    "BINS_FILE",
    "EMAILS_FILE",
    "TAKEN_FILE",
    "DEVIN_OK_FILE",
    "DEVIN_ERRORS_FILE",
    "IDENTITIES_FILE",
    "LIVE_CARDS_FILE",
    "CONFIRMED_LIVE_CARDS_FILE",
    "DB_FILE",
    "LOGS_DIR",
    "SCREENSHOTS_DIR",
    "CAPTCHA_DEBUG_DIR",
    "TEMP_PROFILES_DIR",
    "BROWSER_PROFILE_DIR",
]
