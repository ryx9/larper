from src.ingestion.db.connection import get_connection


def _normalized_raw_text(value: str | None) -> str:
    text = (value or "").strip()
    text = text.removeprefix("- ").removeprefix("* ").strip()
    text = text.removesuffix("\r").strip()
    return text


async def insert_tasks(note_id: int, tasks: list) -> None:
    """Insert/update local tasks for a note."""
    async with get_connection() as conn:
        # Null out block_id on ALL existing tasks for this note first —
        # blocks were just re-inserted with new rowids, so any surviving
        # task row still pointing at the old block rowids would violate the FK.
        await conn.execute(
            "UPDATE tasks SET block_id=NULL WHERE note_id=?", (note_id,)
        )

        cursor = await conn.execute(
            "SELECT * FROM tasks WHERE note_id=? AND is_deleted=0", (note_id,)
        )
        existing_tasks = await cursor.fetchall()
        existing_map = {row['title']: row for row in existing_tasks}

        new_titles = set()
        consumed_ids: set[int] = set()
        updated_count = 0
        inserted_count = 0

        for task in tasks:
            title = task['title']
            new_titles.add(title)

            linked_row = None
            if task.get('todoist_id'):
                cursor = await conn.execute(
                    "SELECT * FROM tasks WHERE todolist_id=?",
                    (task['todoist_id'],),
                )
                linked_row = await cursor.fetchone()

            if linked_row:
                consumed_ids.add(linked_row['id'])
                due_timezone = (
                    linked_row['todoist_due_timezone']
                    if task['due_date'] and 'T' in task['due_date']
                    else None
                )
                linked_needs_sync = any((
                    _normalized_raw_text(linked_row['raw_text'])
                    != _normalized_raw_text(task['raw_text']),
                    linked_row['title'] != title,
                    linked_row['is_done'] != task['is_done'],
                    linked_row['due_date'] != task['due_date'],
                    linked_row['todoist_due_timezone'] != due_timezone,
                    linked_row['priority'] != task.get('priority'),
                    linked_row['tags'] != task.get('tags'),
                    linked_row['todoist_project_name'] != task.get('todoist_project_name'),
                ))
                linked_sync_status = (
                    'local' if linked_needs_sync else linked_row['sync_status']
                )
                await conn.execute("""
                    UPDATE tasks SET note_id=?, block_id=?, raw_text=?, title=?,
                        is_done=?, is_deleted=0, due_date=?, todoist_due_timezone=?, priority=?, tags=?,
                        recurrence=?, start_date=?, todoist_project_name=?,
                        sync_status=?
                    WHERE id=?
                """, (
                    note_id, task['block_id'], task['raw_text'], title,
                    task['is_done'], task['due_date'], due_timezone, task.get('priority'),
                    task.get('tags'), task.get('recurrence'), task.get('start_date'),
                    task.get('todoist_project_name'), linked_sync_status,
                    linked_row['id'],
                ))
                updated_count += 1
                continue

            if title in existing_map:
                old = existing_map[title]
                consumed_ids.add(old['id'])
                due_timezone = (
                    old['todoist_due_timezone']
                    if task['due_date'] and 'T' in task['due_date']
                    else None
                )
                needs_sync = (
                    old['is_done'] != task['is_done']
                    or old['due_date'] != task['due_date']
                    or old['todoist_due_timezone'] != due_timezone
                    or _normalized_raw_text(old['raw_text'])
                    != _normalized_raw_text(task['raw_text'])
                    or old['priority'] != task.get('priority')
                    or old['tags'] != task.get('tags')
                    or old['todoist_project_name'] != task.get('todoist_project_name')
                )
                sync_status = 'local' if needs_sync else (old['sync_status'] or 'local')

                await conn.execute("""
                    UPDATE tasks
                    SET block_id=?, raw_text=?, is_done=?, due_date=?, todoist_due_timezone=?,
                        priority=?, tags=?, recurrence=?, start_date=?,
                        todoist_project_name=?, sync_status=?
                    WHERE id=?
                """, (
                    task['block_id'], task['raw_text'], task['is_done'],
                    task['due_date'], due_timezone, task.get('priority'),
                    task.get('tags'), task.get('recurrence'),
                    task.get('start_date'), task.get('todoist_project_name'),
                    sync_status, old['id'],
                ))
                updated_count += 1
            else:
                await conn.execute("""
                    INSERT INTO tasks
                        (note_id, block_id, raw_text, title, is_done, due_date,
                         priority, tags, recurrence, start_date,
                         todolist_id, todoist_project_name, sync_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'local')
                """, (
                    note_id, task['block_id'], task['raw_text'], title,
                    task['is_done'], task['due_date'], task.get('priority'),
                    task.get('tags'), task.get('recurrence'),
                    task.get('start_date'), task.get('todoist_id'),
                    task.get('todoist_project_name'),
                ))
                inserted_count += 1

        # Mark tasks not in new list as deleted
        deleted_count = 0
        for title, row in existing_map.items():
            if title not in new_titles and row['id'] not in consumed_ids:
                await conn.execute("""
                    UPDATE tasks SET is_deleted=1, sync_status='local' WHERE id=?
                """, (row['id'],))
                deleted_count += 1

        await conn.commit()
        print(f"--> [DB] Tasks for note {note_id}: "
              f"inserted={inserted_count}, updated={updated_count}, deleted={deleted_count}")
        # Deferred import to avoid circular dependency:
        # db/__init__ → tasks → sync_worker → db/__init__
        from src.ingestion.sync_worker import trigger_sync
        trigger_sync()
