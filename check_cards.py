"""check_cards: для каждого BIN из 'бины.txt' прогнать чекинг на chkr.cc
и записать живые карты в 'живые карты.txt'.

Поддерживает параллельный запуск в нескольких вкладках одного браузера
(``--tabs N``, default 3). Все вкладки делят один общий
``asyncio.Queue`` BIN-ов: как только вкладка освободилась — берёт
следующий BIN. Один общий файл-результат с дедупликацией через
``asyncio.Lock``, append + flush + fsync для устойчивости к Ctrl+C.

После основной фазы чекинга, если в ``живые карты.txt`` есть карты,
скрипт пере-проверяет их: вставляет все номера в textarea ``#cc``,
кликает START, дожидается финального вердикта и записывает
карты, оставшиеся «Live», в ``подтверждённые живые карты.txt``.

Сценарий чекинга одной партии (повторяет ручные действия):

1. Прочитать список BIN-ов из ``бины.txt``.
2. Открыть https://chkr.cc/ в N параллельных вкладках.
3. Для каждой вкладки взять BIN из очереди:
   a. ``button[data-bs-target="#bin-generator"]`` → модалка.
   b. ``#bin = BIN``, ``#quantity = --quantity``.
   c. ``a#gen`` (GENERATE) → закрыть модалку.
   d. ``button#start`` → окно проверки.
   e. Пока ``button#modal-stop`` видно — поллим ``#liveResults``,
      каждую новую строку пишем в ``живые карты.txt``.
   f. Окно закрылось — финальный замер, перенос к следующему BIN.

Запуск (из папки проекта)::

    .venv\\Scripts\\python.exe check_cards.py [опции]

Опции:
    --quantity N        сколько карт генерировать на каждый BIN (default 10)
    --tabs N            параллельных вкладок чекинга (default 3)
    --browser-mode      clean / incognito / system (default clean)
    --head              показать окно браузера (по умолчанию visible)
    --headless          невидимый браузер
    --check-timeout S   максимальное время одной партии (default 300)
    --no-recheck        пропустить финальную пере-проверку живых карт
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Iterable

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)

from browser_modes import VALID_MODES, DEFAULT_MODE, launch_browser_async
from paths import (
    BINS_FILE,
    CONFIRMED_LIVE_CARDS_FILE,
    DB_FILE,
    LIVE_CARDS_FILE,
)
from selectors_ import (
    CHKR_BIN_INPUT_SELECTORS,
    CHKR_CC_TEXTAREA_SELECTORS,
    CHKR_GENERATE_BUTTON_SELECTORS,
    CHKR_LIVE_RESULTS_SELECTORS,
    CHKR_OPEN_GENERATOR_SELECTORS,
    CHKR_PROGRESS_STOP_SELECTORS,
    CHKR_QUANTITY_INPUT_SELECTORS,
    CHKR_START_BUTTON_SELECTORS,
    find_any_async,
)
from storage import AccountDB


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

# P2-1: имена в paths.py; алиасы для старого кода.
BINS_PATH = BINS_FILE
LIVE_CARDS_PATH = LIVE_CARDS_FILE
CONFIRMED_LIVE_CARDS_PATH = CONFIRMED_LIVE_CARDS_FILE
DB_PATH = DB_FILE
CHKR_URL = "https://chkr.cc/"

# P2-7: селекторы chkr.cc вынесены в selectors_.py (по кортежу на
# каждую логическую точку). Используем [0]-элемент для
# быстрых ``page.locator(...)``-вызовов (сохраняем былое поведение),
# и ``find_any_async(...)`` в ключевых точках ждуна.
_OPEN_GENERATOR_SELECTOR = CHKR_OPEN_GENERATOR_SELECTORS[0]
_BIN_INPUT_SELECTOR = CHKR_BIN_INPUT_SELECTORS[0]
_QUANTITY_INPUT_SELECTOR = CHKR_QUANTITY_INPUT_SELECTORS[0]
_GENERATE_BUTTON_SELECTOR = CHKR_GENERATE_BUTTON_SELECTORS[0]
_GEN_MODAL_CLOSE_SELECTOR = "#bin-generator button.close"
_START_BUTTON_SELECTOR = CHKR_START_BUTTON_SELECTORS[0]
_PROGRESS_MODAL_STOP_SELECTOR = CHKR_PROGRESS_STOP_SELECTORS[0]
_LIVE_RESULTS_SELECTOR = CHKR_LIVE_RESULTS_SELECTORS[0]
_CC_TEXTAREA_SELECTOR = CHKR_CC_TEXTAREA_SELECTORS[0]

_PROGRESS_POLL_INTERVAL_S = 0.7
_DEFAULT_CHECK_TIMEOUT_S = 1200.0


# ---------------------------------------------------------------------------
# Чтение BIN-ов и запись живых карт
# ---------------------------------------------------------------------------


def load_bins(path: Path) -> list[str]:
    """Прочитать BIN-ы из файла. Игнорирует пустые/комменты/не-цифры."""
    if not path.exists():
        return []
    bins: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if not s.isdigit():
            print(f"  [bins] пропуск (не цифры): {s!r}")
            continue
        if s in seen:
            continue
        seen.add(s)
        bins.append(s)
    return bins


def load_existing_live_cards(path: Path) -> set[str]:
    """Уже записанные карты — для дедупликации между запусками."""
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip().lstrip("\ufeff")
        if s:
            out.add(s)
    return out


def append_card(path: Path, card: str) -> None:
    """Дописать карту с flush + fsync."""
    with path.open("a", encoding="utf-8") as fh:
        fh.write(card + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except (OSError, AttributeError):
            pass


def extract_card_credentials(line: str) -> str | None:
    """Извлечь ``NUMBER|MM|YYYY|CVV`` из строки результата chkr.cc.

    chkr.cc отдаёт строки вида:
        ``Live | 6252419587791991|04|2031|934 | [BIN: ...] | Charge OK ...``

    Берём первый фрагмент, содержащий ровно три ``|``-разделителя
    и состоящий из digit-сегментов. Если не нашли — None.
    """
    # Разрезаем по " | " (с пробелами). Часто карта живёт во второй секции.
    for chunk in line.split(" | "):
        chunk = chunk.strip()
        parts = chunk.split("|")
        if len(parts) != 4:
            continue
        if not all(p.strip().isdigit() for p in parts):
            continue
        # 13-19 цифр номера, 1-2 месяц, 2 или 4 года, 3-4 CVV.
        num, mm, yy, cvv = (p.strip() for p in parts)
        if not (13 <= len(num) <= 19 and 1 <= len(mm) <= 2 and len(yy) in (2, 4) and 3 <= len(cvv) <= 4):
            continue
        return f"{num}|{mm}|{yy}|{cvv}"
    return None


# ---------------------------------------------------------------------------
# Async-операции с одной вкладкой
# ---------------------------------------------------------------------------


async def open_chkr(page: Page) -> None:
    """Открыть chkr.cc и дождаться готовности UI."""
    await page.goto(CHKR_URL, wait_until="domcontentloaded")
    # P2-7: ждём любой из вариантов START-кнопки; раньше был
    # жёсткий ``button#start`` и при редизайне chkr.cc пайплайн бы молча
    # падал по timeout.
    start_loc = await find_any_async(
        page, CHKR_START_BUTTON_SELECTORS, timeout_ms=3_000
    )
    if start_loc is None:
        raise PWTimeout(
            f"chkr.cc: ни один из селекторов START-кнопки не виден за 15s: "
            f"{CHKR_START_BUTTON_SELECTORS}"
        )
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except PWTimeout:
        pass
    # Astro+Bootstrap биндятся не моментально — даём 1 сек.
    await asyncio.sleep(1.0)


async def generate_cards(page: Page, bin_value: str, quantity: int) -> None:
    """Открыть BIN-генератор, ввести BIN+Quantity, нажать GENERATE."""
    await page.locator(_OPEN_GENERATOR_SELECTOR).click(timeout=5_000)
    bin_inp = page.locator(_BIN_INPUT_SELECTOR)
    await bin_inp.wait_for(state="visible", timeout=5_000)
    await asyncio.sleep(0.4)

    await bin_inp.fill(bin_value)
    await page.locator(_QUANTITY_INPUT_SELECTOR).fill(str(quantity))
    await page.locator(_GENERATE_BUTTON_SELECTOR).click(timeout=5_000)

    await asyncio.sleep(0.5)
    try:
        close_btn = page.locator(_GEN_MODAL_CLOSE_SELECTOR).first
        if await close_btn.is_visible(timeout=500):
            await close_btn.click(timeout=2_000)
    except Exception:
        pass
    await asyncio.sleep(0.7)


async def read_live_cards_from_dom(page: Page) -> list[str]:
    """Прочитать список «живых» карт из ``#liveResults``."""
    try:
        text = await page.locator(_LIVE_RESULTS_SELECTOR).inner_text(timeout=2_000)
    except (PWTimeout, Exception):
        return []
    out: list[str] = []
    for raw in text.split("\n"):
        s = raw.strip()
        if s:
            out.append(s)
    return out


async def run_start_and_collect(
    page: Page,
    *,
    seen_lock: asyncio.Lock,
    seen: set[str],
    out_path: Path,
    timeout_s: float,
    tab_label: str,
    db: AccountDB,
    bin_value: str,
) -> int:
    """Нажать START, поллить ``#liveResults``, дописывать новые в файл.

    Возвращает количество новых карт, добавленных в этот прогон.
    """
    await page.locator(_START_BUTTON_SELECTOR).click(timeout=5_000)
    try:
        await page.wait_for_selector(_PROGRESS_MODAL_STOP_SELECTOR, timeout=10_000)
    except PWTimeout:
        # Возможно, проверка уже моментально завершилась.
        pass

    written = 0
    deadline = asyncio.get_event_loop().time() + timeout_s
    loop = asyncio.get_event_loop()

    async def _flush_new_cards(label: str) -> int:
        """Прочитать live-results и записать новые карты. Возвращает count."""
        cards = await read_live_cards_from_dom(page)
        added = 0
        async with seen_lock:
            for c in cards:
                if c in seen:
                    continue
                seen.add(c)
                # Записываем в файл для совместимости
                append_card(out_path, c)
                # Записываем в БД
                cred = extract_card_credentials(c)
                if cred:
                    parts = cred.split('|')
                    if len(parts) == 4:
                        db.add_card(parts[0], parts[1], parts[2], parts[3], bin_value, status='live')
                added += 1
                print(f"  [{tab_label} +live{label}] {c}")
        return added

    while loop.time() < deadline:
        written += await _flush_new_cards("")

        try:
            visible = await page.locator(_PROGRESS_MODAL_STOP_SELECTOR).first.is_visible()
        except Exception:
            visible = False

        if not visible:
            # Финальный замер: окно закрылось.
            await asyncio.sleep(1.0)
            written += await _flush_new_cards(" final")
            return written

        await asyncio.sleep(_PROGRESS_POLL_INTERVAL_S)

    # Таймаут — финальный замер и выход.
    print(f"  [{tab_label}] таймаут ({timeout_s:.0f}с), финальный замер")
    written += await _flush_new_cards(" timeout")
    return written


# ---------------------------------------------------------------------------
# Worker для одной вкладки
# ---------------------------------------------------------------------------


async def tab_worker(
    tab_index: int,
    page: Page,
    *,
    queue: asyncio.Queue,
    quantity: int,
    seen_lock: asyncio.Lock,
    seen: set[str],
    out_path: Path,
    check_timeout_s: float,
    db: AccountDB,
) -> int:
    """Цикл: пока в очереди есть BIN-ы — берёт и проверяет.

    Возвращает суммарное число добавленных живых карт за всю работу
    вкладки.
    """
    label = f"tab{tab_index}"
    total_new = 0

    try:
        await open_chkr(page)
    except Exception as exc:
        print(f"[{label}] не удалось открыть chkr.cc: {exc}")
        return 0

    while True:
        try:
            bin_value = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        try:
            print(f"[{label}] >>> BIN={bin_value}")
            try:
                await generate_cards(page, bin_value, quantity)
            except Exception as exc:
                print(f"[{label}] не удалось сгенерировать карты для {bin_value}: {exc}")
                continue

            try:
                new = await run_start_and_collect(
                    page,
                    seen_lock=seen_lock,
                    seen=seen,
                    out_path=out_path,
                    check_timeout_s=check_timeout_s,
                    tab_label=label,
                    db=db,
                    bin_value=bin_value,
                )
                total_new += new
                print(f"[{label}] BIN {bin_value} завершён, новых живых: {new}")
            except Exception as exc:
                print(f"[{label}] ошибка чекинга {bin_value}: {exc}")
        finally:
            # Помечаем BIN как обработанный
            db.mark_bin_done(bin_value)
            queue.task_done()

    print(f"[{label}] очередь пуста, выход")
    return total_new


# ---------------------------------------------------------------------------
# Recheck-фаза: пере-проверка карт из 'живые карты.txt'
# ---------------------------------------------------------------------------


async def recheck_live_cards(
    page: Page,
    *,
    cards: list[str],
    out_path: Path,
    seen: set[str],
    timeout_s: float,
    db: AccountDB,
) -> int:
    """Пере-проверить кандидатов: вставить в textarea #cc и нажать START.

    Каждая карта в ``cards`` — это уже NUMBER|MM|YYYY|CVV.
    Финал: всё, что попало в #liveResults, дописываем в ``out_path``
    (с дедупликацией по ``seen``).

    Возвращает кол-во новых записей в ``out_path``.
    """
    if not cards:
        print("[recheck] нет кандидатов, пропускаю")
        return 0

    print(f"[recheck] открываю чистую chkr.cc для пере-проверки {len(cards)} карт...")
    await open_chkr(page)

    # Заполняем textarea — chkr.cc ожидает по одной карте на строку.
    payload = "\n".join(cards)
    try:
        await page.locator(_CC_TEXTAREA_SELECTOR).fill(payload, timeout=5_000)
    except Exception as exc:
        print(f"[recheck] не удалось заполнить textarea: {exc}")
        return 0

    await asyncio.sleep(0.5)

    seen_lock = asyncio.Lock()
    new_confirmed = await run_start_and_collect(
        page,
        seen_lock=seen_lock,
        seen=seen,
        out_path=out_path,
        timeout_s=timeout_s,
        tab_label="recheck",
        db=db,
        bin_value="",  # Для recheck BIN неизвестен
    )

    # Обновляем статус карт в БД с live на confirmed
    for card_str in seen:
        parts = card_str.split('|')
        if len(parts) == 4:
            # Находим карту в БД и обновляем статус
            all_live = db.get_cards(status='live')
            for card in all_live:
                if (card['card_number'] == parts[0] and
                    card['exp_month'] == parts[1] and
                    card['exp_year'] == parts[2] and
                    card['cvv'] == parts[3]):
                    db.mark_card_confirmed(card['id'])
                    break

    return new_confirmed


# ---------------------------------------------------------------------------
# Главный async-main
# ---------------------------------------------------------------------------


async def run_check_cards_pipeline(
    context: BrowserContext,
    *,
    bins: list[str],
    quantity: int,
    tabs: int,
    check_timeout_s: float,
    do_recheck: bool,
    clean_first: bool,
    db: AccountDB,
) -> tuple[int, int]:
    """Прогнать полный pipeline чекинга на готовом ``BrowserContext``.

    Используется как из CLI (``check_cards.py``), так и из объединённого
    скрипта (``find_cards_and_open_trial.py``), чтобы не плодить
    дублирующий код запуска браузера.

    Args:
        context: уже созданный Playwright ``BrowserContext``.
        bins: список BIN-ов для чекинга.
        quantity: сколько карт на партию.
        tabs: число параллельных вкладок.
        check_timeout_s: таймаут одной партии.
        do_recheck: запускать ли фазу 2 (пере-проверка).
        clean_first: очищать ли файлы результатов перед запуском.
        db: экземпляр AccountDB для работы с БД.

    Returns:
        ``(phase1_new, confirmed_new)`` — сколько новых живых карт
        добавлено в фазе 1 и сколько подтверждено в фазе 2.
    """
    if clean_first:
        # Очистка БД вместо файлов
        print("[clean] очистка карт в БД...")
        # Удаляем все карты со статусом live и confirmed
        for card in db.get_cards(status='live'):
            db.delete_card(card['id'])
        for card in db.get_cards(status='confirmed'):
            db.delete_card(card['id'])

    # Загружаем уже существующие карты из БД для дедупликации
    existing_live = db.get_cards(status='live')
    existing_confirmed = db.get_cards(status='confirmed')
    seen_cards = set()
    for card in existing_live + existing_confirmed:
        # Формируем строку в формате chkr.cc для дедупликации
        card_str = f"{card['card_number']}|{card['exp_month']}|{card['exp_year']}|{card['cvv']}"
        seen_cards.add(card_str)

    print(f"Уже записано живых карт ранее: {len(seen_cards)}")

    queue: asyncio.Queue = asyncio.Queue()
    for b in bins:
        queue.put_nowait(b)

    seen_lock = asyncio.Lock()

    tab_count = max(1, tabs)
    pages: list[Page] = []
    for _ in range(tab_count):
        pages.append(await context.new_page())

    print(f"\n=== Фаза 1: чекинг в {tab_count} вкладках ===")
    workers = [
        tab_worker(
            i + 1,
            pages[i],
            queue=queue,
            quantity=quantity,
            seen_lock=seen_lock,
            seen=seen_cards,
            out_path=LIVE_CARDS_PATH,
            check_timeout_s=check_timeout_s,
            db=db,
        )
        for i in range(tab_count)
    ]
    results = await asyncio.gather(*workers, return_exceptions=True)

    phase1_new = 0
    for r in results:
        if isinstance(r, BaseException):
            print(f"[main] worker упал: {r!r}")
        else:
            phase1_new += r

    print(
        f"\n=== Фаза 1 завершена: добавлено {phase1_new} новых живых "
        f"карт; всего в '{LIVE_CARDS_PATH.name}': {len(seen_cards)}"
    )

    confirmed_new = 0
    if do_recheck:
        # Загружаем карты из БД вместо файла
        all_cards = db.get_cards(status='live')
        credentials = []
        for card in all_cards:
            cred = f"{card['card_number']}|{card['exp_month']}|{card['exp_year']}|{card['cvv']}"
            credentials.append(cred)

        print(
            f"\n=== Фаза 2: пере-проверка {len(credentials)} карт из БД ==="
        )

        if credentials:
            # Загружаем уже подтвержденные карты из БД
            confirmed_cards = db.get_cards(status='confirmed')
            confirmed_seen = set()
            for card in confirmed_cards:
                card_str = f"{card['card_number']}|{card['exp_month']}|{card['exp_year']}|{card['cvv']}"
                confirmed_seen.add(card_str)
            print(f"[recheck] уже подтверждено ранее: {len(confirmed_seen)}")

            recheck_page = pages[0]
            confirmed_new = await recheck_live_cards(
                recheck_page,
                cards=credentials,
                out_path=CONFIRMED_LIVE_CARDS_PATH,
                seen=confirmed_seen,
                timeout_s=check_timeout_s,
                db=db,
            )
            print(
                f"\n=== Фаза 2 завершена: подтверждено новых {confirmed_new}; "
                f"всего в БД: {len(confirmed_seen)} ==="
            )
        else:
            print("[recheck] нечего пере-проверять")

    # Закрываем вкладки чекинга — context остаётся, его закроет caller.
    for p in pages:
        try:
            await p.close()
        except Exception:
            pass

    return phase1_new, confirmed_new


async def amain(args: argparse.Namespace) -> int:
    db = AccountDB(DB_PATH)

    # Импорт BIN-ов из указанного файла
    bins_file = Path(args.bins_file)
    if bins_file.exists():
        bins_imported = db.import_bins_from_txt(bins_file)
        if bins_imported > 0:
            print(f"[import] импортировано {bins_imported} BIN-ов из {bins_file.name}")

    bins = db.get_pending_bins()
    if not bins:
        print(
            f"В БД нет pending BIN-ов. "
            f"Добавь их через GUI или положи в {bins_file}.",
            file=sys.stderr,
        )
        return 1

    print(f"Прочитано BIN-ов из БД: {len(bins)}: {', '.join(bins)}")
    print(f"Параллельных вкладок: {args.tabs}")
    print(f"Quantity на партию: {args.quantity}")

    async with async_playwright() as pw:
        _browser, context, cleanup = await launch_browser_async(
            pw, args.browser_mode, head=args.head
        )
        try:
            await run_check_cards_pipeline(
                context,
                bins=bins,
                quantity=args.quantity,
                tabs=args.tabs,
                check_timeout_s=args.check_timeout,
                do_recheck=not args.no_recheck,
                clean_first=not args.keep_existing,
                db=db,
            )
        finally:
            await cleanup()

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--quantity",
        type=int,
        default=10,
        help="сколько карт генерировать на каждый BIN (default 10)",
    )
    p.add_argument(
        "--tabs",
        type=int,
        default=3,
        help="параллельных вкладок чекинга (default 3)",
    )
    p.add_argument(
        "--check-timeout",
        type=float,
        default=_DEFAULT_CHECK_TIMEOUT_S,
        metavar="SECONDS",
        help=(
            "максимальное время одной партии проверки. "
            f"По умолчанию {int(_DEFAULT_CHECK_TIMEOUT_S)}с"
        ),
    )
    p.add_argument(
        "--head",
        dest="head",
        action="store_true",
        default=True,
        help="показать окно браузера (по умолчанию)",
    )
    p.add_argument(
        "--headless",
        dest="head",
        action="store_false",
        help="скрыть окно браузера",
    )
    p.add_argument(
        "--no-recheck",
        action="store_true",
        help="пропустить финальную пере-проверку живых карт",
    )
    p.add_argument(
        "--keep-existing",
        action="store_true",
        help=(
            "не очищать 'живые карты.txt' и 'подтверждённые живые карты.txt' "
            "перед запуском (по умолчанию — очищаются, чтобы каждый запуск "
            "искал заново)"
        ),
    )
    p.add_argument(
        "--browser-mode",
        choices=VALID_MODES,
        default=DEFAULT_MODE,
        help=(
            "Режим запуска Chrome: 'clean' (default), 'incognito', "
            "'system' (требует ./browser_profile/, см. copy_chrome_profile.py)."
        ),
    )
    p.add_argument(
        "--bins-file",
        type=str,
        default=str(BINS_PATH),
        help=f"путь к файлу с BIN-кодами (по умолчанию {BINS_PATH.name})",
    )
    return p.parse_args(list(argv))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
