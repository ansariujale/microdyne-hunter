"""
FlowLockHunter v2 — Email Workers (DB-Polling)
Two independent background workers:
  1. Email Worker — polls DB for "New" leads, generates variants, sends
  2. Followup Worker — polls DB for due follow-ups, sends next in sequence

Uses fetch-then-process pattern: fetch batch → process all → cooldown → fetch again.
Zero DB polls during processing. Respects warmup limits.
"""

import os
import re
import time
import random
import logging
import threading
from datetime import datetime, timezone, timedelta

from config import (
    SCORE_THRESHOLDS, JUNK_EMAIL_DOMAINS, INSTANTLY_API_KEY,
    EMAIL_SEND_DELAY, SENDER_EMAIL, SUPABASE_URL,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM_NAME,
)

def _using_smtp() -> bool:
    """Check if we're using SMTP instead of Instantly."""
    import config
    instantly_key = getattr(config, "INSTANTLY_API_KEY", INSTANTLY_API_KEY) or ""
    smtp_user = getattr(config, "SMTP_USER", SMTP_USER) or ""
    smtp_password = getattr(config, "SMTP_PASSWORD", SMTP_PASSWORD) or ""
    no_instantly = not instantly_key or "your" in instantly_key.lower()
    has_smtp = smtp_user and smtp_password and "your" not in smtp_password.lower()
    return no_instantly and has_smtp
from modules.events import emit_log

logger = logging.getLogger("flowlockhunter.email_queue")

# ═══════════════════════════════════════════════════════════════
# WORKER STATE (shared with dashboard via agent_state)
# ═══════════════════════════════════════════════════════════════

_workers_running = False

worker_status = {
    "email": {"status": "idle", "last_action": "", "processed": 0, "batch_size": 0},
    "followup": {"status": "idle", "last_action": "", "processed": 0},
}

EMAIL_BATCH_SIZE = 20         # fetch N leads per DB poll
COOLDOWN_EMPTY = 60           # seconds to wait when no leads found
COOLDOWN_BATCH = 10           # seconds between batches when leads exist
FOLLOWUP_CHECK_INTERVAL = 120 # seconds between followup checks


# ═══════════════════════════════════════════════════════════════
# EMAIL VALIDATION
# ═══════════════════════════════════════════════════════════════

def _is_valid_email(email: str) -> bool:
    """Basic email format validation."""
    if not email or "@" not in email:
        return False
    pattern = r'^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}$'
    return bool(re.match(pattern, email))


def _is_junk_email(email: str) -> bool:
    """Check if email is from a personal/junk domain.
    Whitelisted test emails are always allowed through."""
    if not email or "@" not in email:
        return True
    # Whitelist: allow specific test emails through
    WHITELISTED_EMAILS = {"githubthe12@gmail.com", "aujale30@gmail.com", "janhavipal353@gmail.com", "marediyao61@gmail.com", "alexgender32@gmail.com", "mizanmarediya04@gmail.com"}
    if email.lower().strip() in WHITELISTED_EMAILS:
        return False
    domain = email.split("@")[-1].lower()
    return domain in JUNK_EMAIL_DOMAINS


def _pick_from_email_for_domain(sending_domain: str) -> str:
    import config
    sending_emails = getattr(config, "SENDING_EMAILS", []) or []
    for addr in sending_emails:
        if "@" in addr and addr.split("@")[-1].lower() == (sending_domain or "").lower():
            return addr.strip()
    return ""


def _get_smtp_sender_accounts() -> list[dict]:
    import base64
    import json
    import config

    accounts_b64 = (os.getenv("SMTP_ACCOUNTS_B64", "") or "").strip()
    smtp_host = (getattr(config, "SMTP_HOST", SMTP_HOST) or "").strip()
    smtp_port = int(getattr(config, "SMTP_PORT", SMTP_PORT) or 587)
    smtp_user = (getattr(config, "SMTP_USER", SMTP_USER) or "").strip()
    smtp_password = (getattr(config, "SMTP_PASSWORD", SMTP_PASSWORD) or "").strip()

    parsed = []
    if accounts_b64:
        try:
            raw = base64.b64decode(accounts_b64.encode("ascii")).decode("utf-8")
            arr = json.loads(raw)
            if isinstance(arr, list):
                for a in arr:
                    if not isinstance(a, dict):
                        continue
                    u = (a.get("user") or "").strip()
                    p = (a.get("password") or "").strip()
                    h = (a.get("host") or smtp_host).strip()
                    prt = int(a.get("port") or smtp_port)
                    if u and p and h and prt:
                        parsed.append({"user": u, "password": p, "host": h, "port": prt})
        except Exception:
            parsed = []

    if parsed:
        return parsed

    if smtp_user and smtp_password:
        accounts = [{"user": smtp_user, "password": smtp_password, "host": smtp_host, "port": smtp_port}]
        for addr in (getattr(config, "SENDING_EMAILS", []) or []):
            if "@" not in addr or "@" not in smtp_user:
                continue
            if addr.strip().lower() == smtp_user.lower():
                continue
            if addr.split("@")[-1].lower() == smtp_user.split("@")[-1].lower():
                accounts.append({"user": addr.strip(), "password": smtp_password, "host": smtp_host, "port": smtp_port})
        return accounts

    return []


# ═══════════════════════════════════════════════════════════════
# PROCESS A SINGLE LEAD (shared by email + followup workers)
# ═══════════════════════════════════════════════════════════════

def process_lead_email(lead: dict, sequence_stage: int = 1, return_reason: bool = False):
    """
    Full email pipeline for one lead at a given sequence stage:
    1. Validate email
    2. Check warmup capacity
    3. Generate 5 variants
    4. Score and pick winner
    5. Send or record
    6. Store variants + outreach log
    7. Update lead record
    Returns True if email was sent/recorded, False if skipped.
    """
    from modules.email_variants import generate_and_pick_winner
    from modules.email_warmup import get_best_domain, get_remaining_capacity, record_send
    from modules.database import db, update_lead

    domain = lead.get("company_domain", "?")
    lead_id = lead.get("id")
    email = (lead.get("contact_email") or "").strip()

    # ── Validate ──────────────────────────────────────────
    if not email or not _is_valid_email(email):
        logger.info(f"[Email] Skip {domain}: invalid email '{email}'")
        if return_reason:
            return False, "invalid_email"
        return False

    if _is_junk_email(email):
        logger.info(f"[Email] Skip {domain}: personal email domain")
        if return_reason:
            return False, "personal_email_domain"
        return False

    try:
        # ── 1. Pick sender account ───────────────────────
        if _using_smtp():
            smtp_accounts = _get_smtp_sender_accounts()
            best = None
            best_remaining = 0
            for a in smtp_accounts:
                key = (a.get("user") or "").strip()
                if not key:
                    continue
                rem = get_remaining_capacity(key)
                if rem > best_remaining:
                    best_remaining = rem
                    best = a
            if not best or best_remaining <= 0:
                logger.warning(f"[Email] SMTP accounts at capacity — skipping {domain}")
                if return_reason:
                    return False, "sender_capacity_ended"
                return False
            from_email_account = (best.get("user") or "").strip()
            smtp_host_override = (best.get("host") or "").strip()
            smtp_port_override = int(best.get("port") or 587)
            smtp_pass_override = (best.get("password") or "").strip()
            sending_domain = from_email_account.split("@")[-1] if "@" in from_email_account else "smtp"
        else:
            sender_account = get_best_domain()
            if not sender_account:
                logger.warning(f"[Email] All senders at capacity — skipping {domain}")
                if return_reason:
                    return False, "all_senders_at_capacity"
                return False
            from_email_account = sender_account.strip()
            sending_domain = from_email_account.split("@")[-1] if "@" in from_email_account else "unknown"
            smtp_host_override = ""
            smtp_port_override = 0
            smtp_pass_override = ""

        if not from_email_account:
            from_email_account = (SENDER_EMAIL or "").strip()

        emit_log(
            f"Generating email for {email} from {from_email_account or SENDER_EMAIL}",
            category="email",
            data={"type": "email_generating", "recipient": email, "sender": (from_email_account or SENDER_EMAIL), "company": domain},
        )

        # ── 2+3. Generate variants and pick winner ────────
        all_variants, winner = generate_and_pick_winner(lead, sequence_stage=sequence_stage)

        emit_log(
            f"Generated {len(all_variants)} variants — Winner: #{winner['variant_number']} (score: {winner['score_total']}/100, angle: {winner.get('angle', '?')})",
            category="email",
            data={
                "type": "variant_selected",
                "recipient": email,
                "variant_count": len(all_variants),
                "winner_number": winner["variant_number"],
                "winner_score": winner["score_total"],
                "winner_angle": winner.get("angle", "?"),
                "subject": winner["subject"],
            },
        )

        emit_log(
            f"Sending email to {email} via {from_email_account or SENDER_EMAIL}",
            category="email",
            data={"type": "email_sending", "recipient": email, "sender": (from_email_account or SENDER_EMAIL)},
        )

        # ── 4. Send or record ─────────────────────────────
        delivery_status, delivery_error = _send_or_record(
            to_email=email,
            subject=winner["subject"],
            body=winner["body"],
            from_domain=sending_domain,
            lead=lead,
            sequence_stage=sequence_stage,
            from_email_account=from_email_account,
            return_error=True,
            smtp_host_override=smtp_host_override,
            smtp_port_override=smtp_port_override,
            smtp_user_override=from_email_account if _using_smtp() else "",
            smtp_password_override=smtp_pass_override,
        )

        if delivery_status == "failed" and not _using_smtp():
            import config
            for alt_email in (getattr(config, "SENDING_EMAILS", []) or []):
                alt_email = (alt_email or "").strip()
                if not alt_email:
                    continue
                if alt_email.lower() == (from_email_account or "").lower():
                    continue
                if get_remaining_capacity(alt_email) <= 0:
                    continue
                emit_log(
                    f"Retrying email to {email} via backup sender {alt_email}",
                    category="email",
                    data={"type": "email_retry", "recipient": email, "sender": alt_email},
                )
                alt_status, alt_error = _send_or_record(
                    to_email=email,
                    subject=winner["subject"],
                    body=winner["body"],
                    from_domain=(alt_email.split("@")[-1] if "@" in alt_email else "unknown"),
                    lead=lead,
                    sequence_stage=sequence_stage,
                    from_email_account=alt_email,
                    return_error=True,
                )
                if alt_status in ("sent", "recorded"):
                    from_email_account = alt_email
                    delivery_status = alt_status
                    delivery_error = alt_error
                    break

        status_label = "delivered" if delivery_status == "sent" else delivery_status
        emit_log(
            f"Email {status_label}: {email} ({domain})",
            level="info" if delivery_status != "failed" else "error",
            category="email",
            data={"type": f"email_{status_label}", "recipient": email, "company": domain, "status": delivery_status},
        )

        # ── 5. Store variants in DB ───────────────────────
        if db and lead_id:
            _store_variants(lead_id, all_variants, sequence_stage)

        # ── 6. Record send in warmup tracker ──────────────
        if delivery_status == "sent":
            record_send(from_email_account)

        # ── 7. Log outreach ───────────────────────────────
        if db and lead_id:
            winner_id = None
            winner_rows = db.select("email_variants",
                                    columns="id",
                                    filters={
                                        "lead_id": f"eq.{lead_id}",
                                        "is_winner": "eq.true",
                                        "sequence_stage": f"eq.{sequence_stage}",
                                    }, limit=1)
            if winner_rows:
                winner_id = winner_rows[0]["id"]

            db.insert("outreach_log", {
                "lead_id": lead_id,
                "channel": "email",
                "sequence_stage": sequence_stage,
                "subject": winner["subject"],
                "body": winner["body"],
                "sending_domain": sending_domain,
                "variant_score": winner["score_total"],
                "variant_id": winner_id,
                "delivery_status": delivery_status,
            })

        if delivery_status == "recorded":
            emit_log(
                f"Skipped {email}: no sending method configured",
                level="warning",
                category="email",
                data={"type": "email_skipped", "recipient": email, "reason": "no_sending_method_configured"},
            )
            if return_reason:
                return False, "no_sending_method_configured"
            return False

        # ── 8. Update lead record ─────────────────────────
        if lead_id and delivery_status == "sent":
            now = datetime.now(timezone.utc).isoformat()
            updates = {
                "sequence_stage": sequence_stage,
                "sending_domain": sending_domain,
            }
            if sequence_stage == 1:
                updates["email_sent"] = True
                updates["email_sent_at"] = now
                updates["next_followup"] = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
            elif sequence_stage < 4:
                followup_days = {2: 4, 3: 7}
                days = followup_days.get(sequence_stage, 7)
                updates["next_followup"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
            else:
                updates["next_followup"] = None  # sequence complete

            update_lead(lead_id, updates)

        if delivery_status == "failed":
            err = (delivery_error or "").strip()
            msg = f"send_failed" + (f": {err[:160]}" if err else "")
            emit_log(
                f"Skipped {email}: {msg}",
                level="error",
                category="email",
                data={"type": "email_failed", "recipient": email, "reason": msg},
            )
            if return_reason:
                return False, msg
            return False

        status = "sent" if delivery_status == "sent" else "recorded"
        logger.info(
            f"[Email] ✓ Stage {sequence_stage} {status} → {email} ({domain}) — "
            f"variant #{winner['variant_number']} ({winner.get('angle', '?')}) "
            f"score: {winner['score_total']}/100 via {sending_domain}"
        )
        if return_reason:
            return True, "sent"
        return True

    except Exception as e:
        logger.error(f"[Email] Error processing {domain}: {e}")
        emit_log(
            f"Skipped {email}: processing_error",
            level="error",
            category="email",
            data={"type": "email_failed", "recipient": email, "reason": "processing_error"},
        )
        if return_reason:
            return False, "processing_error"
        return False


def _store_variants(lead_id: str, variants: list[dict], sequence_stage: int) -> None:
    """Store all variants in the email_variants table."""
    from modules.database import db
    if not db:
        return
    for v in variants:
        db.insert("email_variants", {
            "lead_id": lead_id,
            "sequence_stage": sequence_stage,
            "variant_number": v.get("variant_number", 0),
            "subject": v.get("subject", ""),
            "body": v.get("body", ""),
            "angle": v.get("angle", ""),
            "score_total": v.get("score_total", 0),
            "score_subject": v.get("score_subject", 0),
            "score_personalization": v.get("score_personalization", 0),
            "score_cta": v.get("score_cta", 0),
            "score_spam_risk": v.get("score_spam_risk", 0),
            "is_winner": v.get("is_winner", False),
        })


def _create_tracking_pixel(lead: dict, to_email: str, subject: str, sequence_stage: int = 1) -> tuple[str, str | None]:
    """Create a tracking pixel and insert tracking record. Returns (pixel_html, tracking_id)."""
    import uuid
    from modules.database import db
    if not db:
        return "", None

    tracking_id = str(uuid.uuid4())
    try:
        db.insert("email_tracking", {
            "tracking_id": tracking_id,
            "lead_id": lead.get("id"),
            "sequence_stage": sequence_stage,
            "recipient_email": to_email,
            "subject": subject,
        })
        # Supabase Edge Function handles open tracking
        pixel_url = f"{SUPABASE_URL}/functions/v1/clever-function?id={tracking_id}"
        pixel_html = f'<img src="{pixel_url}" width="1" height="1" style="display:none" alt="">'
        return pixel_html, tracking_id
    except Exception as e:
        logger.error(f"[Tracking] Failed to create pixel: {e}")
        return "", None


def _build_html_email(body: str, lead: dict, subject: str, sequence_stage: int, pixel_html: str) -> str:
    """Build HTML email using email-template.html with exact admin panel content.
    Supports <b>bold</b> tags in the body content."""
    import os, re
    from config import ROZPER, EMAIL_BODY, SENDER_EMAIL, SMTP_USER

    company_name = ROZPER.get("company_name", "FlowLock Overseas")
    contact_name = ROZPER.get("contact_name", "H. Khorajiya")
    phone = ROZPER.get("phone", "+91-9082717763")
    email_addr = (SENDER_EMAIL or SMTP_USER or ROZPER.get("contact_email") or "sales@flowlockoverseas.com")
    website = ROZPER.get("website", "https://www.flowlockoverseas.com")

    # Use the EXACT admin panel body content (not AI-generated)
    admin_body = getattr(__import__('config'), 'EMAIL_BODY', EMAIL_BODY)

    # Convert plain text body to styled HTML paragraphs
    # Support <b>text</b> tags — convert to <strong> with styling
    body_html = ""
    paragraphs = admin_body.strip().split("\n\n")  # Split by double newline = paragraphs
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # Convert <b>text</b> to styled <strong>
        para = re.sub(r'<b>(.*?)</b>', r'<strong style="color:#2c3e50;">\1</strong>', para)
        # Also convert **text** markdown style to bold
        para = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color:#2c3e50;">\1</strong>', para)
        # Replace single newlines within a paragraph with <br>
        para = para.replace("\n", "<br>")
        body_html += f'<p style="margin:0 0 14px 0;line-height:1.7;color:#4a4a4a;font-size:14px;font-family:\'Segoe UI\',Roboto,Arial,sans-serif;">{para}</p>\n'

    # Extract greeting (first line if it starts with Dear/Hi/Hello)
    first_para = paragraphs[0].strip() if paragraphs else ""
    greeting_html = ""
    if first_para.lower().startswith(("dear ", "hi ", "hello ")):
        greeting_html = f'<p style="margin:0 0 16px 0;font-size:15px;font-family:\'Segoe UI\',Roboto,Arial,sans-serif;color:#2c3e50;">{first_para}</p>'
        # Remove greeting from body_html (already shown separately)
        body_html = body_html.split("</p>", 1)[-1] if "</p>" in body_html else body_html

    # No CTA button
    cta_html = ""

    # Load template from file
    template_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "email-template.html")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read()
        # Remove the <script> preview block (not needed in actual emails)
        html = re.sub(r'<script>.*?</script>', '', html, flags=re.DOTALL)
    except:
        html = f"<html><body>{greeting_html}{body_html}</body></html>"
        return html

    # Replace template variables
    html = html.replace("{{company_name}}", company_name)
    html = html.replace("{{contact_name}}", contact_name)
    html = html.replace("{{phone}}", phone)
    html = html.replace("{{email}}", email_addr)
    html = html.replace("{{website}}", website.replace("https://", ""))
    html = html.replace("{{greeting}}", greeting_html)
    html = html.replace("{{body_content}}", body_html)
    html = html.replace("{{cta_button}}", cta_html)
    html = html.replace("{{pixel}}", pixel_html)

    return html


def _send_or_record(to_email: str, subject: str, body: str,
                    from_domain: str, lead: dict, sequence_stage: int = 1,
                    from_email_account: str = "", return_error: bool = False,
                    smtp_host_override: str = "", smtp_port_override: int = 0,
                    smtp_user_override: str = "", smtp_password_override: str = ""):
    """
    Send email via:
      1. Instantly.dev API (if API key is configured)
      2. Gmail SMTP (if SMTP credentials are configured)
      3. Just record to DB (if neither is set up)
    Returns delivery_status: 'sent', 'recorded', or 'failed'.
    """
    import config
    instantly_key = getattr(config, "INSTANTLY_API_KEY", INSTANTLY_API_KEY) or ""
    sender_email = (getattr(config, "SENDER_EMAIL", SENDER_EMAIL) or "").strip()
    smtp_host = (smtp_host_override or getattr(config, "SMTP_HOST", SMTP_HOST) or "").strip()
    smtp_port = int(smtp_port_override or getattr(config, "SMTP_PORT", SMTP_PORT) or 587)
    smtp_user = (smtp_user_override or getattr(config, "SMTP_USER", SMTP_USER) or "").strip()
    smtp_password = (smtp_password_override or getattr(config, "SMTP_PASSWORD", SMTP_PASSWORD) or "").strip()
    smtp_from_name = (getattr(config, "SMTP_FROM_NAME", SMTP_FROM_NAME) or "FlowLock Overseas").strip()

    if not (instantly_key and "your" not in instantly_key.lower()) and not (smtp_user and smtp_password and "your" not in smtp_password.lower()):
        if return_error:
            return "recorded", "no_sending_method_configured"
        return "recorded"

    pixel_html, tracking_id = _create_tracking_pixel(lead, to_email, subject, sequence_stage)
    html_body = _build_html_email(body, lead, subject, sequence_stage, pixel_html)

    # ── Option 1: Instantly.dev API ───────────────────
    if instantly_key and "your" not in instantly_key.lower():
        try:
            import httpx
            from_email = (from_email_account or sender_email or smtp_user).strip()
            payload = {
                "api_key": instantly_key,
                "email_account": from_email,
                "to": to_email,
                "subject": subject,
                "body": html_body,
            }
            resp = httpx.post(
                "https://api.instantly.ai/api/v1/unibox/emails/send",
                json=payload,
                timeout=30,
            )
            if resp.status_code in (200, 201):
                if return_error:
                    return "sent", ""
                return "sent"
            else:
                err = f"instantly_error_{resp.status_code}: {resp.text[:200]}"
                logger.error(f"[Email] {err}")
                if return_error:
                    return "failed", err
                return "failed"
        except Exception as e:
            err = f"instantly_send_error: {str(e)[:200]}"
            logger.error(f"[Email] {err}")
            if return_error:
                return "failed", err
            return "failed"

    # ── Option 2: SMTP (Gmail) ────────────────────────
    if smtp_user and smtp_password and "your" not in smtp_password.lower():
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            from email.utils import formatdate, make_msgid

            msg = MIMEMultipart("alternative")
            from_header = sender_email or smtp_user
            if from_email_account:
                from_header = from_email_account.strip()
            # Route replies to the authenticated mailbox so reply tracking works.
            reply_to = smtp_user or from_header
            msg["From"] = f"{smtp_from_name} <{from_header}>"
            msg["Reply-To"] = reply_to
            msg["To"] = to_email
            msg["Subject"] = subject
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid(domain=(from_header.split("@")[-1] if "@" in from_header else None))
            msg["X-Auto-Response-Suppress"] = "OOF, AutoReply"

            # Plain text version
            msg.attach(MIMEText(body, "plain", "utf-8"))
            # HTML version with tracking pixel
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            # Support both SSL (port 465) and TLS (port 587)
            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)

            # IMAP Append to Sent folder (so it shows up in Hostinger Webmail/Outlook)
            try:
                import imaplib, time
                imap_host = smtp_host.replace('smtp', 'imap')
                mail = imaplib.IMAP4_SSL(imap_host, 993)
                mail.login(smtp_user, smtp_password)
                status, folders = mail.list()
                sent_folder = 'INBOX.Sent'
                if status == 'OK':
                    for f in folders:
                        f_str = f.decode()
                        if 'sent' in f_str.lower():
                            sent_folder = f_str.split(' "/" ')[-1].strip('"')
                            break
                mail.append(sent_folder, '\\Seen', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
                mail.logout()
                logger.info(f'[Email] Appended message to IMAP Sent folder ({sent_folder})')
            except Exception as imap_e:
                logger.warning(f'[Email] Failed to append to IMAP Sent folder: {imap_e}')

            logger.info(f"[Email] SMTP sent → {to_email} via {smtp_user}")
            if return_error:
                return "sent", ""
            return "sent"

        except Exception as e:
            err = f"smtp_send_error: {str(e)[:200]}"
            logger.error(f"[Email] {err}")
            if return_error:
                return "failed", err
            return "failed"

    # ── Option 3: No sending method — just record ─────
    logger.info(f"[Email] No sending method configured — recording (not sending)")
    if return_error:
        return "recorded", "no_sending_method_configured"
    return "recorded"


# ═══════════════════════════════════════════════════════════════
# EMAIL WORKER — DB Polling with fetch-then-process
# ═══════════════════════════════════════════════════════════════

def email_worker_thread():
    """
    Background worker that polls DB for "New" leads and sends emails.
    Fetch-then-process pattern:
      1. Poll DB → fetch batch of N leads
      2. Process entire batch (no DB polls during processing)
      3. Cooldown → fetch next batch
    """
    global _workers_running
    logger.info("[Email Worker] Started — polling DB for new leads")

    while _workers_running:
        try:
            # ── FETCH: Poll DB for new leads ──────────────
            from modules.database import get_leads_for_email
            worker_status["email"]["status"] = "polling"
            leads = get_leads_for_email(limit=EMAIL_BATCH_SIZE)

            if not leads:
                worker_status["email"]["status"] = "cooldown"
                worker_status["email"]["last_action"] = "No new leads — cooling down"
                logger.debug("[Email Worker] No new leads — cooldown 60s")
                _sleep_interruptible(COOLDOWN_EMPTY)
                continue

            # ── PROCESS: Handle entire batch (no DB polls) ──
            worker_status["email"]["status"] = "processing"
            worker_status["email"]["batch_size"] = len(leads)
            logger.info(f"[Email Worker] Fetched {len(leads)} new leads — processing batch")

            processed = 0
            for i, lead in enumerate(leads, 1):
                if not _workers_running:
                    logger.info("[Email Worker] Stop signal received — halting batch")
                    break

                domain = lead.get("company_domain", "?")
                worker_status["email"]["last_action"] = f"Emailing {domain} ({i}/{len(leads)})"

                success = process_lead_email(lead, sequence_stage=1)
                if success:
                    processed += 1
                    worker_status["email"]["processed"] += 1

                # Human-like delay between sends (interruptible)
                if not _workers_running:
                    break
                _sleep_interruptible(random.uniform(*EMAIL_SEND_DELAY))

            logger.info(f"[Email Worker] Batch done: {processed}/{len(leads)} sent")
            worker_status["email"]["last_action"] = f"Batch done: {processed} sent"

            # ── COOLDOWN between batches ──────────────────
            _sleep_interruptible(COOLDOWN_BATCH)

        except Exception as e:
            logger.error(f"[Email Worker] Error: {e}")
            worker_status["email"]["status"] = "error"
            worker_status["email"]["last_action"] = f"Error: {str(e)[:50]}"
            _sleep_interruptible(30)

    worker_status["email"]["status"] = "stopped"
    logger.info("[Email Worker] Stopped")


# ═══════════════════════════════════════════════════════════════
# FOLLOWUP WORKER — DB Polling for due follow-ups
# ═══════════════════════════════════════════════════════════════

def followup_worker_thread():
    """
    Background worker that polls DB for leads due follow-up emails.
    Checks every 2 minutes.
    """
    global _workers_running
    logger.info("[Followup Worker] Started — polling for due follow-ups")

    while _workers_running:
        try:
            from modules.database import get_followup_due
            worker_status["followup"]["status"] = "polling"

            leads = get_followup_due()

            if not leads:
                worker_status["followup"]["status"] = "idle"
                worker_status["followup"]["last_action"] = "No follow-ups due"
                _sleep_interruptible(FOLLOWUP_CHECK_INTERVAL)
                continue

            worker_status["followup"]["status"] = "processing"
            logger.info(f"[Followup Worker] {len(leads)} follow-ups due — processing")

            sent = 0
            for lead in leads:
                if not _workers_running:
                    break

                domain = lead.get("company_domain", "?")
                current_stage = lead.get("sequence_stage", 1)
                next_stage = current_stage + 1

                if next_stage > 4:
                    continue

                worker_status["followup"]["last_action"] = f"Followup #{next_stage} → {domain}"

                success = process_lead_email(lead, sequence_stage=next_stage)
                if success:
                    sent += 1
                    worker_status["followup"]["processed"] += 1

                delay = random.uniform(*EMAIL_SEND_DELAY)
                time.sleep(delay)

            logger.info(f"[Followup Worker] Sent {sent} follow-ups")
            worker_status["followup"]["last_action"] = f"Sent {sent} follow-ups"

            _sleep_interruptible(FOLLOWUP_CHECK_INTERVAL)

        except Exception as e:
            logger.error(f"[Followup Worker] Error: {e}")
            worker_status["followup"]["status"] = "error"
            _sleep_interruptible(60)

    worker_status["followup"]["status"] = "stopped"
    logger.info("[Followup Worker] Stopped")


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _sleep_interruptible(seconds: float):
    """Sleep in 2-second chunks so we can stop quickly."""
    elapsed = 0
    while elapsed < seconds and _workers_running:
        time.sleep(min(2, seconds - elapsed))
        elapsed += 2


# ═══════════════════════════════════════════════════════════════
# START / STOP
# ═══════════════════════════════════════════════════════════════

def start_email_workers():
    """Start both email + followup worker threads."""
    global _workers_running
    _workers_running = True

    t1 = threading.Thread(target=email_worker_thread, daemon=True, name="email-worker")
    t2 = threading.Thread(target=followup_worker_thread, daemon=True, name="followup-worker")
    t1.start()
    t2.start()

    logger.info("[Workers] Email + Followup workers started")
    return t1, t2


def stop_email_workers():
    """Signal all workers to stop immediately."""
    global _workers_running
    _workers_running = False
    worker_status["email"]["status"] = "stopped"
    worker_status["followup"]["status"] = "stopped"
    worker_status["email"]["last_action"] = "Stopped by user"
    worker_status["followup"]["last_action"] = "Stopped by user"
    logger.info("[Workers] Stop signal sent — workers will halt within seconds")


def get_worker_status() -> dict:
    """Get status of all workers (for dashboard)."""
    return {
        "email_worker": worker_status["email"],
        "followup_worker": worker_status["followup"],
        "workers_running": _workers_running,
    }


def get_queue_size() -> int:
    """Get number of leads pending email (for dashboard)."""
    try:
        from modules.database import get_leads_for_email
        leads = get_leads_for_email(limit=1)
        return len(leads) if leads else 0
    except Exception:
        return 0
