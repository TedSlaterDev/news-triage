"""
FastAPI REST API and WebSocket endpoint for the news tip dashboard.
"""

import json
import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, Query, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.models.database import TipDatabase

logger = logging.getLogger(__name__)


# ── Pydantic request models ─────────────────────────────────────────

class ManualTipRequest(BaseModel):
    subject: str
    body_text: str
    sender_name: str = "Manual Entry"
    sender_email: str = "dashboard@local"


class UpdateTipRequest(BaseModel):
    assigned_to: Optional[str] = None
    team_notes: Optional[str] = None
    is_archived: Optional[bool] = None
    is_starred: Optional[bool] = None


# ── WebSocket connection manager ─────────────────────────────────────

class ConnectionManager:
    """Manages WebSocket connections for real-time dashboard updates."""

    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        logger.info(f"Dashboard client connected ({len(self.active)} total)")

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)
        logger.info(f"Dashboard client disconnected ({len(self.active)} total)")

    async def broadcast(self, message: dict):
        """Send a JSON message to all connected clients."""
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)


ws_manager = ConnectionManager()


# ── Route factory ────────────────────────────────────────────────────

def create_routes(app: FastAPI, db: TipDatabase, pipeline):
    """Register all API routes on the FastAPI app."""

    # ── Dashboard tips list ──────────────────────────────────────────

    @app.get("/api/tips")
    async def list_tips(
        status: Optional[str] = None,
        priority: Optional[str] = None,
        category: Optional[str] = None,
        is_archived: bool = False,
        is_starred: Optional[bool] = None,
        search: Optional[str] = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        order_by: str = "score_overall DESC",
    ):
        tips = await db.get_tips(
            status=status,
            priority=priority,
            category=category,
            is_archived=is_archived,
            is_starred=is_starred,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
        )
        # Parse JSON fields for the frontend
        for tip in tips:
            for field in ("key_claims_json", "follow_up_questions_json",
                          "related_coverage_json", "attachments_json"):
                if tip.get(field):
                    try:
                        tip[field] = json.loads(tip[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
        return {"tips": tips, "count": len(tips)}

    # ── Single tip detail ────────────────────────────────────────────

    @app.get("/api/tips/{tip_id}")
    async def get_tip(tip_id: int):
        tip = await db.get_tip(tip_id)
        if not tip:
            raise HTTPException(404, "Tip not found")
        for field in ("key_claims_json", "follow_up_questions_json",
                       "related_coverage_json", "attachments_json"):
            if tip.get(field):
                try:
                    tip[field] = json.loads(tip[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return tip

    # ── Update team workflow fields ──────────────────────────────────

    @app.patch("/api/tips/{tip_id}")
    async def update_tip(tip_id: int, body: UpdateTipRequest):
        existing = await db.get_tip(tip_id)
        if not existing:
            raise HTTPException(404, "Tip not found")

        updates = body.model_dump(exclude_none=True)
        if updates:
            await db.update_team_fields(tip_id, updates)
            await ws_manager.broadcast({
                "type": "tip_updated", "tip_id": tip_id, "updates": updates
            })
        return {"ok": True}

    # ── Manual tip submission ────────────────────────────────────────

    @app.post("/api/tips")
    async def submit_tip(body: ManualTipRequest):
        from datetime import datetime
        tip_data = {
            "message_id": "",  # pipeline will generate one
            "subject": body.subject,
            "sender_name": body.sender_name,
            "sender_email": body.sender_email,
            "received_at": datetime.utcnow().isoformat(),
            "body_text": body.body_text,
            "body_html": "",
            "attachments": [],
        }
        tip_id = await pipeline.ingest_manual_tip(tip_data)
        await ws_manager.broadcast({"type": "new_tip", "tip_id": tip_id})
        return {"ok": True, "tip_id": tip_id}

    # ── Dashboard stats ──────────────────────────────────────────────

    @app.get("/api/stats")
    async def get_stats():
        return await db.get_stats()

    # ── WebSocket for real-time updates ──────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws_manager.connect(ws)
        try:
            while True:
                # Keep alive — also receive any client messages
                data = await ws.receive_text()
                # Could handle client-to-server messages here
        except WebSocketDisconnect:
            ws_manager.disconnect(ws)

    # ── Serve the React dashboard ────────────────────────────────────

    @app.get("/")
    async def serve_dashboard():
        return FileResponse("frontend/dist/index.html")

    return app
