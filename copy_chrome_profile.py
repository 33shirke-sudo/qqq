"""Скопировать локальный Chrome-профиль в ``./browser_profile/Default``.

Используется режимом ``--browser-mode system``: чтобы Playwright мог
запустить Chrome с твоими cookies/расширениями, не конфликтуя с уже
открытым обычным Chrome (тот держит блокировку на ``%LOCALAPPDATA%
\\Google\\Chrome\\User Data\\Default``).

Этот скрипт делает одноразовое **копирование** профиля. Запусти его:

* при первой настройке (нет ``./browser_profile/Default``);
* когда хочешь обновить cookies/состояние из реального профиля.

Запуск (из каталога проекта):

    .venv\\Scripts\\python.exe copy_chrome_profile.py

Опции:
    --src PATH    явно указать путь к ``User Data\\Default``
                  (по умолчанию автоматически)
    --dst PATH    куда копировать (по умолчанию ./browser_profile/Default)
    --no-confirm  не спрашивать подтверждения, перезаписать сразу

ВАЖНО: ЗАКРОЙ обычный Chrome перед запуском — иначе на Windows
Chrome держит файл блокировки и копирование может пропустить часть
файлов.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from browser_modes import _system_profile_dir, get_chrome_user_data_default


# Файлы/директории, которые НЕ копируем (огромные кэши, бесполезные
# для cookies/расширений).
_SKIP_DIRS = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "Service Worker",
    "Crashpad",
    "Application Cache",
}


def _ignore(_: str, names: list[str]) -> list[str]:
    return [n for n in names if n in _SKIP_DIRS]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--src",
        type=Path,
        default=None,
        help="путь к Chrome 'User Data/Default' (по умолчанию автоматически)",
    )
    parser.add_argument(
        "--dst",
        type=Path,
        default=None,
        help="куда копировать (по умолчанию ./browser_profile/Default)",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="не спрашивать перед перезаписью существующей копии",
    )
    args = parser.parse_args()

    src = args.src or get_chrome_user_data_default()
    if src is None:
        print(
            "Не нашёл стандартный путь к Chrome User Data/Default. "
            "Передай его явно через --src.",
            file=sys.stderr,
        )
        return 1
    if not src.exists():
        print(f"Source path не существует: {src}", file=sys.stderr)
        return 1

    dst_root = args.dst or (_system_profile_dir() / "Default")
    dst_root.parent.mkdir(parents=True, exist_ok=True)

    if dst_root.exists():
        if not args.no_confirm:
            print(f"Каталог {dst_root} уже существует. Перезаписать? [y/N] ", end="")
            ans = input().strip().lower()
            if ans not in ("y", "yes"):
                print("отмена.")
                return 0
        print(f"Удаляю {dst_root}...")
        shutil.rmtree(dst_root)

    print(f"Копирую {src}\n      -> {dst_root}")
    print("(пропускаются: " + ", ".join(sorted(_SKIP_DIRS)) + ")")
    print("Если копирование споткнётся на отдельном файле — закрой Chrome и попробуй снова.")
    shutil.copytree(src, dst_root, ignore=_ignore, dirs_exist_ok=False, symlinks=False)
    print("Готово. Теперь можно запускать с --browser-mode system.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
