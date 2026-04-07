"""
MicrodyneHunter v2 — Reply Tracker
Checks Gmail inbox via IMAP for replies from leads.
Matches sender email to leads in DB and marks them as replied.
"""

import imaplib
import email
import logging
import threading
import time
from datetime import datetime, timezone
from email.header import decode_header

from config import SMTP_USER, SMTP_PASSWORD

logger = logging.getLogger("microdynehunter.reply_tracker")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
CHECK_INTERVAL = 60  # check every 1 minute

_tracker_running = False


def _decode_header_value(value):
    """Decode email header value."""
    if not value:
        return ""
    decoded = decode_header(value)
    parts = []
    for part, charset in decoded:
        if isinstance(part, bytes):
            parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(part)
    return " ".join(parts)


def _extract_email_address(from_header):
    """Extract email address from From header like 'Name <email@example.com>'."""
    if not from_header:
        return ""
    if "<" in from_header and ">" in from_header:
        return from_header.split("<")[1].split(">")[0].strip().lower()
    return from_header.strip().lower()


def check_replies():
    """
    Connect to Gmail IMAP, fetch recent unread emails,
    match sender to leads in DB, mark as replied.
    Returns count of new replies found.
    """
    from modules.database import db
    from modules.events import emit_log

    if not SMTP_USER or not SMTP_PASSWORD or "your" in SMTP_PASSWORD.lower():
        logger.info("[Replies] No SMTP credentials — skipping reply check")
        return 0

    if not db:
        logger.info("[Replies] No DB connection — skipping reply check")
        return 0

    try:
        # Connect to Gmail IMAP
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(SMTP_USER, SMTP_PASSWORD)
        mail.select("INBOX")

        # Get all lead emails from DB for matching
        leads = db.select("leads",
            columns="id,contact_email,company_name,replied",
            filters={"email_sent": "eq.true", "replied": "eq.false"},
            limit=5000)

        if not leads:
            mail.logout()
            return 0

        # Build lookup: email -> lead
        lead_lookup = {}
        for lead in leads:
            e = (lead.get("contact_email") or "").strip().lower()
            if e:
                lead_lookup[e] = lead

        new_replies = 0

        # Search for emails FROM each lead contact (much faster than scanning all UNSEEN)
        # Also search recent emails (last 3 days) as backup
        all_msg_ids = set()
        for lead_email in lead_lookup.keys():
            try:
                status, messages = mail.search(None, "FROM", f'"{lead_email}"')
                if status == "OK" and messages[0]:
                    for mid in messages[0].split()[-10:]:  # last 10 from each sender
                        all_msg_ids.add(mid)
            except:
                pass

        # Also check recent UNSEEN as fallback
        try:
            from datetime import timedelta
            since_date = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%d-%b-%Y")
            status, messages = mail.search(None, "UNSEEN", "SINCE", since_date)
            if status == "OK" and messages[0]:
                for mid in messages[0].split()[-30:]:
                    all_msg_ids.add(mid)
        except:
            pass

        logger.info(f"[Replies] Checking {len(all_msg_ids)} emails for replies from {len(lead_lookup)} leads")

        for msg_id in all_msg_ids:
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                from_header = _decode_header_value(msg.get("From", ""))
                sender_email = _extract_email_address(from_header)
                subject = _decode_header_value(msg.get("Subject", ""))

                if not sender_email:
                    continue

                # Check if this sender matches a lead
                if sender_email in lead_lookup:
                    lead = lead_lookup[sender_email]
                    lead_id = lead["id"]
                    company = lead.get("company_name", "?")

                    # Mark lead as replied
                    now = datetime.now(timezone.utc).isoformat()
                    db.update("leads", {
                        "replied": True,
                        "replied_at": now,
                    }, {"id": f"eq.{lead_id}"})

                    new_replies += 1
                    logger.info(f"[Replies] Reply detected from {sender_email} ({company})")

                    emit_log(
                        f"Reply received from {sender_email} ({company})",
                        level="info",
                        category="email",
                        data={
                            "type": "reply_received",
                            "sender": sender_email,
                            "company": company,
                            "subject": subject[:60],
                        },
                    )

                    # Remove from lookup so we don't match again
                    del lead_lookup[sender_email]

            except Exception as e:
                logger.error(f"[Replies] Error processing message: {e}")
                continue

        mail.logout()

        if new_replies > 0:
            logger.info(f"[Replies] {new_replies} new replies matched to leads")
            emit_log(
                f"{new_replies} new replies detected",
                level="info",
                category="email",
                data={"type": "replies_summary", "count": new_replies},
            )

        return new_replies

    except imaplib.IMAP4.error as e:
        logger.error(f"[Replies] IMAP error: {e}")
        return 0
    except Exception as e:
        logger.error(f"[Replies] Error checking replies: {e}")
        return 0


def reply_tracker_thread():
    """Background thread that periodically checks for replies."""
    global _tracker_running
    logger.info(f"[Replies] Reply tracker started — checking every {CHECK_INTERVAL}s")

    while _tracker_running:
        try:
            check_replies()
        except Exception as e:
            logger.error(f"[Replies] Tracker error: {e}")

        # Sleep in chunks so we can stop quickly
        elapsed = 0
        while elapsed < CHECK_INTERVAL and _tracker_running:
            time.sleep(2)
            elapsed += 2

    logger.info("[Replies] Reply tracker stopped")


def start_reply_tracker():
    """Start the reply tracker background thread."""
    global _tracker_running
    _tracker_running = True
    t = threading.Thread(target=reply_tracker_thread, daemon=True, name="reply-tracker")
    t.start()
    logger.info("[Replies] Reply tracker thread started")
    return t


def stop_reply_tracker():
    """Stop the reply tracker."""
    global _tracker_running
    _tracker_running = False
    logger.info("[Replies] Reply tracker stop signal sent")
