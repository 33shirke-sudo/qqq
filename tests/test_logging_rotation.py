"""Тест P2-6: ``setup_logging`` использует ``RotatingFileHandler`` и при
переполнении файла создаёт бэкапы.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reset_logger() -> None:
    """Снять все handlers — иначе тесты «загрязняют» друг друга."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def test_setup_logging_uses_rotating_handler(tmp_path, monkeypatch) -> None:
    import logging_utils

    monkeypatch.setattr(logging_utils, "LOGS_DIR", tmp_path)
    _reset_logger()

    logger = logging_utils.setup_logging(debug=False, log_to_file=True)

    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1, "должен быть ровно один RotatingFileHandler"
    h = file_handlers[0]
    assert h.maxBytes == 10 * 1024 * 1024
    assert h.backupCount == 5
    # Проверяем, что путь файла внутри tmp_path и имя — qqq.log.
    assert Path(h.baseFilename).parent == tmp_path
    assert Path(h.baseFilename).name == "qqq.log"

    _reset_logger()


def test_setup_logging_creates_logs_dir(tmp_path, monkeypatch) -> None:
    import logging_utils

    logs_subdir = tmp_path / "logs"
    monkeypatch.setattr(logging_utils, "LOGS_DIR", logs_subdir)
    _reset_logger()

    logging_utils.setup_logging(debug=False, log_to_file=True)
    assert logs_subdir.is_dir()

    _reset_logger()


def test_cleanup_old_logs_skips_active_qqq_log(tmp_path, monkeypatch) -> None:
    """``cleanup_old_logs`` чистит legacy ``debug_*.log`` И ``qqq.log.*`` бэкапы,
    но НЕ трогает активный ``qqq.log`` (он держится RotatingFileHandler-ом)."""
    import os
    import time

    import logging_utils

    monkeypatch.setattr(logging_utils, "LOGS_DIR", tmp_path)

    # Создаём legacy timestamped и rotated файлы, делаем их старыми.
    old_time = time.time() - (40 * 24 * 60 * 60)
    legacy = tmp_path / "debug_2024-01-01_00-00-00.log"
    legacy.write_text("old")
    os.utime(legacy, (old_time, old_time))

    rotated = tmp_path / "qqq.log.1"
    rotated.write_text("rotated")
    os.utime(rotated, (old_time, old_time))

    # Активный файл — свежий.
    active = tmp_path / "qqq.log"
    active.write_text("fresh")

    deleted = logging_utils.cleanup_old_logs(days=30)
    assert deleted == 2, f"должны были удалить legacy + rotated, удалили {deleted}"
    assert not legacy.exists()
    assert not rotated.exists()
    assert active.exists(), "активный qqq.log не должен удаляться"
