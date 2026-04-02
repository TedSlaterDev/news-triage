#!/usr/bin/env python3
"""
Re-queues all tips with status 'error' for analysis.
Run this while the main server (run.py) is active.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config.settings import load_config
from backend.models.database import TipDatabase


async def main():
    config = load_config()
    db = TipDatabase(config.database.db_path)
    await db.connect()

    # Find all failed tips
    cursor = await db._db.execute(
        "SELECT id, subject FROM tips WHERE status = 'error'"
    )
    failed = await cursor.fetchall()

    if not failed:
        print("No failed tips found — nothing to retry.")
        await db.close()
        return

    print(f"Found {len(failed)} failed tip(s). Resetting to 'pending'...")

    # Reset their status so the pipeline picks them up
    await db._db.execute(
        "UPDATE tips SET status = 'pending', updated_at = datetime('now') "
        "WHERE status = 'error'"
    )
    await db._db.commit()

    print(f"Done. {len(failed)} tip(s) reset to 'pending'.")
    print()
    print("Now restart the server (Ctrl+C, then python run.py) to re-queue them.")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
