"""
Send emails to 10 leads via Gmail SMTP with tracking pixels.
Uses the full pipeline: generate email -> create tracking pixel -> send via SMTP -> update DB.
"""
import os
import sys
import uuid
import time
import random
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from dotenv import load_dotenv
import httpx

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Shohail Maredia")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

def db_select(table, params=""):
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{table}?{params}", headers=HEADERS, timeout=15)
    return r.json() if r.status_code == 200 else []

def db_insert(table, data):
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, json=data, timeout=15)
    return r.json() if r.status_code in (200, 201) else None

def db_update(table, match_col, match_val, data):
    h = {**HEADERS, "Prefer": "return=representation"}
    r = httpx.patch(f"{SUPABASE_URL}/rest/v1/{table}?{match_col}=eq.{match_val}", headers=h, json=data, timeout=15)
    return r.status_code in (200, 204)


def generate_email(lead):
    """Generate a simple cold email for the lead."""
    company = lead.get("company_name", "your company")
    name = lead.get("contact_name") or "there"
    country = lead.get("country", "")

    subject = f"Mechanical Seals & CNC Components for {company}"
    body = f"""Hi {name},

I came across {company} and noticed you're in the industrial/manufacturing space{' in ' + country if country else ''}.

We are FlowLock Overseas, a Mumbai-based manufacturer of precision mechanical seals and CNC machined components. We offer high-quality cartridge seals, spring seals, and bellow seals in SS316, Hastelloy, and Silicon Carbide.

Would you be open to a free consultation or sample to evaluate our quality?

Best regards,
Shohail Maredia
FlowLock Overseas
https://www.flowlockoverseas.com"""

    return subject, body


def send_email(to_email, subject, body_text, body_html):
    """Send via Gmail SMTP. Returns 'sent' or 'failed'."""
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_USER}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return "sent"
    except Exception as e:
        print(f"  SMTP ERROR: {e}")
        return "failed"


def process_lead(lead, index, total):
    """Full pipeline for one lead."""
    lead_id = lead["id"]
    email = lead.get("contact_email", "").strip()
    company = lead.get("company_name", "?")
    domain = lead.get("company_domain", "?")

    print(f"\n[{index}/{total}] {company} ({domain})")
    print(f"  To: {email}")

    # Skip bad emails
    if not email or "@" not in email or email == "example@mysite.com":
        print("  SKIP: invalid email")
        return False

    junk_domains = {"gmail.com","yahoo.com","hotmail.com","outlook.com","example.com"}
    if email.split("@")[-1].lower() in junk_domains:
        print("  SKIP: junk email domain")
        return False

    # 1. Generate email
    subject, body_text = generate_email(lead)
    print(f"  Subject: {subject}")

    # 2. Create tracking pixel
    tracking_id = str(uuid.uuid4())
    db_insert("email_tracking", {
        "tracking_id": tracking_id,
        "lead_id": lead_id,
        "sequence_stage": 1,
        "recipient_email": email,
        "subject": subject,
    })
    pixel_url = f"{SUPABASE_URL}/functions/v1/track-email?id={tracking_id}"
    body_html = body_text.replace("\n", "<br>")
    body_html += f'<img src="{pixel_url}" width="1" height="1" style="display:none" alt="">'
    print(f"  Tracking ID: {tracking_id[:8]}...")

    # 3. Send via SMTP
    status = send_email(email, subject, body_text, body_html)
    print(f"  Status: {status.upper()}")

    if status == "failed":
        return False

    # 4. Log outreach
    db_insert("outreach_log", {
        "lead_id": lead_id,
        "channel": "email",
        "sequence_stage": 1,
        "subject": subject,
        "sending_domain": SMTP_USER.split("@")[-1] if SMTP_USER else "gmail.com",
        "delivery_status": status,
    })

    # 5. Update lead
    now = datetime.now(timezone.utc).isoformat()
    db_update("leads", "id", lead_id, {
        "email_sent": True,
        "email_sent_at": now,
        "sequence_stage": 1,
        "sending_domain": SMTP_USER.split("@")[-1] if SMTP_USER else "gmail.com",
    })

    print(f"  DB updated: email_sent=true, sequence_stage=1")
    return True


def main():
    print("=" * 60)
    print("FlowLockHunter v2 — Send 10 Test Emails")
    print(f"From: {SMTP_FROM_NAME} <{SMTP_USER}>")
    print("=" * 60)

    if not SMTP_USER or not SMTP_PASSWORD or "your" in SMTP_PASSWORD.lower():
        print("ERROR: SMTP credentials not set in .env")
        sys.exit(1)

    # Fetch 10 unsent leads
    leads = db_select("leads",
        "email_sent=eq.false&contact_email=not.is.null&score=gte.40&order=score.desc&limit=10"
    )

    if not leads:
        print("No leads available to email!")
        sys.exit(0)

    print(f"\nFound {len(leads)} leads to process\n")

    sent = 0
    failed = 0
    skipped = 0

    for i, lead in enumerate(leads, 1):
        result = process_lead(lead, i, len(leads))
        if result:
            sent += 1
        elif result is False:
            if lead.get("contact_email", "") in ("", "example@mysite.com"):
                skipped += 1
            else:
                failed += 1

        # Delay between sends (2-5 seconds)
        if i < len(leads):
            delay = random.uniform(2, 5)
            print(f"  Waiting {delay:.1f}s...")
            time.sleep(delay)

    print("\n" + "=" * 60)
    print(f"DONE: {sent} sent, {failed} failed, {skipped} skipped")
    print("=" * 60)


if __name__ == "__main__":
    main()
