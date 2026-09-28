import json

import httpx
import pytest
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from src.ingestion.db.connection import get_connection
from src.ingestion.db.notes import upsert_note
from src.ingestion.db.schema import init_db
from src.ingestion.db.tasks import insert_tasks
from src.ingestion.parser.core import parse_markdown
from src.ingestion.sync_worker import _update_note_task, sync_once
from src.ingestion.todoist import TodoistClient


@pytest.mark.asyncio
async def test_sync_once_pushes_local_and_imports_remote_tasks(tmp_path, monkeypatch):
    settings.ACTIVE_FOLDER = str(tmp_path)
    settings.DB_PATH = "notes.db"
    monkeypatch.setattr(settings, "TODOIST_ENABLED", True)
    monkeypatch.setattr(settings, "TODOIST_API_KEY", "test-token")
    await init_db()

    note_path = tmp_path / "tasks.md"
    note_path.write_text("- [ ] Old title\n", encoding="utf-8")
    note_id = await upsert_note(
        str(note_path), "tasks", "page", "- [ ] Old title\n", "created"
    )
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO tasks
                (note_id, raw_text, title, is_done, todolist_id, sync_status)
            VALUES (?, '- [ ] Old title', 'Old title', 0, 'remote-1', 'synced')
        """, (note_id,))
        await conn.execute("""
            INSERT INTO tasks
                (raw_text, title, is_done, tags, todoist_project_name, sync_status)
            VALUES ('- [ ] Local task #Work proj:"Work"', 'Local task', 0,
                    '["Work"]', 'Work', 'local')
        """)
        await conn.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/projects"):
            return httpx.Response(200, json={"results": [{
                "id": "project-1", "name": "Work"
            }], "next_cursor": None})
        if request.method == "GET" and request.url.path.endswith("/tasks"):
            created_at = datetime.now(timezone.utc).isoformat()
            return httpx.Response(200, json={"results": [{
                "id": "remote-1",
                "content": "Changed remotely",
                "checked": False,
                "due": {
                    "datetime": "2026-10-03T14:30:00",
                    "timezone": "America/New_York",
                },
                "priority": 4,
                "labels": ["work"],
                "project_id": "project-1",
            }, {
                "id": "remote-new",
                "content": "Added in Todoist",
                "checked": False,
                "labels": ["Project"],
                "project_id": "project-1",
                "created_at": created_at,
            }], "next_cursor": None})
        if request.method == "GET" and "completed" in request.url.path:
            return httpx.Response(200, json={"results": [], "next_cursor": None})
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            assert request.headers.get("X-Request-Id")
            assert request.read()
            return httpx.Response(200, json={"id": "created-1"})
        return httpx.Response(404, json={"error": "unexpected request"})

    client = TodoistClient("test-token", transport=httpx.MockTransport(handler))
    try:
        await sync_once(client)
    finally:
        await client.close()

    async with get_connection() as conn:
        cursor = await conn.execute(
                """SELECT title, todolist_id, sync_status, is_deleted, due_date,
                             todoist_due_timezone,
                             priority, tags, todoist_project_id, todoist_project_name
                    FROM tasks ORDER BY id"""
        )
        rows = await cursor.fetchall()

    assert rows[0]["title"] == "Changed remotely"
    assert rows[0]["sync_status"] == "synced"
    assert rows[0]["due_date"] == "2026-10-03T14:30:00"
    assert rows[0]["todoist_due_timezone"] == "America/New_York"
    assert rows[0]["priority"] == "high"
    assert rows[0]["tags"] == "work"
    assert rows[1]["title"] == "Local task"
    assert rows[1]["todolist_id"] == "created-1"
    assert rows[1]["sync_status"] == "synced"
    assert rows[1]["is_deleted"] == 0
    assert rows[0]["todoist_project_id"] == "project-1"
    assert rows[0]["todoist_project_name"] == "Work"
    assert rows[1]["todoist_project_id"] == "project-1"
    assert rows[1]["todoist_project_name"] == "Work"
    assert note_path.read_text(encoding="utf-8").startswith(
        "- [ ] Changed remotely #work [!] @due 2026-10-03T14:30:00 proj:\"Work\" "
        "<!-- todoist:remote-1 -->\n"
    )
    _, _, parsed_tasks, _, _ = parse_markdown(
        note_path, note_path.read_text(encoding="utf-8")
    )
    assert parsed_tasks[0]["title"] == "Changed remotely"
    assert parsed_tasks[0]["due_date"] == "2026-10-03T14:30:00"
    assert parsed_tasks[0]["priority"] == "high"
    assert parsed_tasks[0]["tags"] == "work"
    assert parsed_tasks[0]["todoist_id"] == "remote-1"
    assert parsed_tasks[0]["todoist_project_name"] == "Work"

    journal_path = next((tmp_path / "journals").glob("*.md"))
    journal_text = journal_path.read_text(encoding="utf-8")
    assert journal_text.count("todoist:remote-new") == 1
    journal_note_id = await upsert_note(
        str(journal_path), journal_path.stem, "journal", journal_text, "created"
    )
    _, _, journal_tasks, _, _ = parse_markdown(journal_path, journal_text)
    for task in journal_tasks:
        task["block_id"] = None
    await insert_tasks(journal_note_id, journal_tasks)
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) AS count FROM tasks")
        assert (await cursor.fetchone())["count"] == 3
        cursor = await conn.execute(
            "SELECT sync_status FROM tasks WHERE todolist_id='remote-new'"
        )
        assert (await cursor.fetchone())["sync_status"] == "synced"


@pytest.mark.asyncio
async def test_todoist_429_honors_retry_after(monkeypatch):
    attempts = 0
    delays = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"results": [], "next_cursor": None})

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("src.ingestion.todoist.asyncio.sleep", record_sleep)
    client = TodoistClient("test-token", transport=httpx.MockTransport(handler))
    try:
        assert await client.list_tasks() == []
    finally:
        await client.close()

    assert attempts == 2
    assert delays == [3.0]


@pytest.mark.asyncio
async def test_offline_local_task_remains_queued_until_reconnected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_FOLDER", str(tmp_path))
    monkeypatch.setattr(settings, "DB_PATH", "notes.db")
    monkeypatch.setattr(settings, "TODOIST_ENABLED", True)
    monkeypatch.setattr(settings, "TODOIST_API_KEY", "test-token")
    await init_db()
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO tasks (raw_text, title, is_done, sync_status)
            VALUES ('- [ ] Persist me', 'Persist me', 0, 'local')
        """)
        await conn.commit()

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    offline_client = TodoistClient("test-token", transport=httpx.MockTransport(offline))
    try:
        with pytest.raises(httpx.ConnectError):
            await sync_once(offline_client)
    finally:
        await offline_client.close()

    async with get_connection() as conn:
        cursor = await conn.execute("SELECT sync_status, todolist_id FROM tasks")
        row = await cursor.fetchone()
    assert row["sync_status"] == "local"
    assert row["todolist_id"] is None

    async def online(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/projects"):
            return httpx.Response(200, json={"results": [], "next_cursor": None})
        if request.method == "GET":
            return httpx.Response(200, json={"results": [], "next_cursor": None})
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"id": "after-reconnect"})
        return httpx.Response(404)

    online_client = TodoistClient("test-token", transport=httpx.MockTransport(online))
    try:
        await sync_once(online_client)
    finally:
        await online_client.close()

    async with get_connection() as conn:
        cursor = await conn.execute("SELECT sync_status, todolist_id FROM tasks")
        row = await cursor.fetchone()
    assert row["sync_status"] == "synced"
    assert row["todolist_id"] == "after-reconnect"


@pytest.mark.asyncio
async def test_markdown_edits_update_existing_todoist_task(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_FOLDER", str(tmp_path))
    monkeypatch.setattr(settings, "DB_PATH", "notes.db")
    monkeypatch.setattr(settings, "TODOIST_ENABLED", True)
    monkeypatch.setattr(settings, "TODOIST_API_KEY", "test-token")
    await init_db()

    note_path = tmp_path / "tasks.md"
    original = (
        "- [ ] Book flight @due 2026-10-05T09:00:00 "
        "<!-- todoist:linked-1 -->\n"
    )
    note_path.write_text(original, encoding="utf-8")
    note_id = await upsert_note(
        str(note_path), "tasks", "page", original, "created"
    )
    _, _, parsed, _, _ = parse_markdown(note_path, original)
    for task in parsed:
        task["block_id"] = None
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO tasks
                (note_id, raw_text, title, is_done, due_date, todolist_id,
                 sync_status)
            VALUES (?, ?, 'Book flight', 0, '2026-10-05T09:00:00',
                    'linked-1', 'synced')
        """, (note_id, parsed[0]["raw_text"]))
        await conn.commit()

    edited = (
        "- [ ] Book flight for Sam @due 2026-10-06T11:45:00 "
        "<!-- todoist:linked-1 -->\n"
    )
    note_path.write_text(edited, encoding="utf-8")
    _, _, parsed, _, _ = parse_markdown(note_path, edited)
    for task in parsed:
        task["block_id"] = None
    await insert_tasks(note_id, parsed)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, title, due_date, todolist_id, sync_status FROM tasks"
        )
        row = await cursor.fetchone()
        cursor = await conn.execute("SELECT COUNT(*) AS count FROM tasks")
        count = (await cursor.fetchone())["count"]
    assert count == 1
    assert row["title"] == "Book flight for Sam"
    assert row["due_date"] == "2026-10-06T11:45:00"
    assert row["todolist_id"] == "linked-1"
    assert row["sync_status"] == "local"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/projects"):
            return httpx.Response(200, json={"results": [], "next_cursor": None})
        if request.method == "GET" and request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"results": [{
                "id": "linked-1", "content": "Book flight", "checked": False,
            }], "next_cursor": None})
        if request.method == "GET":
            return httpx.Response(200, json={"results": [], "next_cursor": None})
        if request.method == "POST" and request.url.path.endswith("/linked-1"):
            payload = json.loads(request.content)
            assert payload["content"] == "Book flight for Sam"
            assert payload["due_datetime"] == "2026-10-06T11:45:00"
            return httpx.Response(200, json={"id": "linked-1"})
        return httpx.Response(404)

    client = TodoistClient("test-token", transport=httpx.MockTransport(handler))
    try:
        await sync_once(client)
    finally:
        await client.close()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS count, sync_status FROM tasks"
        )
        row = await cursor.fetchone()
    assert row["count"] == 1
    assert row["sync_status"] == "synced"


@pytest.mark.asyncio
async def test_markdown_due_date_removal_clears_todoist_and_database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_FOLDER", str(tmp_path))
    monkeypatch.setattr(settings, "DB_PATH", "notes.db")
    monkeypatch.setattr(settings, "TODOIST_ENABLED", True)
    monkeypatch.setattr(settings, "TODOIST_API_KEY", "test-token")
    await init_db()

    note_path = tmp_path / "tasks.md"
    original = "- [ ] Submit form @due 2026-10-05T09:00:00 <!-- todoist:clear-1 -->\n"
    note_path.write_text(original, encoding="utf-8")
    note_id = await upsert_note(
        str(note_path), "tasks", "page", original, "created"
    )
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO tasks
                (note_id, raw_text, title, due_date, todoist_due_timezone,
                 todolist_id, sync_status)
            VALUES (?, ?, 'Submit form', '2026-10-05T09:00:00',
                    'America/New_York', 'clear-1', 'synced')
        """, (note_id, original.strip()))
        await conn.commit()

    edited = "- [ ] Submit form <!-- todoist:clear-1 -->\n"
    note_path.write_text(edited, encoding="utf-8")
    _, _, parsed, _, _ = parse_markdown(note_path, edited)
    for task in parsed:
        task["block_id"] = None
    await insert_tasks(note_id, parsed)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/projects"):
            return httpx.Response(200, json={"results": [], "next_cursor": None})
        if request.method == "GET" and request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"results": [{
                "id": "clear-1",
                "content": "Submit form",
                "checked": False,
                "due": {
                    "datetime": "2026-10-05T09:00:00",
                    "timezone": "America/New_York",
                },
            }], "next_cursor": None})
        if request.method == "GET":
            return httpx.Response(200, json={"results": [], "next_cursor": None})
        if request.method == "POST" and request.url.path.endswith("/clear-1"):
            assert json.loads(request.content)["due_date"] is None
            return httpx.Response(200, json={"id": "clear-1"})
        return httpx.Response(404)

    client = TodoistClient("test-token", transport=httpx.MockTransport(handler))
    try:
        await sync_once(client)
    finally:
        await client.close()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT due_date, todoist_due_timezone, sync_status FROM tasks"
        )
        row = await cursor.fetchone()
    assert row["due_date"] is None
    assert row["todoist_due_timezone"] is None
    assert row["sync_status"] == "synced"


@pytest.mark.asyncio
async def test_markdown_project_change_moves_existing_todoist_task(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_FOLDER", str(tmp_path))
    monkeypatch.setattr(settings, "DB_PATH", "notes.db")
    monkeypatch.setattr(settings, "TODOIST_ENABLED", True)
    monkeypatch.setattr(settings, "TODOIST_API_KEY", "test-token")
    await init_db()

    note_path = tmp_path / "tasks.md"
    old_markdown = '- [ ] Prepare report proj:"Old Project" <!-- todoist:move-1 -->\n'
    note_path.write_text(old_markdown, encoding="utf-8")
    note_id = await upsert_note(str(note_path), "tasks", "page", old_markdown, "created")
    _, _, old_tasks, _, _ = parse_markdown(note_path, old_markdown)
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO tasks
                (note_id, raw_text, title, is_done, todolist_id,
                 todoist_project_id, todoist_project_name, sync_status)
            VALUES (?, ?, ?, 0, 'move-1', 'old-id', 'Old Project', 'synced')
        """, (note_id, old_tasks[0]["raw_text"], old_tasks[0]["title"]))
        await conn.commit()

    new_markdown = '- [ ] Prepare report proj:"New Project" <!-- todoist:move-1 -->\n'
    note_path.write_text(new_markdown, encoding="utf-8")
    _, _, parsed_tasks, _, _ = parse_markdown(note_path, new_markdown)
    for task in parsed_tasks:
        task["block_id"] = None
    await insert_tasks(note_id, parsed_tasks)

    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/projects"):
            return httpx.Response(200, json={"results": [
                {"id": "old-id", "name": "Old Project"},
                {"id": "new-id", "name": "New Project"},
            ], "next_cursor": None})
        if request.method == "GET" and request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"results": [{
                "id": "move-1", "content": "Prepare report",
                "project_id": "old-id", "checked": False,
            }], "next_cursor": None})
        if request.method == "GET":
            return httpx.Response(200, json={"results": [], "next_cursor": None})
        if request.method == "POST" and request.url.path.endswith("/move-1/move"):
            assert json.loads(request.content) == {"project_id": "new-id"}
            return httpx.Response(204)
        if request.method == "POST" and request.url.path.endswith("/move-1"):
            payload = json.loads(request.content)
            assert "project_id" not in payload
            assert payload["content"] == "Prepare report"
            return httpx.Response(200, json={"id": "move-1"})
        return httpx.Response(404)

    client = TodoistClient("test-token", transport=httpx.MockTransport(handler))
    try:
        await sync_once(client)
    finally:
        await client.close()

    assert any(request.url.path.endswith("/move-1/move") for request in requests)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT todolist_id, todoist_project_id, todoist_project_name, sync_status FROM tasks"
        )
        row = await cursor.fetchone()
        cursor = await conn.execute("SELECT COUNT(*) AS count FROM tasks")
        count = (await cursor.fetchone())["count"]
    assert count == 1
    assert row["todolist_id"] == "move-1"
    assert row["todoist_project_id"] == "new-id"
    assert row["todoist_project_name"] == "New Project"
    assert row["sync_status"] == "synced"


@pytest.mark.asyncio
async def test_todoist_project_move_updates_markdown_without_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ACTIVE_FOLDER", str(tmp_path))
    monkeypatch.setattr(settings, "DB_PATH", "notes.db")
    monkeypatch.setattr(settings, "TODOIST_ENABLED", True)
    monkeypatch.setattr(settings, "TODOIST_API_KEY", "test-token")
    await init_db()

    note_path = tmp_path / "tasks.md"
    markdown = '- [ ] Prepare report proj:"Old Project" <!-- todoist:move-2 -->\n'
    note_path.write_text(markdown, encoding="utf-8")
    note_id = await upsert_note(str(note_path), "tasks", "page", markdown, "created")
    _, _, parsed, _, _ = parse_markdown(note_path, markdown)
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO tasks
                (note_id, raw_text, title, is_done, todolist_id,
                 todoist_project_id, todoist_project_name, sync_status)
            VALUES (?, ?, ?, 0, 'move-2', 'old-id', 'Old Project', 'synced')
        """, (note_id, parsed[0]["raw_text"], parsed[0]["title"]))
        await conn.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/projects"):
            return httpx.Response(200, json={"results": [
                {"id": "old-id", "name": "Old Project"},
                {"id": "new-id", "name": "New Project"},
            ], "next_cursor": None})
        if request.method == "GET" and request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"results": [{
                "id": "move-2", "content": "Prepare report",
                "project_id": "new-id", "checked": False,
            }], "next_cursor": None})
        if request.method == "GET":
            return httpx.Response(200, json={"results": [], "next_cursor": None})
        return httpx.Response(404)

    client = TodoistClient("test-token", transport=httpx.MockTransport(handler))
    try:
        await sync_once(client)
    finally:
        await client.close()

    assert 'proj:"New Project"' in note_path.read_text(encoding="utf-8")
    assert note_path.read_text(encoding="utf-8").count("todoist:move-2") == 1
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT todolist_id, todoist_project_id, todoist_project_name FROM tasks"
        )
        row = await cursor.fetchone()
        cursor = await conn.execute("SELECT COUNT(*) AS count FROM tasks")
        count = (await cursor.fetchone())["count"]
    assert count == 1
    assert row["todolist_id"] == "move-2"
    assert row["todoist_project_id"] == "new-id"
    assert row["todoist_project_name"] == "New Project"


def test_marking_todo_done_updates_original_markdown_syntax(tmp_path):
    note_path = tmp_path / "journal.md"
    note_path.write_text("TODO: Review notes #work <!-- todoist:task-1 -->\n")

    _update_note_task(
        str(note_path),
        "TODO: Review notes #work <!-- todoist:task-1 -->",
        "Review notes",
        "- [x] Review notes #work <!-- todoist:task-1 -->",
    )

    assert note_path.read_text() == "DONE: Review notes #work <!-- todoist:task-1 -->\n"


def test_timed_due_and_project_metadata_round_trip():
    markdown = (
        '- [ ] Call supplier #Work proj:"Work" '
        "@due 2026-10-03T09:30:00 <!-- todoist:task-2 -->\n"
    )
    _, _, tasks, _, _ = parse_markdown(
        Path("journal.md"), markdown
    )

    task = tasks[0]
    assert task["title"] == "Call supplier"
    assert task["tags"] == "Work"
    assert task["todoist_project_name"] == "Work"
    assert task["due_date"] == "2026-10-03T09:30:00"
    assert task["todoist_id"] == "task-2"

    payload = TodoistClient._task_payload({
        "title": task["title"],
        "priority": None,
        "tags": task["tags"],
        "due_date": task["due_date"],
        "todoist_due_timezone": "America/New_York",
        "todoist_project_id": "project-1",
    })
    assert payload["due_datetime"] == "2026-10-03T09:30:00"
    assert "due_date" not in payload
    assert payload["due_timezone"] == "America/New_York"
    assert payload["project_id"] == "project-1"
    assert payload["labels"] == ["Work"]


@pytest.mark.parametrize(
    ("task_line", "expected_due"),
    [
        ("- [ ] ISO @due 2026-10-03T09:30:00", "2026-10-03T09:30:00"),
        ("- [ ] Space @due 2026-10-03 09:30", "2026-10-03T09:30:00"),
        ("- [ ] AM/PM @due 2026-10-03 9:30 AM", "2026-10-03T09:30"),
        ("- [ ] Relative @due tomorrow at 9:30 AM", None),
    ],
)
def test_due_time_is_not_dropped(task_line, expected_due):
    _, _, tasks, _, _ = parse_markdown(Path("journal.md"), task_line + "\n")

    if expected_due is None:
        assert "T" in tasks[0]["due_date"]
        assert tasks[0]["due_date"].endswith("09:30")
    else:
        assert tasks[0]["due_date"] == expected_due