import asyncio
import datetime
import json
import logging
import re
import uuid
from pathlib import Path

import httpx

from config import settings
from src.ingestion.db.connection import get_connection
from src.ingestion.db.sync_log import log_sync_event
from src.ingestion.todoist import TodoistClient


sync_trigger = asyncio.Event()
log = logging.getLogger(__name__)


async def sync_once(client: TodoistClient | None = None) -> None:
    """Reconcile local task changes with Todoist and import remote changes."""
    if not settings.TODOIST_ENABLED or not settings.TODOIST_API_KEY:
        return

    owns_client = client is None
    if client is None:
        client = TodoistClient(settings.TODOIST_API_KEY)
    try:
        await _sync_todoist(client)
    finally:
        if owns_client:
            await client.close()


async def _sync_todoist(client: TodoistClient) -> None:
    projects = await client.list_projects()
    project_by_id = {str(project["id"]): project for project in projects}
    project_by_name = {
        str(project["name"]).strip().casefold(): str(project["id"])
        for project in projects
        if project.get("name")
    }
    remote_tasks = await client.list_tasks()
    completion_history_available = True
    try:
        remote_tasks.extend(await client.list_recently_completed())
    except Exception:
        completion_history_available = False
        log.exception("Could not fetch Todoist completion history")
    remote_by_id = {str(task["id"]): task for task in remote_tasks if task.get("id")}
    known_remote_ids = set(remote_by_id)

    async with get_connection() as conn:
        cursor = await conn.execute("""
            SELECT t.*, n.file_path FROM tasks t
            LEFT JOIN notes n ON t.note_id = n.id
            WHERE t.sync_status IN ('local', 'pending')
                            AND (t.is_deleted=1 OR t.note_id IS NULL OR n.deleted_at IS NULL)
            ORDER BY t.id
        """)
        local_changes = await cursor.fetchall()

    for task in local_changes:
        remote_id = task["todolist_id"]
        task_data = dict(task)
        project_name = task["todoist_project_name"]
        if not task["is_deleted"] and project_name:
            project_id = project_by_name.get(str(project_name).strip().casefold())
            if not project_id:
                raise ValueError(
                    f"Todoist project {project_name!r} was not found; task remains queued"
                )
            task_data["todoist_project_id"] = project_id
        if task["is_deleted"]:
            if remote_id:
                await client.delete_task(str(remote_id))
        elif remote_id:
            remote_before = remote_by_id.get(str(remote_id), {})
            remote_done = bool(
                remote_before.get("checked", remote_before.get("is_completed", False))
            )
            if remote_before and remote_done and not task["is_done"]:
                await client.set_completed(str(remote_id), False)
            try:
                await client.update_task(
                    str(remote_id),
                    task_data,
                    current_project_id=remote_before.get("project_id"),
                )
                if remote_before and not remote_done and task["is_done"]:
                    await client.set_completed(str(remote_id), True)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                created = await client.create_task(
                    task_data, _create_request_id(task["id"])
                )
                remote_id = str(created["id"])
                if task["is_done"]:
                    await client.set_completed(remote_id, True)
        else:
            created = await client.create_task(
                task_data, _create_request_id(task["id"])
            )
            remote_id = str(created["id"])
            if task["is_done"]:
                await client.set_completed(remote_id, True)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        updated_raw_text = task["raw_text"]
        if remote_id and not task["is_deleted"]:
            updated_raw_text = _add_todoist_marker(
                task["file_path"], task["raw_text"], task["title"], remote_id
            )
        async with get_connection() as conn:
            await conn.execute(
                """UPDATE tasks SET todolist_id=?, raw_text=?,
                   todoist_project_id=COALESCE(?, todoist_project_id),
                   sync_status='synced', last_synced_at=? WHERE id=?""",
                (
                    remote_id, updated_raw_text,
                    task_data.get("todoist_project_id"), now, task["id"],
                ),
            )
            await conn.commit()
        if remote_id:
            known_remote_ids.add(str(remote_id))
            remote_by_id.pop(str(remote_id), None)
        await log_sync_event(
            event_type="todoist_sync",
            entity_type="task",
            entity_id=task["id"],
            file_path="",
            status="synced",
        )

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with get_connection() as conn:
        for remote_id, remote in remote_by_id.items():
            cursor = await conn.execute(
                """SELECT t.id, t.sync_status, t.raw_text, t.title,
                          t.note_id, t.todoist_project_id, t.todoist_project_name,
                          n.file_path
                   FROM tasks t LEFT JOIN notes n ON t.note_id=n.id
                   WHERE t.todolist_id=?""",
                (remote_id,),
            )
            local = await cursor.fetchone()
            if local and local["sync_status"] in ("local", "pending"):
                continue

            title = str(remote.get("content") or "").strip()
            if not title:
                continue
            due = remote.get("due") or {}
            due_date = due.get("datetime") or due.get("date")
            labels = remote.get("labels") or []
            tags = ",".join(str(label) for label in labels) or None
            is_done = int(bool(remote.get("checked", remote.get("is_completed", False))))
            remote_priority = int(remote.get("priority", 1))
            priority = {2: "low", 3: "medium", 4: "high"}.get(remote_priority)
            project_id = str(remote.get("project_id") or "") or None
            project_name = project_by_id.get(project_id, {}).get("name") if project_id else None
            raw_text = _format_markdown_task(
                title, is_done, labels, remote_priority, due_date, project_name, remote_id
            )

            if local:
                await conn.execute("""
                    UPDATE tasks SET title=?, raw_text=?, is_done=?, is_deleted=0,
                        due_date=?, todoist_due_timezone=?, priority=?, tags=?, todoist_project_id=?,
                        todoist_project_name=?, sync_status='synced',
                        last_synced_at=? WHERE id=?
                """, (
                    title, raw_text, is_done, due_date, due.get("timezone"), priority, tags,
                    project_id, project_name, now, local["id"],
                ))
                if local["file_path"]:
                    _update_note_task(
                        local["file_path"], local["raw_text"], local["title"],
                        raw_text,
                    )
                else:
                    _append_to_journal(remote, raw_text)
            else:
                await conn.execute("""
                    INSERT INTO tasks
                        (raw_text, title, is_done, due_date, priority, tags,
                         todolist_id, todoist_due_timezone,
                         todoist_project_id, todoist_project_name,
                         sync_status, last_synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'synced', ?)
                """, (
                    raw_text, title, is_done, due_date, priority, tags,
                    remote_id, due.get("timezone"), project_id, project_name, now,
                ))
                _append_to_journal(remote, raw_text)
        await conn.commit()

    if completion_history_available:
        await _mark_remote_deletions(known_remote_ids)


def _update_note_task(
    file_path: str, old_raw_text: str, old_title: str, new_raw_text: str
) -> None:
    path = Path(file_path)
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        marker = re.search(r"<!--\s*todoist:([^\s>]+)\s*-->", new_raw_text)
        old_normalized = re.sub(
            r"<!--\s*todoist:[^\s>]+\s*-->", "", old_raw_text
        ).strip()
        old_normalized = re.sub(r"^\s*(?:[-*]\s+)?", "", old_normalized).strip()
        selected = None
        exact_matches = []
        for index, line in enumerate(lines):
            content = line.rstrip("\r\n")
            if marker and marker.group(0) in content:
                selected = index
                break
            content_normalized = re.sub(
                r"<!--\s*todoist:[^\s>]+\s*-->", "", content
            ).strip()
            content_normalized = re.sub(
                r"^\s*(?:[-*]\s+)?", "", content_normalized
            ).strip()
            if content_normalized == old_normalized:
                exact_matches.append(index)
        if selected is None and len(exact_matches) == 1:
            selected = exact_matches[0]
        if selected is None:
            title_matches = [index for index, line in enumerate(lines) if old_title in line]
            if len(title_matches) == 1:
                selected = title_matches[0]
        if selected is not None:
            index = selected
            line = lines[index]
            content = line.rstrip("\r\n")
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            checkbox = re.match(r"^(\s*[-*]\s+)\[([ xX])\](.*?)$", content)
            if checkbox:
                prefix = checkbox.group(1)
                new_checkbox = "[x]" if new_raw_text.startswith("- [x]") else "[ ]"
                lines[index] = f"{prefix}{new_checkbox} {new_raw_text[6:]}{newline}"
            elif re.match(r"^(\s*(?:[-*]\s+)?)(todo|done)\s*:", content, re.IGNORECASE):
                prefix_match = re.match(
                    r"^(\s*(?:[-*]\s+)?)(todo|done)\s*:", content, re.IGNORECASE
                )
                status = "DONE" if new_raw_text.startswith("- [x]") else "TODO"
                lines[index] = f"{prefix_match.group(1)}{status}: {new_raw_text[6:]}{newline}"
            else:
                lines[index] = f"{new_raw_text}{newline}"
            path.write_text("".join(lines), encoding="utf-8")
    except OSError:
        log.exception("Could not update Markdown task in %s", path)


def _create_request_id(task_id: int) -> str:
    database = Path(settings.DB_PATH)
    if not database.is_absolute():
        database = Path(settings.ACTIVE_FOLDER) / database
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"larper:{database.resolve()}:{task_id}"))


def _format_markdown_task(
    title: str,
    is_done: int,
    labels: list[str],
    priority: int,
    due_date: str | None,
    project_name: str | None,
    todoist_id: str,
) -> str:
    metadata = [f"#{label}" for label in labels]
    priority_marker = {2: "[??]", 3: "[?]", 4: "[!]"}.get(priority)
    if priority_marker:
        metadata.append(priority_marker)
    if due_date:
        metadata.append(f"@due {due_date}")
    if project_name:
        if '"' not in project_name:
            metadata.append(f'proj:"{project_name}"')
        elif "'" not in project_name:
            metadata.append(f"proj:'{project_name}'")
        else:
            metadata.append(f"proj:{project_name}")
    line = f"- [{'x' if is_done else ' '}] {title}"
    if metadata:
        line += " " + " ".join(metadata)
    return f"{line} <!-- todoist:{todoist_id} -->"


def _add_todoist_marker(
    file_path: str | None, raw_text: str, title: str, todoist_id: str
) -> str:
    marker = f"<!-- todoist:{todoist_id} -->"
    if marker in raw_text or not file_path:
        return raw_text
    path = Path(file_path)
    if not path.is_file():
        return raw_text
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        exact_matches = []
        for index, line in enumerate(lines):
            content = line.rstrip("\r\n")
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            if marker in content:
                return content
            normalized = re.sub(r"^\s*(?:[-*]\s+)?", "", content).strip()
            if normalized == raw_text.strip():
                exact_matches.append((index, content, newline))
        if exact_matches:
            index, content, newline = exact_matches[0]
        else:
            title_matches = [
                (index, line.rstrip("\r\n"),
                 "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else "")
                for index, line in enumerate(lines)
                if title in line
            ]
            if len(title_matches) != 1:
                return raw_text
            index, content, newline = title_matches[0]
        lines[index] = f"{content} {marker}{newline}"
        path.write_text("".join(lines), encoding="utf-8")
        return f"{content} {marker}"
    except OSError:
        log.exception("Could not write Todoist identity marker to %s", path)
    return raw_text


def _append_to_journal(remote: dict, raw_text: str) -> None:
    todoist_id = str(remote.get("id", ""))
    active_folder = Path(settings.ACTIVE_FOLDER).resolve()
    journals = active_folder / "journals"
    if todoist_id:
        marker = f"<!-- todoist:{todoist_id} -->"
        for candidate in active_folder.rglob("*.md"):
            try:
                if marker in candidate.read_text(encoding="utf-8"):
                    return
            except OSError:
                log.warning("Could not inspect Markdown file %s for Todoist ID", candidate)

    created = remote.get("created_at") or remote.get("added_at")
    try:
        journal_date = datetime.datetime.fromisoformat(
            str(created).replace("Z", "+00:00")
        ).astimezone().date()
    except (TypeError, ValueError):
        journal_date = datetime.date.today()
    day = journal_date.isoformat()
    path = journals / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# {day}\n\n", encoding="utf-8")
    content = path.read_text(encoding="utf-8")
    if todoist_id and f"<!-- todoist:{todoist_id} -->" in content:
        return
    if content and not content.endswith("\n"):
        content += "\n"
    path.write_text(f"{content}{raw_text}\n", encoding="utf-8")


async def _mark_remote_deletions(remote_ids: set[str]) -> None:
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=89)
    ).isoformat()
    async with get_connection() as conn:
        cursor = await conn.execute("""
            SELECT t.id, t.todolist_id, t.raw_text, t.title, n.file_path
            FROM tasks t LEFT JOIN notes n ON n.id=t.note_id
            WHERE t.todolist_id IS NOT NULL AND t.is_done=0
              AND t.is_deleted=0 AND t.last_synced_at >= ?
              AND (t.note_id IS NULL OR n.deleted_at IS NULL)
        """, (cutoff,))
        rows = await cursor.fetchall()
        for task in rows:
            if str(task["todolist_id"]) in remote_ids:
                continue
            if task["file_path"]:
                _remove_note_task(
                    task["file_path"], task["raw_text"], task["title"],
                    str(task["todolist_id"]),
                )
            await conn.execute(
                "UPDATE tasks SET is_deleted=1, sync_status='synced' WHERE id=?",
                (task["id"],),
            )
        await conn.commit()


def _remove_note_task(
    file_path: str, raw_text: str, title: str, todoist_id: str
) -> None:
    path = Path(file_path)
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, line in enumerate(lines):
            content = line.rstrip("\r\n")
            if f"<!-- todoist:{todoist_id} -->" in content or content == raw_text:
                del lines[index]
                path.write_text("".join(lines), encoding="utf-8")
                return
    except OSError:
        log.exception("Could not remove Markdown task from %s", path)


def trigger_sync() -> None:
    """Signal the sync worker to run immediately (useful after DB writes)."""
    try:
        sync_trigger.set()
    except Exception:
        pass


async def sync_worker() -> None:
    """Periodically reconcile task changes when Todoist sync is enabled."""
    poll_interval = max(10, settings.TODOIST_SYNC_INTERVAL_SECONDS)
    wait_seconds = float(poll_interval)
    failure_backoff = 5.0
    while True:
        try:
            await sync_once()
            wait_seconds = float(poll_interval)
            failure_backoff = 5.0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Todoist synchronization failed")
            wait_seconds = failure_backoff
            failure_backoff = min(failure_backoff * 2, 300.0)
        try:
            await asyncio.wait_for(sync_trigger.wait(), timeout=wait_seconds)
            sync_trigger.clear()
        except asyncio.TimeoutError:
            pass