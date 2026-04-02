#!/usr/bin/env python3
"""
Entry point for the News Tip Triage system.
Loads .env and starts the FastAPI server.
"""

import sys
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional

import uvicorn
from config.settings import load_config

config = load_config()

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════════╗
║         ⚡  News Tip Triage System                   ║
╠══════════════════════════════════════════════════════╣
║  Dashboard:  http://localhost:{config.dashboard.port:<5}                ║
║  IMAP host:  {config.imap.host:<39} ║
║  AI model:   {config.claude.model:<39} ║
║  Polling:    every {config.imap.poll_interval_seconds}s{' ' * 31}║
╚══════════════════════════════════════════════════════╝
    """)

    uvicorn.run(
        "backend.app:app",
        host=config.dashboard.host,
        port=config.dashboard.port,
        reload=False,
        log_level="info",
    )
