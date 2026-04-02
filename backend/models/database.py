"""
SQLite database layer for storing and querying news tips.
Uses aiosqlite for async operations compatible with FastAPI.
"""

import aiosqlite
import json
from datetime import datetime
from pathlib import Path
from typing import Optional


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS tips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    subject TEXT,
    sender_email TEXT,
    sender_name TEXT,
    received_at TEXT NOT NULL,
    body_text TEXT,
    body_html TEXT,
    attachments_json TEXT DEFAULT '[]',

    -- AI analysis fields
    status TEXT DEFAULT 'pending',  -- pending, analyzing, analyzed, error
    category TEXT,
    subcategory TEXT,
    summary TEXT,
    key_claims_json TEXT DEFAULT '[]',
    research_notes TEXT,
    follow_up_questions_json TEXT DEFAULT '[]',
    related_coverage_json TEXT DEFAULT '[]',
    source_credibility TEXT,

    -- Scoring (each 0-100)
    score_timeliness REAL DEFAULT 0,
    score_impact REAL DEFAULT 0,
    score_novelty REAL DEFAULT 0,
    score_credibility REAL DEFAULT 0,
    score_public_interest REAL DEFAULT 0,
    score_overall REAL DEFAULT 0,
    priority TEXT DEFAULT 'low',  -- critical, high, medium, low

    -- Flags
    is_urgent BOOLEAN DEFAULT 0,
    is_breaking BOOLEAN DEFAULT 0,
    is_duplicate BOOLEAN DEFAULT 0,
    duplicate_of_id INTEGER,

    -- Team workflow
    assigned_to TEXT,
    team_notes TEXT,
    is_archived BOOLEAN DEFAULT 0,
    is_starred BOOLEAN DEFAULT 0,

    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    analyzed_at TEXT,

    FOREIGN KEY (duplicate_of_id) REFERENCES tips(id)
);

CREATE INDEX IF NOT EXISTS idx_tips_status ON tips(status);
CREATE INDEX IF NOT EXISTS idx_tips_priority ON tips(priority);
CREATE INDEX IF NOT EXISTS idx_tips_score ON tips(score_overall DESC);
CREATE INDEX IF NOT EXISTS idx_tips_category ON tips(category);
CREATE INDEX IF NOT EXISTS idx_tips_received ON tips(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_tips_message_id ON tips(message_id);
CREATE INDEX IF NOT EXISTS idx_tips_archived ON tips(is_archived);
"""


class TipDatabase:
    """Async SQLite database manager for news tips."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Initialize database connection and create tables."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(DB_SCHEMA)
        await self._db.commit()

    async def close(self):
        """Close database connection."""
        if self._db:
            await self._db.close()

    # ── Insert & Update ──────────────────────────────────────────────

    async def insert_tip(self, tip_data: dict) -> int:
        """Insert a new tip from an email. Returns the new tip ID."""
        sql = """
            INSERT OR IGNORE INTO tips
                (message_id, subject, sender_email, sender_name,
                 received_at, body_text, body_html, attachments_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self._db.execute(sql, (
            tip_data["message_id"],
            tip_data.get("subject", ""),
            tip_data.get("sender_email", ""),
            tip_data.get("sender_name", ""),
            tip_data.get("received_at", datetime.utcnow().isoformat()),
            tip_data.get("body_text", ""),
            tip_data.get("body_html", ""),
            json.dumps(tip_data.get("attachments", [])),
        ))
        await self._db.commit()
        return cursor.lastrowid

    async def update_analysis(self, tip_id: int, analysis: dict):
        """Update a tip with Claude's analysis results."""
        sql = """
            UPDATE tips SET
                status = 'analyzed',
                category = ?,
                subcategory = ?,
                summary = ?,
                key_claims_json = ?,
                research_notes = ?,
                follow_up_questions_json = ?,
                related_coverage_json = ?,
                source_credibility = ?,
                score_timeliness = ?,
                score_impact = ?,
                score_novelty = ?,
                score_credibility = ?,
                score_public_interest = ?,
                score_overall = ?,
                priority = ?,
                is_urgent = ?,
                is_breaking = ?,
                is_duplicate = ?,
                analyzed_at = datetime('now'),
                updated_at = datetime('now')
            WHERE id = ?
        """
        await self._db.execute(sql, (
            analysis.get("category", "other"),
            analysis.get("subcategory", ""),
            analysis.get("summary", ""),
            json.dumps(analysis.get("key_claims", [])),
            analysis.get("research_notes", ""),
            json.dumps(analysis.get("follow_up_questions", [])),
            json.dumps(analysis.get("related_coverage", [])),
            analysis.get("source_credibility", "unknown"),
            analysis.get("scores", {}).get("timeliness", 0),
            analysis.get("scores", {}).get("impact", 0),
            analysis.get("scores", {}).get("novelty", 0),
            analysis.get("scores", {}).get("credibility", 0),
            analysis.get("scores", {}).get("public_interest", 0),
            analysis.get("score_overall", 0),
            analysis.get("priority", "low"),
            analysis.get("is_urgent", False),
            analysis.get("is_breaking", False),
            analysis.get("is_duplicate", False),
            tip_id,
        ))
        await self._db.commit()

    async def set_status(self, tip_id: int, status: str):
        """Update the processing status of a tip."""
        await self._db.execute(
            "UPDATE tips SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, tip_id),
        )
        await self._db.commit()

    async def update_team_fields(self, tip_id: int, fields: dict):
        """Update team workflow fields (assigned_to, notes, starred, archived)."""
        allowed = {"assigned_to", "team_notes", "is_archived", "is_starred"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        sql = f"UPDATE tips SET {set_clause}, updated_at = datetime('now') WHERE id = ?"
        await self._db.execute(sql, (*updates.values(), tip_id))
        await self._db.commit()

    # ── Queries ──────────────────────────────────────────────────────

    async def get_tip(self, tip_id: int) -> Optional[dict]:
        """Get a single tip by ID."""
        cursor = await self._db.execute("SELECT * FROM tips WHERE id = ?", (tip_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_tips(
        self,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        category: Optional[str] = None,
        is_archived: bool = False,
        is_starred: Optional[bool] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "score_overall DESC",
    ) -> list[dict]:
        """Query tips with filters, pagination, and sorting."""
        conditions = ["is_archived = ?"]
        params: list = [int(is_archived)]

        if status:
            conditions.append("status = ?")
            params.append(status)
        if priority:
            conditions.append("priority = ?")
            params.append(priority)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if is_starred is not None:
            conditions.append("is_starred = ?")
            params.append(int(is_starred))
        if search:
            conditions.append("(subject LIKE ? OR body_text LIKE ? OR summary LIKE ?)")
            term = f"%{search}%"
            params.extend([term, term, term])

        where = " AND ".join(conditions)
        # Whitelist order_by to prevent injection
        allowed_orders = {
            "score_overall DESC", "score_overall ASC",
            "received_at DESC", "received_at ASC",
            "created_at DESC", "created_at ASC",
            "priority DESC",
        }
        if order_by not in allowed_orders:
            order_by = "score_overall DESC"

        sql = f"""
            SELECT * FROM tips
            WHERE {where}
            ORDER BY is_urgent DESC, is_breaking DESC, {order_by}
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_stats(self) -> dict:
        """Get dashboard statistics."""
        stats = {}

        cursor = await self._db.execute(
            "SELECT COUNT(*) as total FROM tips WHERE is_archived = 0"
        )
        stats["total_active"] = (await cursor.fetchone())["total"]

        cursor = await self._db.execute(
            "SELECT priority, COUNT(*) as count FROM tips "
            "WHERE is_archived = 0 GROUP BY priority"
        )
        stats["by_priority"] = {row["priority"]: row["count"] for row in await cursor.fetchall()}

        cursor = await self._db.execute(
            "SELECT category, COUNT(*) as count FROM tips "
            "WHERE is_archived = 0 AND category IS NOT NULL GROUP BY category "
            "ORDER BY count DESC"
        )
        stats["by_category"] = {row["category"]: row["count"] for row in await cursor.fetchall()}

        cursor = await self._db.execute(
            "SELECT status, COUNT(*) as count FROM tips GROUP BY status"
        )
        stats["by_status"] = {row["status"]: row["count"] for row in await cursor.fetchall()}

        cursor = await self._db.execute(
            "SELECT COUNT(*) as count FROM tips WHERE is_urgent = 1 AND is_archived = 0"
        )
        stats["urgent_count"] = (await cursor.fetchone())["count"]

        cursor = await self._db.execute(
            "SELECT COUNT(*) as count FROM tips WHERE is_breaking = 1 AND is_archived = 0"
        )
        stats["breaking_count"] = (await cursor.fetchone())["count"]

        return stats

    async def get_recent_subjects(self, limit: int = 20) -> list[str]:
        """Get recent tip subjects for duplicate detection context."""
        cursor = await self._db.execute(
            "SELECT subject, summary FROM tips ORDER BY received_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [f"{r['subject']}: {r['summary'] or ''}" for r in rows]

    async def message_id_exists(self, message_id: str) -> bool:
        """Check if we've already ingested an email by Message-ID."""
        cursor = await self._db.execute(
            "SELECT 1 FROM tips WHERE message_id = ?", (message_id,)
        )
        return (await cursor.fetchone()) is not None
