# LARPer

Local-first note, task, and retrieval manager with markdown ingestion, hybrid search, and a terminal-native TUI.

> LARPer is built for fast local workflows: watch files, parse markdown into tasks/blocks/tags, store structured data in SQLite, and retrieve knowledge with combined embeddings + graph search.

---

## 🚀 What is LARPer?

LARPer is a modular pipeline for:

- ingesting markdown content from a watched folder
- extracting tasks, headings, tags, and events
- storing notes, blocks, and task metadata into a local SQLite database
- generating embeddings for vector retrieval
- exposing a terminal user interface with task and note navigation
- enabling hybrid retrieval through BM25, fuzzy search, and embedding-based ranking

It is designed for personal knowledge management, task-driven journaling, and rapid local search.

---

## 🔧 Key Features

- **Markdown-aware ingestion**
  - Recognizes checkbox tasks, TODO labels, explicit todo/done markers, due dates, priorities, and tags
  - Parses headings, lists, and inline metadata into structured blocks

- **Local-first storage**
  - Uses SQLite for notes, tasks, and block metadata
  - Keeps data locally in the configured workspace

- **Hybrid retrieval stack**
  - Embeddings via `sentence-transformers`
  - Vector search powered by FAISS
  - Keyword search and tag extraction
  - Graph expansion and relational retrieval support

- **Textual UI**
  - Terminal-native interface with `textual`
  - Task panel and notes browsing
  - Keyboard navigation inspired by `vim`

- **Task management**
  - Stores completed tasks while hiding them from active todo lists
  - Detects overdue tasks based on due dates
  - Synchronizes UI refreshes after parser ingestion completes

---

## 📁 Repository Layout

- `config.py` — application settings loader from `.env`
- `main.py` — CLI entrypoint for launching the app
- `src/ingestion/` — file watchers, parser workers, ingestion pipeline, and database adapters
- `src/ingestion/parser/` — markdown parsing, task detection, and metadata extractors
- `src/ingestion/db/` — SQLite models and helper functions for notes/tasks/tags
- `src/TUI/` — textual user interface components and app state
- `src/rag/` — retrieval-augmented generation and query tooling
- `tests/` — unit and integration tests for parser, retrieval, and UI behavior

---

## ⚙️ Requirements

- Python `>=3.13`
- `uv` or a Python environment manager
- supported packages installed from `pyproject.toml`

Recommended dependencies are already listed in `pyproject.toml`.

---

## 🧩 Setup

1. Clone the repository

```bash
git clone <repo-url> larper
cd larper
```

2. Create environment variables in `.env`

```env
ACTIVE_FOLDER=/absolute/path/to/your/project
DB_PATH=notes.db
VECTOR_DB_PATH=faiss_index.bin
OPENROUTER_API_KEY=your_openrouter_key
OPENROUTER_MODEL=openai/gpt-4.1-mini
EMBEDDING_MODEL=all-MiniLM-L6-v2
HF_DIR=/absolute/path/to/model/cache/

# Optional two-way Todoist task synchronization
TODOIST_ENABLED=false
TODOIST_API_KEY=your_todoist_api_token

# Optional Gemini fallback settings
GEMINI_API_KEY=
GEMINI_MODEL=gpt-4o-mini
GEMINI_API_BASE=

# Search / RAG toggles
ENABLE_BM25=true
ENABLE_FZF=true
ENABLE_GRAPH_EXPANSION=true
RAG_DEFAULT_K=6
```

3. Install dependencies

```bash
uv install
```

If you prefer `pip`:

```bash
python -m pip install -e .
```

---

## ▶️ Running the App

Use the entrypoint defined in `main.py` or run the TUI directly:

```bash
uv run python main.py
```

If the app starts successfully, it will begin watching `ACTIVE_FOLDER` for markdown changes and ingest new content.

---

## 📝 Markdown Parsing and Task Support

LARPer parses content with a focus on task metadata and markdown structure.

Supported task forms:

- `- [ ] Buy milk`
- `- [x] Done task @due 2026-01-01`
- `TODO: Review notes`
- `todo: Fix bug`
- `done: Archive doc`

Metadata extraction includes:

- due dates (`@due YYYY-MM-DD`)
- priorities (`[!]`, `[!!]`, `[???]`)
- tags (`#tag`, `#work`, `#personal`)
- recurrence patterns

---

## 🔍 Search and Retrieval

The app supports hybrid retrieval across:

- local full-text / keyword search
- tag-based lookups
- embedding similarity search via FAISS
- graph/parent-child expansion

Search can be configured with `.env` toggles:

- `ENABLE_BM25`
- `ENABLE_FZF`
- `ENABLE_GRAPH_EXPANSION`

---

## 🧪 Testing

Run the test suite with:

```bash
uv run pytest -q
```

Or target specific tests:

```bash
uv run pytest -q tests/test_parser.py tests/test_tasks.py
```

---

## ⌨️ Keybindings

The TUI includes vim-inspired navigation:

- `h`, `j`, `k`, `l` — move between panes and items
- `gg` / `G` — jump to top/bottom
- `gn` — focus notes panel
- `gj` — create/open new journal entry
- `/` — open search modal
- `Ctrl+T` — toggle nvim mode
- `Ctrl+I` — focus input field
- `Esc` — return to main workspace
- `Shift-H` / `Shift-L` — switch tabs
- `x` — close active tab
- `m` — minimize tab

---

## 🛠️ Configuration Notes

The `config.py` loader is powered by `pydantic-settings` and reads from `.env`.
Todoist synchronization is disabled by default. Set `TODOIST_ENABLED=true` and
provide `TODOIST_API_KEY` to sync tasks between Todoist and the local database.
The worker runs at startup, after local task changes, and periodically while the
application is running. Todoist-created tasks appear in the Todos panel; edits,
completion changes, and deletions are reconciled in both directions.
The default poll interval is 60 seconds and can be changed with
`TODOIST_SYNC_INTERVAL_SECONDS`. Failed requests stay queued in SQLite and retry
with bounded backoff; HTTP 429 responses honor `Retry-After`. Todoist-created
tasks are written to the journal for their creation date with a stable task ID
marker, which prevents duplicate rows when Markdown is ingested. Use
`proj:"Project Name"` on a task to assign its Todoist project; `#ProjectName`
remains a label, even when it matches a project name. ISO `@due` timestamps are
preserved along with Todoist's timezone.

Example config class:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    ACTIVE_FOLDER: str = "."
    DB_PATH: str = "notes.db"
    VECTOR_DB_PATH: str = "faiss_index.bin"
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    HF_DIR: str = ".cache/huggingface"
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "openai/gpt-4.1-mini"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
```

---

## 📈 Why Use LARPer?

LARPer is ideal when you want a self-hosted, offline-friendly knowledge and task system with:

- automatic markdown ingestion
- structured task extraction
- fast local search
- modern NLP retrieval without vendor lock-in
- a keyboard-driven terminal UI

---

## 🤝 Contributing

Contributions are welcome. Suggested next improvements:

- add more parser edge cases for markdown tasks
- improve RAG ranking and search combiners
- add user preferences to the TUI
- support additional note formats
