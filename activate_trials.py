"""activate_trials: финальный шаг конвейера — активация Devin-триала
со Stripe Checkout (карта + hCaptcha) под Camoufox.

Логика
======

Каждому воркеру выдаётся свой Camoufox persistent_context (копия
Firefox-профиля из ``--profile``). Это нужно для двух вещей:

* hCaptcha-checkbox триггерится антибот-детектором; обычный Playwright-
  Chromium его не проходит, а ``camoufox`` (`humanize=True`) — проходит.
* Изоляция cookies/расширений между воркерами, чтобы Devin-сессии не
  пересекались.

Воркер берёт следующий Devin-аккаунт из общей ``asyncio.Queue``,
поднимает свой контекст, делает:

    mail-login → Devin-login (через email-код) → My Team / Upgrade /
    Start free trial → Stripe Checkout: Card → адрес из identity →
    карта (NUMBER|MM|YYYY|CVV из БД) → consent-checkbox'ы → Submit →
    hCaptcha-checkbox → wait_for_url('**/dashboard**')

Карты берутся в порядке `confirmed → live` через :meth:`AccountDB.get_cards`
с дедупликацией по уже сделанным попыткам.

GUI ожидает функцию :func:`activate_trials_pipeline`, возвращающую
``{"success": N, "failed": M, "total": K}`` — именно этот dict
``gui.py:_run_step5`` использует для пост-апдейта прогрессбара.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import threading
from pathlib import Path
from typing import Iterable, Union

from camoufox.async_api import AsyncNewBrowser
from playwright.async_api import async_playwright, BrowserContext

from activate_accounts import (
    _try_solve_hcaptcha_async,
    check_consent_checkboxes_async,
    click_submit_async,
    fill_card_async,
)
from devin_async import (
    Identity,
    fill_address_async,
    find_stripe_checkout_frame_async,
    go_to_plans_and_start_trial_async,
    login_to_devin_async,
    login_to_mailclient_async,
    select_card_payment_method_async,
)
from logging_utils import setup_logging
from register_devin import Account, StepError
from storage import AccountDB

# `is_set()` есть и у threading.Event, и у asyncio.Event — нам этого
# достаточно. GUI передаёт threading.Event (его можно безопасно
# выставлять из main-треда Tk), в тестах/CLI удобнее asyncio.Event.
StopEventLike = Union[threading.Event, asyncio.Event]

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "accounts.db"
TEMP_PROFILES_DIR = ROOT / "temp_profiles"

# Сколько секунд ждать /dashboard после submit + hCaptcha.
_DASHBOARD_WAIT_S = 60.0


# ---------------------------------------------------------------------------
# Помощники по сборке доменных объектов
# ---------------------------------------------------------------------------


def _identity_from_dict(d: dict) -> Identity:
    """`db.get_identity_by_email(email)` отдаёт dict — конвертируем в Identity."""
    return Identity(
        full_name=d.get("full_name", ""),
        street=d.get("street", ""),
        zip_code=d.get("zip_code", ""),
        city=d.get("city", ""),
    )


def _card_to_str(card_row: dict) -> str:
    """`db.get_cards()` возвращает строки cards-таблицы. Собираем
    обратно в формат ``NUMBER|MM|YYYY|CVV``."""
    return (
        f"{card_row['card_number']}|"
        f"{str(card_row['exp_month']).zfill(2)}|"
        f"{card_row['exp_year']}|"
        f"{card_row['cvv']}"
    )


def _holder_name(identity: Identity) -> str:
    return identity.full_name or "Cardholder"


# ---------------------------------------------------------------------------
# Активация одного аккаунта
# ---------------------------------------------------------------------------


async def activate_single_account(
    context: BrowserContext,
    *,
    account: Account,
    identity: Identity,
    card_str: str,
    timeout: float = 300.0,
) -> str:
    """Активировать Devin-триал для одного аккаунта.

    Returns:
        ``"success"`` — на /dashboard, карта прошла.
        ``"declined"`` — карта не прошла Stripe (или hCaptcha challenge).
        ``"failed"`` — техническая ошибка флоу (логин, навигация).
    """
    mail_page = await context.new_page()
    devin_page = await context.new_page()

    try:
        print(f"[trial-async] аккаунт: {account.email}")
        print(f"[trial-async] identity: {identity.full_name}, {identity.street}, "
              f"{identity.zip_code}, {identity.city}")
        print(f"[trial-async] карта: {card_str[:6]}...{card_str.split('|', 1)[0][-3:]}")

        # Шаг 1: mail-login.
        await login_to_mailclient_async(mail_page, account)
        print(f"[trial-async] {account.email}: mail-login OK")

        # Шаг 2: Devin login (через код из почты).
        await login_to_devin_async(devin_page, mail_page, account)
        print(f"[trial-async] {account.email}: devin-login OK, url={devin_page.url}")
        try:
            await devin_page.bring_to_front()
        except Exception:
            pass

        # Шаг 3: My Team → Upgrade → Start free trial.
        await go_to_plans_and_start_trial_async(devin_page)
        await asyncio.sleep(2.5)

        # Шаг 4: Stripe iframe + Card payment method + адрес.
        stripe_frame = await find_stripe_checkout_frame_async(devin_page)
        if stripe_frame is None:
            raise StepError("Stripe iframe не найден")

        await select_card_payment_method_async(stripe_frame)
        await asyncio.sleep(1.5)

        await fill_address_async(stripe_frame, identity)

        # Шаг 5: карта + consent + submit.
        await fill_card_async(stripe_frame, card_str, _holder_name(identity))
        await check_consent_checkboxes_async(stripe_frame)

        if not await click_submit_async(stripe_frame):
            print(f"[trial-async] {account.email}: submit-кнопка не сработала")
            return "failed"

        # Шаг 6: hCaptcha (если попросят).
        hcap_result = await _try_solve_hcaptcha_async(devin_page, timeout_s=20.0)
        print(f"[trial-async] {account.email}: hcaptcha={hcap_result!r}")
        if hcap_result in ("challenge", "failed"):
            return "declined"

        # Шаг 7: ждём /dashboard.
        try:
            await devin_page.wait_for_url(
                "**/dashboard**", timeout=int(_DASHBOARD_WAIT_S * 1000)
            )
            print(f"[trial-async] {account.email}: dashboard OK")
            return "success"
        except Exception:
            # Финальная проверка — может, URL уже корректный.
            if "/dashboard" in (devin_page.url or ""):
                return "success"
            print(f"[trial-async] {account.email}: dashboard timeout, "
                  f"url={devin_page.url}")
            return "declined"

    finally:
        for page in (mail_page, devin_page):
            try:
                await page.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Pipeline: воркеры + Camoufox-контексты
# ---------------------------------------------------------------------------


def _prepare_worker_profile(worker_id: int, base_profile: Path | None) -> Path:
    """Подготовить персональный профиль воркера в ``temp_profiles/``.

    Если ``base_profile`` задан и существует — копируем его. Иначе
    создаём пустую папку (Camoufox создаст там профиль с нуля при первом
    запуске).
    """
    TEMP_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    target = TEMP_PROFILES_DIR / f"worker_{worker_id}_profile"

    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    if base_profile is not None and base_profile.exists():
        shutil.copytree(base_profile, target)
    else:
        target.mkdir(parents=True, exist_ok=True)

    return target


async def _worker(
    worker_id: int,
    pw,
    queue: "asyncio.Queue[tuple[Account, Identity, str, int]]",
    db: AccountDB,
    *,
    profile_path: Path | None,
    timeout: float,
    stats: dict[str, int],
    stop_event: StopEventLike,
) -> None:
    """Async-воркер активации.

    Поднимает свой Camoufox persistent_context, тянет аккаунты из
    queue, обновляет ``stats`` и БД (`card_attempts`, `activated_accounts`).
    """
    worker_profile = _prepare_worker_profile(worker_id, profile_path)
    print(f"[worker {worker_id}] профиль: {worker_profile}")

    browser = None
    context = None
    try:
        browser = await AsyncNewBrowser(
            pw,
            headless=False,
            humanize=True,
            persistent_context=True,
            user_data_dir=str(worker_profile),
        )
        context = browser

        while not stop_event.is_set():
            try:
                account, identity, card_str, card_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            # Повторный чек после выборки из queue: если stop_event взвели
            # в этот момент, даже не начинаем активацию этого аккаунта.
            if stop_event.is_set():
                print(f"[worker {worker_id}] отмена до старта {account.email}")
                queue.task_done()
                break

            try:
                result = await asyncio.wait_for(
                    activate_single_account(
                        context,
                        account=account,
                        identity=identity,
                        card_str=card_str,
                        timeout=timeout,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                print(f"[worker {worker_id}] {account.email}: timeout {timeout}s")
                result = "failed"
            except Exception as exc:
                print(f"[worker {worker_id}] {account.email}: исключение {exc!r}")
                result = "failed"

            # Учёт в БД.
            try:
                outcome = (
                    "success" if result == "success"
                    else "declined" if result == "declined"
                    else "failed"
                )
                db.add_card_attempt(card_str, account.email, outcome)
                if result == "success":
                    db.mark_card_used(card_id, account.email)
                    db.add_activated_account(
                        account.email, card_str, _holder_name(identity)
                    )
            except Exception as exc:
                print(f"[worker {worker_id}] DB error: {exc!r}")

            if result == "success":
                stats["success"] += 1
            else:
                stats["failed"] += 1
            stats["done"] += 1

            queue.task_done()
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Сборка очереди: аккаунты × карты
# ---------------------------------------------------------------------------


def _build_queue(
    db: AccountDB,
) -> tuple[list[tuple[Account, Identity, str, int]], list[str]]:
    """Подобрать пары (аккаунт, identity, карта).

    Returns:
        (jobs, warnings) — jobs готовы к раздаче воркерам, warnings —
        строки-причины, по которым некоторые аккаунты не попали в очередь
        (нет identity, нет карты, уже активирован).
    """
    activated = {row["email"].lower() for row in db.get_activated_accounts()}
    identities = db.get_all_identities()

    devin_accounts = db.get_devin_accounts(status="success")
    cards = db.get_cards(status="confirmed") or db.get_cards(status="live")

    warnings: list[str] = []
    jobs: list[tuple[Account, Identity, str, int]] = []

    if not devin_accounts:
        warnings.append("нет аккаунтов с devin_status='success'")
        return jobs, warnings

    if not cards:
        warnings.append("нет карт со статусом 'confirmed' или 'live'")
        return jobs, warnings

    card_iter = iter(cards)

    for acc_row in devin_accounts:
        email = acc_row["email"]
        if email.lower() in activated:
            continue

        ident_dict = identities.get(email.lower())
        if ident_dict is None:
            warnings.append(f"{email}: нет identity — пропускаю")
            continue

        try:
            card_row = next(card_iter)
        except StopIteration:
            warnings.append("карты закончились — оставшиеся аккаунты пропущены")
            break

        account = Account(email=email, password=acc_row["password"])
        identity = _identity_from_dict(ident_dict)
        card_str = _card_to_str(card_row)
        jobs.append((account, identity, card_str, card_row["id"]))

    return jobs, warnings


# ---------------------------------------------------------------------------
# Pipeline (вызывается из GUI или CLI)
# ---------------------------------------------------------------------------


async def activate_trials_pipeline(
    db: AccountDB,
    parallel: int = 3,
    timeout: float = 300.0,
    profile_path: Path | None = None,
    stop_event: StopEventLike | None = None,
) -> dict[str, int]:
    """Параллельная активация Devin-триалов.

    Args:
        db: открытое подключение к accounts.db.
        parallel: количество одновременных Camoufox-воркеров.
        timeout: таймаут одной активации в секундах.
        profile_path: путь к базовому Firefox-профилю Camoufox (его
            копируют каждому воркеру). Может быть None — Camoufox
            создаст свежий профиль с нуля.
        stop_event: внешний event для отмены. Подходит как
            ``threading.Event`` (GUI взводит его из main-треда в
            ``stop_pipeline``), так и ``asyncio.Event`` (для тестов).
            Воркеры проверяют его между аккаунтами — текущий
            аккаунт доживёт до конца или timeout (Camoufox нельзя
            рвать в середине Stripe-checkout — браузер останется
            в зомби-состоянии).

    Returns:
        {"success": int, "failed": int, "total": int}
    """
    if stop_event is None:
        stop_event = asyncio.Event()

    jobs, warnings = _build_queue(db)
    for w in warnings:
        print(f"[trials] {w}")

    total = len(jobs)
    stats = {"success": 0, "failed": 0, "done": 0}

    if total == 0:
        return {"success": 0, "failed": 0, "total": 0}

    queue: "asyncio.Queue[tuple[Account, Identity, str, int]]" = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)

    parallel = max(1, min(parallel, total))
    print(f"[trials] стартую {parallel} воркер(ов), задач: {total}")

    async with async_playwright() as pw:
        workers = [
            asyncio.create_task(
                _worker(
                    i + 1,
                    pw,
                    queue,
                    db,
                    profile_path=profile_path,
                    timeout=timeout,
                    stats=stats,
                    stop_event=stop_event,
                ),
                name=f"trials-worker-{i + 1}",
            )
            for i in range(parallel)
        ]
        await asyncio.gather(*workers, return_exceptions=True)

    return {
        "success": stats["success"],
        "failed": stats["failed"],
        "total": total,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Активация Devin-триалов (шаг 5)")
    p.add_argument(
        "--parallel", type=int, default=3,
        help="одновременных Camoufox-воркеров (default 3)",
    )
    p.add_argument(
        "--timeout", type=int, default=300,
        help="таймаут одной активации в секундах (default 300)",
    )
    p.add_argument(
        "--profile", default=None,
        help="базовый Firefox-профиль Camoufox для копирования воркерам",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="детальные debug-логи + скриншоты при ошибках",
    )
    return p.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logger = setup_logging(debug=args.debug, log_to_file=True)
    logger.info(f"activate_trials запущен с {args}")

    db = AccountDB(DB_PATH)
    try:
        profile_path = Path(args.profile) if args.profile else None
        stats = asyncio.run(
            activate_trials_pipeline(
                db,
                parallel=args.parallel,
                timeout=args.timeout,
                profile_path=profile_path,
            )
        )
    finally:
        db.close()

    print()
    print("=" * 60)
    print(f" activate_trials: успешно {stats['success']} из {stats['total']} "
          f"(ошибок {stats['failed']})")
    print("=" * 60)
    return 0 if stats["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
