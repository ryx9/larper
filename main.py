import asyncio
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path patch — must happen before any src.* imports
# ---------------------------------------------------------------------------
tui_path = Path(__file__).parent / "src" / "TUI"
if str(tui_path) not in sys.path:
    sys.path.insert(0, str(tui_path))

# ---------------------------------------------------------------------------
# Lightweight imports only — heavy NLP/ML libs are deferred to background tasks
# ---------------------------------------------------------------------------
from src.ingestion.db import init_db          # pure aiosqlite, fast
from src.core.watchdog import start_watchdog  # watchdog observer, fast
from src.ingestion.worker import ingestion_worker
from src.ingestion.sync_worker import sync_worker
from src.TUI.app import DevWorkspaceApp

# NOTE: parser_worker and _preload_vector_db are intentionally NOT imported at
# the top level. Both drag in sentence-transformers + faiss which spend ~1-2 s
# at import time even before any model weights load. We import them inside
# background coroutines so that cost is paid off the critical path.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background bootstrap helpers
# ---------------------------------------------------------------------------

async def _start_parser_worker() -> None:
    """Import-then-run wrapper so sentence-transformers loads off the critical path."""
    from src.ingestion.parser import parser_worker  # deferred heavy import
    await parser_worker()


async def _start_preload() -> None:
    """
    Warm up the embedding model and FAISS index entirely off the event loop.
    SentenceTransformer.encode() is pure CPU — running it in an executor
    prevents it from blocking the TUI's asyncio event loop.
    """
    loop = asyncio.get_running_loop()
    try:
        # run_in_executor offloads to a ThreadPoolExecutor thread, freeing the
        # event loop to keep the TUI responsive while weights load.
        await loop.run_in_executor(None, _blocking_preload)
        logger.info("Vector DB preload complete.")
    except Exception:
        logger.exception("Vector DB preload failed — RAG will initialise lazily on first use.")


def _blocking_preload() -> None:
    """Pure-blocking model + index warm-up (runs in executor thread)."""
    from src.rag import _preload_vector_db  # deferred heavy import
    import asyncio as _asyncio
    # _preload_vector_db is itself async (calls run_in_executor internally),
    # so we spin up a fresh event loop inside this executor thread.
    _asyncio.run(_preload_vector_db())


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def async_main() -> None:
    # ------------------------------------------------------------------
    # 1. DB init — fast (just CREATE TABLE IF NOT EXISTS + indices).
    #    Keep this before the TUI so workers have a schema to write to,
    #    but it completes in <50 ms on a warm FS so startup is not blocked.
    # ------------------------------------------------------------------
    await init_db()

    # ------------------------------------------------------------------
    # 2. Lightweight workers — start immediately, no heavy imports needed.
    # ------------------------------------------------------------------
    watchdog_task  = asyncio.create_task(start_watchdog(),   name="watchdog")
    ingestion_task = asyncio.create_task(ingestion_worker(), name="ingestion")  # event_queue → processor → parser_queue
    sync_task = asyncio.create_task(sync_worker(), name="todoist-sync")
    # ------------------------------------------------------------------
    # 3. Heavy NLP workers — started as tasks so their deferred imports
    #    and model loading happen concurrently with the TUI rendering,
    #    not before it. The parser_queue will buffer events until
    #    parser_worker is ready to consume them.
    # ------------------------------------------------------------------
    parser_task  = asyncio.create_task(_start_parser_worker(), name="parser")
    preload_task = asyncio.create_task(_start_preload(),        name="preload")

    # ------------------------------------------------------------------
    # 4. TUI — launches immediately; NLP warms up in the background.
    # ------------------------------------------------------------------
    app = DevWorkspaceApp(ansi_color=True)

    try:
        await app.run_async()
    finally:
        for task in (watchdog_task, ingestion_task, sync_task, parser_task, preload_task):
            task.cancel()

        for task in (watchdog_task, ingestion_task, sync_task, parser_task, preload_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error("Error shutting down task %s: %s", task.get_name(), exc)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
