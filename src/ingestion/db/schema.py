import aiosqlite

from src.ingestion.db.connection import get_connection


async def _create_notes_table(conn: aiosqlite.Connection) -> None:
    """Create notes table for storing markdown files."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            note_type TEXT NOT NULL,
            raw_content TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            deleted_at DATETIME NULLABLE
        )
    """)


async def _create_blocks_table(conn: aiosqlite.Connection) -> None:
    """Create blocks table for hierarchical document structure."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER,
            block_type TEXT NOT NULL,
            content TEXT NOT NULL,
            level INTEGER NULLABLE,
            position INTEGER NOT NULL,
            parent_block INTEGER NULLABLE,
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_block) REFERENCES blocks(id) ON DELETE CASCADE
        )
    """)


async def _create_tasks_table(conn: aiosqlite.Connection) -> None:
    """Create tasks table for todo items with sync tracking."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER,
            block_id INTEGER,
            raw_text TEXT NOT NULL,
            title TEXT NOT NULL,
            is_done INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0,
            due_date TEXT NULLABLE,
            todoist_due_timezone TEXT NULLABLE,
            priority TEXT NULLABLE,
            tags TEXT NULLABLE,
            recurrence TEXT NULLABLE,
            start_date TEXT NULLABLE,
            todolist_id TEXT NULLABLE UNIQUE,
            todoist_project_id TEXT NULLABLE,
            todoist_project_name TEXT NULLABLE,
            gcal_event_id TEXT NULLABLE,
            sync_status TEXT DEFAULT 'pending',
            last_synced_at DATETIME NULLABLE,
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
            FOREIGN KEY (block_id) REFERENCES blocks(id) ON DELETE SET NULL
        )
    """)


async def _migrate_tasks_table(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(tasks)")
    columns = {row["name"] for row in await cursor.fetchall()}
    for column in (
        "todoist_project_id", "todoist_project_name", "todoist_due_timezone"
    ):
        if column not in columns:
            await conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT NULLABLE")


async def _create_sync_log_table(conn: aiosqlite.Connection) -> None:
    """Create sync_log table for audit trail."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NULLABLE,
            file_path TEXT NOT NULL,
            status TEXT NOT NULL,
            error_msg TEXT NULLABLE,
            logged_at DATETIME NOT NULL
        )
    """)


async def _create_block_references_table(conn: aiosqlite.Connection) -> None:
    """Create block_references table for document graph/links."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS block_references (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_block_id INTEGER,
            target_note_id INTEGER,
            target_block_id INTEGER NULLABLE,
            reference_type TEXT NOT NULL DEFAULT 'link',
            target_title TEXT NULLABLE,
            created_at DATETIME NOT NULL,
            FOREIGN KEY (source_block_id) REFERENCES blocks(id) ON DELETE CASCADE,
            FOREIGN KEY (target_note_id) REFERENCES notes(id) ON DELETE CASCADE,
            FOREIGN KEY (target_block_id) REFERENCES blocks(id) ON DELETE SET NULL
        )
    """)


async def _create_block_tags_table(conn: aiosqlite.Connection) -> None:
    """Create block_tags table for hashtag searchability."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS block_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_id INTEGER,
            tag TEXT NOT NULL,
            FOREIGN KEY (block_id) REFERENCES blocks(id) ON DELETE CASCADE
        )
    """)


async def _create_indices(conn: aiosqlite.Connection) -> None:
    """Create all indices for query performance."""
    indices = [
        "CREATE INDEX IF NOT EXISTS idx_notes_file_path ON notes(file_path);",
        "CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(note_type);",
        "CREATE INDEX IF NOT EXISTS idx_blocks_note_id ON blocks(note_id);",
        "CREATE INDEX IF NOT EXISTS idx_tasks_note_id ON tasks(note_id);",
        "CREATE INDEX IF NOT EXISTS idx_tasks_sync_status ON tasks(sync_status);",
        "CREATE INDEX IF NOT EXISTS idx_tasks_todolist_id ON tasks(todolist_id);",
        "CREATE INDEX IF NOT EXISTS idx_sync_log_logged_at ON sync_log(logged_at);",
        "CREATE INDEX IF NOT EXISTS idx_block_references_source ON block_references(source_block_id);",
        "CREATE INDEX IF NOT EXISTS idx_block_references_target ON block_references(target_note_id);",
        "CREATE INDEX IF NOT EXISTS idx_block_tags_tag ON block_tags(tag);",
        "CREATE INDEX IF NOT EXISTS idx_block_tags_block_id ON block_tags(block_id);",
    ]
    for sql in indices:
        await conn.execute(sql)
    print(f"--> [DB] Created/verified {len(indices)} indices")


async def init_db() -> None:
    """Initialize database schema — creates all tables and indices."""
    async with get_connection() as conn:
        print("--> [DB] Initializing database schema...")
        await _create_notes_table(conn)
        await _create_blocks_table(conn)
        await _create_tasks_table(conn)
        await _migrate_tasks_table(conn)
        await _create_sync_log_table(conn)
        await _create_block_references_table(conn)
        await _create_block_tags_table(conn)
        await _create_indices(conn)
        await conn.commit()
        print("--> [DB] Database initialization complete!")
