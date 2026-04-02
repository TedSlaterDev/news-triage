"""
Configuration settings for the News Tip Triage System.
Uses environment variables with sensible defaults.
"""

import os
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class IMAPConfig:
    """IMAP email server configuration."""
    host: str = os.getenv("IMAP_HOST", "imap.gmail.com")
    port: int = int(os.getenv("IMAP_PORT", "993"))
    username: str = os.getenv("IMAP_USERNAME", "")
    password: str = os.getenv("IMAP_PASSWORD", "")
    mailbox: str = os.getenv("IMAP_MAILBOX", "INBOX")
    use_ssl: bool = os.getenv("IMAP_USE_SSL", "true").lower() == "true"
    poll_interval_seconds: int = int(os.getenv("IMAP_POLL_INTERVAL", "60"))


@dataclass
class ClaudeConfig:
    """Anthropic Claude API configuration."""
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
    max_tokens: int = int(os.getenv("CLAUDE_MAX_TOKENS", "4096"))
    max_concurrent_analyses: int = int(os.getenv("CLAUDE_MAX_CONCURRENT", "5"))


@dataclass
class DatabaseConfig:
    """SQLite database configuration."""
    db_path: str = os.getenv(
        "DB_PATH",
        str(Path(__file__).parent.parent / "data" / "tips.db")
    )


@dataclass
class DashboardConfig:
    """Dashboard and API server configuration."""
    host: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port: int = int(os.getenv("DASHBOARD_PORT", "8000"))
    auto_refresh_seconds: int = int(os.getenv("DASHBOARD_REFRESH", "30"))


@dataclass
class AppConfig:
    """Top-level application configuration."""
    imap: IMAPConfig = field(default_factory=IMAPConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)

    # Newsworthiness scoring weights (must sum to 1.0)
    score_weights: dict = field(default_factory=lambda: {
        "timeliness": 0.20,
        "impact": 0.25,
        "novelty": 0.20,
        "credibility": 0.15,
        "public_interest": 0.20,
    })

    # Categories for tip classification
    categories: list = field(default_factory=lambda: [
        "politics", "crime", "business", "health", "environment",
        "technology", "education", "sports", "entertainment",
        "human_interest", "breaking", "investigative", "other",
    ])

    # Priority thresholds (out of 100)
    priority_thresholds: dict = field(default_factory=lambda: {
        "critical": 85,   # Immediate attention
        "high": 70,       # Review within the hour
        "medium": 50,     # Review today
        "low": 0,         # Backlog
    })


def load_config() -> AppConfig:
    """Load configuration from environment variables."""
    return AppConfig()
