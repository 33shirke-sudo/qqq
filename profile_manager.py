"""Управление профилями Chrome с изоляцией для каждого экземпляра.

Этот модуль предоставляет ProfileManager для создания изолированных копий
профиля Chrome. Каждая копия независима и может использоваться параллельно
без конфликтов (cookies, localStorage, история).

Пример использования:
    with ProfileManager(source_profile=Path("./my_profile")) as pm:
        profile1 = pm.create_isolated_profile()
        profile2 = pm.create_isolated_profile()
        # Используем profile1 и profile2 в разных браузерах
    # Автоматическая очистка при выходе из контекста
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional


class ProfileManager:
    """Менеджер профилей Chrome с копированием для изоляции."""

    def __init__(self, source_profile: Optional[Path] = None):
        """Инициализация менеджера профилей.

        Args:
            source_profile: Путь к исходному профилю Chrome для копирования.
                           Если None - используется чистый профиль.
        """
        self.source_profile = source_profile
        self.temp_profiles: list[Path] = []

    def create_isolated_profile(self) -> Path:
        """Создать изолированную копию профиля для одного экземпляра браузера.

        Создает уникальную временную папку и копирует в нее исходный профиль
        (если указан). Исключает временные файлы, которые могут вызвать конфликты.

        Returns:
            Путь к временной копии профиля.
        """
        # Создаем уникальную временную папку
        temp_dir = Path(tempfile.gettempdir()) / f"chrome_profile_{uuid.uuid4().hex[:8]}"
        temp_dir.mkdir(parents=True, exist_ok=True)

        if self.source_profile and self.source_profile.exists():
            print(f"[ProfileManager] Копирование профиля из {self.source_profile} в {temp_dir}")
            # Копируем исходный профиль
            self._copy_profile(self.source_profile, temp_dir)
        else:
            print(f"[ProfileManager] Создан чистый профиль в {temp_dir}")

        self.temp_profiles.append(temp_dir)
        return temp_dir

    def _copy_profile(self, src: Path, dst: Path):
        """Копировать профиль Chrome, исключая временные файлы.

        Args:
            src: Исходная папка профиля
            dst: Целевая папка профиля
        """
        # Исключаем файлы, которые могут вызвать конфликты
        exclude_patterns = {
            'Singleton*',      # Файлы блокировки Chrome
            'lockfile',        # Файлы блокировки
            '*.lock',          # Все lock-файлы
            'chrome_debug.log',# Логи отладки
            'GPUCache',        # Кэш GPU (большой и не нужен)
            'ShaderCache',     # Кэш шейдеров (большой и не нужен)
            'Code Cache',      # Кэш кода (большой и не нужен)
        }

        def ignore_func(directory, contents):
            """Функция для shutil.copytree - что игнорировать."""
            ignored = []
            for item in contents:
                # Проверяем каждый паттерн
                for pattern in exclude_patterns:
                    item_path = Path(directory) / item
                    if item_path.match(pattern):
                        ignored.append(item)
                        break
            return ignored

        try:
            for item in src.iterdir():
                dst_item = dst / item.name

                # Пропускаем исключенные паттерны
                skip = False
                for pattern in exclude_patterns:
                    if item.match(pattern):
                        skip = True
                        break

                if skip:
                    continue

                if item.is_dir():
                    shutil.copytree(item, dst_item, ignore=ignore_func, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst_item)

        except Exception as e:
            print(f"[ProfileManager] Ошибка копирования профиля: {e}")
            raise

    def cleanup(self):
        """Удалить все временные профили."""
        for profile_path in self.temp_profiles:
            if profile_path.exists():
                try:
                    print(f"[ProfileManager] Удаление временного профиля {profile_path}")
                    shutil.rmtree(profile_path)
                except Exception as e:
                    print(f"[ProfileManager] Не удалось удалить {profile_path}: {e}")

        self.temp_profiles.clear()

    def __enter__(self):
        """Вход в контекстный менеджер."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Выход из контекстного менеджера с автоматической очисткой."""
        self.cleanup()
        return False


# Пример использования
if __name__ == "__main__":
    # Тест 1: Чистый профиль
    print("=== Тест 1: Чистый профиль ===")
    with ProfileManager() as pm:
        profile1 = pm.create_isolated_profile()
        profile2 = pm.create_isolated_profile()
        print(f"Создано 2 профиля: {profile1}, {profile2}")
        print(f"Профиль 1 существует: {profile1.exists()}")
        print(f"Профиль 2 существует: {profile2.exists()}")

    print(f"После cleanup профиль 1 существует: {profile1.exists()}")
    print(f"После cleanup профиль 2 существует: {profile2.exists()}")

    # Тест 2: Копирование существующего профиля
    print("\n=== Тест 2: Копирование профиля ===")
    source = Path("./browser_profile")
    if source.exists():
        with ProfileManager(source_profile=source) as pm:
            profile = pm.create_isolated_profile()
            print(f"Скопирован профиль в {profile}")
            print(f"Содержимое: {list(profile.iterdir())[:5]}...")
    else:
        print(f"Исходный профиль {source} не найден, пропускаем тест")
