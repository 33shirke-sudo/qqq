# STATUS

Состояние работ по приведению репо в порядок согласно [PLAN.md](PLAN.md).
Этот файл — single source of truth для возобновления после сброса контекста.

## Ветка и PR

- Ветка: `devin/1779496680-cleanup`
- База: `main`
- PR: будет открыт первым же коммитом — ссылку дописать сюда.

## Воркфлоу

- По одной задаче из плана за коммит.
- После каждого пункта: `git commit` → `git push` → обновить этот файл (`STATUS.md`) и тоже запушить.
- Если задача крупная (P2-2, P2-3) — допустима серия коммитов; в STATUS.md помечаем «в процессе» и фиксируем промежуточные коммиты.
- При остановке/сбросе контекста: открыть `STATUS.md`, посмотреть `In progress` и `Queue`, продолжить со следующего пункта `Queue` (или дочистить `In progress`).

## Done

Закрыто ещё до этого спринта (зафиксировано в первичном `init`-коммите `a9719da`):

- **P0-1** `activate_single_account`: правильные сигнатуры, две вкладки `mail_page`/`devin_page`, конструкция `Account(email, password)`.
- **P0-2** `camoufox>=0.4` в `requirements.txt`.
- **P0-3** GUI и `check_cards.py` default = `бины.txt`.
- **P0-4** Обновления Tk из фона через `self.root.after(0, ...)`.
- **P1-1** Валидация ввода `_parse_positive_int` + `messagebox.showerror`.
- **P1-3** Запрет `step3_parallel` при включённых Шагах 1/2.
- **P1-5** `add_identities.process` возвращает `(created, total)`.
- **P1-7** `threading.Event _step5_stop_event` ставится в `stop_pipeline()`.
- **P1-8** `TEMP_PROFILES_DIR = ROOT / "temp_profiles"`.
- **P1-9** Шаги 1–4 in-process через `pipeline_runner.py` (нет `.venv\Scripts\python.exe`).
- **P1-11 (CLI-часть)** `import_from_txt_files` автоматически вызывается на старте Шагов 1 и 2.
- Тесты: `test_activate_trials_stop_event`, `test_add_identities`, `test_done_tracking_property`, `test_extract_code_property`, `test_gui_stop_pipeline`, `test_parse_account_property`, `test_validate_inputs`.
- **P2-6 (частично)** `cleanup_old_logs(days=30)` и `cleanup_old_screenshots(days=7)` уже есть, но без `RotatingFileHandler`.

## In progress

- **STATUS.md + PR-инфраструктура** — этот коммит.

## Queue (в порядке исполнения)

1. **P1-2** Persistent `self.db = AccountDB(DB_PATH)` в `__init__` GUI; закрывать в `WM_DELETE_WINDOW`. Сейчас открываем/закрываем на каждом тике `update_progress` (`gui.py:911-…`).
2. **P1-4** `psutil` + kill-tree для Camoufox/Chrome детей при `stop_pipeline`. Добавить `psutil` в `requirements.txt`. Шаги 1–4 теперь in-process, но Шаг 5 запускает Firefox через Camoufox — он должен корректно умирать.
3. **P1-10** Пинмx-пароль из перехваченного API-ответа `create-by-device` (`code=200`). DOM-парсер `extract_password_from_success_dialog` оставить как fallback с `warn`-логом.
4. **P1-11 (GUI-часть)** Кнопка «Импортировать из .txt» с явным `db.import_from_txt_files(...)`.
5. **P1-6** Разделение stdout/stderr в `pipeline_runner.LogQueueStream`: префиксы `[stderr]`/`[stdout]`. Стримы уже разные, но в очереди мерджатся без префикса.
6. **P2-4** `.gitignore` (см. список путей в `PLAN.md` → P2-4).
7. **P2-5** CI (GitHub Actions): `pytest` + `ruff` на каждый push/PR.
8. **P3-1** Убрать `✓`/`✗` из `print()` в `register_devin.py:1902,1906,1912` и `create_emails.py:644,649,652,657` (Windows cp1251 ломает).
9. **P3-3** Дописать в README разделы про Шаг 4 (`check_cards`) и Шаг 5 (`activate_trials`).
10. **P3-6** Убрать `multiprocessing.freeze_support()` из `gui.py:1537` — `multiprocessing` фактически не используется.
11. **P2-6** Подключить `RotatingFileHandler` (cleanup уже есть, ротация — нет).
12. **P2-1** `paths.py` с латинскими константами (`NICKS_FILE`, `EMAILS_FILE`, …); кириллицу оставить как алиасы.
13. **P2-7** `selectors.py` + helper `find_any(page, selectors, timeout)`.
14. **P2-8** `config.py` или `.env` + `python-dotenv` для URL-ов.
15. **P2-3** Разбить `gui.py` (1544 строк, один класс) — `TableViewer`, `gui/tab_stepN.py`.
16. **P2-2** Дедуп `register_devin.py` (1939, sync) ↔ `devin_async.py` (1183, async). Решение: async-only.
17. **Остальные P3** (P3-2 типы, P3-5 print→logger, P3-7 расширить LOCALES) — по мере касания соответствующих файлов.

## Открытые вопросы пользователю

Из `PLAN.md` (раздел «Открытые вопросы»):

1. Целевая платформа: только Windows или ещё mac/Linux? Влияет на P1-9 (in-process уже сделан) и оформление путей.
2. PyInstaller-сборка реально используется? Влияет на выбор async-only vs sync-only для P2-2.
3. Совместимость со старой `accounts.db` при рефакторинге `storage.py` (P2-1) — сохраняем или допускаем миграцию?

Пока работаю в предположении: **Windows-first, PyInstaller используется, миграции аккуратные с сохранением совместимости.**

## Команды для возобновления

```bash
cd /home/ubuntu/repos/qqq
git fetch origin
git checkout devin/1779496680-cleanup
git pull --ff-only
# открыть STATUS.md и смотреть «In progress» / «Queue»
```
