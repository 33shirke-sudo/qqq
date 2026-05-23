@echo off
REM Полный конвейер: pinmx.com (имейлы) + регистрация на Devin AI + identity.
REM
REM При первом запуске автоматически создаёт venv в .venv\ и ставит
REM зависимости из requirements.txt. Использует уже установленный системный
REM Chrome через Playwright channel="chrome" — Chromium не качается.
REM
REM Шаги (в этом порядке):
REM   1) create_emails.py    — массовая регистрация имейлов на pinmx.com
REM   2) register_devin.py   — для каждого email из 'имейлы pingmx.txt' создаёт
REM                            аккаунт на app.devin.ai
REM   3) add_identities.py   — генерирует Имя/Адрес из fakenamegenerator
REM                            (последним шагом, чтобы не тратить генерацию
REM                             личностей на аккаунты, которые не прошли
REM                             регистрацию на Devin — identity нужна для
REM                             следующих этапов работы с аккаунтами)
REM
REM Все флаги, переданные в run_all.cmd (например, --head или --limit 5),
REM пробрасываются в шаги 1 и 2 (они оба понимают --head/--limit/--no-skip-*).
REM Шаг 3 запускается без аргументов.

setlocal
cd /d "%~dp0"

REM Ищем готовое окружение
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "captcha_solver\.venv\Scripts\python.exe" set "PY=captcha_solver\.venv\Scripts\python.exe"

REM Если нет — создаём
if not defined PY (
    where py >nul 2>nul
    if errorlevel 1 (
        echo Не найден Python. Установи Python 3.11+ и поставь галочку "Add Python to PATH".
        pause
        exit /b 1
    )
    echo === Первый запуск: создаю окружение и ставлю зависимости...
    py -m venv .venv
    if errorlevel 1 ( echo Не удалось создать venv & pause & exit /b 1 )
    REM Используем литеральный путь, потому что %PY% внутри parens ещё пуст.
    .venv\Scripts\python.exe -m pip install --upgrade pip
    if errorlevel 1 ( echo Не удалось обновить pip & pause & exit /b 1 )
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 ( echo Не удалось установить зависимости & pause & exit /b 1 )
    set "PY=.venv\Scripts\python.exe"
    echo === Зависимости установлены.
) else (
    REM Venv уже есть — на случай, если в requirements.txt прилетели новые
    REM зависимости (например, после обновления архива), доставим их тихо.
    "%PY%" -m pip install -r requirements.txt --quiet
    if errorlevel 1 ( echo Не удалось обновить зависимости & pause & exit /b 1 )
)

echo.
echo === ШАГ 1/3: Регистрация имейлов на pinmx.com
"%PY%" create_emails.py %*
if errorlevel 1 ( echo Шаг 1 завершился с ошибкой & pause & exit /b 1 )

echo.
echo === ШАГ 2/3: Регистрация на Devin AI
"%PY%" register_devin.py %*
if errorlevel 1 ( echo Шаг 2 завершился с ошибкой & pause & exit /b 1 )

echo.
echo === ШАГ 3/3: Добавление имени и адреса (только для Devin-аккаунтов)
"%PY%" add_identities.py --only-from "аккаунты devin.txt"
if errorlevel 1 ( echo Шаг 3 завершился с ошибкой & pause & exit /b 1 )

echo.
echo Готово.
echo   - имейлы pingmx.txt:    созданные email-ы и пароли
echo   - аккаунты devin.txt:   email-ы успешно зарегистрированных Devin-аккаунтов
echo   - личности.txt:         Имя/Адрес для каждого Devin-аккаунта (TSV)
echo   - devin_errors.txt:     причины неудачных регистраций (если были)
pause
endlocal
