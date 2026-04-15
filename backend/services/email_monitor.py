"""
IMAP email monitor that polls a mailbox for new tips.
Parses emails into structured data and hands them off for analysis.
"""

import asyncio
import email
import email.policy
import imaplib
import logging
import re
from datetime import datetime
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Optional, Callable, Awaitable

from config.settings import IMAPConfig

logger = logging.getLogger(__name__)


class _HTMLTextExtractor(HTMLParser):
    """Strips HTML tags and collects readable text."""

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip > 0:
            self._skip -= 1
        elif tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip == 0 and data.strip():
            self.parts.append(data)

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = unescape(text)
        # Collapse excessive whitespace
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(html: str) -> str:
    """Convert HTML email body to readable plain text."""
    if not html:
        return ""
    try:
        parser = _HTMLTextExtractor()
        parser.feed(html)
        return parser.get_text()
    except Exception:
        # Fallback: crude tag stripping
        text = re.sub(r"<[^>]+>", " ", html)
        text = unescape(text)
        return re.sub(r"\s+", " ", text).strip()


def _decode_header_value(value: str) -> str:
    """Decode RFC 2047 encoded email headers."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for content, charset in parts:
        if isinstance(content, bytes):
            decoded.append(content.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(content)
    return " ".join(decoded)


def _extract_body(msg: email.message.EmailMessage) -> tuple[str, str]:
    """Extract plain text and HTML body from an email message."""
    text_body = ""
    html_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in disposition:
                continue

            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                continue

            if content_type == "text/plain" and not text_body:
                text_body = decoded
            elif content_type == "text/html" and not html_body:
                html_body = decoded
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    html_body = decoded
                else:
                    text_body = decoded
        except Exception:
            pass

    return text_body, html_body


def _extract_attachments(msg: email.message.EmailMessage) -> list[dict]:
    """Extract attachment metadata (not content) from an email."""
    attachments = []
    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        if "attachment" not in disposition:
            continue

        filename = part.get_filename()
        if filename:
            filename = _decode_header_value(filename)

        attachments.append({
            "filename": filename or "unnamed",
            "content_type": part.get_content_type(),
            "size": len(part.get_payload(decode=True) or b""),
        })

    return attachments


def parse_email(raw_bytes: bytes) -> dict:
    """Parse raw email bytes into a structured tip dictionary."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    sender_name, sender_email = parseaddr(msg.get("From", ""))
    sender_name = _decode_header_value(sender_name) if sender_name else ""
    subject = _decode_header_value(msg.get("Subject", "(no subject)"))
    message_id = msg.get("Message-ID", "")

    # For Gateway Pundit emails, use the Reply-To address as the contact email
    display_email = sender_email
    if "gateway pundit" in sender_name.lower() or "gatewaypundit" in sender_email.lower():
        reply_to = msg.get("Reply-To", "")
        if reply_to:
            _, reply_email = parseaddr(reply_to)
            if reply_email:
                display_email = reply_email

    # Parse date
    date_str = msg.get("Date", "")
    try:
        received_at = parsedate_to_datetime(date_str).isoformat()
    except Exception:
        received_at = datetime.utcnow().isoformat()

    text_body, html_body = _extract_body(msg)
    # Fall back to HTML-stripped text if no plain text part was present
    if not text_body.strip() and html_body:
        text_body = html_to_text(html_body)
    attachments = _extract_attachments(msg)

    return {
        "message_id": message_id,
        "subject": subject,
        "sender_email": display_email,
        "sender_name": sender_name,
        "received_at": received_at,
        "body_text": text_body,
        "body_html": html_body,
        "attachments": attachments,
    }


class EmailMonitor:
    """
    Monitors an IMAP mailbox for new emails, parses them,
    and invokes a callback for each new tip discovered.
    """

    def __init__(
        self,
        config: IMAPConfig,
        on_new_tip: Callable[[dict], Awaitable[None]],
    ):
        self.config = config
        self.on_new_tip = on_new_tip
        self._running = False
        self._seen_uids: set[str] = set()

    def _connect(self) -> imaplib.IMAP4_SSL | imaplib.IMAP4:
        """Establish IMAP connection."""
        if self.config.use_ssl:
            conn = imaplib.IMAP4_SSL(self.config.host, self.config.port)
        else:
            conn = imaplib.IMAP4(self.config.host, self.config.port)

        conn.login(self.config.username, self.config.password)
        conn.select(self.config.mailbox)
        return conn

    def _fetch_new_emails(self, conn) -> list[dict]:
        """Fetch unread emails from the mailbox."""
        tips = []

        # Search for unseen messages
        status, data = conn.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            return tips

        uids = data[0].split()
        logger.info(f"Found {len(uids)} unread email(s)")

        for uid in uids:
            uid_str = uid.decode()
            if uid_str in self._seen_uids:
                continue

            status, msg_data = conn.fetch(uid, "(RFC822)")
            if status != "OK" or not msg_data[0]:
                continue

            raw_email = msg_data[0][1]
            try:
                tip = parse_email(raw_email)
                tips.append(tip)
                self._seen_uids.add(uid_str)
                logger.info(f"Parsed tip: {tip['subject'][:80]}")
            except Exception as e:
                logger.error(f"Failed to parse email UID {uid_str}: {e}")

        return tips

    async def poll_once(self):
        """Run a single poll cycle (connects, fetches, disconnects)."""
        conn = None
        try:
            # IMAP operations are blocking; run in executor
            loop = asyncio.get_event_loop()
            conn = await loop.run_in_executor(None, self._connect)
            tips = await loop.run_in_executor(None, self._fetch_new_emails, conn)

            for tip in tips:
                try:
                    await self.on_new_tip(tip)
                except Exception as e:
                    logger.error(f"Error processing tip '{tip.get('subject', '?')}': {e}")

        except imaplib.IMAP4.error as e:
            logger.error(f"IMAP error: {e}")
        except Exception as e:
            logger.error(f"Email monitor error: {e}")
        finally:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass

    async def start(self):
        """Start the polling loop."""
        self._running = True
        logger.info(
            f"Email monitor started — polling {self.config.host} "
            f"every {self.config.poll_interval_seconds}s"
        )

        while self._running:
            await self.poll_once()
            await asyncio.sleep(self.config.poll_interval_seconds)

    def stop(self):
        """Signal the polling loop to stop."""
        self._running = False
        logger.info("Email monitor stopping")
