from textual.widget import Widget
from textual.app import ComposeResult
from textual.widgets import Static, ListView, ListItem, Label
from textual.binding import Binding
from textual.worker import get_current_worker
from textual.events import Click
from datetime import date, datetime, timedelta
import asyncio
import re

# Import store based on how the module is being imported
try:
    from ..state.store import store, Item
except ImportError:
    from state.store import store, Item

# Import database connection
try:
    from ...ingestion.db.connection import get_connection
except ImportError:
    from src.ingestion.db.connection import get_connection


# Matches 'todo: my task' or 'done: my task' format
TASK_PREFIX_RE = re.compile(r"^(\s*)(?:[-*]\s+)?(todo|done)\s*:\s*(.+)$", re.IGNORECASE)


def format_due_date(value: str | None, today: date | None = None) -> str:
    """Format stored ISO due values as concise local dates with optional time."""
    if not value:
        return ""
    today = today or date.today()
    try:
        due = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            due = datetime.combine(date.fromisoformat(value[:10]), datetime.min.time())
        except ValueError:
            return value

    if due.tzinfo is not None:
        due = due.astimezone()
    due_day = due.date()
    difference = (due_day - today).days
    if difference == 0:
        label = "Today"
    elif difference == 1:
        label = "Tomorrow"
    elif difference == -1:
        label = "Yesterday"
    elif 1 < difference < 7:
        label = due.strftime("%A")
    elif due_day.year == today.year:
        label = due.strftime("%b %d").replace(" 0", " ")
    else:
        label = due.strftime("%b %d, %Y").replace(" 0", " ")

    if "T" in value or (" " in value and len(value) > 10):
        label += f" at {due.strftime('%I:%M %p').lstrip('0')}"
    return label


class TodoItem(ListItem):
    DEFAULT_CSS = """
    TodoItem {
        padding: 0 1;
        height: auto;
        min-height: 1;
        background: transparent;
        color: $text-muted;
    }
    TodoItem Label {
        width: 1fr;
        height: auto;
        background: transparent;
    }
    TodoItem.highlighted { color: $accent; }
    TodoItem:hover { background: $accent 14%; color: ansi_default; }
    """

    def __init__(
        self, item: Item, task_id: int = 0, raw_text: str = "", is_done: bool = False
    ):
        super().__init__()
        self._item = item
        self._task_id = task_id
        self._raw_text = raw_text
        self._is_done = bool(is_done)
        self._label_content = ""
        self._is_overdue = False
        self._check_overdue()

    def _check_overdue(self):
        """Check if task is overdue based on ISO date."""
        if self._item.date and not self._is_done:
            from datetime import date

            try:
                # ISO dates start with YYYY-MM-DD, slice the first 10 characters
                due_date_str = self._item.date[:10]
                due_date = date.fromisoformat(due_date_str)
                self._is_overdue = due_date < date.today()
            except (ValueError, TypeError):
                self._is_overdue = False

    def compose(self) -> ComposeResult:
        due = (
            f"  [dim #3b4261]{format_due_date(self._item.date)}[/dim #3b4261]"
            if self._item.date else ""
        )
        checkbox = " ☒ " if self._is_done else " ☐ "

        # Add overdue indicator
        if self._is_overdue:
            overdue_marker = "[bold #f7768e]⚠[/bold #f7768e] "
            self._label_content = (
                f"{overdue_marker}{checkbox}[#f7768e]{self._item.text}[/#f7768e]{due}"
            )
        else:
            self._label_content = f"{checkbox}{self._item.text}{due}"

        yield Label(self._label_content)

    def on_mount(self):
        async def highlight():
            self.add_class("highlighted")
            await asyncio.sleep(0.8)
            self.remove_class("highlighted")

        asyncio.get_event_loop().create_task(highlight())

    async def toggle_done(self):
        """Toggle task done status in database and markdown."""
        self._is_done = not self._is_done
        await self._update_markdown()
        await self._update_database()
        if self._is_done:
            self.call_after_refresh(
                lambda: asyncio.create_task(self.remove())
            )
        else:
            self._update_display()

    async def _update_database(self):
        """Update task status in database."""
        if self._task_id:
            try:
                async with get_connection() as conn:
                    await conn.execute(
                        "UPDATE tasks SET is_done = ?, sync_status = 'pending' WHERE id = ?",
                        (1 if self._is_done else 0, self._task_id),
                    )
                    await conn.commit()
                try:
                    from src.ingestion.sync_worker import trigger_sync

                    trigger_sync()
                except Exception:
                    pass
            except Exception as e:
                print(f"Error updating task status: {e}")

    async def _update_markdown(self):
        """Update the task status in the source markdown file."""
        try:
            filepath = store.find_note_path(self._item.file)
            if not filepath or not filepath.exists():
                return

            original = filepath.read_text(encoding="utf-8")
            lines = original.splitlines(keepends=True)
            target_title = self._item.text.strip()

            for idx, line in enumerate(lines):
                body_line = line.rstrip("\n")

                # Check if this line contains our task
                if target_title not in body_line:
                    continue

                newline = "\n" if line.endswith("\n") else ""

                # 1. Try to match standard markdown tasks: - [ ] or - [x]
                cb_match = re.match(r"^(\s*[-*]\s+)\[([ xX])\](.*?)$", body_line)
                if cb_match:
                    prefix = cb_match.group(1)
                    rest = cb_match.group(3)
                    new_box = "[x]" if self._is_done else "[ ]"
                    lines[idx] = f"{prefix}{new_box}{rest}{newline}"
                    filepath.write_text("".join(lines), encoding="utf-8")
                    return

                # 2. Try to match 'todo:' / 'done:' syntax
                match = TASK_PREFIX_RE.match(body_line)
                if match:
                    prefix = match.group(1)  # whitespace/bullet
                    task_body = match.group(3)
                    new_status = "done" if self._is_done else "todo"
                    lines[idx] = f"{prefix}{new_status}: {task_body}{newline}"
                    filepath.write_text("".join(lines), encoding="utf-8")
                    return
        except Exception as e:
            print(f"Error updating task markdown: {e}")

    def _update_display(self):
        """Update the checkbox display."""
        try:
            label = self.query_one(Label)
            due = (
                f"  [dim #3b4261]{format_due_date(self._item.date)}[/dim #3b4261]"
                if self._item.date
                else ""
            )
            checkbox = " ☒ " if self._is_done else " ☐ "

            # Re-check overdue status
            self._check_overdue()

            if self._is_overdue:
                overdue_marker = "[bold #f7768e]⚠[/bold #f7768e] "
                self._label_content = f"{overdue_marker}{checkbox}[#f7768e]{self._item.text}[/#f7768e]{due}"
            else:
                self._label_content = f"{checkbox}{self._item.text}{due}"

            label.update(self._label_content)
        except Exception:
            pass  # Label not ready yet, will be set in compose()

    async def on_click(self, event: Click):
        """Handle click on the todo item to toggle done status."""
        event.stop()
        await self.toggle_done()


class TodosPanel(Widget):
    can_focus = True

    DEFAULT_CSS = """
    TodosPanel {
        height: 1fr;
        background: transparent;
        layout: vertical;
    }
    TodosPanel .panel-title {
        color: $text-muted;
        padding: 0 2;
        height: 1;
        background: transparent;
        border-bottom: tall $foreground 10%;
    }
    TodosPanel ListView {
        background: transparent;
        border: none;
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("j", "move_down", "↓", show=False),
        Binding("k", "move_up", "↑", show=False),
        Binding("gg", "go_top", "Top", show=False),
        Binding("G", "go_bottom", "Bottom", show=False),
        Binding("d", "delete_task", "Delete", show=False),
        Binding("x", "toggle_done", "Toggle", show=False),
        Binding("enter", "open_task_note", "Open", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(
            "  tasks  [dim #3b4261]jk=nav  gg/G=top/bottom  enter=open  x=toggle  d=delete[/dim #3b4261]",
            classes="panel-title",
        )
        yield ListView(id="todos-list")

    def on_mount(self):
        self.refresh_todos()

    async def _load_db_tasks(self):
        """Load tasks from database asynchronously."""
        worker = get_current_worker()
        tasks_with_ids = await self._get_tasks_with_ids()
        if worker.is_cancelled:
            return
        self._update_todos(tasks_with_ids)

    async def _get_tasks_with_ids(self) -> list[tuple[Item, int, int, str]]:
        """Get tasks from database with their IDs, utilizing SQLite's native ISO date handling."""
        try:
            from datetime import date

            # ISO 8601 date string 'YYYY-MM-DD'
            today = date.today().isoformat()

            async with get_connection() as conn:
                cursor = await conn.execute(
                    """
                    SELECT 
                        t.id, 
                        t.title, 
                        t.raw_text, 
                        t.due_date, 
                        COALESCE(n.file_path, 'Todoist') AS file_path,
                        t.is_done,
                        CASE 
                            WHEN t.due_date IS NOT NULL AND date(t.due_date) = ? THEN 0
                            WHEN t.due_date IS NOT NULL AND date(t.due_date) < ? THEN 1
                            WHEN t.due_date IS NOT NULL AND date(t.due_date) > ? THEN 2
                            ELSE 3
                        END as priority_group
                    FROM tasks t
                                        LEFT JOIN notes n ON t.note_id = n.id
                    WHERE t.is_deleted = 0 AND t.is_done = 0
                                            AND (t.note_id IS NULL OR n.deleted_at IS NULL)
                    ORDER BY 
                        priority_group ASC,
                        t.due_date ASC,
                        t.id ASC
                """,
                    (today, today, today),
                )
                rows = await cursor.fetchall()

                tasks = []
                for row in rows:
                    task = Item(
                        id=str(row["id"]),
                        text=row["title"],
                        file=row["file_path"],
                        created_at=datetime.now(),
                        date=row["due_date"],
                    )
                    tasks.append((task, row["id"], row["is_done"], row["raw_text"]))
                return tasks
        except Exception as e:
            print(f"Error loading tasks: {e}")
            return []

    def _update_todos(self, tasks: list[tuple[Item, int, int, str]]):
        """Update the UI with tasks from database."""
        lv = self.query_one("#todos-list", ListView)
        lv.clear()
        for item, task_id, is_done, raw_text in tasks:
            todo_item = TodoItem(item, task_id, raw_text, is_done=bool(is_done))
            lv.append(todo_item)

    def refresh_todos(self):
        """Refresh tasks from database."""
        self.run_worker(self._load_db_tasks(), name="load-tasks")

    def add_todo(self, item: Item):
        lv = self.query_one("#todos-list", ListView)
        lv.append(TodoItem(item, raw_text=f"[ ] {item.text}"))
        if lv.index is None:
            lv.index = 0

    def action_move_down(self):
        lv = self.query_one("#todos-list", ListView)
        if lv.children:
            lv.index = min(len(lv.children) - 1, (lv.index or 0) + 1)

    def action_move_up(self):
        lv = self.query_one("#todos-list", ListView)
        if lv.children:
            lv.index = max(0, (lv.index or 0) - 1)

    def action_go_top(self):
        lv = self.query_one("#todos-list", ListView)
        lv.index = 0 if lv.children else None

    def action_go_bottom(self):
        lv = self.query_one("#todos-list", ListView)
        if lv.children:
            lv.index = len(lv.children) - 1

    def action_delete_task(self):
        lv = self.query_one("#todos-list", ListView)
        if lv.index is not None and lv.children:
            # Mark task as deleted in database
            child = lv.children[lv.index]
            if isinstance(child, TodoItem):
                # TODO: Implement database delete
                lv.remove_child(child)

    def action_toggle_done(self):
        lv = self.query_one("#todos-list", ListView)
        if lv.index is not None and lv.children:
            child = lv.children[lv.index]
            if isinstance(child, TodoItem):
                asyncio.create_task(child.toggle_done())

    def action_open_task_note(self):
        lv = self.query_one("#todos-list", ListView)
        if lv.index is not None and lv.children:
            child = lv.children[lv.index]
            if isinstance(child, TodoItem):
                self.app.action_open_note(child._item.file)
