# TestRepo003 — Разбор проекта автоматизации и парсинга

Репозиторий: `OrgTestTest001/TestRepo003` (`devin-acc-creator`).
Стек: **Python 3.11+, Tkinter (GUI), Playwright (sync + async), Camoufox,
ddddocr (OCR-капча), Faker, SQLite (WAL).**
Платформа: **Windows** (`.cmd`-обвязка, ожидаемый Chrome через
`channel="chrome"`).

Назначение: единый конвейер для массовой регистрации email-ящиков
на `pinmx.com`, регистрации этих ящиков как аккаунтов на `app.devin.ai`,
генерации личности (имя+адрес), чекинга карт по BIN-ам на `chkr.cc` и
финальной активации Devin-триала через Stripe Checkout с проходом
hCaptcha.

---

## 1. Архитектура (модули)

| Модуль | Роль |
|---|---|
| `gui.py` | Tkinter-GUI: 5 вкладок-шагов, прогресс, логи, просмотр БД |
| `create_emails.py` | Шаг 1: регистрация email-ов на `pinmx.com/ru` (Playwright sync + ddddocr) |
| `register_devin.py` | Шаг 2: signup в `app.devin.ai` через email-код из `mail-client.pinmx.com` |
| `add_identities.py` | Шаг 3: генерация Имя/Адрес через `Faker` (по умолчанию `el_GR`) |
| `check_cards.py` | Шаг 4: чекинг карт по BIN-ам на `chkr.cc` (Playwright async, N вкладок) |
| `activate_trials.py` | Шаг 5: активация Devin-триала со Stripe Checkout (Camoufox, hCaptcha) |
| `start_devin_trial.py` | Интерактивный standalone: ручной запуск trial-флоу (не из GUI) |
| `find_and_pay.py` | Hybrid: параллельно «вводит адрес в Stripe» и «чекинг карт», ждёт ручной вставки карты |
| `devin_async.py` | Async-копия Devin/mail-логики (login, код, Stripe-фрейм) — для шага 5 и `find_and_pay` |
| `storage.py` | SQLite слой (`AccountDB`): таблицы `accounts`, `nicks`, `bins`, `cards`, `card_attempts`, `activated_accounts`. WAL, retry на lock, импорт/экспорт `.txt` |
| `browser_modes.py` | Запуск Chrome в режимах `clean` / `incognito` / `system` / `isolated` |
| `profile_manager.py` | Создание изолированных копий Chrome-профилей для параллельных воркеров |
| `copy_chrome_profile.py` | Копирование системного Chrome-профиля в `./browser_profile/Default` |
| `logging_utils.py` | `setup_logging`, `log_step`, `log_timing`, `log_exception` — пишет в `logs/debug_*.log` |
| `run_all.cmd` | Запуск шагов 1→2→3 из консоли без GUI |
| `build_exe.cmd` | Сборка GUI в `.exe` через PyInstaller (`onedir`, `windowed`) |

Дополнительно: `tests/`, отдельные `test_camoufox*.py`, `test_hcaptcha.py`,
`test_hybrid.py`, `test_multiple_browsers.py` — отладочные песочницы.

---

## 2. Хранилище данных (storage.AccountDB)

SQLite-файл `accounts.db` (WAL, autocommit, `BEGIN IMMEDIATE` с retry на
`database is locked`). Заменяет 5 исторических `.txt`-файлов, но при
первом запуске импортирует их и умеет экспортировать обратно.

Таблицы:

- `accounts(email PK, password, nick, pinmx_created_at, devin_status, devin_error, devin_registered_at, identity_name, identity_address, created_at)`
- `nicks(id, nick UNIQUE, status='pending'|..., created_at)` — очередь шага 1
- `bins(id, bin UNIQUE, status, created_at)` — очередь шага 4
- `cards(id, card_number, exp_month, exp_year, cvv, bin, status='live'|'confirmed', checked_at, confirmed_at, used_at, used_by_email, UNIQUE(card_number, exp_month, exp_year, cvv))`
- `card_attempts(card, email, outcome, attempted_at)` — журнал попыток оплаты
- `activated_accounts(email UNIQUE, card, holder_name, activated_at)` — финальный результат шага 5

Поля статусов:
- `devin_status`: `NULL` (pending) / `success` / `error` / `taken` (ник).
- `cards.status`: `live` (нашли) / `confirmed` (прошёл recheck).

Идемпотентность всего конвейера держится на этом БД-стейте: `get_pending_*`
выдаёт только необработанные сущности.

Сопутствующие `.txt`-файлы (используются как I/O и для совместимости):
`имена для имейлов.txt`, `имейлы pingmx.txt`, `taken.txt`,
`аккаунты devin.txt`, `devin_errors.txt`, `личности.txt`, `бины.txt`,
`живые карты.txt`, `подтверждённые живые карты.txt`.

---

## 3. GUI (`gui.py`) — структура и функции

Окно `tk.Tk` 900×800, класс `PipelineGUI`. Состоит из `ttk.Notebook` с
6 вкладками, блока кнопок управления, блока прогресс-баров и
прокручиваемого лога.

### 3.1. Вкладка «Глобальные настройки»

Чекбокс **«Применить ко всем шагам»** (`use_global`) + поля:
- `Workers` (`global_workers`, default `5`)
- `Limit` (`global_limit`, default `100`)
- `Delay` (`global_delay`, default `5.0`)
- `Show browser (--head)` (`global_head`)
- `Debug mode` (`global_debug`)

Эти значения подмешиваются в CLI каждому шагу через `get_stepN_args()`,
если для шага не включён собственный набор настроек.

### 3.2. Вкладка «Step 1: Create Emails» → `create_emails.py`

- `step1_enabled` — включить шаг
- Поле «Файл с никами» + кнопка **«Выбрать…»** (`select_step1_nicks`) →
  передаётся как `--nicks-file PATH`. По умолчанию `имена для имейлов.txt`.
- Чекбокс «Использовать свои настройки» (`toggle_step1`) активирует
  собственные `Workers / Limit / Delay / Show browser / Debug`.
- Кнопка **«Просмотр email аккаунтов»** (`show_emails`) — открывает
  Toplevel с `Treeview` (email/password/nick/created_at) на SELECT-е
  `WHERE email IS NOT NULL`.

Сборка CLI (`get_step1_args`): `--nicks-file …` + `--workers / --limit /
--worker-delay / --head / --debug`.

### 3.3. Вкладка «Step 2: Register Devin» → `register_devin.py`

- `step2_enabled`, кастомные `Workers / Limit / Delay / Show browser / Debug`.
- Кнопка **«Просмотр Devin аккаунтов»** (`show_devin_accounts`) — Treeview
  (email/password/devin_status/devin_registered_at), счётчик success.

`get_step2_args` собирает те же флаги.

### 3.4. Вкладка «Step 3: Add Identities» → `add_identities.py`

- `step3_enabled`, `step3_parallel` (можно гонять параллельно с другими
  шагами — выделяется в отдельный поток в `run_pipeline`).
- Кастомные настройки: `Limit` и `Страна` (Combobox по словарю
  `LOCALES = {Южная Корея: ko_KR, США: en_US, Германия: de_DE,
  Сингапур: en_SG, Англия: en_GB, Греция: el_GR}`).
- Кнопка **«Просмотр личностей»** (`show_identities`) — Treeview
  (email/identity_name/identity_address).

`get_step3_args`: `--limit N --locale <code>`. Дефолт locale — `el_GR`.

### 3.5. Вкладка «Step 4: Check Cards» → `check_cards.py`

- `step4_enabled`, `step4_parallel`.
- Поле «Файл с BIN-кодами» + **«Выбрать…»** (`select_step4_bins`) →
  `--bins-file PATH` (default `bins.txt`).
- Кастомные: `Quantity` (карт/BIN, default 10), `Tabs` (параллельных
  вкладок, default 3), `Timeout` (default 1200с), `Show browser`,
  `No recheck`, `Debug`.
- Кнопка **«Просмотр живых карт»** (`show_live_cards`) — Treeview
  (card_number/exp/cvv/bin/status/checked_at) для
  `status IN ('live','confirmed')`.

`get_step4_args`: `--bins-file --quantity --tabs --check-timeout
[--headless] [--no-recheck] [--debug]`.

### 3.6. Вкладка «Step 5: Activate Trials» → `activate_trials.activate_trials_pipeline`

- `step5_enabled`. Шаг 5 всегда последний (после всех остальных
  последовательных), запускается через **импорт**, а не subprocess.
- Кастомные: `Параллельных браузеров` (default 3), `Таймаут` (default 300),
  `Профиль Chrome` + кнопка **«Выбрать папку…»** (`select_chrome_profile`)
  для базового Firefox-профиля Camoufox.
- Кнопка **«Просмотр активированных триалов»** (`show_activated_trials`)
  — Treeview (email/card/holder_name/activated_at) на `activated_accounts`.

`get_step5_args` возвращает dict `{parallel, timeout, profile_path}`.
`_run_step5` запускает `activate_trials_pipeline(db, parallel, timeout,
profile_path)` в отдельном asyncio-loop в фоновом потоке.

### 3.7. Нижняя панель

- `Запустить выбранные шаги` → `run_pipeline`:
  - собирает `sequential_steps` (Step 1, Step 2; Step 3/4 если без
    `*_parallel`),
  - `parallel_steps` (Step 3/4 при `*_parallel=True`),
  - стартует поток `_run_steps(sequential_steps, run_step5)` и отдельные
    потоки `_run_single_step(...)` для параллельных,
  - заводит `update_progress()` (раз в 2с).
- `Остановить` → `stop_pipeline`: `running=False` и `process.terminate()`
  для всех subprocess-ов.
- Прогресс-бары Step 1…5 (`update_progress`):
  - Step 1: `done_nicks / total_nicks` (берётся из БД и файла ников),
  - Step 2: `done_devin / (pending+done)`,
  - Step 3: `accounts.identity_name IS NOT NULL`,
  - Step 4: `count(живые карты.txt) / (bins × quantity)`,
  - Step 5: `activated / devin_status='success'`.
- Лог — `scrolledtext.ScrolledText`, читает `log_queue`
  (`update_logs` каждые 100мс).
- `Просмотр БД` (`show_database`) — Treeview по всей `accounts`.
- `Экспорт в .txt` (`export_txt`) — вызывает
  `db.export_emails_txt / export_taken_txt / export_devin_accounts_txt /
  export_devin_errors_txt / export_identities_txt`.

### 3.8. Запуск subprocess-ов (`_run_steps`, `_run_single_step`)

- Python: `.venv\Scripts\python.exe` (Windows-only) либо `sys.executable`
  как fallback.
- `subprocess.Popen([python, script.py, *args], stdout=PIPE,
  stderr=STDOUT)`; каждую строку `stdout` кидаем в `log_queue`.
- Step 5 запускается напрямую через `import activate_trials` и
  `asyncio.new_event_loop()` — без subprocess.
- `multiprocessing.freeze_support()` в `main()` — для совместимости
  с PyInstaller.

---

## 4. Шаги конвейера (что и как парсится/автоматизируется)

### Шаг 1 — `create_emails.py` (Playwright sync + ddddocr)

Регистрация ящиков на `https://www.pinmx.com/ru` (получаем `*@pingmx.com`).

CLI: `--head --limit N --no-skip-taken --debug --workers N --worker-delay
SEC --nicks-file PATH --browser-mode {clean|incognito|system|isolated}`.

Ключевые функции:
- `select_pingmx(page)` — переключить суффикс выпадашки на `@pingmx.com`.
- `install_message_observer(page)` / `collect_messages` / `reset_messages`
  — MutationObserver-инъекция, ловит тосты «Email Already Exists»,
  «Invalid Captcha Code», «Invalid Parameters».
- `collect_api_responses(page)` — перехват `/v1/random-mail/create-by-device`,
  парсит `code`: `200=success`, `81001=invalid captcha`, `1001=retry`.
- `fetch_captcha_bytes(page)` / `refresh_captcha` — скачивает PNG прямо
  по `<img src>`-URL.
- `solve_digits(png, ocr)` — `ddddocr.set_ranges("0123456789")`, принимает
  только 6 цифр.
- `extract_password_from_success_dialog(page)` — пароль из DOM модалки
  успеха.
- `attempt_register(page, nick, logger, ocr)` → `AttemptResult` (статус +
  email/password/reason).
- `register_one` — оборачивает `attempt_register` с `RETRIES_PER_NICK=8`
  полных перезаходов и `CAPTCHA_REFRESHES=15` обновлений капчи.
- `process_nick_worker(...)` — воркер `ThreadPoolExecutor` для
  параллельного режима (`--workers > 1`).
- `backup_results()` — `имейлы pingmx.txt → .bak` перед запуском.

Результат: запись через `db.add_email/mark_nick_taken`, плюс экспорт в
`имейлы pingmx.txt` / `taken.txt`.

### Шаг 2 — `register_devin.py` (Playwright sync + ddddocr)

Для каждого `email:password` из БД:

1. Логинится в `https://mail-client.pinmx.com/` (rainloop). При появлении
   6-значной цифровой капчи — `_solve_mailclient_login_captcha` через
   `ddddocr` (общий `_get_ocr()`).
2. В соседней вкладке открывает `https://app.devin.ai/auth/signup`
   (`start_devin_signup`), вводит email, жмёт «Sign up».
3. Ждёт письмо от Devin до 5 минут — `wait_for_devin_email_code`:
   листает inbox селектором `.messageListPlace .messageListItem`,
   открывает, тянет тело по `.messageView .messageItem.fixIndex .content`
   (+ fallback на `.b-message-view-wrapper`, `iframe`), regex
   `(?<!\d)(\d{6})(?!\d)` — `extract_code`.
4. Вводит код в поле `autocomplete="one-time-code"` — `submit_devin_code`
   (auto-submit при 6-й цифре, иначе клик «Continue»).
5. Дожидается ухода с `/auth/...` (`_is_post_login_url`) → пишет
   `db.mark_devin_success`. При ошибке — `db.mark_devin_error(email,
   reason)` (`StepError` / `InvalidCodeError`).

CLI: `--head --limit N --no-skip-done --delay SEC --workers N
--worker-delay SEC --debug --browser-mode ...`.

Воркер для параллельного режима: `process_account_worker`.

### Шаг 3 — `add_identities.py` (Faker)

Генерация чисто локальная, без сети.

CLI: `--limit N --locale LOCALE --only-from PATH`.

- `Faker(locale)` → `name = fake.name()`, `address = f"{street_address},
  {postcode}, {city}"`.
- Берём `db.get_pending_identities()`. Если `--only-from PATH` — фильтр
  по email-ам из этого файла (например `аккаунты devin.txt`, чтобы не
  тратить identity на проваленные регистрации).
- `db.add_identity(email, name, address)`.
- Идемпотентен: email-ы с уже заполненной identity пропускаются.

### Шаг 4 — `check_cards.py` (Playwright async, chkr.cc, N вкладок)

`https://chkr.cc/`. Генерация карт по BIN-ам и онлайн-чекинг.

CLI: `--quantity N --tabs N --check-timeout S --head/--headless
--no-recheck --keep-existing --browser-mode ... --bins-file PATH`.

Фиксированные селекторы chkr.cc:
- `button[data-bs-target="#bin-generator"]` — открыть модалку,
- `#bin`, `#quantity`, `a#gen` — заполнить + Generate,
- `#bin-generator button.close`, `button#start` — закрыть/Start,
- `button#modal-stop`, `#liveResults` — статус и список «Live»,
- `textarea#cc` — для recheck-режима.

Поток:
1. `load_bins(path)` → очередь `asyncio.Queue`.
2. N вкладок (`tab_worker`), общая queue, общий файл + `asyncio.Lock` для
   дедупа `append + flush + fsync`.
3. На вкладку: открыть генератор → `#bin = BIN`, `#quantity = N` →
   `a#gen` → закрыть модалку → `button#start` → опрашивать `#liveResults`
   каждые 0.7с пока виден `button#modal-stop` → каждую новую строку
   писать как «live» (DB + `живые карты.txt`).
4. Phase 2 — `recheck_live_cards`: вставить все live-карты в
   `textarea#cc`, прогнать START, оставшиеся живыми → `status='confirmed'`
   и `подтверждённые живые карты.txt`.

`extract_card_credentials(line)` парсит формат `номер|MM|YYYY|CVV`
из `#liveResults`.

### Шаг 5 — `activate_trials.py` (Camoufox + Playwright async)

Реальная активация Devin-триала с подстановкой карты в Stripe Checkout.
Запускается из GUI (через `activate_trials_pipeline`) или CLI.

CLI standalone: `--parallel N --timeout SEC --profile PATH`.

Логика воркера (`worker(worker_id)`):
1. Для каждого воркера копируется базовый Firefox-профиль в
   `temp_profiles/worker_<id>_profile/` (изоляция cookies/расширений
   между воркерами).
2. `AsyncNewBrowser` Camoufox в persistent_context с `humanize=True` —
   именно для прохода hCaptcha (антифингерпринт).
3. Берёт `account = queue.get_nowait()` (источник — `db.get_devin_accounts
   (status='success')` минус `db.get_activated_accounts()`), `identity =
   db.get_all_identities()[email]`, `card = db.get_cards('confirmed')` или
   `'live'`.
4. `activate_single_account` (использует функции из `devin_async.py`):
   - `login_to_mailclient_async` → код-логин,
   - `login_to_devin_async` → код по почте, ввод в `one-time-code`,
   - `go_to_plans_and_start_trial_async` → «My Team → Upgrade → Start
     free trial»,
   - `find_stripe_checkout_frame_async` → найти iframe Stripe,
   - `select_card_payment_method_async` → выбрать «Card»,
   - `fill_address_async` → подставить адрес из identity,
   - вручную через локаторы: `input[name="number"]`, `name="expiry"`
     (`MM / YY`), `name="cvc"`, `name="name"`,
   - клик `button[type="submit"]`,
   - `_try_solve_hcaptcha_async` (тайм-аут 20с, ждёт iframe
     `hcaptcha.com`, кликает `#checkbox`, ждёт `aria-checked='true'` или
     детач). Возможные исходы: `clicked / none / challenge / failed`.
   - Ждать `wait_for_url("**/dashboard**")` → успех.
5. Запись результатов:
   - success → `db.add_card_attempt(card, email, 'success')`,
     `db.mark_card_used(card.id, email)`,
     `db.add_activated_account(email, card_str, holder_name)`.
   - declined / timeout → `db.add_card_attempt(...,
     'declined'|'failed')`, аккаунт остаётся в очереди.

### Standalone-режимы (не из GUI)

- `start_devin_trial.py` — синхронный, интерактивный: логинится в Devin
  под существующим аккаунтом, доходит до Stripe Checkout, заполняет
  адрес и **тестовые** данные карты, оставляет окно открытым (`input()`).
- `find_and_pay.py` — async-гибрид: одновременно держит 2 Devin-вкладки
  (логин + Stripe с введённым адресом, **без карты**) и N чекинг-вкладок;
  когда чекинг отдаёт `confirmed`-карты, пользователь руками вставляет
  одну из них в открытую форму Stripe.

---

## 5. Поддерживающая инфраструктура

### `browser_modes.py`

`launch_browser(playwright, mode, *, head)` → `LaunchedBrowser(browser,
context, cleanup)`. Режимы:
- `clean` — обычный `chromium.launch(channel="chrome")` + `new_context()`.
- `incognito` — то же + `--incognito` в args.
- `system` — `launch_persistent_context(user_data_dir=./browser_profile/Default)`.
- `isolated` — отдельные ephemeral-копии профиля (для параллельных
  воркеров).

`add_browser_mode_arg(parser)` подключает флаг `--browser-mode` ко всем
скриптам. `launch_browser_async` — async-аналог для шага 4 и
`activate_trials`.

### `profile_manager.ProfileManager`

Контекст-менеджер: `create_isolated_profile()` копирует исходный профиль
в `tempfile.gettempdir()/chrome_profile_<uuid>`, исключая мусорные
файлы; при выходе из контекста чистит за собой.

### `logging_utils.py`

`setup_logging(debug, log_to_file=True)` пишет в `logs/debug_YYYY-MM-DD_
HH-MM-SS.log` + stdout. Декоратор `log_timing` замеряет время функций,
`log_step(logger, ...)` и `log_exception(...)` — структурированные
сообщения.

### `copy_chrome_profile.py`

Однократная подготовка для `--browser-mode system`: копирует
`%LOCALAPPDATA%\Google\Chrome\User Data\Default` в `./browser_profile/
Default/`, пропуская кеши/тяжёлые служебные папки.

---

## 6. Сквозной алгоритм работы

Полный «happy path» из GUI:

```
[имена для имейлов.txt]            (вход — ники)
        │
        ▼
Step 1: create_emails.py
  Playwright Chrome → pinmx.com/ru
  ddddocr (6 цифр) → API /random-mail/create-by-device
  ─► accounts(email, password, nick)  +  имейлы pingmx.txt
                                       +  taken.txt
        │
        ▼
Step 2: register_devin.py
  mail-client.pinmx.com  (логин, капча ddddocr)
        +
  app.devin.ai/auth/signup  (email → код из письма → submit)
  ─► accounts.devin_status='success'  +  аккаунты devin.txt
     accounts.devin_status='error'    +  devin_errors.txt
        │
        ▼
Step 3: add_identities.py
  Faker(locale) → name + address
  фильтр --only-from аккаунты devin.txt
  ─► accounts.identity_name/_address  +  личности.txt
        │
        ▼                              (вход — бины.txt)
Step 4: check_cards.py  ─────────────────┐
  chkr.cc  (N async-вкладок)             │
  generate cards from BIN  →  Start      │
  poll #liveResults                      │
  ─► cards.status='live'   +  живые карты.txt
  recheck → cards.status='confirmed'
                          +  подтверждённые живые карты.txt
        │
        ▼
Step 5: activate_trials.py  (Camoufox + persistent profile copy per worker)
  для каждого devin-аккаунта со status='success' и неактивированного:
    login mail → login Devin → Upgrade → Start free trial
    Stripe Checkout: Card → address из identity → номер/exp/cvc/имя
    hCaptcha (humanize-клик)
    wait_for_url(/dashboard)
  ─► activated_accounts(email, card, holder_name)
     card_attempts(card, email, outcome)
     cards.used_at / used_by_email
```

### Параллелизм

- В шагах 1 и 2 — `ThreadPoolExecutor` с пулом из `--workers N`
  воркеров (`--worker-delay` — стартовый rate-limit). Каждый воркер
  поднимает свой `BrowserContext` (изолированная сессия).
- В шаге 4 — один `BrowserContext`, N асинхронных вкладок с общей
  `asyncio.Queue` BIN-ов и общим лок-ом на запись.
- В шаге 5 — N процессов Camoufox, у каждого своя физическая копия
  Firefox-профиля (для расширений и для прохода hCaptcha).
- GUI умеет помечать шаги 3/4 как `parallel` — тогда они стартуют в
  отдельных потоках одновременно с последовательной цепочкой 1→2.

### Идемпотентность

На каждом шаге `db.get_pending_*(...)` возвращает только то, что ещё
не сделано:
- Шаг 1 — ник не в `accounts` и не помечен `taken`.
- Шаг 2 — email c `devin_status IS NULL`.
- Шаг 3 — `identity_name IS NULL`.
- Шаг 4 — карта не в `cards` (UNIQUE на `(number, exp_month, exp_year,
  cvv)`).
- Шаг 5 — email из `accounts(devin_status='success')` минус
  `activated_accounts`.

### Анти-бот / прохождение защит

- `pinmx.com` и `mail-client.pinmx.com` — цифровая капча 6 знаков,
  решается локально `ddddocr` с `set_ranges("0123456789")`.
- `app.devin.ai` — нет капчи, только email-код (regex `(?<!\d)(\d{6})
  (?!\d)`).
- Stripe Checkout (Devin trial) — **hCaptcha checkbox**, решается
  Camoufox c `humanize=True` (антифингерпринт+эмуляция мыши); при
  показе визуальной задачи карта считается declined.

---

## 7. Резюме функций GUI (быстрый справочник)

Управление пайплайном:
- `run_pipeline()` — собирает шаги, разделяет последовательные/параллельные, стартует потоки.
- `_run_steps(steps, run_step5)` — последовательное выполнение subprocess-ов.
- `_run_single_step(script, args)` — параллельный subprocess-шаг.
- `_run_step5()` — in-process async-запуск `activate_trials_pipeline`.
- `stop_pipeline()` — `terminate()` всем subprocess-ам.

Сбор аргументов: `get_step1_args / get_step2_args / get_step3_args /
get_step4_args / get_step5_args`.

Переключатели «свои настройки»: `toggle_step1 … toggle_step5`,
`toggle_global`.

Выбор файлов/папок: `select_step1_nicks`, `select_step4_bins`,
`select_chrome_profile`.

Просмотр результатов (Toplevel + Treeview):
- `show_emails` / `refresh_emails_view` — email-аккаунты.
- `show_devin_accounts` / `refresh_devin_view` — Devin-аккаунты.
- `show_identities` / `refresh_identities_view` — личности.
- `show_live_cards` / `refresh_cards_view` — live/confirmed карты.
- `show_activated_trials` / `refresh_activated_view` — активированные триалы.
- `show_database` / `refresh_db_view` — общая `accounts`-таблица.

Прочее: `log()` (в `log_queue`), `update_logs()` (раз в 100мс),
`update_progress()` (раз в 2с), `export_txt()` (БД → 5 `.txt`-файлов),
`_on_pipeline_finished()` (включить кнопку Run, выключить Stop).

Точка входа: `main()` → `multiprocessing.freeze_support()`
(для PyInstaller) → `tk.Tk()` → `PipelineGUI(root)` → `mainloop()`.
