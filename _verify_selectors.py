"""Одноразовый исследовательский скрипт для mail-client.pinmx.com.

ЭТО НЕ АВТОТЕСТ. Имя файла начинается с подчёркивания, поэтому pytest его
не подхватывает. Скрипт реальным аккаунтом из ``results.txt`` входит в
почтовый клиент и снимает HTML-дампы UI, чтобы по ним зафиксировать
стабильные селекторы для задачи 11 (ожидание письма + извлечение кода).

Запуск (из ``pinmx-mailer/pinmx-mailer/``)::

    .venv\\Scripts\\python.exe tests\\_explore_mailclient.py --head

Опции:
    --head           Видимый браузер (по умолчанию тоже True — это
                     исследование, а не headless-запуск).
    --headless       Невидимый браузер.
    --account-index  Индекс аккаунта в ``results.txt`` (по умолчанию 0).
    --out-dir        Каталог для дампов (по умолчанию
                     ``tests/_explore_mailclient_dumps``).
    --post-login-wait Время в секундах, которое ждём после клика
                     «Авторизоваться» перед снятием дампов (по умолчанию 15).

Скрипт:
    1) Логинится самостоятельно (по плейсхолдерам / роли кнопки) и НЕ
       полагается на ``login_to_mailclient`` — нам важно увидеть UI
       даже если пост-логин-индикатор не сработал.
    2) Сохраняет ``body.inner_html()`` в ``inbox.html``.
    3) Печатает количество элементов по списку селекторов-кандидатов
       (refresh-кнопка, элементы списка писем, контейнер деталей).
    4) Кликает первое письмо (если найдено) и сохраняет ``detail.html``.

Ничего из найденного не пушится в репозиторий автоматически — оператор
просто читает дампы и принимает решение, какие селекторы зафиксировать
в ``register_devin.py``.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Делаем модуль ``register_devin`` импортируемым, когда скрипт запускают
# из произвольного места (например, ``python tests\_explore_mailclient.py``
# из корня проекта).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from register_devin import (  # noqa: E402
    Account,
    MAIL_LOGIN_URL,
    load_accounts,
    solve_digit_captcha,
)


# При логине mail-client может показать модалку с капчей. Селекторы
# взяты из живого DOM (см. tests/_explore_mailclient_dumps/inbox.html
# в первой итерации исследования).
_LOGIN_CAPTCHA_DIALOG = 'div[role="dialog"][aria-label="Введите капчу"]'
_LOGIN_CAPTCHA_IMG = '#weiqu_captcha_img_url'
_LOGIN_CAPTCHA_REFRESH = '#weiqu_captcha_img_change'
_LOGIN_CAPTCHA_INPUT = '.el-message-box__input input'
_LOGIN_CAPTCHA_SUBMIT = (
    '.el-message-box__btns button:has-text("проверять")'
)


# Кандидаты в селекторы, которые мы проверяем «количеством совпадений».
REFRESH_BUTTON_CANDIDATES: tuple[str, ...] = (
    'button:has-text("Обновить")',
    'button[title="Обновить"]',
    'button[aria-label="Обновить"]',
    'button:has-text("Refresh")',
    '[role="button"]:has-text("Обновить")',
    'i.el-icon-refresh',
    '.el-icon-refresh',
    '[class*="refresh"]',
    'button .el-icon-refresh',
    'button.el-button:has(i.el-icon-refresh)',
)

LIST_ITEM_CANDIDATES: tuple[str, ...] = (
    '.el-table__row',
    '[role="row"]',
    '.mail-list-item',
    '.mail-item',
    '.email-list-item',
    'li.mail',
    'ul li[class*="mail"]',
    '[class*="mail-list"] [class*="item"]',
    '.el-table__body tr',
    'tr.el-table__row',
    '.list-item',
    '[class*="message-item"]',
)

DETAIL_BODY_CANDIDATES: tuple[str, ...] = (
    '.mail-detail',
    '.email-detail',
    '[class*="mail-detail"]',
    '[class*="email-content"]',
    '[class*="mail-content"]',
    'div[role="dialog"]',
    'iframe',
    '.el-dialog__body',
    '[class*="detail"] iframe',
)


def _count(page, selectors: tuple[str, ...]) -> dict[str, int]:
    """Посчитать число элементов под каждый селектор-кандидат."""
    counts: dict[str, int] = {}
    for sel in selectors:
        try:
            counts[sel] = page.locator(sel).count()
        except Exception as exc:  # pragma: no cover - диагностика
            counts[sel] = -1
            print(f"  [warn] selector failed: {sel!r}: {exc}")
    return counts


def _print_counts(label: str, counts: dict[str, int]) -> None:
    print(f"\n=== {label} ===")
    for sel, n in counts.items():
        marker = "  " if n == 0 else "✓ " if n > 0 else "× "
        print(f"  {marker}{n:>4}  {sel}")


def _dump_html(page, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        html = page.locator("body").inner_html()
    except Exception as exc:
        html = f"<!-- inner_html failed: {exc} -->"
    out_path.write_text(html, encoding="utf-8")
    print(f"  → dumped {len(html):,} chars to {out_path}")


def _maybe_solve_login_captcha(page) -> bool:
    """Если после клика «Авторизоваться» появилась модалка с капчей —
    решить её локально и нажать «проверять». Возвращает True, если
    модалка была обнаружена и обработана (не обязательно успешно)."""
    try:
        page.wait_for_selector(_LOGIN_CAPTCHA_DIALOG, timeout=3_000)
    except Exception:
        return False

    print("[explore] обнаружена модалка капчи, решаю...")

    def _refresh() -> None:
        try:
            page.locator(_LOGIN_CAPTCHA_REFRESH).click(timeout=3_000)
        except Exception as exc:
            print(f"  [warn] refresh failed: {exc}")

    # На сайте mail-client.pinmx.com капча — 6 цифр (тот же стиль, что
    # и на pinmx.com/ru — см. create_emails.py).
    code = solve_digit_captcha(page, _LOGIN_CAPTCHA_IMG, _refresh, expected_len=6)
    if code is None:
        print("[explore] solve_digit_captcha не дал 6 цифр — пропускаю эту попытку")
        return True

    print(f"[explore] ввожу капчу: {code}")
    try:
        page.locator(_LOGIN_CAPTCHA_INPUT).fill(code)
        page.locator(_LOGIN_CAPTCHA_SUBMIT).click()
    except Exception as exc:
        print(f"  [warn] не получилось отправить капчу: {exc}")
        return True
    # Подождём пока модалка либо закроется, либо мигнёт «error».
    time.sleep(1.5)
    return True


def _do_login(page, account: Account, post_login_wait_s: float) -> None:
    """Логин «руками», максимально терпимый к нестабильности SPA."""
    print(f"[explore] открываю {MAIL_LOGIN_URL} ...")
    page.goto(MAIL_LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_selector(
        'input[placeholder="Введите свой адрес электронной почты"]',
        timeout=30_000,
    )
    page.locator(
        'input[placeholder="Введите свой адрес электронной почты"]'
    ).fill(account.email)
    page.locator('input[placeholder="Введите пароль"]').fill(account.password)
    print("[explore] нажимаю «Авторизоваться»...")
    page.get_by_role("button", name="Авторизоваться").click()

    # Возможна модальная капча — решаем её, пока URL не сменится либо
    # пока не исчерпаем попытки. Каждая неуспешная попытка вызывает
    # перерисовку модалки (или повторное появление формы), поэтому
    # дополнительно кликаем «Авторизоваться» по необходимости.
    deadline = time.monotonic() + max(post_login_wait_s, 30.0)
    attempts = 0
    while time.monotonic() < deadline:
        # Если уже не на форме логина — успех.
        if "#" in page.url and page.url.rsplit("#", 1)[-1].strip("/"):
            print(f"[explore] URL сменился → {page.url}")
            break
        # Если модалки нет, но мы всё ещё на форме — кликнем submit ещё раз.
        if not _maybe_solve_login_captcha(page):
            try:
                page.get_by_role("button", name="Авторизоваться").click(timeout=2_000)
            except Exception:
                pass
            time.sleep(1.5)
        attempts += 1
        if attempts > 25:
            print("[explore] не справился с капчей за 25 попыток — продолжаю как есть")
            break

    # Просто ждём ещё пару секунд — даём SPA дорисовать UI.
    # каким бы он ни был. Если логин не прошёл, это тоже полезный сигнал.
    print(f"[explore] жду {post_login_wait_s:.0f}с ...")
    time.sleep(post_login_wait_s)
    print(f"[explore] текущий URL: {page.url}")


def _pick_account(accounts: list[Account], index: int) -> Account:
    if not accounts:
        raise SystemExit("results.txt пустой или отсутствует — нечего исследовать.")
    if index < 0 or index >= len(accounts):
        raise SystemExit(
            f"--account-index {index} вне диапазона: всего {len(accounts)} аккаунтов."
        )
    return accounts[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", action="store_true", default=True)
    parser.add_argument("--headless", dest="head", action="store_false")
    parser.add_argument("--account-index", type=int, default=0)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_PROJECT_ROOT / "tests" / "_explore_mailclient_dumps",
    )
    parser.add_argument("--post-login-wait", type=float, default=15.0)
    args = parser.parse_args()

    accounts = load_accounts(_PROJECT_ROOT / "results.txt")
    account = _pick_account(accounts, args.account_index)
    print(f"[explore] использую аккаунт #{args.account_index}: {account.email}")
    print(f"[explore] dumps будут сохранены в {args.out_dir}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=not args.head)
        context = browser.new_context()
        page = context.new_page()
        try:
            _do_login(page, account, args.post_login_wait)

            _dump_html(page, args.out_dir / "inbox.html")

            inbox_refresh = _count(page, REFRESH_BUTTON_CANDIDATES)
            _print_counts("REFRESH BUTTON candidates", inbox_refresh)

            inbox_list = _count(page, LIST_ITEM_CANDIDATES)
            _print_counts("LIST ITEM candidates", inbox_list)

            # Попробуем все варианты по очереди — это исследование.
            opened = False
            for sel, n in inbox_list.items():
                if n <= 0:
                    continue
                print(f"\n[explore] кликаю первый элемент '{sel}' (n={n})...")
                try:
                    page.locator(sel).first.click(timeout=5_000)
                    time.sleep(2.0)
                    opened = True
                    break
                except Exception as exc:
                    print(f"  не получилось: {exc}")

            if not opened:
                print(
                    "\n[explore] не удалось кликнуть ни один элемент списка. "
                    "Возможно, во входящих 0 писем — это нормально, всё равно "
                    "снимаю дамп после ожидания."
                )
            else:
                _dump_html(page, args.out_dir / "detail.html")
                detail_body = _count(page, DETAIL_BODY_CANDIDATES)
                _print_counts("DETAIL BODY candidates", detail_body)

            print(f"\n[explore] финальный URL: {page.url}")

        except Exception as exc:
            print(f"\n[explore] исключение в main: {exc!r}")
            # Всё равно пытаемся снять последний дамп — он может быть полезен.
            try:
                _dump_html(page, args.out_dir / "crash.html")
            except Exception:
                pass
            raise
        finally:
            print("\n[explore] закрываю браузер...")
            context.close()
            browser.close()

    print("\n[explore] готово. Изучите дампы и счётчики выше, чтобы выбрать селекторы.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
