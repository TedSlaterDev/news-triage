"""
Main application entry point.
Wires together the FastAPI server, email monitor, and analysis pipeline.
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import load_config
from backend.models.database import TipDatabase
from backend.services.tip_pipeline import TipPipeline
from backend.api.routes import create_routes, ws_manager

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("newstriage")

# ── Globals ──────────────────────────────────────────────────────────

config = load_config()
db = TipDatabase(config.database.db_path)
pipeline = TipPipeline(config, db)
monitor_task = None


# ── Lifespan ─────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    global monitor_task

    await db.connect()
    logger.info("Database connected")

    # Start the email → analysis pipeline in the background
    if config.imap.username and config.claude.api_key:
        monitor_task = asyncio.create_task(pipeline.start())
        logger.info("Tip pipeline started")
    else:
        logger.warning(
            "IMAP or Claude API key not configured — "
            "pipeline disabled, dashboard-only mode"
        )

    # Start a periodic stats broadcaster for the dashboard
    async def broadcast_stats():
        while True:
            await asyncio.sleep(config.dashboard.auto_refresh_seconds)
            try:
                stats = await db.get_stats()
                await ws_manager.broadcast({"type": "stats_update", "stats": stats})
            except Exception as e:
                logger.debug(f"Stats broadcast error: {e}")

    stats_task = asyncio.create_task(broadcast_stats())

    yield  # ← app is running

    # Shutdown
    stats_task.cancel()
    await pipeline.stop()
    await db.close()
    logger.info("Shutdown complete")


# ── FastAPI app ──────────────────────────────────────────────────────

app = FastAPI(
    title="News Tip Triage",
    description="AI-powered newsroom tip analysis and ranking dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Fine for local use; restrict in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routes
create_routes(app, db, pipeline)

# Serve React build as static files (after API routes so /api/* takes priority)
frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")


# ── CLI entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.app:app",
        host=config.dashboard.host,
        port=config.dashboard.port,
        reload=False,
        log_level="info",
    )
