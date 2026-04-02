"""
Claude-powered tip analysis engine.

For each incoming tip, Claude acts as a full editorial assistant:
  1. Classifies the tip by category
  2. Extracts and verifies key claims via web research
  3. Scores newsworthiness across five dimensions
  4. Drafts an editorial summary
  5. Suggests follow-up questions for reporters
  6. Flags urgent / breaking / duplicate tips
"""

import asyncio
import json
import logging
from typing import Optional

import anthropic

from config.settings import AppConfig

logger = logging.getLogger(__name__)

# ── System prompt for Claude ─────────────────────────────────────────

ANALYST_SYSTEM_PROMPT = """You are a veteran news editor and tip analyst working in a busy newsroom.
Your job is to evaluate incoming tips from the public and rank them by newsworthiness.

For every tip you receive, produce a JSON analysis with EXACTLY this structure:

{
  "category": "<one of: politics, crime, business, health, environment, technology, education, sports, entertainment, human_interest, breaking, investigative, other>",
  "subcategory": "<more specific label, e.g. 'local government corruption'>",
  "summary": "<2-3 sentence editorial summary of the tip suitable for a newsroom briefing>",
  "key_claims": ["<claim 1>", "<claim 2>", ...],
  "research_notes": "<what you found when verifying the claims — cite any existing coverage, public records, or context that supports or contradicts the tip>",
  "related_coverage": [{"title": "...", "source": "...", "url": "...", "relevance": "..."}],
  "follow_up_questions": ["<question a reporter should ask>", ...],
  "source_credibility": "<high | medium | low | unknown> with brief justification",
  "scores": {
    "timeliness": <0-100>,
    "impact": <0-100>,
    "novelty": <0-100>,
    "credibility": <0-100>,
    "public_interest": <0-100>
  },
  "is_urgent": <true if this needs immediate attention>,
  "is_breaking": <true if this is a breaking news event>,
  "is_duplicate": <true if this is substantially the same as a recent tip>,
  "reasoning": "<brief explanation of your scoring decisions>"
}

Scoring guide:
- **Timeliness**: Is this happening right now or very recently? Breaking events score 90+.
- **Impact**: How many people are affected? Government/public safety issues score higher.
- **Novelty**: Is this new information not yet reported? Scoops score 90+.
- **Credibility**: Does the source provide verifiable details? Named sources with evidence score higher.
- **Public interest**: Would the general public care about this? Consider civic importance.

IMPORTANT:
- Always output valid JSON and nothing else.
- Be skeptical but fair — newsrooms need both caution and speed.
- If the tip is vague or low quality, still analyze it but score accordingly.
- For duplicate detection, compare against the recent tips provided in context.
"""

# ── Web research tool definition for Claude ──────────────────────────

WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Search the web for recent news coverage, public records, or context "
        "related to a news tip. Use this to verify claims, find related stories, "
        "and assess whether this is a known or new story."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to research the tip.",
            }
        },
        "required": ["query"],
    },
}


class TipAnalyzer:
    """
    Orchestrates Claude-based analysis of news tips.
    Manages concurrency and handles the tool-use loop for web research.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=config.claude.api_key)
        self._semaphore = asyncio.Semaphore(config.claude.max_concurrent_analyses)

    def _build_user_message(
        self, tip: dict, recent_tips: list[str]
    ) -> str:
        """Construct the user message with tip content and context."""
        parts = [
            "## Incoming Tip\n",
            f"**From:** {tip.get('sender_name', '')} <{tip.get('sender_email', '')}>",
            f"**Subject:** {tip.get('subject', '(no subject)')}",
            f"**Received:** {tip.get('received_at', 'unknown')}",
            "",
        ]

        body = tip.get("body_text", "").strip()
        if body:
            parts.append("**Body:**")
            # Truncate very long emails
            if len(body) > 5000:
                parts.append(body[:5000] + "\n\n[... truncated ...]")
            else:
                parts.append(body)

        attachments = tip.get("attachments", [])
        if attachments:
            parts.append(f"\n**Attachments:** {len(attachments)} file(s)")
            for att in attachments[:5]:
                parts.append(f"  - {att.get('filename', '?')} ({att.get('content_type', '?')})")

        if recent_tips:
            parts.append("\n## Recent Tips (for duplicate detection)")
            for i, rt in enumerate(recent_tips[:15], 1):
                parts.append(f"{i}. {rt}")

        return "\n".join(parts)

    async def _do_web_search(self, query: str) -> str:
        """
        Perform a web search to help Claude verify claims.
        In production, plug in your preferred search API here
        (e.g., Brave Search, SerpAPI, Google Custom Search).
        Returns search result text for Claude to consume.
        """
        # ── Placeholder implementation ──
        # Replace this with a real search API call.
        # The return value is fed back to Claude as a tool result.
        try:
            # Example with httpx + Brave Search API:
            # import httpx
            # async with httpx.AsyncClient() as client:
            #     resp = await client.get(
            #         "https://api.search.brave.com/res/v1/web/search",
            #         params={"q": query, "count": 5},
            #         headers={"X-Subscription-Token": os.getenv("BRAVE_API_KEY")},
            #     )
            #     results = resp.json().get("web", {}).get("results", [])
            #     return "\n\n".join(
            #         f"**{r['title']}** ({r['url']})\n{r.get('description', '')}"
            #         for r in results
            #     )
            return (
                f"[Web search for '{query}' — integrate a search API "
                f"(Brave, SerpAPI, etc.) for real results. "
                f"For now, proceed with analysis based on the tip content alone.]"
            )
        except Exception as e:
            logger.error(f"Web search failed: {e}")
            return f"Web search failed: {e}"

    async def analyze_tip(
        self, tip: dict, recent_tips: list[str]
    ) -> dict:
        """
        Run full editorial analysis on a tip using Claude.
        Handles the tool-use loop for web research.
        Returns the parsed analysis dict.
        """
        async with self._semaphore:
            user_message = self._build_user_message(tip, recent_tips)
            messages = [{"role": "user", "content": user_message}]

            # Tool-use loop: Claude may call web_search multiple times
            max_rounds = 5
            for _ in range(max_rounds):
                response = await self.client.messages.create(
                    model=self.config.claude.model,
                    max_tokens=self.config.claude.max_tokens,
                    system=ANALYST_SYSTEM_PROMPT,
                    tools=[WEB_SEARCH_TOOL],
                    messages=messages,
                )

                # Check if Claude wants to use a tool
                if response.stop_reason == "tool_use":
                    # Collect all tool uses and results
                    assistant_content = response.content
                    tool_results = []

                    for block in assistant_content:
                        if block.type == "tool_use":
                            query = block.input.get("query", "")
                            logger.info(f"Claude searching: {query}")
                            result = await self._do_web_search(query)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            })

                    messages.append({"role": "assistant", "content": assistant_content})
                    messages.append({"role": "user", "content": tool_results})
                else:
                    # Final response — extract JSON
                    break

            # Parse Claude's final response
            return self._parse_response(response, tip)

    def _parse_response(self, response, tip: dict) -> dict:
        """Extract and validate the JSON analysis from Claude's response."""
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        # Try to parse JSON (Claude may wrap it in markdown code fences)
        text = text.strip()
        if text.startswith("```"):
            # Strip code fences
            lines = text.split("\n")
            text = "\n".join(
                l for l in lines
                if not l.strip().startswith("```")
            )

        try:
            analysis = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {e}")
            logger.debug(f"Raw response: {text[:500]}")
            return self._fallback_analysis(tip, str(e))

        # Calculate weighted overall score
        scores = analysis.get("scores", {})
        weights = self.config.score_weights
        overall = sum(
            scores.get(dim, 0) * weight
            for dim, weight in weights.items()
        )
        analysis["score_overall"] = round(overall, 1)

        # Determine priority
        thresholds = self.config.priority_thresholds
        if overall >= thresholds["critical"]:
            analysis["priority"] = "critical"
        elif overall >= thresholds["high"]:
            analysis["priority"] = "high"
        elif overall >= thresholds["medium"]:
            analysis["priority"] = "medium"
        else:
            analysis["priority"] = "low"

        return analysis

    def _fallback_analysis(self, tip: dict, error: str) -> dict:
        """Return a minimal analysis when Claude's response can't be parsed."""
        return {
            "category": "other",
            "subcategory": "",
            "summary": f"Analysis failed: {error}. Manual review required.",
            "key_claims": [],
            "research_notes": "",
            "follow_up_questions": [],
            "related_coverage": [],
            "source_credibility": "unknown",
            "scores": {
                "timeliness": 50,
                "impact": 50,
                "novelty": 50,
                "credibility": 50,
                "public_interest": 50,
            },
            "score_overall": 50.0,
            "priority": "medium",
            "is_urgent": False,
            "is_breaking": False,
            "is_duplicate": False,
        }
