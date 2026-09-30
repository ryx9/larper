import asyncio

import pytest
from datetime import date, datetime, timedelta
from pathlib import Path

from config import settings
from src.ingestion.db.connection import get_connection
from src.ingestion.db.notes import upsert_note
from src.ingestion.db.schema import init_db
from src.ingestion.db.tasks import insert_tasks
from src.TUI.state.store import Item
from src.TUI.widgets.todos import TodoItem, TodosPanel, format_due_date


@pytest.mark.asyncio
async def test_insert_tasks_preserves_completed_and_open(tmp_path):
    settings.ACTIVE_FOLDER = str(tmp_path)
    settings.DB_PATH = "notes.db"
    await init_db()

    note_path = tmp_path / "task_note.md"
    note_path.write_text("- [ ] Open task\n- [x] Done task @due 2020-01-01\n", encoding="utf-8")

    note_id = await upsert_note(
        str(note_path),
        "task_note",
        "page",
        note_path.read_text(encoding="utf-8"),
        "created",
    )

    tasks = [
        {
            "block_id": None,
            "raw_text": "- [ ] Open task",
            "title": "Open task",
            "is_done": 0,
            "due_date": None,
            "priority": None,
            "tags": None,
            "recurrence": None,
            "start_date": None,
        },
        {
            "block_id": None,
            "raw_text": "- [x] Done task @due 2020-01-01",
            "title": "Done task",
            "is_done": 1,
            "due_date": "2020-01-01",
            "priority": None,
            "tags": None,
            "recurrence": None,
            "start_date": None,
        },
    ]

    await insert_tasks(note_id, tasks)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT title, is_done, is_deleted, due_date FROM tasks ORDER BY title"
        )
        rows = await cursor.fetchall()

    assert len(rows) == 2
    assert rows[0]["title"] == "Done task"
    assert rows[0]["is_done"] == 1
    assert rows[0]["is_deleted"] == 0
    assert rows[0]["due_date"] == "2020-01-01"
    assert rows[1]["title"] == "Open task"
    assert rows[1]["is_done"] == 0
    assert rows[1]["is_deleted"] == 0


@pytest.mark.asyncio
async def test_todos_panel_hides_completed_tasks(tmp_path):
    settings.ACTIVE_FOLDER = str(tmp_path)
    settings.DB_PATH = "notes.db"
    await init_db()

    note_path = tmp_path / "task_note.md"
    note_path.write_text("- [ ] Open task\n- [x] Done task @due 2020-01-01\n", encoding="utf-8")

    note_id = await upsert_note(
        str(note_path),
        "task_note",
        "page",
        note_path.read_text(encoding="utf-8"),
        "created",
    )

    tasks = [
        {
            "block_id": None,
            "raw_text": "- [ ] Open task",
            "title": "Open task",
            "is_done": 0,
            "due_date": None,
            "priority": None,
            "tags": None,
            "recurrence": None,
            "start_date": None,
        },
        {
            "block_id": None,
            "raw_text": "- [x] Done task @due 2020-01-01",
            "title": "Done task",
            "is_done": 1,
            "due_date": "2020-01-01",
            "priority": None,
            "tags": None,
            "recurrence": None,
            "start_date": None,
        },
    ]

    await insert_tasks(note_id, tasks)

    panel = TodosPanel()
    entries = await panel._get_tasks_with_ids()

    assert len(entries) == 1
    assert entries[0][0].text == "Open task"
    assert entries[0][2] == 0


def test_todo_item_marks_overdue_for_past_due_date():
    yesterday = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    item = Item(
        id="1",
        text="Past due task",
        file=str(Path("task_note.md")),
        created_at=datetime.utcnow(),
        date=yesterday,
    )

    todo_item = TodoItem(item, task_id=1, raw_text="- [ ] Past due task", is_done=False)

    assert todo_item._is_overdue is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-29", "Today"),
        ("2026-09-29T09:30:00", "Today at 9:30 AM"),
        ("2026-09-30T14:05:00", "Tomorrow at 2:05 PM"),
        ("2026-10-03", "Saturday"),
    ],
)
def test_due_dates_are_displayed_naturally(value, expected):
    assert format_due_date(value, today=date(2026, 9, 29)) == expected


@pytest.mark.asyncio
async def test_completed_todo_removal_is_deferred_until_after_refresh(monkeypatch):
    todo_item = TodoItem(
        Item(
            id="1",
            text="Complete this",
            file="missing.md",
            created_at=datetime.now(),
            date=None,
        ),
        task_id=0,
    )
    scheduled = []
    removed = []

    async def fake_remove():
        removed.append(True)

    monkeypatch.setattr(todo_item, "remove", fake_remove)
    monkeypatch.setattr(
        todo_item, "call_after_refresh", lambda callback: scheduled.append(callback)
    )

    await todo_item.toggle_done()

    assert todo_item._is_done is True
    assert len(scheduled) == 1
    assert removed == []
    await scheduled[0]()
    assert removed == [True]
