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
import os
from datetime import datetime, timezone
from email.header import decode_header

import config

logger = logging.getLogger("microdynehunter.reply_tracker")

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


def _imap_settings_for_smtp() -> tuple[str, int, str, str]:
    """
    Resolve IMAP host/port/user/password dynamically from current runtime settings.
    This allows Admin Panel changes to work immediately (no server restart).
    """
    smtp_user = (os.getenv("SMTP_USER") or getattr(config, "SMTP_USER", "") or "").strip()
    smtp_password = (os.getenv("SMTP_PASSWORD") or getattr(config, "SMTP_PASSWORD", "") or "").strip()
    smtp_host = (os.getenv("SMTP_HOST") or getattr(config, "SMTP_HOST", "") or "").strip().lower()

    imap_host = (os.getenv("SMTP_IMAP_HOST") or "").strip()
    if not imap_host:
        if smtp_host.startswith("smtp."):
            imap_host = "imap." + smtp_host.split("smtp.", 1)[1]
        elif smtp_user.endswith("@gmail.com"):
            imap_host = "imap.gmail.com"
        else:
            imap_host = "imap.gmail.com"

    try:
        imap_port = int((os.getenv("SMTP_IMAP_PORT") or "993").strip())
    except Exception:
        imap_port = 993

    return imap_host, imap_port, smtp_user, smtp_password


def check_replies():
    """
    Connect to Gmail IMAP, fetch recent unread emails,
    match sender to leads in DB, mark as replied.
    Returns count of new replies found.
    """
    from modules.database import db
    from modules.events import emit_log

    imap_host, imap_port, smtp_user, smtp_password = _imap_settings_for_smtp()
    if not smtp_user or not smtp_password or "your" in smtp_password.lower():
        logger.info("[Replies] No SMTP credentials — skipping reply check")
        return 0

    if not db:
        logger.info("[Replies] No DB connection — skipping reply check")
        return 0

    try:
        # Connect to Gmail IMAP
        mail = imaplib.IMAP4_SSL(imap_host, imap_port)
        mail.login(smtp_user, smtp_password)
        mail.select("INBOX")

        # Get all lead emails from DB for matching — include email_sent_at for time filtering
        leads = db.select("leads",
            columns="id,contact_email,company_name,company_domain,website_url,replied,email_sent,email_sent_at,form_filled",
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

        # Search for emails FROM each lead — only SINCE the email was sent
        all_msg_ids = set()
        for lead_email, lead_data in lead_lookup.items():
            try:
                # Only search for emails received AFTER we sent our email
                sent_at = lead_data.get("email_sent_at", "")
                if sent_at:
                    from datetime import timedelta
                    from email.utils import parsedate_to_datetime
                    try:
                        sent_dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
                        since_str = sent_dt.strftime("%d-%b-%Y")
                        status, messages = mail.search(None, "FROM", f'"{lead_email}"', "SINCE", since_str)
                    except:
                        status, messages = mail.search(None, "FROM", f'"{lead_email}"')
                else:
                    status, messages = mail.search(None, "FROM", f'"{lead_email}"')

                if status == "OK" and messages[0]:
                    for mid in messages[0].split()[-5:]:
                        all_msg_ids.add(mid)
            except:
                pass

        logger.info(f"[Replies] Checking {len(all_msg_ids)} emails for replies from {len(lead_lookup)} leads")

        # Also build domain lookup for form reply detection (domain -> lead)
        domain_lookup = {}
        for lead in leads:
            domain = lead.get("company_domain", "")
            if not domain:
                web = lead.get("website_url", "")
                if web:
                    try:
                        from urllib.parse import urlparse
                        domain = urlparse(web).netloc.replace("www.", "")
                    except:
                        pass
            if domain:
                domain_lookup[domain.lower()] = lead

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

                sender_domain = sender_email.split("@")[-1].lower()

                # Try to find matching lead
                matched_lead = None
                reply_source = ""

                # Detect if this is a "Re:" reply or a fresh email
                is_reply_thread = subject.lower().startswith("re:") or subject.lower().startswith("re :")

                # METHOD 1: Exact email match (sender = lead's contact_email)
                if sender_email in lead_lookup:
                    matched_lead = lead_lookup[sender_email]
                    if is_reply_thread:
                        # "Re:" subject = direct reply to our cold email
                        reply_source = "email"
                    else:
                        # Fresh email (no "Re:") from the same person
                        if matched_lead.get("form_filled"):
                            # Form was filled for this lead — fresh email likely triggered by form
                            reply_source = "form"
                        elif matched_lead.get("email_sent"):
                            # Only email was sent, no form — still counts as email response
                            reply_source = "email"
                        else:
                            reply_source = "email"

                # METHOD 2: Domain match (different person from same company domain)
                if not matched_lead:
                    for lead_domain, lead_data in domain_lookup.items():
                        if sender_domain == lead_domain or sender_domain.endswith("." + lead_domain) or lead_domain.endswith("." + sender_domain):
                            matched_lead = lead_data
                            # Different person from same domain = always form reply
                            reply_source = "form"
                            break

                if not matched_lead:
                    continue

                lead_id = matched_lead["id"]
                company = matched_lead.get("company_name", "?")

                # CRITICAL: Only count if email was received AFTER we sent ours
                sent_at_str = matched_lead.get("email_sent_at", "")
                if sent_at_str:
                    try:
                        sent_dt = datetime.fromisoformat(sent_at_str.replace("Z", "+00:00"))
                        email_date_str = msg.get("Date", "")
                        if email_date_str:
                            from email.utils import parsedate_to_datetime as pdt
                            email_dt = pdt(email_date_str)
                            if email_dt.timestamp() < (sent_dt.timestamp() - 60):
                                continue  # Older than our sent email — skip
                    except:
                        pass

                # Update lead
                now = datetime.now(timezone.utc).isoformat()
                current = db.select("leads", columns="reply_source,replied", filters={"id": f"eq.{lead_id}"}, limit=1)
                if current and current[0].get("replied"):
                    # Already replied — check if we need to upgrade to "both"
                    existing_source = current[0].get("reply_source", "")
                    if existing_source and existing_source != reply_source:
                        reply_source = "both"
                    elif existing_source:
                        continue  # Already tracked this reply

                db.update("leads", {
                    "replied": True,
                    "replied_at": now,
                    "reply_source": reply_source,
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
                if sender_email in lead_lookup:
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


def check_form_replies():
    """
    Check for replies to form submissions using DOMAIN MATCHING.
    When we submit a form on company.com, if someone from @company.com
    emails our form submission email, we match it as a form reply.

    Uses FORM_REPLY_IMAP_USER and FORM_REPLY_IMAP_PASSWORD from .env
    (the email used in form submissions, e.g. sales@microdyneengineering.com)
    """
    from modules.database import db
    from modules.events import emit_log

    form_email = os.getenv("FORM_REPLY_IMAP_USER", "")
    form_password = os.getenv("FORM_REPLY_IMAP_PASSWORD", "")

    if not form_email or not form_password:
        # No credentials configured yet — skip silently
        return 0

    if not db:
        return 0

    try:
        # Connect to IMAP for the form submission email
        imap_host = os.getenv("FORM_REPLY_IMAP_HOST", "imap.gmail.com")
        form_imap_port = int(os.getenv("FORM_REPLY_IMAP_PORT", "993"))
        mail = imaplib.IMAP4_SSL(imap_host, form_imap_port)
        mail.login(form_email, form_password)
        mail.select("INBOX")

        # Get all leads where form was filled but no reply yet
        form_leads = db.select("leads",
            columns="id,company_name,company_domain,website_url,form_filled,replied",
            filters={"form_filled": "eq.true", "replied": "eq.false"},
            limit=5000)

        if not form_leads:
            mail.logout()
            return 0

        # Build domain lookup: domain -> lead
        domain_lookup = {}
        for lead in form_leads:
            # Extract domain from website_url or company_domain
            domain = lead.get("company_domain", "")
            if not domain and lead.get("website_url"):
                from urllib.parse import urlparse
                try:
                    domain = urlparse(lead["website_url"]).netloc.replace("www.", "")
                except:
                    pass
            if domain:
                domain_lookup[domain.lower()] = lead

        new_form_replies = 0

        # Search for recent emails (last 7 days) in the form email inbox
        since_date = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=7)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, "SINCE", since_date)
        if status != "OK" or not messages[0]:
            mail.logout()
            return 0

        msg_ids = messages[0].split()
        logger.info(f"[FormReplies] Checking {len(msg_ids)} recent emails for form replies from {len(domain_lookup)} domains")

        for msg_id in msg_ids[-100:]:  # Check last 100
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)
                from_header = _decode_header_value(msg.get("From", ""))
                sender_email = _extract_email_address(from_header)

                if not sender_email:
                    continue

                # Extract sender domain
                sender_domain = sender_email.split("@")[-1].lower()

                # Check if sender domain matches any form-submitted lead
                matched_lead = None
                for lead_domain, lead in domain_lookup.items():
                    if sender_domain == lead_domain or sender_domain.endswith("." + lead_domain) or lead_domain.endswith("." + sender_domain):
                        matched_lead = lead
                        break

                if matched_lead:
                    lead_id = matched_lead["id"]
                    company = matched_lead.get("company_name", "?")
                    subject = _decode_header_value(msg.get("Subject", ""))

                    # Mark lead as replied via FORM
                    now = datetime.now(timezone.utc).isoformat()
                    # Check if already replied via email — if so, set "both"
                    current = db.select("leads", columns="reply_source", filters={"id": f"eq.{lead_id}"}, limit=1)
                    existing_source = current[0].get("reply_source", "") if current else ""
                    new_source = "both" if existing_source == "email" else "form"
                    db.update("leads", {
                        "replied": True,
                        "replied_at": now,
                        "interested": True,
                        "reply_source": new_source,
                    }, {"id": f"eq.{lead_id}"})

                    new_form_replies += 1
                    logger.info(f"[FormReplies] Form reply detected: {sender_email} -> {company}")

                    emit_log(
                        f"Form reply from {sender_email} ({company})",
                        level="info",
                        category="form",
                        data={
                            "type": "form_reply_received",
                            "sender": sender_email,
                            "company": company,
                            "subject": subject[:60],
                        },
                    )

                    # Remove from lookup to avoid matching again
                    for k, v in list(domain_lookup.items()):
                        if v["id"] == lead_id:
                            del domain_lookup[k]
                            break

            except Exception as e:
                logger.error(f"[FormReplies] Error processing message: {e}")
                continue

        mail.logout()

        if new_form_replies > 0:
            logger.info(f"[FormReplies] {new_form_replies} form replies matched")
            emit_log(
                f"{new_form_replies} form replies detected",
                level="info",
                category="form",
                data={"type": "form_replies_summary", "count": new_form_replies},
            )

        return new_form_replies

    except imaplib.IMAP4.error as e:
        logger.error(f"[FormReplies] IMAP error: {e}")
        return 0
    except Exception as e:
        logger.error(f"[FormReplies] Error: {e}")
        return 0


def reply_tracker_thread():
    """Background thread that periodically checks for replies."""
    global _tracker_running
    logger.info(f"[Replies] Reply tracker started — checking every {CHECK_INTERVAL}s")

    while _tracker_running:
        try:
            # Run both checks every cycle so dashboard polling is not required.
            email_hits = check_replies()
            form_hits = check_form_replies()
            if email_hits or form_hits:
                logger.info(f"[Replies] Cycle sync complete — email: {email_hits}, form: {form_hits}")
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
