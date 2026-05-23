# Система диагностики и логирования pinmx-mailer

## Что добавлено

### 1. Модуль `logging_utils.py`

Предоставляет инструменты для структурированного логирования:

- **`setup_logging(debug=False, log_to_file=True)`** — настройка логирования
  - `debug=True` — уровень DEBUG, детальные логи
  - `log_to_file=True` — сохранение в `logs/debug_YYYY-MM-DD_HH-MM-SS.log`

- **`@log_timing`** — декоратор для замера времени выполнения функций
  - Работает с sync и async функциями
  - Логирует время начала, завершения и ошибок

- **`log_step(logger, step, details)`** — логирование шагов выполнения
- **`log_exception(logger, exc, context)`** — логирование исключений с traceback
- **`log_metric(logger, metric, value, unit)`** — логирование метрик
- **`save_screenshot_on_error(page, error, prefix)`** — скриншоты при ошибках (только в debug-режиме)

### 2. Флаг `--debug` во всех скриптах пайплайна

Добавлен в:
- `create_emails.py` (шаг 1: создание email-аккаунтов)
- `register_devin.py` (шаг 2: регистрация Devin)
- `find_and_pay.py` (шаг 3: генерация карт + открытие Stripe)
- `activate_accounts.py` (шаг 4: массовая активация)

**Что делает `--debug`:**
- Включает уровень логирования DEBUG
- Сохраняет детальные логи в `logs/`
- Сохраняет скриншоты при ошибках в `screenshots/`
- Не удаляет debug-файлы капчи

### 3. Улучшенное логирование в `devin_async.py`

#### `wait_for_devin_email_code_async`
- Логирование каждой итерации поллинга (каждые 10 итераций)
- Отслеживание количества писем в inbox
- **Защита от зависания**: если inbox не меняется 15 итераций (30 секунд) — автоматический reload страницы
- Детальное логирование попыток чтения тела письма
- Логирование успешного получения кода

#### `login_to_mailclient_async`
- Логирование каждого шага: goto, поиск формы, заполнение, submit
- Замер времени ожидания формы и пост-логин URL
- Логирование каждые 10 секунд во время ожидания (до 90s)
- Детальное логирование капчи

### 4. Улучшенное логирование в `create_emails.py`

#### `attempt_register`
- Логирование каждой попытки регистрации
- Детальное логирование API-ответов от `/random-mail/create-by-device`
- Логирование результатов OCR (ddddocr)
- Логирование успешных регистраций и ошибок

#### `register_one`
- Логирование каждого retry
- Логирование исключений с полным traceback

### 5. Улучшенные сообщения об ошибках

Все скрипты теперь показывают правильную последовательность шагов при ошибках:

**register_devin.py** (если нет email-аккаунтов):
```
Не найден или пуст файл с аккаунтами: имейлы pingmx.txt

Это шаг 2 пайплайна. Сначала нужно создать email-аккаунты:
  .venv\Scripts\python.exe create_emails.py --debug --limit 5 --head

После этого файл 'имейлы pingmx.txt' будет содержать созданные аккаунты.
```

**find_and_pay.py** (если нет аккаунтов или identity):
```
В имейлы pingmx.txt нет аккаунтов.

Это шаг 3 пайплайна. Сначала нужно:
  1. Создать email-аккаунты: .venv\Scripts\python.exe create_emails.py --debug --limit 5
  2. Зарегистрировать Devin: .venv\Scripts\python.exe register_devin.py --debug --limit 5
```

**activate_accounts.py** (если нет аккаунтов):
```
В имейлы pingmx.txt нет аккаунтов.

Это шаг 4 пайплайна. Сначала нужно:
  1. Создать email: .venv\Scripts\python.exe create_emails.py --debug --limit 5
  2. Зарегистрировать Devin: .venv\Scripts\python.exe register_devin.py --debug --limit 5
  3. Сгенерировать identity: .venv\Scripts\python.exe add_identities.py
```

## Как использовать

### Полный пайплайн с диагностикой

```bash
# Шаг 1: Создание email-аккаунтов
.venv\Scripts\python.exe create_emails.py --debug --limit 5 --head

# Шаг 2: Регистрация Devin
.venv\Scripts\python.exe register_devin.py --debug --limit 5 --head

# Шаг 3: Генерация identity (без --debug, быстрая операция)
.venv\Scripts\python.exe add_identities.py

# Шаг 4: Генерация карт + открытие Stripe
.venv\Scripts\python.exe find_and_pay.py --debug --head

# Шаг 5: Массовая активация
.venv\Scripts\python.exe activate_accounts.py --debug --max-accounts 10 --head
```

### Базовый запуск с логированием

```bash
# Шаг 1: С debug-логами
.venv\Scripts\python.exe create_emails.py --debug --limit 5 --head

# Шаг 2: С debug-логами
.venv\Scripts\python.exe register_devin.py --debug --limit 5 --head

# Без debug (только INFO)
.venv\Scripts\python.exe create_emails.py --limit 10
```

### Анализ логов

Логи сохраняются в `logs/debug_YYYY-MM-DD_HH-MM-SS.log`:

```
2026-05-21 12:30:15 | INFO     | create_emails | create_emails запущен с флагами: ...
2026-05-21 12:30:16 | DEBUG    | create_emails | [TIMING] attempt_register started
2026-05-21 12:30:16 | DEBUG    | create_emails | [STEP] attempt_register: nick=aurorapearlbox
2026-05-21 12:30:17 | DEBUG    | create_emails | [aurorapearlbox] r1: пробуем код 123456
2026-05-21 12:30:18 | DEBUG    | create_emails | [aurorapearlbox] r1: API ответ code=200 msg='success'
2026-05-21 12:30:19 | INFO     | create_emails | [aurorapearlbox] r1: SUCCESS email=aurorapearlbox@pingmx.com
2026-05-21 12:30:19 | DEBUG    | create_emails | [TIMING] attempt_register completed in 3.1s
```

### Поиск проблем в логах

**Застревания:**
```bash
# Найти места, где ожидание превышает 30 секунд
grep "ожидание.*[3-9][0-9]\." logs/debug_*.log

# Найти reload страниц (признак зависания)
grep "reload страницы" logs/debug_*.log
```

**Таймауты:**
```bash
# Найти все таймауты
grep "TIMEOUT" logs/debug_*.log

# Найти таймауты ожидания писем
grep "verification email timeout" logs/debug_*.log
```

**Ошибки:**
```bash
# Все ошибки
grep "ERROR" logs/debug_*.log

# С полным traceback
grep -A 20 "EXCEPTION" logs/debug_*.log
```

### Метрики производительности

Логи содержат метки времени `[TIMING]`:

```bash
# Время выполнения функций
grep "TIMING.*completed" logs/debug_*.log

# Самые медленные операции
grep "TIMING.*completed" logs/debug_*.log | sort -t'=' -k2 -n
```

## Ключевые улучшения

### 1. Защита от зависания при ожидании писем

**Проблема:** Если письмо не распознаётся — зависает на 5 минут.

**Решение:** 
- Отслеживание количества писем в inbox
- Если inbox не меняется 15 итераций (30 секунд) — автоматический reload
- Детальное логирование каждой попытки чтения

### 2. Детальное логирование mail-client login

**Проблема:** Ожидание до 90 секунд без информации о прогрессе.

**Решение:**
- Логирование каждые 10 секунд
- Замер времени каждого шага
- Понятные сообщения о текущем состоянии

### 3. Структурированные логи

**Проблема:** `print()` не сохраняется, сложно анализировать.

**Решение:**
- Все логи в файлы с timestamp
- Уровни логирования (DEBUG, INFO, ERROR)
- Контекст для каждого сообщения

### 4. Понятные сообщения об ошибках

**Проблема:** Пользователь запускает шаг 2 без шага 1 и получает непонятную ошибку.

**Решение:**
- Каждый скрипт проверяет наличие необходимых файлов
- При ошибке показывает правильную последовательность команд
- Указывает, какой шаг пайплайна это и что нужно сделать сначала

## Примеры команд

```bash
# Диагностический запуск шага 1 (3 аккаунта, с окном, debug-логи)
.venv\Scripts\python.exe create_emails.py --debug --limit 3 --head

# Диагностический запуск шага 2 (3 аккаунта, с окном, debug-логи)
.venv\Scripts\python.exe register_devin.py --debug --limit 3 --head

# Полный прогон шага 1 с логированием
.venv\Scripts\python.exe create_emails.py --debug --limit 50

# Анализ логов после запуска
grep "TIMING.*completed" logs/debug_*.log | grep "attempt_register"
grep "TIMEOUT\|ERROR" logs/debug_*.log
```

## Очистка старых файлов

```python
from logging_utils import cleanup_old_logs, cleanup_old_screenshots

# Удалить логи старше 30 дней
deleted_logs = cleanup_old_logs(days=30)

# Удалить скриншоты старше 7 дней
deleted_screenshots = cleanup_old_screenshots(days=7)
```

Или вручную:
```bash
# Удалить логи старше 30 дней
find logs/ -name "debug_*.log" -mtime +30 -delete

# Удалить скриншоты старше 7 дней
find screenshots/ -name "*.png" -mtime +7 -delete
```
