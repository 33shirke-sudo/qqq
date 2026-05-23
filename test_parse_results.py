"""Быстрая проверка финальных селекторов поверх готового дампа.

Берём ``inbox.html`` (снятый в _explore_mailclient.py) и через BeautifulSoup-
подобный парсинг (на самом деле — простую регулярку и ``.count()`` через
sub-string match) проверяем, что селекторы действительно находят элементы.

Основная цель — убедиться, что селекторы стабильны на снятом DOM, без
повторного открытия живого браузера.
"""

from __future__ import annotations

from pathlib import Path


def _count_substring_occurrences(html: str, needle: str) -> int:
    """Количество вхождений подстроки в HTML."""
    return html.count(needle)


def main() -> int:
    here = Path(__file__).resolve().parent
    inbox = here / "_explore_mailclient_dumps" / "inbox.html"
    if not inbox.exists():
        print(f"[verify] нет дампа {inbox}")
        return 1

    html = inbox.read_text(encoding="utf-8")

    # Маркеры, которые подтверждают, что мы вошли (а не остались на форме).
    markers = {
        "RL-MailMessageList view-model": '"rl-view-model RL-MailMessageList"',
        "класс messageList": 'class="messageList',
        "RL-MailMessageView view-model": '"rl-view-model RL-MailMessageView"',
        "класс messageListItem": 'class="messageListItem"',
        "буtton buttonReload": '"btn single btn-dark-disabled-border buttonReload',
        "контейнер b-message-view-wrapper": '"b-content b-message-view-wrapper',
        "контейнер messageItem fixIndex": '"messageItem fixIndex"',
        "папка Входящие текст": '>Входящие<',
        "класс is-inbox selected": 'is-inbox selected',
    }

    print(f"[verify] {inbox} ({len(html):,} chars)\n")
    for label, marker in markers.items():
        n = _count_substring_occurrences(html, marker)
        ok = "✓" if n > 0 else "×"
        print(f"  {ok} {n:>3}  {label!s}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
