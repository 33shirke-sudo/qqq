"""P2-1: проверяем, что все ключевые модули импортируют пути из paths.py
и значения совпадают между алиасами и каноническими константами."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_paths_constants_are_path_objects() -> None:
    import paths

    for name in paths.__all__:
        if name in {"ROOT"}:
            continue
        value = getattr(paths, name)
        assert isinstance(value, Path), f"{name} должен быть Path, а не {type(value)}"


def test_paths_root_matches_repo_root() -> None:
    import paths

    assert paths.ROOT == ROOT


def test_legacy_aliases_match_canonical() -> None:
    """Старые имена констант (RESULTS_PATH, DEVIN_DONE_PATH, …) должны
    указывать на те же файлы, что и канонические из paths.py."""
    import create_emails
    import paths

    assert create_emails.NICKS_PATH == paths.NICKS_FILE
    assert create_emails.RESULTS_PATH == paths.EMAILS_FILE
    assert create_emails.TAKEN_PATH == paths.TAKEN_FILE
    assert create_emails.DB_PATH == paths.DB_FILE
    assert create_emails.DEBUG_DIR == paths.CAPTCHA_DEBUG_DIR

    import register_devin

    assert register_devin.RESULTS_PATH == paths.EMAILS_FILE
    assert register_devin.DEVIN_DONE_PATH == paths.DEVIN_OK_FILE
    assert register_devin.DEVIN_ERRORS_PATH == paths.DEVIN_ERRORS_FILE
    assert register_devin.IDENTITIES_PATH == paths.IDENTITIES_FILE
    assert register_devin.DB_PATH == paths.DB_FILE

    import add_identities

    assert add_identities.IDENTITIES_PATH == paths.IDENTITIES_FILE
    assert add_identities.DB_PATH == paths.DB_FILE

    import check_cards

    assert check_cards.BINS_PATH == paths.BINS_FILE
    assert check_cards.LIVE_CARDS_PATH == paths.LIVE_CARDS_FILE
    assert check_cards.CONFIRMED_LIVE_CARDS_PATH == paths.CONFIRMED_LIVE_CARDS_FILE
    assert check_cards.DB_PATH == paths.DB_FILE
