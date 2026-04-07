"""
MicrodyneHunter v2 — Email Workers (DB-Polling)
Two independent background workers:
  1. Email Worker — polls DB for "New" leads, generates variants, sends
  2. Followup Worker — polls DB for due follow-ups, sends next in sequence

Uses fetch-then-process pattern: fetch batch → process all → cooldown → fetch again.
Zero DB polls during processing. Respects warmup limits.
"""

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
    no_instantly = not INSTANTLY_API_KEY or "your" in INSTANTLY_API_KEY.lower()
    has_smtp = SMTP_USER and SMTP_PASSWORD and "your" not in SMTP_PASSWORD.lower()
    return no_instantly and has_smtp
from modules.events import emit_log

logger = logging.getLogger("microdynehunter.email_queue")

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
    WHITELISTED_EMAILS = {"githubthe12@gmail.com", "aujale30@gmail.com"}
    if email.lower().strip() in WHITELISTED_EMAILS:
        return False
    domain = email.split("@")[-1].lower()
    return domain in JUNK_EMAIL_DOMAINS


# ═══════════════════════════════════════════════════════════════
# PROCESS A SINGLE LEAD (shared by email + followup workers)
# ═══════════════════════════════════════════════════════════════

def process_lead_email(lead: dict, sequence_stage: int = 1) -> bool:
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
    from modules.email_warmup import get_best_domain, record_send
    from modules.database import db, update_lead

    domain = lead.get("company_domain", "?")
    lead_id = lead.get("id")
    email = (lead.get("contact_email") or "").strip()

    # ── Validate ──────────────────────────────────────────
    if not email or not _is_valid_email(email):
        logger.info(f"[Email] Skip {domain}: invalid email '{email}'")
        return False

    if _is_junk_email(email):
        logger.info(f"[Email] Skip {domain}: personal email domain")
        return False

    try:
        # ── 1. Get sending domain ────────────────────────
        if _using_smtp():
            # SMTP mode: use the SMTP sender domain directly, skip warmup check
            sending_domain = SMTP_USER.split("@")[-1] if SMTP_USER else "gmail.com"
        else:
            # Instantly mode: check warmup capacity
            sending_domain = get_best_domain()
            if not sending_domain:
                logger.warning(f"[Email] All domains at capacity — skipping {domain}")
                return False

        emit_log(
            f"Generating email for {email} from {SENDER_EMAIL}",
            category="email",
            data={"type": "email_generating", "recipient": email, "sender": SENDER_EMAIL, "company": domain},
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
            f"Sending email to {email} via {SENDER_EMAIL}",
            category="email",
            data={"type": "email_sending", "recipient": email, "sender": SENDER_EMAIL},
        )

        # ── 4. Send or record ─────────────────────────────
        delivery_status = _send_or_record(
            to_email=email,
            subject=winner["subject"],
            body=winner["body"],
            from_domain=sending_domain,
            lead=lead,
            sequence_stage=sequence_stage,
        )

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

        # ── 6. Record send in warmup tracker (skip for SMTP) ──
        if not _using_smtp():
            record_send(sending_domain)

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

        # ── 8. Update lead record ─────────────────────────
        if lead_id:
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

        status = "sent" if delivery_status == "sent" else "recorded"
        logger.info(
            f"[Email] ✓ Stage {sequence_stage} {status} → {email} ({domain}) — "
            f"variant #{winner['variant_number']} ({winner.get('angle', '?')}) "
            f"score: {winner['score_total']}/100 via {sending_domain}"
        )
        return True

    except Exception as e:
        logger.error(f"[Email] Error processing {domain}: {e}")
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
    """Build a professional HTML email with Microdyne Engineering branding."""
    from config import ROZPER

    company_name = ROZPER.get("company_name", "Microdyne Engineering")
    contact_name = ROZPER.get("contact_name", "Shohail Maredia")
    phone = ROZPER.get("phone", "+91-9082121601")
    email_addr = ROZPER.get("contact_email", "sales@microdyneengineering.com")
    website = ROZPER.get("website", "https://www.microdyneengineering.com")

    # Split body into greeting + content + signature
    lines = body.strip().split("\n")
    greeting = ""
    signature_lines = []
    content_lines = []
    in_signature = False

    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("hi ") or stripped.lower().startswith("hello ") or stripped.lower().startswith("dear "):
            greeting = stripped
        elif stripped.lower() in ("best,", "best regards,", "regards,", "thanks,", "thank you,", "cheers,"):
            in_signature = True
            signature_lines.append(stripped)
        elif in_signature:
            signature_lines.append(stripped)
        elif stripped:
            content_lines.append(stripped)

    # Bold key terms in the content
    bold_terms = [
        "mechanical seals", "CNC precision components", "CNC machined components",
        "precision turned parts", "cartridge seals", "spring seals", "bellow seals",
        "pump seals", "agitator seals", "seal repair", "refurbishment",
        "SS316", "SS304", "Hastelloy", "Silicon Carbide", "Tungsten Carbide",
        "PTFE", "Viton", "API 682", "free consultation", "free sample",
        "Microdyne Engineering", "manufacturer-direct",
    ]

    body_html = ""
    for line in content_lines:
        styled_line = line
        for term in bold_terms:
            if term.lower() in styled_line.lower():
                import re
                pattern = re.compile(re.escape(term), re.IGNORECASE)
                styled_line = pattern.sub(f'<strong style="color:#2c3e50;">{term}</strong>', styled_line, count=1)
        body_html += f'<p style="margin:0 0 12px 0;line-height:1.7;color:#4a4a4a;font-size:14px;">{styled_line}</p>\n'

    greeting_html = f'<p style="margin:0 0 16px 0;font-size:14px;color:#2c3e50;">{greeting}</p>' if greeting else ''

    lead_company = (lead.get("company_name", "") or "").replace(" ", "%20")
    cta_html = ""
    if sequence_stage <= 2:
        cta_html = f'''<tr><td style="padding:0 36px 30px 36px;" align="center">
      <table cellpadding="0" cellspacing="0"><tr>
        <td style="background:linear-gradient(135deg,#3498db,#217dbb);border-radius:8px;box-shadow:0 4px 12px rgba(52,152,219,0.3);">
          <a href="mailto:{email_addr}?subject=Inquiry%20from%20{lead_company}" style="display:inline-block;padding:14px 36px;color:#ffffff;text-decoration:none;font-family:'Segoe UI',Roboto,Arial,sans-serif;font-size:15px;font-weight:700;letter-spacing:0.5px;">Request Free Consultation</a>
        </td>
      </tr></table>
      <p style="margin:10px 0 0 0;font-size:11px;color:#95a5a6;font-family:'Segoe UI',Roboto,Arial,sans-serif;">or call directly: <a href="tel:+919082121601" style="color:#3498db;text-decoration:none;font-weight:600;">+91 9082121601</a></p>
    </td></tr>'''

    html = f'''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<!--[if mso]><style>body,table,td{{font-family:Segoe UI,Arial,sans-serif !important;}}</style><![endif]-->
</head>
<body style="margin:0;padding:0;background:#f0f4f8;-webkit-font-smoothing:antialiased;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;padding:28px 0;">
<tr><td align="center">
<table role="presentation" width="620" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.06);border:1px solid #e8eef3;">

  <!-- Header Bar -->
  <tr>
    <td style="background:linear-gradient(135deg,#3498db 0%,#2176ad 100%);padding:22px 36px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td>
            <div style="font-family:'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;font-size:20px;font-weight:800;color:#ffffff;letter-spacing:0.3px;">{company_name}</div>
            <div style="font-family:'Segoe UI',Roboto,Arial,sans-serif;font-size:11px;color:rgba(255,255,255,0.75);margin-top:3px;letter-spacing:0.8px;text-transform:uppercase;">Mechanical Seals &bull; CNC Components &bull; Precision Parts</div>
          </td>
          <td align="right" valign="middle">
            <a href="tel:+919082121601" style="display:inline-block;padding:8px 18px;background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.3);border-radius:6px;color:#ffffff;text-decoration:none;font-family:'Segoe UI',Roboto,Arial,sans-serif;font-size:12px;font-weight:600;">&#9742; +91 9082121601</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Email Body -->
  <tr>
    <td style="padding:36px 36px 24px 36px;font-family:'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
      {greeting_html.replace('font-size:14px', "font-size:15px;font-family:'Segoe UI',Roboto,Arial,sans-serif")}
      {body_html.replace('font-size:14px', "font-size:14px;font-family:'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif")}
    </td>
  </tr>

  <!-- CTA Button -->
  {cta_html}

  <!-- Signature Block -->
  <tr>
    <td style="padding:0 36px 28px 36px;">
      <table width="100%" cellpadding="0" cellspacing="0" style="border-top:2px solid #3498db;padding-top:20px;">
        <tr>
          <td width="50%" valign="top">
            <div style="font-family:'Segoe UI',Roboto,Arial,sans-serif;font-size:16px;font-weight:800;color:#2c3e50;letter-spacing:0.2px;">{contact_name}</div>
            <div style="font-family:'Segoe UI',Roboto,Arial,sans-serif;font-size:12px;color:#3498db;margin-top:3px;font-weight:600;">{company_name}</div>
          </td>
          <td width="50%" valign="top" align="right">
            <table cellpadding="0" cellspacing="0">
              <tr>
                <td style="font-family:'Segoe UI',Roboto,Arial,sans-serif;font-size:13px;color:#2c3e50;line-height:2;">
                  <a href="tel:+919082121601" style="color:#2c3e50;text-decoration:none;font-weight:600;">&#9742;&nbsp; +91 9082121601</a><br>
                  <a href="mailto:{email_addr}" style="color:#3498db;text-decoration:none;">&#9993;&nbsp; {email_addr}</a><br>
                  <a href="{website}" style="color:#3498db;text-decoration:none;">&#127760;&nbsp; {website.replace("https://","")}</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:#f8fafb;padding:20px 36px;border-top:1px solid #edf1f5;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="font-family:'Segoe UI',Roboto,Arial,sans-serif;">
            <div style="font-size:12px;font-weight:700;color:#2c3e50;margin-bottom:4px;">{company_name}</div>
            <div style="font-size:11px;color:#7f8c8d;line-height:1.7;">
              Manufacturer of <strong style="color:#5a6c7d;">Mechanical Seals</strong>, <strong style="color:#5a6c7d;">CNC Components</strong> &amp; <strong style="color:#5a6c7d;">Precision Turned Parts</strong><br>
              Mumbai, Maharashtra, India &nbsp;&bull;&nbsp; GST: 27BWWPM3354A1ZC &nbsp;&bull;&nbsp; <a href="tel:+919082121601" style="color:#3498db;text-decoration:none;font-weight:600;">+91 9082121601</a>
            </div>
          </td>
        </tr>
        <tr>
          <td style="padding-top:12px;">
            <table cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding:4px 10px;background:#eaf3fb;border-radius:4px;font-size:10px;font-family:'Segoe UI',Roboto,Arial,sans-serif;color:#3498db;font-weight:600;letter-spacing:0.3px;">Cartridge Seals</td>
                <td width="6"></td>
                <td style="padding:4px 10px;background:#eaf3fb;border-radius:4px;font-size:10px;font-family:'Segoe UI',Roboto,Arial,sans-serif;color:#3498db;font-weight:600;letter-spacing:0.3px;">Spring Seals</td>
                <td width="6"></td>
                <td style="padding:4px 10px;background:#eaf3fb;border-radius:4px;font-size:10px;font-family:'Segoe UI',Roboto,Arial,sans-serif;color:#3498db;font-weight:600;letter-spacing:0.3px;">Bellow Seals</td>
                <td width="6"></td>
                <td style="padding:4px 10px;background:#eaf3fb;border-radius:4px;font-size:10px;font-family:'Segoe UI',Roboto,Arial,sans-serif;color:#3498db;font-weight:600;letter-spacing:0.3px;">Pump Seals</td>
                <td width="6"></td>
                <td style="padding:4px 10px;background:#eaf3fb;border-radius:4px;font-size:10px;font-family:'Segoe UI',Roboto,Arial,sans-serif;color:#3498db;font-weight:600;letter-spacing:0.3px;">CNC Parts</td>
                <td width="6"></td>
                <td style="padding:4px 10px;background:#eaf3fb;border-radius:4px;font-size:10px;font-family:'Segoe UI',Roboto,Arial,sans-serif;color:#3498db;font-weight:600;letter-spacing:0.3px;">Seal Repair</td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>

</table>
{pixel_html}
</td></tr>
</table>
</body>
</html>'''

    return html


def _send_or_record(to_email: str, subject: str, body: str,
                    from_domain: str, lead: dict, sequence_stage: int = 1) -> str:
    """
    Send email via:
      1. Instantly.dev API (if API key is configured)
      2. Gmail SMTP (if SMTP credentials are configured)
      3. Just record to DB (if neither is set up)
    Returns delivery_status: 'sent' or 'recorded'.
    """
    # Create tracking pixel
    pixel_html, tracking_id = _create_tracking_pixel(lead, to_email, subject, sequence_stage)

    # Build professional HTML email from plain text body
    html_body = _build_html_email(body, lead, subject, sequence_stage, pixel_html)

    # ── Option 1: Instantly.dev API ───────────────────
    if INSTANTLY_API_KEY and "your" not in INSTANTLY_API_KEY.lower():
        try:
            import httpx
            from_email = SENDER_EMAIL
            payload = {
                "api_key": INSTANTLY_API_KEY,
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
                return "sent"
            else:
                logger.error(f"[Email] Instantly error {resp.status_code}: {resp.text[:200]}")
                return "failed"
        except Exception as e:
            logger.error(f"[Email] Instantly send error: {e}")
            return "failed"

    # ── Option 2: SMTP (Gmail) ────────────────────────
    if SMTP_USER and SMTP_PASSWORD and "your" not in SMTP_PASSWORD.lower():
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            msg = MIMEMultipart("alternative")
            msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_USER}>"
            msg["To"] = to_email
            msg["Subject"] = subject

            # Plain text version
            msg.attach(MIMEText(body, "plain", "utf-8"))
            # HTML version with tracking pixel
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)

            logger.info(f"[Email] SMTP sent → {to_email} via {SMTP_USER}")
            return "sent"

        except Exception as e:
            logger.error(f"[Email] SMTP send error: {e}")
            return "failed"

    # ── Option 3: No sending method — just record ─────
    logger.info(f"[Email] No sending method configured — recording (not sending)")
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
