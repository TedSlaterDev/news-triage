"""
Main application entry point.
Wires together the FastAPI server, email monitor, and analysis pipeline.
"""

import asyncio
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

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


# ── Basic Auth middleware ─────────────────────────────────────────────

class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Simple HTTP Basic Auth. Set DASHBOARD_USER and DASHBOARD_PASS in .env."""

    def __init__(self, app, username: str, password: str):
        super().__init__(app)
        self.username = username
        self.password = password

    async def dispatch(self, request: Request, call_next):
        # Allow WebSocket upgrades to pass (browser sends auth via URL or first message)
        if request.url.path == "/ws":
            return await call_next(request)

        import base64
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, passwd = decoded.split(":", 1)
                if secrets.compare_digest(user, self.username) and \
                   secrets.compare_digest(passwd, self.password):
                    return await call_next(request)
            except Exception:
                pass

        return Response(
            content="Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="News Tip Triage"'},
        )


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

# Add password protection if credentials are configured
dash_user = os.getenv("DASHBOARD_USER", "")
dash_pass = os.getenv("DASHBOARD_PASS", "")
if dash_user and dash_pass:
    app.add_middleware(BasicAuthMiddleware, username=dash_user, password=dash_pass)
    logger.info("Dashboard password protection enabled")
else:
    logger.warning(
        "DASHBOARD_USER / DASHBOARD_PASS not set — dashboard is unprotected"
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
