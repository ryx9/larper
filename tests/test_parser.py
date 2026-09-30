import pytest
from src.ingestion.parser.patterns import TASK_PATTERN, DUE_DATE_PATTERN, TAG_PATTERN

def test_task_pattern():
    # Valid done tasks
    assert TASK_PATTERN.match("- [x] text").group('status_char') == "x"
    assert TASK_PATTERN.match("* [X] text").group('status_char') == "X"
    assert TASK_PATTERN.match("[v] text").group('status_char') == "v"
    assert TASK_PATTERN.match("[V] Some task").group('status_char') == "V"
    assert TASK_PATTERN.match("[-] In progress").group('status_char') == "-"
    assert TASK_PATTERN.match("[~] cancelled").group('status_char') == "~"
    
    # Valid open tasks
    m = TASK_PATTERN.match("[ ] Open task")
    assert m is not None
    assert m.group('status_char') == "" or m.group('status_char').isspace()

    # Weird spacing
    assert TASK_PATTERN.match("[  ] Open task").group('status_char') == ""
    assert TASK_PATTERN.match("[ x ] text").group('status_char') == "x"

    # TODOs
    assert TASK_PATTERN.match("TODO Buy milk").group('todo_text') == "Buy milk"
    assert TASK_PATTERN.match("Todo: Buy milk").group('todo_text') == "Buy milk"
    assert TASK_PATTERN.match("TODO : Buy milk").group('todo_text') == "Buy milk"

    # Negative test
    assert not TASK_PATTERN.match("Just normal text")
    assert not TASK_PATTERN.match("-[ ] Not at start")

def test_due_date_pattern():
    assert DUE_DATE_PATTERN.search("Task due: 2026-04-22").group(1) == "2026-04-22"
    assert DUE_DATE_PATTERN.search("Task @due 2026/04/22").group(1) == "2026/04/22"

def test_tag_pattern():
    assert TAG_PATTERN.findall("Hello #world and #python-3") == ["world", "python-3"]
    # Should not match hashtags in standard markdown links or wikilinks if they start right after [
    assert TAG_PATTERN.findall("See [[#Reference]] or [#1](url)") == []

from src.ingestion.parser.extractors import _extract_task_meta

def test_extract_task_meta():
    res1 = _extract_task_meta("content", "Buy milk @due 2026-04-22 [!]", "x", 1)
    assert res1['is_done'] == 1
    assert res1['due_date'] == "2026-04-22"
    assert res1['priority'] == "high"
    assert res1['title'] == "Buy milk"

    res2 = _extract_task_meta("content", "Watch movie TODO: get popcorn #fun", None, 2)
    assert res2['is_done'] == 0
    assert res2['tags'] == "fun"
    assert res2['title'] == "Watch movie TODO: get popcorn"

def test_extract_task_meta_preserves_natural_due_times():
    from datetime import datetime
    from src.ingestion.parser.extractors import _resolve_natural_date

    base = datetime(2026, 9, 30, 9, 0)
    assert _resolve_natural_date("today 4pm", base) == "2026-09-30T16:00"
    assert _resolve_natural_date("tomorrow 12:10pm", base) == "2026-10-01T12:10"

    task = _extract_task_meta(
        "done: call home @due today 4pm",
        "call home @due today 4pm",
        "done",
        1,
    )
    assert task["due_date"].endswith("T16:00")

if __name__ == "__main__":
    test_task_pattern()
    test_due_date_pattern()
    test_tag_pattern()
    test_extract_task_meta()
    print("ALL TESTS PASSED")
