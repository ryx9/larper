from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """
    Central app config, loaded from (in order of precedence):
      1. real environment variables
      2. the .env file in the project root
      3. the defaults below

    Field names/defaults are unchanged from the old hand-rolled loader —
    every existing .env file and every `settings.X` call site keeps working
    exactly as before. Unknown keys in .env (e.g. leftover/legacy vars) are
    ignored rather than raising, matching the old `hasattr` guard.
    """

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Anthropic (primary LLM — claude-sonnet-4-6) ───────────────────────────
    ANTHROPIC_API_KEY: str = ""

    # ── Todoist sync ──────────────────────────────────────────────────────────
    TODOIST_ENABLED: bool = False
    TODOIST_API_KEY: str = ""
    TODOIST_SYNC_INTERVAL_SECONDS: int = 60

    # ── Paths ─────────────────────────────────────────────────────────────────
    ACTIVE_FOLDER: str = "."
    DB_PATH: str = "notes.db"
    VECTOR_DB_PATH: str = "faiss_index.bin"
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    HF_DIR: str = ".cache/huggingface"

    # ── Legacy / fallback (kept so existing .env files don't break) ──────────
    API_KEY: str = ""
    MODEL: str = "openai/gpt-4.1-mini"
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = ""
    OPENROUTER_API_BASE: str = "https://openrouter.ai/api/v1"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"
    GEMINI_API_BASE: str = ""

    # ── RAG toggles ───────────────────────────────────────────────────────────
    ENABLE_BM25: bool = True
    ENABLE_FZF: bool = True
    ENABLE_GRAPH_EXPANSION: bool = True
    RAG_DEFAULT_K: int = 6


settings = Settings()
