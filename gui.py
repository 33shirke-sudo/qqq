"""GUI interface for account registration pipeline.

Provides a Tkinter-based interface to run create_emails.py, register_devin.py,
and add_identities.py with configurable parameters.
"""

import os
import sys
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import subprocess  # оставлен на случай standalone-запусков; шаги 1–4 идут in-process
import threading
import queue
import multiprocessing
from pathlib import Path
from storage import AccountDB
import pipeline_runner

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover — psutil опционален в unit-тестах
    psutil = None  # type: ignore

# P1-4: имена дочерних браузерных процессов, которые Camoufox/Playwright
# поднимают в Шаге 5. При stop_pipeline их надо убивать принуди-
# тельно, иначе окна Firefox/Chrome остаются висеть и держат lock-файлы
# на профилях — следующий запуск валится на ProfileLocked.
_BROWSER_PROC_NAMES = (
    "firefox", "firefox-bin", "firefox.exe",
    "chrome", "chrome.exe", "chromium", "chromium.exe",
    "camoufox", "camoufox.exe",
    "playwright", "playwright.exe",
)

DB_PATH = Path(__file__).parent / "accounts.db"
NICKS_FILE = Path(__file__).parent / "имена для имейлов.txt"

LOCALES = {
    "Южная Корея": "ko_KR",
    "США": "en_US",
    "Германия": "de_DE",
    "Сингапур": "en_SG",
    "Англия": "en_GB",
    "Греция": "el_GR"
}

class PipelineGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Account Registration Pipeline - GUI")
        self.root.geometry("900x800")

        self.processes = {}
        self.running = False
        # P1-7: stop-event для Step 5 (activate_trials_pipeline).
        # GUI взводит его в stop_pipeline() из main-треда; воркеры в
        # activate_trials проверяют .is_set() между аккаунтами и
        # завершают цикл. threading.Event удобен тем, что .set() —
        # thread-safe, в отличие от asyncio.Event (требует
        # loop.call_soon_threadsafe).
        self._step5_stop_event: threading.Event | None = None
        self.log_queue = queue.Queue()

        # Единое подключение к БД на жизнь GUI — были десятки
        # AccountDB(DB_PATH)/db.close() пар, по одной на каждый show_/refresh_-метод. AccountDB
        # внутри хранит threading.local() Connection, поэтому безопасно де-
        # лить его между GUI и фоновыми потоками шагов.
        self._db = AccountDB(DB_PATH)

        self.create_widgets()
        self.update_logs()

        # Корректное закрытие БД при выходе.
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        # При закрытии окна также убиваем оставшиеся браузерные процессы —
        # иначе на Windows lock-файлы профиля Firefox/Chrome держатся ещё
        # десятки секунд и мешают следующему запуску.
        try:
            self._kill_browser_descendants()
        except Exception:
            pass
        try:
            self._db.close()
        except Exception:
            pass
        self.root.destroy()

    def _kill_browser_descendants(self, grace_seconds: float = 2.0) -> int:
        """Принудительно убить все дочерние браузерные процессы текущего GUI.

        P1-4: ``process.terminate()`` шлёт SIGTERM только Python-у, а
        Playwright/Camoufox форкают ``firefox``/``chrome`` отдельными
        процессами. На Windows их штатно гасят через ``taskkill /T /F``;
        кросс-платформенный аналог — обход дерева детей через ``psutil``.

        Возвращает количество убитых процессов. Если psutil не установлен,
        тихо возвращает 0 и ничего не делает — это допустимый fallback для
        минимальных unit-окружений.
        """
        if psutil is None:
            return 0
        try:
            me = psutil.Process(os.getpid())
            descendants = me.children(recursive=True)
        except Exception:
            return 0

        targets = []
        for proc in descendants:
            try:
                name = (proc.name() or "").lower()
            except Exception:
                continue
            if any(name == n or name.startswith(n) for n in _BROWSER_PROC_NAMES):
                targets.append(proc)

        if not targets:
            return 0

        # Сначала SIGTERM всем, даём пару секунд на graceful exit,
        # потом добиваем выживших через SIGKILL.
        for proc in targets:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            gone, alive = psutil.wait_procs(targets, timeout=grace_seconds)
        except Exception:
            alive = targets
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            psutil.wait_procs(alive, timeout=grace_seconds)
        except Exception:
            pass
        return len(targets)

    def create_widgets(self):
        # Main container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)

        # Создаем Notebook для вкладок
        notebook = ttk.Notebook(main_frame)
        notebook.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=5)

        # Вкладка "Глобальные настройки"
        global_tab = ttk.Frame(notebook, padding="10")
        notebook.add(global_tab, text="Глобальные настройки")

        # Вкладка "Step 1"
        step1_tab = ttk.Frame(notebook, padding="10")
        notebook.add(step1_tab, text="Step 1: Create Emails")

        # Вкладка "Step 2"
        step2_tab = ttk.Frame(notebook, padding="10")
        notebook.add(step2_tab, text="Step 2: Register Devin")

        # Вкладка "Step 3"
        step3_tab = ttk.Frame(notebook, padding="10")
        notebook.add(step3_tab, text="Step 3: Add Identities")

        # Вкладка "Step 4"
        step4_tab = ttk.Frame(notebook, padding="10")
        notebook.add(step4_tab, text="Step 4: Check Cards")

        # Вкладка "Step 5"
        step5_tab = ttk.Frame(notebook, padding="10")
        notebook.add(step5_tab, text="Step 5: Activate Trials")

        # === Глобальные настройки (в global_tab) ===
        self.use_global = tk.BooleanVar(value=True)
        ttk.Checkbutton(global_tab, text="Применить ко всем шагам",
                       variable=self.use_global, command=self.toggle_global).grid(row=0, column=0, columnspan=4, sticky=tk.W)

        ttk.Label(global_tab, text="Workers:").grid(row=1, column=0, sticky=tk.W, padx=5)
        self.global_workers = tk.StringVar(value="5")
        ttk.Entry(global_tab, textvariable=self.global_workers, width=10).grid(row=1, column=1, padx=5)

        ttk.Label(global_tab, text="Limit:").grid(row=1, column=2, sticky=tk.W, padx=5)
        self.global_limit = tk.StringVar(value="100")
        ttk.Entry(global_tab, textvariable=self.global_limit, width=10).grid(row=1, column=3, padx=5)

        ttk.Label(global_tab, text="Delay:").grid(row=2, column=0, sticky=tk.W, padx=5)
        self.global_delay = tk.StringVar(value="5.0")
        ttk.Entry(global_tab, textvariable=self.global_delay, width=10).grid(row=2, column=1, padx=5)

        self.global_head = tk.BooleanVar(value=False)
        ttk.Checkbutton(global_tab, text="Show browser (--head)",
                       variable=self.global_head).grid(row=2, column=2, columnspan=2, sticky=tk.W, padx=5)

        self.global_debug = tk.BooleanVar(value=False)
        ttk.Checkbutton(global_tab, text="Debug mode",
                       variable=self.global_debug).grid(row=3, column=0, columnspan=2, sticky=tk.W, padx=5)

        # === Step 1: Create Emails (в step1_tab) ===
        step1_frame = ttk.LabelFrame(step1_tab, text="Настройки", padding="10")
        step1_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=5)

        self.step1_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(step1_frame, text="Запустить этот шаг",
                       variable=self.step1_enabled).grid(row=0, column=0, sticky=tk.W)

        # Входные данные
        ttk.Label(step1_frame, text="Файл с никами:").grid(row=1, column=0, sticky=tk.W, padx=5)
        self.step1_nicks_file = tk.StringVar(value="имена для имейлов.txt")
        ttk.Entry(step1_frame, textvariable=self.step1_nicks_file, width=40).grid(row=1, column=1, columnspan=2, sticky=(tk.W, tk.E), padx=5)
        ttk.Button(step1_frame, text="Выбрать...", command=self.select_step1_nicks).grid(row=1, column=3, padx=5)

        self.step1_custom = tk.BooleanVar(value=False)
        ttk.Checkbutton(step1_frame, text="Использовать свои настройки",
                       variable=self.step1_custom, command=self.toggle_step1).grid(row=2, column=0, sticky=tk.W)

        self.step1_settings_frame = ttk.Frame(step1_frame)
        self.step1_settings_frame.grid(row=3, column=0, sticky=(tk.W, tk.E))

        ttk.Label(self.step1_settings_frame, text="Workers:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.step1_workers = tk.StringVar(value="3")
        self.step1_workers_entry = ttk.Entry(self.step1_settings_frame, textvariable=self.step1_workers, width=10, state='disabled')
        self.step1_workers_entry.grid(row=0, column=1, padx=5)

        ttk.Label(self.step1_settings_frame, text="Limit:").grid(row=0, column=2, sticky=tk.W, padx=5)
        self.step1_limit = tk.StringVar(value="50")
        self.step1_limit_entry = ttk.Entry(self.step1_settings_frame, textvariable=self.step1_limit, width=10, state='disabled')
        self.step1_limit_entry.grid(row=0, column=3, padx=5)

        ttk.Label(self.step1_settings_frame, text="Delay:").grid(row=1, column=0, sticky=tk.W, padx=5)
        self.step1_delay = tk.StringVar(value="3.0")
        self.step1_delay_entry = ttk.Entry(self.step1_settings_frame, textvariable=self.step1_delay, width=10, state='disabled')
        self.step1_delay_entry.grid(row=1, column=1, padx=5)

        self.step1_head = tk.BooleanVar(value=False)
        self.step1_head_check = ttk.Checkbutton(self.step1_settings_frame, text="Show browser",
                                                variable=self.step1_head, state='disabled')
        self.step1_head_check.grid(row=1, column=2, columnspan=2, sticky=tk.W, padx=5)

        self.step1_debug = tk.BooleanVar(value=False)
        self.step1_debug_check = ttk.Checkbutton(self.step1_settings_frame, text="Debug",
                                                 variable=self.step1_debug, state='disabled')
        self.step1_debug_check.grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=5)

        # Кнопка просмотра данных
        ttk.Button(step1_frame, text="Просмотр email аккаунтов",
                  command=self.show_emails).grid(row=4, column=0, sticky=tk.W, pady=10)

        # === Step 2: Register Devin (в step2_tab) ===
        step2_frame = ttk.LabelFrame(step2_tab, text="Настройки", padding="10")
        step2_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=5)

        self.step2_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(step2_frame, text="Запустить этот шаг",
                       variable=self.step2_enabled).grid(row=0, column=0, sticky=tk.W)

        self.step2_custom = tk.BooleanVar(value=False)
        ttk.Checkbutton(step2_frame, text="Использовать свои настройки",
                       variable=self.step2_custom, command=self.toggle_step2).grid(row=1, column=0, sticky=tk.W)

        self.step2_settings_frame = ttk.Frame(step2_frame)
        self.step2_settings_frame.grid(row=2, column=0, sticky=(tk.W, tk.E))

        ttk.Label(self.step2_settings_frame, text="Workers:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.step2_workers = tk.StringVar(value="5")
        self.step2_workers_entry = ttk.Entry(self.step2_settings_frame, textvariable=self.step2_workers, width=10, state='disabled')
        self.step2_workers_entry.grid(row=0, column=1, padx=5)

        ttk.Label(self.step2_settings_frame, text="Limit:").grid(row=0, column=2, sticky=tk.W, padx=5)
        self.step2_limit = tk.StringVar(value="100")
        self.step2_limit_entry = ttk.Entry(self.step2_settings_frame, textvariable=self.step2_limit, width=10, state='disabled')
        self.step2_limit_entry.grid(row=0, column=3, padx=5)

        ttk.Label(self.step2_settings_frame, text="Delay:").grid(row=1, column=0, sticky=tk.W, padx=5)
        self.step2_delay = tk.StringVar(value="5.0")
        self.step2_delay_entry = ttk.Entry(self.step2_settings_frame, textvariable=self.step2_delay, width=10, state='disabled')
        self.step2_delay_entry.grid(row=1, column=1, padx=5)

        self.step2_head = tk.BooleanVar(value=False)
        self.step2_head_check = ttk.Checkbutton(self.step2_settings_frame, text="Show browser",
                                                variable=self.step2_head, state='disabled')
        self.step2_head_check.grid(row=1, column=2, columnspan=2, sticky=tk.W, padx=5)

        self.step2_debug = tk.BooleanVar(value=False)
        self.step2_debug_check = ttk.Checkbutton(self.step2_settings_frame, text="Debug",
                                                 variable=self.step2_debug, state='disabled')
        self.step2_debug_check.grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=5)

        # Кнопка просмотра данных
        ttk.Button(step2_frame, text="Просмотр Devin аккаунтов",
                  command=self.show_devin_accounts).grid(row=3, column=0, sticky=tk.W, pady=10)

        # === Step 3: Generate Identities (в step3_tab) ===
        step3_frame = ttk.LabelFrame(step3_tab, text="Настройки", padding="10")
        step3_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=5)

        self.step3_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(step3_frame, text="Запустить этот шаг",
                       variable=self.step3_enabled).grid(row=0, column=0, sticky=tk.W)

        self.step3_parallel = tk.BooleanVar(value=False)
        ttk.Checkbutton(step3_frame, text="Запустить параллельно с другими шагами",
                       variable=self.step3_parallel).grid(row=0, column=1, sticky=tk.W, padx=20)

        self.step3_custom = tk.BooleanVar(value=False)
        ttk.Checkbutton(step3_frame, text="Использовать свои настройки",
                       variable=self.step3_custom, command=self.toggle_step3).grid(row=1, column=0, sticky=tk.W)

        self.step3_settings_frame = ttk.Frame(step3_frame)
        self.step3_settings_frame.grid(row=2, column=0, sticky=(tk.W, tk.E))

        ttk.Label(self.step3_settings_frame, text="Limit:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.step3_limit = tk.StringVar(value="100")
        self.step3_limit_entry = ttk.Entry(self.step3_settings_frame, textvariable=self.step3_limit, width=10, state='disabled')
        self.step3_limit_entry.grid(row=0, column=1, padx=5)

        ttk.Label(self.step3_settings_frame, text="Страна:").grid(row=0, column=2, sticky=tk.W, padx=5)
        self.step3_locale = tk.StringVar(value="Греция")
        self.step3_locale_combo = ttk.Combobox(
            self.step3_settings_frame,
            textvariable=self.step3_locale,
            values=list(LOCALES.keys()),
            width=15,
            state='disabled'
        )
        self.step3_locale_combo.grid(row=0, column=3, padx=5)

        # Кнопка просмотра данных
        ttk.Button(step3_frame, text="Просмотр личностей",
                  command=self.show_identities).grid(row=3, column=0, sticky=tk.W, pady=10)

        # === Step 4: Check Cards (в step4_tab) ===
        step4_frame = ttk.LabelFrame(step4_tab, text="Настройки", padding="10")
        step4_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=5)

        self.step4_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(step4_frame, text="Запустить этот шаг",
                       variable=self.step4_enabled).grid(row=0, column=0, sticky=tk.W)

        self.step4_parallel = tk.BooleanVar(value=False)
        ttk.Checkbutton(step4_frame, text="Запустить параллельно с другими шагами",
                       variable=self.step4_parallel).grid(row=0, column=1, sticky=tk.W, padx=20)

        # Входные данные
        ttk.Label(step4_frame, text="Файл с BIN-кодами:").grid(row=1, column=0, sticky=tk.W, padx=5)
        self.step4_bins_file = tk.StringVar(value="бины.txt")
        ttk.Entry(step4_frame, textvariable=self.step4_bins_file, width=40).grid(row=1, column=1, columnspan=2, sticky=(tk.W, tk.E), padx=5)
        ttk.Button(step4_frame, text="Выбрать...", command=self.select_step4_bins).grid(row=1, column=3, padx=5)

        self.step4_custom = tk.BooleanVar(value=False)
        ttk.Checkbutton(step4_frame, text="Использовать свои настройки",
                       variable=self.step4_custom, command=self.toggle_step4).grid(row=2, column=0, sticky=tk.W)

        self.step4_settings_frame = ttk.Frame(step4_frame)
        self.step4_settings_frame.grid(row=3, column=0, sticky=(tk.W, tk.E))

        ttk.Label(self.step4_settings_frame, text="Quantity:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.step4_quantity = tk.StringVar(value="10")
        self.step4_quantity_entry = ttk.Entry(self.step4_settings_frame, textvariable=self.step4_quantity, width=10, state='disabled')
        self.step4_quantity_entry.grid(row=0, column=1, padx=5)

        ttk.Label(self.step4_settings_frame, text="Tabs:").grid(row=0, column=2, sticky=tk.W, padx=5)
        self.step4_tabs = tk.StringVar(value="3")
        self.step4_tabs_entry = ttk.Entry(self.step4_settings_frame, textvariable=self.step4_tabs, width=10, state='disabled')
        self.step4_tabs_entry.grid(row=0, column=3, padx=5)

        ttk.Label(self.step4_settings_frame, text="Timeout:").grid(row=1, column=0, sticky=tk.W, padx=5)
        self.step4_timeout = tk.StringVar(value="1200")
        self.step4_timeout_entry = ttk.Entry(self.step4_settings_frame, textvariable=self.step4_timeout, width=10, state='disabled')
        self.step4_timeout_entry.grid(row=1, column=1, padx=5)

        self.step4_head = tk.BooleanVar(value=True)
        self.step4_head_check = ttk.Checkbutton(self.step4_settings_frame, text="Show browser",
                                                variable=self.step4_head, state='disabled')
        self.step4_head_check.grid(row=1, column=2, columnspan=2, sticky=tk.W, padx=5)

        self.step4_no_recheck = tk.BooleanVar(value=False)
        self.step4_no_recheck_check = ttk.Checkbutton(self.step4_settings_frame, text="No recheck",
                                                       variable=self.step4_no_recheck, state='disabled')
        self.step4_no_recheck_check.grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=5)

        self.step4_debug = tk.BooleanVar(value=False)
        self.step4_debug_check = ttk.Checkbutton(self.step4_settings_frame, text="Debug",
                                                 variable=self.step4_debug, state='disabled')
        self.step4_debug_check.grid(row=2, column=2, columnspan=2, sticky=tk.W, padx=5)

        # Кнопка просмотра данных
        ttk.Button(step4_frame, text="Просмотр живых карт",
                  command=self.show_live_cards).grid(row=4, column=0, sticky=tk.W, pady=10)

        # === Step 5: Activate Trials (в step5_tab) ===
        step5_frame = ttk.LabelFrame(step5_tab, text="Настройки", padding="10")
        step5_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=5)
        self.step5_frame = step5_frame

        self.step5_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(step5_frame, text="Запустить этот шаг",
                       variable=self.step5_enabled).grid(row=0, column=0, sticky=tk.W)

        self.step5_custom = tk.BooleanVar(value=False)
        ttk.Checkbutton(step5_frame, text="Использовать свои настройки",
                       variable=self.step5_custom, command=self.toggle_step5).grid(row=1, column=0, sticky=tk.W)

        self.step5_settings_frame = ttk.Frame(step5_frame)
        self.step5_settings_frame.grid(row=2, column=0, sticky=(tk.W, tk.E))

        ttk.Label(self.step5_settings_frame, text="Параллельных браузеров:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.step5_parallel = tk.StringVar(value="3")
        self.step5_parallel_entry = ttk.Entry(self.step5_settings_frame, textvariable=self.step5_parallel, width=10, state='disabled')
        self.step5_parallel_entry.grid(row=0, column=1, padx=5)

        ttk.Label(self.step5_settings_frame, text="Таймаут (сек):").grid(row=0, column=2, sticky=tk.W, padx=5)
        self.step5_timeout = tk.StringVar(value="300")
        self.step5_timeout_entry = ttk.Entry(self.step5_settings_frame, textvariable=self.step5_timeout, width=10, state='disabled')
        self.step5_timeout_entry.grid(row=0, column=3, padx=5)

        ttk.Label(self.step5_settings_frame, text="Профиль Chrome:").grid(row=1, column=0, sticky=tk.W, padx=5)
        self.step5_profile_path = tk.StringVar(value="")
        self.step5_profile_entry = ttk.Entry(self.step5_settings_frame, textvariable=self.step5_profile_path, width=40, state='disabled')
        self.step5_profile_entry.grid(row=1, column=1, columnspan=2, sticky=(tk.W, tk.E), padx=5)

        self.step5_profile_btn = ttk.Button(self.step5_settings_frame, text="Выбрать папку...",
                                           command=self.select_chrome_profile, state='disabled')
        self.step5_profile_btn.grid(row=1, column=3, padx=5)

        # Кнопка просмотра данных
        ttk.Button(step5_frame, text="Просмотр активированных триалов",
                  command=self.show_activated_trials).grid(row=3, column=0, sticky=tk.W, pady=10)

        # === Control buttons (внизу main_frame, после notebook) ===
        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=10)

        self.btn_run = ttk.Button(btn_frame, text="Запустить выбранные шаги", command=self.run_pipeline)
        self.btn_run.pack(side=tk.LEFT, padx=5)

        self.btn_stop = ttk.Button(btn_frame, text="Остановить", command=self.stop_pipeline, state='disabled')
        self.btn_stop.pack(side=tk.LEFT, padx=5)

        # === Progress (внизу main_frame) ===
        progress_frame = ttk.LabelFrame(main_frame, text="Прогресс", padding="10")
        progress_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), pady=5)

        ttk.Label(progress_frame, text="Step 1:").grid(row=0, column=0, sticky=tk.W)
        self.progress1 = ttk.Progressbar(progress_frame, length=400, mode='determinate')
        self.progress1.grid(row=0, column=1, padx=5)
        self.label1 = ttk.Label(progress_frame, text="0/0")
        self.label1.grid(row=0, column=2, padx=5)

        ttk.Label(progress_frame, text="Step 2:").grid(row=1, column=0, sticky=tk.W)
        self.progress2 = ttk.Progressbar(progress_frame, length=400, mode='determinate')
        self.progress2.grid(row=1, column=1, padx=5)
        self.label2 = ttk.Label(progress_frame, text="0/0")
        self.label2.grid(row=1, column=2, padx=5)

        ttk.Label(progress_frame, text="Step 3:").grid(row=2, column=0, sticky=tk.W)
        self.progress3 = ttk.Progressbar(progress_frame, length=400, mode='determinate')
        self.progress3.grid(row=2, column=1, padx=5)
        self.label3 = ttk.Label(progress_frame, text="0/0")
        self.label3.grid(row=2, column=2, padx=5)

        ttk.Label(progress_frame, text="Step 4:").grid(row=3, column=0, sticky=tk.W)
        self.progress4 = ttk.Progressbar(progress_frame, length=400, mode='determinate')
        self.progress4.grid(row=3, column=1, padx=5)
        self.label4 = ttk.Label(progress_frame, text="0 cards")
        self.label4.grid(row=3, column=2, padx=5)

        ttk.Label(progress_frame, text="Step 5:").grid(row=4, column=0, sticky=tk.W)
        self.progress5 = ttk.Progressbar(progress_frame, length=400, mode='determinate')
        self.progress5.grid(row=4, column=1, padx=5)
        self.label5 = ttk.Label(progress_frame, text="0/0")
        self.label5.grid(row=4, column=2, padx=5)

        # === Logs (внизу main_frame) ===
        log_frame = ttk.LabelFrame(main_frame, text="Логи", padding="10")
        log_frame.grid(row=3, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=5)
        main_frame.rowconfigure(3, weight=1)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, state='disabled')
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # === Bottom buttons (внизу main_frame) ===
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.grid(row=4, column=0, sticky=(tk.W, tk.E), pady=5)

        ttk.Button(bottom_frame, text="Просмотр БД", command=self.show_database).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Экспорт в .txt", command=self.export_txt).pack(side=tk.LEFT, padx=5)

    def toggle_global(self):
        pass

    def toggle_step1(self):
        state = 'normal' if self.step1_custom.get() else 'disabled'
        self.step1_workers_entry.config(state=state)
        self.step1_limit_entry.config(state=state)
        self.step1_delay_entry.config(state=state)
        self.step1_head_check.config(state=state)
        self.step1_debug_check.config(state=state)

    def toggle_step2(self):
        state = 'normal' if self.step2_custom.get() else 'disabled'
        self.step2_workers_entry.config(state=state)
        self.step2_limit_entry.config(state=state)
        self.step2_delay_entry.config(state=state)
        self.step2_head_check.config(state=state)
        self.step2_debug_check.config(state=state)

    def toggle_step3(self):
        state = 'readonly' if self.step3_custom.get() else 'disabled'
        self.step3_limit_entry.config(state='normal' if self.step3_custom.get() else 'disabled')
        self.step3_locale_combo.config(state=state)

    def toggle_step4(self):
        state = 'normal' if self.step4_custom.get() else 'disabled'
        self.step4_quantity_entry.config(state=state)
        self.step4_tabs_entry.config(state=state)
        self.step4_timeout_entry.config(state=state)
        self.step4_head_check.config(state=state)
        self.step4_no_recheck_check.config(state=state)
        self.step4_debug_check.config(state=state)

    def toggle_step5(self):
        """Включить/выключить настройки Step 5."""
        state = 'normal' if self.step5_custom.get() else 'disabled'
        self.step5_parallel_entry.config(state=state)
        self.step5_timeout_entry.config(state=state)
        self.step5_profile_entry.config(state=state)
        self.step5_profile_btn.config(state=state)

    def select_chrome_profile(self):
        """Выбрать папку профиля Chrome."""
        from tkinter import filedialog

        folder = filedialog.askdirectory(
            title="Выберите папку профиля Chrome",
            initialdir=os.path.expanduser("~")
        )

        if folder:
            self.step5_profile_path.set(folder)
            self.log(f"Выбран профиль Chrome: {folder}")

    def select_step1_nicks(self):
        """Выбрать файл с никами для Step 1."""
        from tkinter import filedialog

        file = filedialog.askopenfilename(
            title="Выберите файл с никами",
            initialdir=str(Path(__file__).parent),
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )

        if file:
            self.step1_nicks_file.set(file)
            self.log(f"Выбран файл с никами: {file}")

    def select_step4_bins(self):
        """Выбрать файл с BIN-кодами для Step 4."""
        from tkinter import filedialog

        file = filedialog.askopenfilename(
            title="Выберите файл с BIN-кодами",
            initialdir=str(Path(__file__).parent),
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )

        if file:
            self.step4_bins_file.set(file)
            self.log(f"Выбран файл с BIN-кодами: {file}")

    # ——— Валидация входных данных ——— ----------------------------------------

    @staticmethod
    def _parse_positive_int(value: str, field: str) -> int:
        """Парсить строку как положительное int. Поднимает ValueError с
        читаемым сообщением с указанием поля."""
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"Поле '{field}' должно быть целым числом, получено: {value!r}")
        if n <= 0:
            raise ValueError(f"Поле '{field}' должно быть > 0, получено {n}")
        return n

    def _validate_inputs(self, sequential_steps, parallel_steps, step5_enabled) -> str | None:
        """Проверить входные перед запуском пайплайна.

        Возвращает сообщение об ошибке или ``None``, если всё ок.
        """
        # Числовые поля глобальных настроек.
        try:
            self._parse_positive_int(self.global_workers.get(), 'Workers (global)')
            self._parse_positive_int(self.global_limit.get(), 'Limit (global)')
            self._parse_positive_int(self.global_delay.get(), 'Worker delay (global)')
        except ValueError as e:
            return str(e)

        # Степовые (если enable custom).
        if self.step1_enabled.get() and self.step1_custom.get():
            try:
                self._parse_positive_int(self.step1_workers.get(), 'Step1 Workers')
                self._parse_positive_int(self.step1_limit.get(), 'Step1 Limit')
                self._parse_positive_int(self.step1_delay.get(), 'Step1 Worker delay')
            except ValueError as e:
                return str(e)
        if self.step2_enabled.get() and self.step2_custom.get():
            try:
                self._parse_positive_int(self.step2_workers.get(), 'Step2 Workers')
                self._parse_positive_int(self.step2_limit.get(), 'Step2 Limit')
                self._parse_positive_int(self.step2_delay.get(), 'Step2 Worker delay')
            except ValueError as e:
                return str(e)
        if self.step3_enabled.get() and self.step3_custom.get():
            try:
                self._parse_positive_int(self.step3_limit.get(), 'Step3 Limit')
            except ValueError as e:
                return str(e)
        if self.step4_enabled.get() and self.step4_custom.get():
            try:
                self._parse_positive_int(self.step4_quantity.get(), 'Step4 Quantity')
                self._parse_positive_int(self.step4_tabs.get(), 'Step4 Tabs')
                self._parse_positive_int(self.step4_timeout.get(), 'Step4 Timeout')
            except ValueError as e:
                return str(e)
        if step5_enabled and self.step5_custom.get():
            try:
                self._parse_positive_int(self.step5_parallel.get(), 'Step5 Parallel')
                self._parse_positive_int(self.step5_timeout.get(), 'Step5 Timeout')
            except ValueError as e:
                return str(e)

        # Файлы: ники для шага 1 и BIN-коды для шага 4.
        if self.step1_enabled.get():
            nicks_path = Path(self.step1_nicks_file.get())
            if not nicks_path.is_absolute():
                nicks_path = Path(__file__).parent / nicks_path
            if not nicks_path.exists():
                return f"Файл с никами не найден: {nicks_path}"
        if self.step4_enabled.get():
            bins_path = Path(self.step4_bins_file.get())
            if not bins_path.is_absolute():
                bins_path = Path(__file__).parent / bins_path
            if not bins_path.exists():
                return f"Файл с BIN-кодами не найден: {bins_path}"

        # P1-3: Шаг 3 берёт email-ы из БД, которые пишет
        # Step 1 → Step 2. Если запустить Шаг 3 параллельно с
        # 1/2, ему достанется пустой pending. Шаг 4 формально
        # независим (читает бины из .txt) — для него хватает уже
        # сделанной выше проверки наличия bins-файла.
        if self.step3_enabled.get() and self.step3_parallel.get():
            if self.step1_enabled.get() or self.step2_enabled.get():
                return (
                    "Шаг 3 нельзя запускать параллельно при включённых Шагах 1/2:\n"
                    "ему нужны email-ы из их БД-записей. Снимите галку\n"
                    "\"Параллельный запуск\" у Шага 3 или отключите Шаги 1/2."
                )

        # Хотя бы один шаг включён.
        if not sequential_steps and not parallel_steps and not step5_enabled:
            return "Выберите хотя бы один шаг для запуска"

        return None

    def get_step1_args(self):
        args = []
        # Путь к файлу с никами
        args.extend(['--nicks-file', self.step1_nicks_file.get()])

        if self.step1_custom.get():
            args.extend(['--workers', self.step1_workers.get()])
            args.extend(['--limit', self.step1_limit.get()])
            args.extend(['--worker-delay', self.step1_delay.get()])
            if self.step1_head.get():
                args.append('--head')
            if self.step1_debug.get():
                args.append('--debug')
        else:
            args.extend(['--workers', self.global_workers.get()])
            args.extend(['--limit', self.global_limit.get()])
            args.extend(['--worker-delay', self.global_delay.get()])
            if self.global_head.get():
                args.append('--head')
            if self.global_debug.get():
                args.append('--debug')
        return args

    def get_step2_args(self):
        args = []
        if self.step2_custom.get():
            args.extend(['--workers', self.step2_workers.get()])
            args.extend(['--limit', self.step2_limit.get()])
            args.extend(['--worker-delay', self.step2_delay.get()])
            if self.step2_head.get():
                args.append('--head')
            if self.step2_debug.get():
                args.append('--debug')
        else:
            args.extend(['--workers', self.global_workers.get()])
            args.extend(['--limit', self.global_limit.get()])
            args.extend(['--worker-delay', self.global_delay.get()])
            if self.global_head.get():
                args.append('--head')
            if self.global_debug.get():
                args.append('--debug')
        return args

    def get_step3_args(self):
        args = []
        if self.step3_custom.get():
            args.extend(['--limit', self.step3_limit.get()])
            country = self.step3_locale.get()
            locale_code = LOCALES.get(country, 'el_GR')
            args.extend(['--locale', locale_code])
        else:
            args.extend(['--limit', self.global_limit.get()])
            args.append('--locale=el_GR')
        return args

    def get_step4_args(self):
        args = []
        # Путь к файлу с BIN-кодами
        args.extend(['--bins-file', self.step4_bins_file.get()])

        if self.step4_custom.get():
            args.extend(['--quantity', self.step4_quantity.get()])
            args.extend(['--tabs', self.step4_tabs.get()])
            args.extend(['--check-timeout', self.step4_timeout.get()])
            if not self.step4_head.get():
                args.append('--headless')
            if self.step4_no_recheck.get():
                args.append('--no-recheck')
            if self.step4_debug.get():
                args.append('--debug')
        else:
            args.extend(['--quantity', '10'])
            args.extend(['--tabs', '3'])
            args.extend(['--check-timeout', '1200'])
            if not self.global_head.get():
                args.append('--headless')
            if self.global_debug.get():
                args.append('--debug')
        return args

    def get_step5_args(self):
        """Получить аргументы для Step 5."""
        if self.step5_custom.get():
            return {
                'parallel': int(self.step5_parallel.get()),
                'timeout': int(self.step5_timeout.get()),
                'profile_path': self.step5_profile_path.get() or None,
            }
        else:
            # Используем глобальные настройки
            return {
                'parallel': int(self.global_workers.get()),
                'timeout': 300,  # default timeout
                'profile_path': None,
            }

    def log(self, message):
        """Добавить сообщение в лог."""
        self.log_queue.put(message)

    def run_pipeline(self):
        if self.running:
            messagebox.showwarning("Уже запущено", "Пайплайн уже выполняется")
            return

        # Разделяем шаги на последовательные и параллельные
        sequential_steps = []
        parallel_steps = []

        if self.step1_enabled.get():
            sequential_steps.append(('create_emails.py', self.get_step1_args()))
        if self.step2_enabled.get():
            sequential_steps.append(('register_devin.py', self.get_step2_args()))

        if self.step3_enabled.get():
            if self.step3_parallel.get():
                parallel_steps.append(('add_identities.py', self.get_step3_args()))
            else:
                sequential_steps.append(('add_identities.py', self.get_step3_args()))

        if self.step4_enabled.get():
            if self.step4_parallel.get():
                parallel_steps.append(('check_cards.py', self.get_step4_args()))
            else:
                sequential_steps.append(('check_cards.py', self.get_step4_args()))

        # Step 5 всегда последовательный (после всех остальных)
        step5_enabled = self.step5_enabled.get()
        # Аргументы Step 5 читаем из Tk-vars ЗДЕСЬ, в main-thread (P0-4):
        # если бы их читал _run_step5 из background-потока, .get() на
        # Tk-переменных могли давать гонку с main loop.
        step5_args = self.get_step5_args() if step5_enabled else None

        # Валидация всех числовых полей и файлов до старта потоков.
        err = self._validate_inputs(sequential_steps, parallel_steps, step5_enabled)
        if err is not None:
            messagebox.showerror("Неверные данные", err)
            return

        self.running = True
        self.btn_run.config(state='disabled')
        self.btn_stop.config(state='normal')

        # Запускаем последовательные шаги, затем Step 5
        thread = threading.Thread(
            target=self._run_steps,
            args=(sequential_steps, step5_enabled, step5_args),
        )
        thread.daemon = True
        thread.start()

        # Запускаем параллельные шаги
        for script, args in parallel_steps:
            parallel_thread = threading.Thread(target=self._run_single_step, args=(script, args))
            parallel_thread.daemon = True
            parallel_thread.start()

        self.update_progress()

    def _run_steps(self, steps, run_step5=False, step5_args=None):
        """Последовательный запуск шагов 1–4 в одном процессе.

        Раньше тут было ``subprocess.Popen([sys.executable, "script.py"])``,
        что в PyInstaller-сборке запускало копию GUI вместо шага (``.venv\\
        Scripts\\python.exe`` отсутствует в exe-дистрибутиве). Теперь
        каждый шаг вызывается напрямую через ``pipeline_runner.run_step``,
        а stdout/stderr/logging захватываются в ``self.log_queue``.
        """
        for script, args in steps:
            if not self.running:
                break
            try:
                pipeline_runner.run_step(script, args, self.log_queue)
            except Exception as e:
                self.log_queue.put(f"\nОшибка запуска {script}: {e}\n")

        # После всех последовательных шагов запускаем Step 5 (если включен)
        if run_step5 and self.running:
            self._run_step5(step5_args)

        self.running = False
        self.root.after(0, self._on_pipeline_finished)

    def _run_single_step(self, script, args):
        """Запуск одного шага в параллельном фоновом потоке.

        Каждый параллельный шаг крутится в своём потоке; ``pipeline_runner``
        внутри ставит общий захват ``sys.stdout`` на время вызова, поэтому
        логи всех параллельных шагов попадают в ``log_queue`` корректно
        (запись построчная, не пересекающаяся).
        """
        self.log_queue.put(f"\n=== Параллельный запуск {script} ===\n")
        try:
            pipeline_runner.run_step(script, args, self.log_queue)
        except Exception as e:
            self.log_queue.put(f"\nОшибка запуска {script}: {e}\n")

    def _ui(self, fn):
        """Безопасно выполнить ``fn`` в main-thread Tk (для вызовов из
        background-потоков). Tk не thread-safe — любые касания виджетов
        вне main loop приводят к гонкам и зависаниям на Windows.
        """
        self.root.after(0, fn)

    def _run_step5(self, step5_args=None):
        """Запуск Step 5: Activate Trials через activate_trials.py.

        Args:
            step5_args: dict с ключами ``parallel``, ``timeout``,
                ``profile_path`` — собранный в main-thread'е через
                :meth:`get_step5_args`. Если ``None`` (обратная
                совместимость с прежним API), читаем Tk-vars прямо
                отсюда, но это не thread-safe — оставлено только на
                переходный период.
        """
        import asyncio
        from pathlib import Path
        from storage import AccountDB
        from activate_trials import activate_trials_pipeline

        self.log_queue.put(f"\n=== Запуск Step 5: Activate Trials ===\n")

        try:
            args = step5_args if step5_args is not None else self.get_step5_args()
            profile_path = Path(args['profile_path']) if args['profile_path'] else None

            # Обновляем прогресс-бар (Tk — только из main thread)
            self._ui(lambda: self.progress5.configure(value=0))
            self._ui(lambda: self.label5.configure(text="Запуск..."))

            # Запускаем async функцию
            db = self._db

            # P1-7: threading.Event — GUI выставляет его из main-треда
            # в stop_pipeline, воркеры в activate_trials._worker между
            # аккаунтами зовут .is_set() и выходят из цикла.
            self._step5_stop_event = threading.Event()

            # Создаем новый event loop для этого потока
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                stats = loop.run_until_complete(
                    activate_trials_pipeline(
                        db=db,
                        parallel=args['parallel'],
                        timeout=args['timeout'],
                        profile_path=profile_path,
                        stop_event=self._step5_stop_event,
                    )
                )

                # Обновляем прогресс
                self._ui(lambda: self.progress5.configure(value=100))
                self._ui(
                    lambda s=stats: self.label5.configure(
                        text=f"{s['success']}/{s['total']}"
                    )
                )

                self.log_queue.put(
                    f"\n=== Step 5 завершён: {stats['success']} успешно, "
                    f"{stats['failed']} ошибок ===\n"
                )
            finally:
                loop.close()
                self._step5_stop_event = None

        except Exception as e:
            self.log_queue.put(f"\nОшибка Step 5: {e}\n")
            import traceback
            self.log_queue.put(traceback.format_exc())
            self._ui(lambda: self.progress5.configure(value=0))
            self._ui(lambda: self.label5.configure(text="Ошибка"))

    def _on_pipeline_finished(self):
        self.btn_run.config(state='normal')
        self.btn_stop.config(state='disabled')
        self.log_queue.put("\n=== Пайплайн завершён ===\n")

    def stop_pipeline(self):
        if not self.running:
            return

        # Шаги 1–4 теперь in-process — ``terminate`` нет; ставим флаг.
        # Между шагами _run_steps проверяет self.running и прерывает
        # цикл. Внутри уже запущенного шага (Шаги 1–4) корректная
        # отмена ещё впереди (нужно tracking threading.Event в
        # pipeline_runner). Для Шага 5 (P1-7) — взводим stop-event
        # ниже.
        self.running = False

        # P1-7: если сейчас крутится Step 5 (activate_trials), даём
        # воркерам сигнал «закончите текущий аккаунт и выходите».
        # threading.Event.set() — thread-safe, не нужно прокидывать
        # на event loop через call_soon_threadsafe.
        step5_stop = self._step5_stop_event
        if step5_stop is not None:
            step5_stop.set()
            self.log_queue.put(
                "\n=== Step 5: запрошена остановка (воркеры дойдут до конца текущего аккаунта) ===\n"
            )

        # Подчищаем хвосты подпроцессов, если кто-то всё-таки оставил их
        # в self.processes (например, ручные subprocess-вызовы пользователя).
        for process in list(self.processes.values()):
            try:
                process.terminate()
            except Exception:
                pass

        # P1-4: браузерные подпроцессы Шага 5 не реагируют на SIGTERM их
        # python-родителя — обходим дерево детей вручную. Делаем это в
        # фоне, чтобы GUI не висел на grace_seconds-ждании; сам stop
        # остаётся «мягким» для воркеров, но реальные firefox/chrome мы
        # всё равно вырубаем — иначе они будут продолжать заполнять
        # форму Stripe / ловить hCaptcha.
        threading.Thread(
            target=self._kill_browser_descendants,
            name="kill-browser-descendants",
            daemon=True,
        ).start()

        self.log_queue.put(
            "\n=== Остановка пайплайна (текущий шаг завершит итерацию) ===\n"
        )

    def update_progress(self):
        if not self.running:
            return

        try:
            db = self._db

            # Step 1: Create Emails
            if NICKS_FILE.exists():
                total_nicks = len([line.strip() for line in NICKS_FILE.read_text(encoding='utf-8').splitlines() if line.strip()])
                done_nicks = len(db.get_done_nicks())
                self.progress1['maximum'] = total_nicks
                self.progress1['value'] = done_nicks
                self.label1.config(text=f"{done_nicks}/{total_nicks}")

            # Step 2: Register Devin
            pending = db.get_pending_devin()
            done = db.get_devin_done_emails()
            total = len(pending) + len(done)
            if total > 0:
                self.progress2['maximum'] = total
                self.progress2['value'] = len(done)
                self.label2.config(text=f"{len(done)}/{total}")

            # Step 3: Generate Identities
            pending_ids = db.get_pending_identities()
            conn = db._get_conn()
            cursor = conn.execute("SELECT COUNT(*) FROM accounts WHERE identity_name IS NOT NULL")
            done_ids = cursor.fetchone()[0]
            total_ids = len(pending_ids) + done_ids
            if total_ids > 0:
                self.progress3['maximum'] = total_ids
                self.progress3['value'] = done_ids
                self.label3.config(text=f"{done_ids}/{total_ids}")

            # Step 4: Check Cards
            bins_file = Path(__file__).parent / "бины.txt"
            live_cards_file = Path(__file__).parent / "живые карты.txt"
            if bins_file.exists():
                total_bins = len([line.strip() for line in bins_file.read_text(encoding='utf-8').splitlines() if line.strip() and not line.startswith('#')])
                if live_cards_file.exists():
                    done_cards = len([line.strip() for line in live_cards_file.read_text(encoding='utf-8').splitlines() if line.strip()])
                else:
                    done_cards = 0
                quantity = int(self.step4_quantity.get() if self.step4_custom.get() else '10')
                self.progress4['maximum'] = total_bins * quantity
                self.progress4['value'] = done_cards
                self.label4.config(text=f"{done_cards} cards")

            # Step 5: Activate Trials
            activated = db.get_activated_accounts()
            devin_accounts = db.get_devin_accounts(status='success')
            total_activations = len(devin_accounts)
            done_activations = len(activated)
            if total_activations > 0:
                self.progress5['maximum'] = total_activations
                self.progress5['value'] = done_activations
                self.label5.config(text=f"{done_activations}/{total_activations}")

        except Exception as e:
            pass

        self.root.after(2000, self.update_progress)

    def update_logs(self):
        while not self.log_queue.empty():
            try:
                line = self.log_queue.get_nowait()
                self.log_text.config(state='normal')
                self.log_text.insert(tk.END, line + '\n')
                self.log_text.see(tk.END)
                self.log_text.config(state='disabled')
            except queue.Empty:
                break

        self.root.after(100, self.update_logs)

    def show_database(self):
        db_window = tk.Toplevel(self.root)
        db_window.title("Database Viewer")
        db_window.geometry("1000x600")

        # Treeview
        tree_frame = ttk.Frame(db_window)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        columns = ('email', 'password', 'nick', 'devin_status', 'identity_name')
        tree = ttk.Treeview(tree_frame, columns=columns, show='headings')

        tree.heading('email', text='Email')
        tree.heading('password', text='Password')
        tree.heading('nick', text='Nick')
        tree.heading('devin_status', text='Devin Status')
        tree.heading('identity_name', text='Identity')

        tree.column('email', width=200)
        tree.column('password', width=150)
        tree.column('nick', width=150)
        tree.column('devin_status', width=100)
        tree.column('identity_name', width=200)

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Load data
        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT email, password, nick, devin_status, identity_name
                FROM accounts
                ORDER BY created_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['email'] or '',
                    row['password'] or '',
                    row['nick'] or '',
                    row['devin_status'] or '',
                    row['identity_name'] or ''
                ))

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить данные: {e}")

        # Buttons
        btn_frame = ttk.Frame(db_window)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Button(btn_frame, text="Обновить", command=lambda: self.refresh_db_view(tree)).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Закрыть", command=db_window.destroy).pack(side=tk.RIGHT, padx=5)

    def show_emails(self):
        """Показать список созданных email аккаунтов."""
        window = tk.Toplevel(self.root)
        window.title("Email аккаунты")
        window.geometry("900x600")

        tree_frame = ttk.Frame(window)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        columns = ('email', 'password', 'nick', 'created_at')
        tree = ttk.Treeview(tree_frame, columns=columns, show='headings')

        tree.heading('email', text='Email')
        tree.heading('password', text='Password')
        tree.heading('nick', text='Nick')
        tree.heading('created_at', text='Создан')

        tree.column('email', width=250)
        tree.column('password', width=150)
        tree.column('nick', width=150)
        tree.column('created_at', width=200)

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT email, password, nick, pinmx_created_at
                FROM accounts
                WHERE email IS NOT NULL
                ORDER BY pinmx_created_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['email'] or '',
                    row['password'] or '',
                    row['nick'] or '',
                    row['pinmx_created_at'] or ''
                ))

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить данные: {e}")

        btn_frame = ttk.Frame(window)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(btn_frame, text=f"Всего: {len(tree.get_children())} аккаунтов").pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Обновить", command=lambda: self.refresh_emails_view(tree, btn_frame)).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Закрыть", command=window.destroy).pack(side=tk.RIGHT, padx=5)

    def refresh_emails_view(self, tree, btn_frame):
        """Обновить список email аккаунтов."""
        for item in tree.get_children():
            tree.delete(item)

        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT email, password, nick, pinmx_created_at
                FROM accounts
                WHERE email IS NOT NULL
                ORDER BY pinmx_created_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['email'] or '',
                    row['password'] or '',
                    row['nick'] or '',
                    row['pinmx_created_at'] or ''
                ))


            # Обновить счетчик
            for widget in btn_frame.winfo_children():
                if isinstance(widget, ttk.Label):
                    widget.config(text=f"Всего: {len(tree.get_children())} аккаунтов")
                    break

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось обновить данные: {e}")

    def show_devin_accounts(self):
        """Показать список зарегистрированных Devin аккаунтов."""
        window = tk.Toplevel(self.root)
        window.title("Devin аккаунты")
        window.geometry("900x600")

        tree_frame = ttk.Frame(window)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        columns = ('email', 'password', 'status', 'registered_at')
        tree = ttk.Treeview(tree_frame, columns=columns, show='headings')

        tree.heading('email', text='Email')
        tree.heading('password', text='Password')
        tree.heading('status', text='Status')
        tree.heading('registered_at', text='Зарегистрирован')

        tree.column('email', width=250)
        tree.column('password', width=150)
        tree.column('status', width=100)
        tree.column('registered_at', width=200)

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT email, password, devin_status, devin_registered_at
                FROM accounts
                WHERE devin_status IS NOT NULL
                ORDER BY devin_registered_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['email'] or '',
                    row['password'] or '',
                    row['devin_status'] or '',
                    row['devin_registered_at'] or ''
                ))

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить данные: {e}")

        btn_frame = ttk.Frame(window)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)

        success_count = sum(1 for item in tree.get_children() if tree.item(item)['values'][2] == 'success')
        ttk.Label(btn_frame, text=f"Всего: {len(tree.get_children())} (успешно: {success_count})").pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Обновить", command=lambda: self.refresh_devin_view(tree, btn_frame)).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Закрыть", command=window.destroy).pack(side=tk.RIGHT, padx=5)

    def refresh_devin_view(self, tree, btn_frame):
        """Обновить список Devin аккаунтов."""
        for item in tree.get_children():
            tree.delete(item)

        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT email, password, devin_status, devin_registered_at
                FROM accounts
                WHERE devin_status IS NOT NULL
                ORDER BY devin_registered_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['email'] or '',
                    row['password'] or '',
                    row['devin_status'] or '',
                    row['devin_registered_at'] or ''
                ))


            success_count = sum(1 for item in tree.get_children() if tree.item(item)['values'][2] == 'success')
            for widget in btn_frame.winfo_children():
                if isinstance(widget, ttk.Label):
                    widget.config(text=f"Всего: {len(tree.get_children())} (успешно: {success_count})")
                    break

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось обновить данные: {e}")

    def show_identities(self):
        """Показать список сгенерированных личностей."""
        window = tk.Toplevel(self.root)
        window.title("Личности")
        window.geometry("1000x600")

        tree_frame = ttk.Frame(window)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        columns = ('email', 'name', 'address')
        tree = ttk.Treeview(tree_frame, columns=columns, show='headings')

        tree.heading('email', text='Email')
        tree.heading('name', text='Имя')
        tree.heading('address', text='Адрес')

        tree.column('email', width=250)
        tree.column('name', width=200)
        tree.column('address', width=400)

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT email, identity_name, identity_address
                FROM accounts
                WHERE identity_name IS NOT NULL
                ORDER BY created_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['email'] or '',
                    row['identity_name'] or '',
                    row['identity_address'] or ''
                ))

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить данные: {e}")

        btn_frame = ttk.Frame(window)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(btn_frame, text=f"Всего: {len(tree.get_children())} личностей").pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Обновить", command=lambda: self.refresh_identities_view(tree, btn_frame)).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Закрыть", command=window.destroy).pack(side=tk.RIGHT, padx=5)

    def refresh_identities_view(self, tree, btn_frame):
        """Обновить список личностей."""
        for item in tree.get_children():
            tree.delete(item)

        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT email, identity_name, identity_address
                FROM accounts
                WHERE identity_name IS NOT NULL
                ORDER BY created_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['email'] or '',
                    row['identity_name'] or '',
                    row['identity_address'] or ''
                ))


            for widget in btn_frame.winfo_children():
                if isinstance(widget, ttk.Label):
                    widget.config(text=f"Всего: {len(tree.get_children())} личностей")
                    break

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось обновить данные: {e}")

    def show_live_cards(self):
        """Показать список живых карт."""
        window = tk.Toplevel(self.root)
        window.title("Живые карты")
        window.geometry("1000x600")

        tree_frame = ttk.Frame(window)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        columns = ('card_number', 'exp', 'cvv', 'bin', 'status', 'checked_at')
        tree = ttk.Treeview(tree_frame, columns=columns, show='headings')

        tree.heading('card_number', text='Номер карты')
        tree.heading('exp', text='Срок')
        tree.heading('cvv', text='CVV')
        tree.heading('bin', text='BIN')
        tree.heading('status', text='Статус')
        tree.heading('checked_at', text='Проверена')

        tree.column('card_number', width=200)
        tree.column('exp', width=80)
        tree.column('cvv', width=60)
        tree.column('bin', width=100)
        tree.column('status', width=100)
        tree.column('checked_at', width=200)

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT card_number, exp_month, exp_year, cvv, bin, status, checked_at
                FROM cards
                WHERE status IN ('live', 'confirmed')
                ORDER BY checked_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['card_number'] or '',
                    f"{row['exp_month']}/{row['exp_year']}" if row['exp_month'] else '',
                    row['cvv'] or '',
                    row['bin'] or '',
                    row['status'] or '',
                    row['checked_at'] or ''
                ))

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить данные: {e}")

        btn_frame = ttk.Frame(window)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)

        confirmed_count = sum(1 for item in tree.get_children() if tree.item(item)['values'][4] == 'confirmed')
        ttk.Label(btn_frame, text=f"Всего: {len(tree.get_children())} (подтверждено: {confirmed_count})").pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Обновить", command=lambda: self.refresh_cards_view(tree, btn_frame)).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Закрыть", command=window.destroy).pack(side=tk.RIGHT, padx=5)

    def refresh_cards_view(self, tree, btn_frame):
        """Обновить список карт."""
        for item in tree.get_children():
            tree.delete(item)

        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT card_number, exp_month, exp_year, cvv, bin, status, checked_at
                FROM cards
                WHERE status IN ('live', 'confirmed')
                ORDER BY checked_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['card_number'] or '',
                    f"{row['exp_month']}/{row['exp_year']}" if row['exp_month'] else '',
                    row['cvv'] or '',
                    row['bin'] or '',
                    row['status'] or '',
                    row['checked_at'] or ''
                ))


            confirmed_count = sum(1 for item in tree.get_children() if tree.item(item)['values'][4] == 'confirmed')
            for widget in btn_frame.winfo_children():
                if isinstance(widget, ttk.Label):
                    widget.config(text=f"Всего: {len(tree.get_children())} (подтверждено: {confirmed_count})")
                    break

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось обновить данные: {e}")

    def show_activated_trials(self):
        """Показать список активированных триалов."""
        window = tk.Toplevel(self.root)
        window.title("Активированные триалы")
        window.geometry("1000x600")

        tree_frame = ttk.Frame(window)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        columns = ('email', 'card', 'holder_name', 'activated_at')
        tree = ttk.Treeview(tree_frame, columns=columns, show='headings')

        tree.heading('email', text='Email')
        tree.heading('card', text='Карта')
        tree.heading('holder_name', text='Имя держателя')
        tree.heading('activated_at', text='Активирован')

        tree.column('email', width=250)
        tree.column('card', width=250)
        tree.column('holder_name', width=200)
        tree.column('activated_at', width=200)

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT email, card, holder_name, activated_at
                FROM activated_accounts
                ORDER BY activated_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['email'] or '',
                    row['card'] or '',
                    row['holder_name'] or '',
                    row['activated_at'] or ''
                ))

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить данные: {e}")

        btn_frame = ttk.Frame(window)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(btn_frame, text=f"Всего: {len(tree.get_children())} активированных триалов").pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Обновить", command=lambda: self.refresh_activated_view(tree, btn_frame)).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Закрыть", command=window.destroy).pack(side=tk.RIGHT, padx=5)

    def refresh_activated_view(self, tree, btn_frame):
        """Обновить список активированных триалов."""
        for item in tree.get_children():
            tree.delete(item)

        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT email, card, holder_name, activated_at
                FROM activated_accounts
                ORDER BY activated_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['email'] or '',
                    row['card'] or '',
                    row['holder_name'] or '',
                    row['activated_at'] or ''
                ))


            for widget in btn_frame.winfo_children():
                if isinstance(widget, ttk.Label):
                    widget.config(text=f"Всего: {len(tree.get_children())} активированных триалов")
                    break

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось обновить данные: {e}")

    def refresh_db_view(self, tree):
        for item in tree.get_children():
            tree.delete(item)

        try:
            db = self._db
            conn = db._get_conn()
            cursor = conn.execute("""
                SELECT email, password, nick, devin_status, identity_name
                FROM accounts
                ORDER BY created_at DESC
            """)

            for row in cursor.fetchall():
                tree.insert('', tk.END, values=(
                    row['email'] or '',
                    row['password'] or '',
                    row['nick'] or '',
                    row['devin_status'] or '',
                    row['identity_name'] or ''
                ))

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось обновить данные: {e}")

    def export_txt(self):
        try:
            db = self._db
            base_path = Path(__file__).parent

            count_emails = db.export_emails_txt(base_path / "имейлы pingmx.txt")
            count_taken = db.export_taken_txt(base_path / "taken.txt")
            count_devin = db.export_devin_accounts_txt(base_path / "аккаунты devin.txt")
            count_errors = db.export_devin_errors_txt(base_path / "devin_errors.txt")
            count_identities = db.export_identities_txt(base_path / "личности.txt")


            messagebox.showinfo("Экспорт завершён",
                f"Экспортировано:\n"
                f"- Emails: {count_emails}\n"
                f"- Taken: {count_taken}\n"
                f"- Devin accounts: {count_devin}\n"
                f"- Devin errors: {count_errors}\n"
                f"- Identities: {count_identities}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось экспортировать: {e}")

def main():
    # Необходимо для корректной работы с PyInstaller на Windows
    multiprocessing.freeze_support()

    root = tk.Tk()
    app = PipelineGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
