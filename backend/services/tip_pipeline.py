"""
Orchestration layer that wires together email ingestion → analysis → storage.
"""

import asyncio
import logging

from config.settings import AppConfig
from backend.models.database import TipDatabase
from backend.services.email_monitor import EmailMonitor
from backend.services.tip_analyzer import TipAnalyzer

logger = logging.getLogger(__name__)


class TipPipeline:
    """
    Manages the full lifecycle:
      Email arrives → parse → store → analyze with Claude → update DB
    """

    def __init__(self, config: AppConfig, db: TipDatabase):
        self.config = config
        self.db = db
        self.analyzer = TipAnalyzer(config)
        self.monitor = EmailMonitor(config.imap, self._on_new_tip)
        self._analysis_queue: asyncio.Queue = asyncio.Queue()
        self._workers: list[asyncio.Task] = []

    async def _on_new_tip(self, tip_data: dict):
        """Callback fired by EmailMonitor for each new email."""
        # Deduplicate by Message-ID
        if await self.db.message_id_exists(tip_data["message_id"]):
            logger.debug(f"Skipping duplicate message: {tip_data['message_id']}")
            return

        # Persist the raw tip
        tip_id = await self.db.insert_tip(tip_data)
        if not tip_id:
            logger.warning(f"Failed to insert tip: {tip_data.get('subject', '?')}")
            return

        logger.info(f"Queued tip #{tip_id}: {tip_data.get('subject', '?')[:60]}")
        await self._analysis_queue.put((tip_id, tip_data))

    async def _analysis_worker(self, worker_id: int):
        """Worker coroutine that pulls tips off the queue and analyzes them."""
        while True:
            tip_id, tip_data = await self._analysis_queue.get()
            try:
                logger.info(f"[Worker {worker_id}] Analyzing tip #{tip_id}")
                await self.db.set_status(tip_id, "analyzing")

                # Get recent tips for context (duplicate detection)
                recent = await self.db.get_recent_subjects(limit=20)

                # Run Claude analysis
                analysis = await self.analyzer.analyze_tip(tip_data, recent)

                # Store results
                await self.db.update_analysis(tip_id, analysis)
                logger.info(
                    f"[Worker {worker_id}] Tip #{tip_id} analyzed — "
                    f"priority={analysis.get('priority', '?')}, "
                    f"score={analysis.get('score_overall', 0):.1f}"
                )

            except Exception as e:
                logger.error(f"[Worker {worker_id}] Analysis failed for tip #{tip_id}: {e}")
                await self.db.set_status(tip_id, "error")

            finally:
                self._analysis_queue.task_done()

    async def _requeue_pending_tips(self):
        """Re-queue any tips stuck in 'pending' or 'analyzing' state (e.g. after a restart)."""
        cursor = await self.db._db.execute(
            "SELECT id, message_id, subject, sender_email, sender_name, "
            "received_at, body_text, body_html, attachments_json "
            "FROM tips WHERE status IN ('pending', 'analyzing') "
            "ORDER BY received_at ASC"
        )
        rows = await cursor.fetchall()
        if not rows:
            return

        logger.info(f"Re-queuing {len(rows)} unfinished tip(s) from previous run")
        for row in rows:
            tip_data = {
                "message_id": row["message_id"],
                "subject": row["subject"],
                "sender_email": row["sender_email"],
                "sender_name": row["sender_name"],
                "received_at": row["received_at"],
                "body_text": row["body_text"],
                "body_html": row["body_html"] or "",
                "attachments": [],
            }
            await self._analysis_queue.put((row["id"], tip_data))

    async def start(self):
        """Start the email monitor and analysis workers."""
        num_workers = self.config.claude.max_concurrent_analyses
        logger.info(f"Starting tip pipeline with {num_workers} analysis workers")

        # Spin up analysis workers
        for i in range(num_workers):
            task = asyncio.create_task(self._analysis_worker(i))
            self._workers.append(task)

        # Re-queue any tips left in pending/analyzing state
        await self._requeue_pending_tips()

        # Start the email polling loop
        await self.monitor.start()

    async def stop(self):
        """Gracefully shut down the pipeline."""
        self.monitor.stop()

        # Wait for the queue to drain
        if not self._analysis_queue.empty():
            logger.info("Waiting for analysis queue to drain...")
            await asyncio.wait_for(self._analysis_queue.join(), timeout=120)

        # Cancel workers
        for task in self._workers:
            task.cancel()

        logger.info("Tip pipeline stopped")

    async def ingest_manual_tip(self, tip_data: dict) -> int:
        """Manually submit a tip (e.g., from the dashboard). Returns tip ID."""
        if not tip_data.get("message_id"):
            import uuid
            tip_data["message_id"] = f"manual-{uuid.uuid4()}"

        tip_id = await self.db.insert_tip(tip_data)
        if tip_id:
            await self._analysis_queue.put((tip_id, tip_data))
        return tip_id
