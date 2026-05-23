# devin-acc-creator

Полный конвейер регистрации аккаунтов на [Devin AI](https://app.devin.ai):

1. Массовая регистрация имейлов на `https://www.pinmx.com/ru`
   (`@pingmx.com`) с локальным решением капчи через `ddddocr`.
2. Создание аккаунта на `app.devin.ai` для каждого имейла (с
   автоматическим извлечением кода подтверждения из почты).
3. Подстановка случайной личности из fakenamegenerator — только тем
   аккаунтам, которые успешно прошли регистрацию на Devin.

Всё локально, бесплатно, в один клик.

## Требования

- Windows
- Python 3.11+ в PATH (`py -V`)
- Установленный Google Chrome

## Использование

1. Положи ники в `имена для имейлов.txt` (по одному в строке).
2. Запусти `run_all.cmd` — двойным кликом или из терминала.
   - Первый запуск создаст `.venv` и поставит `playwright` + `ddddocr`.
     Chromium **не качается** — используется уже установленный Chrome.
   - Дальше каждый запуск стартует сразу.
   - Скрипт делает все три шага последовательно:
     1. Регистрация имейлов на pinmx.com (`create_emails.py`).
     2. Регистрация каждого email на app.devin.ai (`register_devin.py`).
     3. Генерация имени и адреса (`add_identities.py`) — **только** для
        тех аккаунтов, которые успешно зарегистрировались на Devin
        (фильтр по `аккаунты devin.txt`). Identity нужна для следующих
        этапов работы с аккаунтами, поэтому тратить её на провалившиеся
        signup-ы смысла нет.
3. Готовые email-аккаунты в `имейлы pingmx.txt`:
   ```
   nick@pingmx.com:password
   ```
4. Email-ы, успешно зарегистрированные на Devin, копятся в
   `аккаунты devin.txt` (по одному в строке). Ошибки регистрации —
   в `devin_errors.txt`.
5. Identity (имя + адрес) для каждого Devin-аккаунта — в `личности.txt`
   в формате TSV `email<TAB>Имя Фамилия, Улица, Индекс, Город`.

Опции `run_all.cmd` (пробрасываются в `create_emails.py` и `register_devin.py`):
- `--head` — показать окно браузера
- `--limit N` — обработать только первые N ников/аккаунтов
- `--browser-mode {clean|incognito|system}` — см. ниже раздел «Режимы запуска»

Опции `add_identities.py` (если хочешь запускать отдельно):
```cmd
.venv\Scripts\python.exe add_identities.py --url https://www.fakenamegenerator.com/gen-female-us-us.php
.venv\Scripts\python.exe add_identities.py --only-from "аккаунты devin.txt"
```

## Файлы

| Файл | Что |
|---|---|
| `имена для имейлов.txt` | вход: список ников |
| `имейлы pingmx.txt` | выход (шаг 1): пары `email:password` |
| `имейлы pingmx.txt.bak` | автобэкап перед каждым запуском шага 1 |
| `taken.txt` | автонакопление: ники, занятые на сайте, пропускаются в следующих запусках |
| `аккаунты devin.txt` | выход (шаг 2): email-ы, успешно зарегистрированные на Devin |
| `devin_errors.txt` | выход (шаг 2): TSV `email<TAB>причина` для неудачных регистраций Devin |
| `личности.txt` | выход (шаг 3): TSV `email<TAB>Имя Фамилия, Улица, Индекс, Город` |
| `бины.txt` | вход: список BIN-номеров карт (используется отдельной задачей) |
| `create_emails.py` | шаг 1: регистрация (Playwright + ddddocr) |
| `register_devin.py` | шаг 2: регистрация каждого email на app.devin.ai |
| `add_identities.py` | шаг 3: личности из fakenamegenerator (только для Devin-аккаунтов) |
| `start_devin_trial.py` | отдельная задача: переход к Stripe-checkout «Start free trial» |
| `browser_modes.py` | общий модуль для трёх режимов запуска браузера |
| `copy_chrome_profile.py` | копирует системный Chrome-профиль в `./browser_profile/` для режима `system` |
| `run_all.cmd` | запускает шаги 1 → 2 → 3; делает setup при первом запуске |
| `requirements.txt` | зависимости |

## Архитектура

- `create_emails.py` запускает Chrome через Playwright (`channel="chrome"`),
  переключает суффикс на `@pingmx.com`, скачивает картинку капчи прямо с её
  URL, решает локально через `ddddocr` (принимает только 6 цифр).
  Перехватывает ответ API `/v1/random-mail/create-by-device` и по `code`
  определяет результат: `200` — успех (email из API, пароль из DOM),
  `81001` — Invalid Captcha, `Email Already Exists` — занято, `1001` —
  Invalid Parameters (retry).
- `register_devin.py` логинится в `mail-client.pinmx.com` (тоже через
  локальный `ddddocr` для капчи), открывает signup на `app.devin.ai`,
  ждёт письмо с кодом, вводит код, дожидается успешного редиректа.
- `add_identities.py` тянет HTML с `fakenamegenerator.com`, парсит `<h3>`
  и `<div class="adr">`, **записывает** результат в `личности.txt` в
  формате TSV. Существующие записи не перетираются — пропуск идемпотентный.
- `start_devin_trial.py` — интерактивный скрипт: логинится Devin-аккаунтом,
  переходит «My Team → Upgrade → Start free trial», заполняет адрес и
  тестовые данные карты в Stripe Checkout, оставляет окно открытым.
- Идемпотентность: все скрипты пропускают уже обработанное.

## Шаг 2: Регистрация на Devin

После того как `create_emails.py` создал аккаунты, скрипт
`register_devin.py` берёт пары `email:password` из `имейлы pingmx.txt` и
регистрирует каждый email на
[`https://app.devin.ai/auth/signup`](https://app.devin.ai/auth/signup).

### Запуск

```cmd
.venv\Scripts\python.exe register_devin.py [опции]
```

Опции:

- `--head` — показать окно браузера (по умолчанию headless).
- `--limit N` — обработать только первые N аккаунтов из очереди.
- `--no-skip-done` — не пропускать email-ы, уже отмеченные в
  `аккаунты devin.txt`.
- `--delay SEC` — пауза между аккаунтами в секундах (по умолчанию `2`).
- `--browser-mode {clean|incognito|system}` — режим запуска Chrome
  (см. ниже).
- `--help` — справка по флагам.

### Что делает скрипт

Для каждого аккаунта последовательно:

1. Открывает [`https://mail-client.pinmx.com/`](https://mail-client.pinmx.com/),
   логинится по email и паролю из `имейлы pingmx.txt`. На форме
   появляется модалка с 6-значной цифровой капчей — она решается
   локально через `ddddocr` тем же способом, что и капча на pinmx.com/ru.
2. В соседней вкладке открывает `https://app.devin.ai/auth/signup`,
   вводит email и нажимает Sign up.
3. Возвращается во вкладку почты, ждёт письмо от Devin (до 5 минут),
   открывает его и регуляркой `(?<!\d)(\d{6})(?!\d)` достаёт 6-значный
   код подтверждения.
4. Возвращается на вкладку Devin, вводит код в поле
   `autocomplete="one-time-code"`. Devin auto-submit-ит код при наборе
   6-й цифры; если по какой-то причине этого не произошло — скрипт
   кликает кнопку Continue.
5. После успешного редиректа с `/auth/...` записывает email в
   `аккаунты devin.txt`. При любой ошибке — пишет строку
   `email<TAB>причина` в `devin_errors.txt` и идёт к следующему аккаунту.

### Идемпотентность

При повторном запуске скрипт читает `аккаунты devin.txt` и пропускает
все email-ы, уже там перечисленные. Сравнение нечувствительно к
регистру. Чтобы переобработать «успешные» аккаунты — передай
`--no-skip-done`.

## Шаг 3: Identity для Devin-аккаунтов

`add_identities.py` запускается последним и добавляет случайную
личность (имя + адрес) **только** к тем email-ам, которые есть в
`аккаунты devin.txt`. Identity пишутся в **отдельный** файл
`личности.txt` в формате TSV (одна запись в строке):

```
email<TAB>Имя Фамилия, Улица, Индекс, Город
```

Файл `имейлы pingmx.txt` не модифицируется — это позволяет переиспользовать
один и тот же список email-ов под разные пайплайны без риска повредить
данные.

`run_all.cmd` запускает шаг 3 как:

```cmd
.venv\Scripts\python.exe add_identities.py --only-from "аккаунты devin.txt"
```

Если хочешь запустить без фильтра (для всех email-ов из
`имейлы pingmx.txt`, у которых ещё нет identity) — просто
`.venv\Scripts\python.exe add_identities.py`.

## Режимы запуска браузера

Все три скрипта (`create_emails.py`, `register_devin.py`,
`start_devin_trial.py`) принимают флаг `--browser-mode`:

| Режим | Что |
|---|---|
| `clean` (default) | Свежий пустой Playwright-context. Без расширений, истории, авторизаций. Полная изоляция между аккаунтами. |
| `incognito` | Chrome с флагом `--incognito`. Поведенчески эквивалентен `clean`. |
| `system` | Запуск Chrome с **копией** твоего реального профиля из `./browser_profile/`. Видит твои расширения, закладки, авторизации. |

Чтобы использовать `system`-режим:

1. **Закрой все окна Chrome** (включая фоновые процессы — проверь
   Диспетчер задач). На Windows Chrome держит блокировку на User Data,
   и если не все процессы убиты, копирование профиля упадёт с
   `Permission denied` на части файлов.
2. Запусти однократно:
   ```cmd
   .venv\Scripts\python.exe copy_chrome_profile.py
   ```
   Скрипт скопирует `%LOCALAPPDATA%\Google\Chrome\User Data\Default`
   в `./browser_profile/Default/`. Каши и тяжёлые служебные папки
   пропускаются.
3. Дальше можно запускать любой скрипт с `--browser-mode system`.
   Чтобы обновить копию (новые cookies/закладки) — снова закрой Chrome
   и снова запусти `copy_chrome_profile.py`.

## Лицензия

Личное использование. Распространение исходников — на свой страх и риск.
