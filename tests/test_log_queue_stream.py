"""Тест P1-6: ``LogQueueStream`` префиксует строки и корректно работает
без префикса для stdout-стрима.
"""

from __future__ import annotations

import queue as queue_module
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def stream_no_prefix():
    from pipeline_runner import LogQueueStream

    q = queue_module.Queue()
    return LogQueueStream(q), q


@pytest.fixture
def stream_with_prefix():
    from pipeline_runner import LogQueueStream

    q = queue_module.Queue()
    return LogQueueStream(q, prefix="[stderr] "), q


def _drain(q):
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue_module.Empty:
            break
    return items


def test_stream_without_prefix_keeps_lines_as_is(stream_no_prefix) -> None:
    sink, q = stream_no_prefix
    sink.write("hello\nworld\n")
    assert _drain(q) == ["hello", "world"]


def test_stream_with_prefix_adds_prefix_per_line(stream_with_prefix) -> None:
    sink, q = stream_with_prefix
    sink.write("err1\nerr2\n")
    assert _drain(q) == ["[stderr] err1", "[stderr] err2"]


def test_stream_with_prefix_handles_partial_lines(stream_with_prefix) -> None:
    """Префикс применяется только к полным строкам (на newline); partial-fragment
    остаётся в буфере до flush."""
    sink, q = stream_with_prefix
    sink.write("partial")
    assert _drain(q) == [], "до newline ничего не уходит в очередь"

    sink.write(" line\n")
    assert _drain(q) == ["[stderr] partial line"]


def test_stream_with_prefix_flush_adds_prefix(stream_with_prefix) -> None:
    """``flush()`` высыпает остаток буфера, тоже с префиксом."""
    sink, q = stream_with_prefix
    sink.write("orphan no newline")
    sink.flush()
    assert _drain(q) == ["[stderr] orphan no newline"]


def test_stream_with_empty_prefix_is_no_op(stream_no_prefix) -> None:
    """Префикс = пустая строка не должен ничего добавлять."""
    sink, q = stream_no_prefix
    sink.write("plain\n")
    sink.write("more\n")
    sink.flush()
    assert _drain(q) == ["plain", "more"]


def test_stream_with_prefix_handles_blank_lines(stream_with_prefix) -> None:
    """Пустые строки тоже префиксуются (это видимое поведение, и пустая
    строка от traceback'а должна быть промаркирована)."""
    sink, q = stream_with_prefix
    sink.write("a\n\nb\n")
    assert _drain(q) == ["[stderr] a", "[stderr] ", "[stderr] b"]
