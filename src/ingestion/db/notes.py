import datetime
from pathlib import Path

from src.ingestion.db.connection import get_connection


async def upsert_note(
    file_path: str, title: str, note_type: str, raw_content: str, event_type: str
) -> int:
    """Insert or update a note in the database."""
    now = datetime.datetime.utcnow().isoformat()
    note_id = -1
    # Always use absolute path for DB lookup
    abs_path = str(Path(file_path).resolve())
    try:
        async with get_connection() as conn:
            if event_type == "created":
                cursor = await conn.execute(
                    """
                    INSERT INTO notes (file_path, title, note_type, raw_content, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (abs_path, title, note_type, raw_content, now, now),
                )
                note_id = cursor.lastrowid
                print(f"--> [DB] Created note: {title} (ID: {note_id})")

            elif event_type == "modified":
                check = await conn.execute(
                    "SELECT id FROM notes WHERE file_path=?", (abs_path,)
                )
                existing = await check.fetchone()

                if existing:
                    await conn.execute(
                        """
                        UPDATE notes SET title=?, raw_content=?, updated_at=? WHERE file_path=?
                    """,
                        (title, raw_content, now, abs_path),
                    )
                    note_id = existing["id"]
                    print(f"--> [DB] Updated note: {title} (ID: {note_id})")
                else:
                    cursor = await conn.execute(
                        """
                        INSERT INTO notes (file_path, title, note_type, raw_content, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """,
                        (abs_path, title, note_type, raw_content, now, now),
                    )
                    note_id = cursor.lastrowid
                    print(
                        f"--> [DB] Created note (from modified event): {title} (ID: {note_id})"
                    )

            await conn.commit()
        try:
            from src.ingestion.sync_worker import trigger_sync

            trigger_sync()
        except Exception:
            pass
    except Exception as e:
        print(f"[ERROR] upsert_note failed for {file_path}: {e}")
        note_id = -1
    return note_id


async def delete_note(file_path: str) -> None:
    """Soft-delete a note by setting deleted_at timestamp, and purge its
    blocks from the FAISS vector index so searches never return stale results."""
    now = datetime.datetime.utcnow().isoformat()
    abs_path = str(Path(file_path).resolve())
    async with get_connection() as conn:
        # Collect block IDs *before* the soft-delete so we can purge them from
        # the vector index while we still have a reliable handle on them.
        cursor = await conn.execute(
            """SELECT b.id FROM blocks b
               JOIN notes n ON b.note_id = n.id
               WHERE n.file_path = ? AND n.deleted_at IS NULL""",
            (abs_path,),
        )
        rows = await cursor.fetchall()
        block_ids_to_remove = [row["id"] for row in rows]

        await conn.execute(
            "UPDATE notes SET deleted_at=? WHERE file_path=?", (now, abs_path)
        )
        await conn.execute(
            "UPDATE tasks SET is_deleted=1, sync_status='local' WHERE note_id=(SELECT id FROM notes WHERE file_path=?) AND is_deleted=0",
            (abs_path,),
        )
        await conn.commit()
        print(f"--> [DB] Deleted note: {abs_path}")

    # Purge embeddings from FAISS outside the DB transaction so a vector-index
    # failure never rolls back the soft-delete.
    if block_ids_to_remove:
        try:
            import asyncio
            from src.rag.vector_db import _get_vector_db

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: _get_vector_db().remove_by_block_ids(block_ids_to_remove),
            )
            print(
                f"--> [VectorDB] Removed {len(block_ids_to_remove)} blocks "
                f"for deleted note: {abs_path}"
            )
        except Exception as exc:
            print(f"[WARN] VectorDB cleanup failed for {abs_path}: {exc}")

    try:
        from src.ingestion.sync_worker import trigger_sync

        trigger_sync()
    except Exception:
        pass
