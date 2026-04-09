"""Quick test: send a test email via Gmail SMTP with tracking pixel."""
import os
import sys
import uuid
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Shohail Maredia")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")

# Send to yourself as a test
TO_EMAIL = SMTP_USER  # sends to yourself

tracking_id = str(uuid.uuid4())
pixel_url = f"{SUPABASE_URL}/functions/v1/track-email?id={tracking_id}"

subject = "FlowLockHunter Test - Email + Tracking Pixel"
body_text = "This is a test email from FlowLockHunter v2.\n\nIf you can read this, SMTP sending works!\n\nTracking pixel is embedded in the HTML version."
body_html = f"""
<div style="font-family:Arial,sans-serif;font-size:14px;color:#333;">
  <p>This is a <strong>test email</strong> from FlowLockHunter v2.</p>
  <p>If you can read this, SMTP sending works!</p>
  <p style="color:#888;font-size:12px;">Tracking ID: {tracking_id}</p>
  <p style="color:#888;font-size:12px;">When you open this email, the tracking pixel fires to:<br>
  <code>{pixel_url}</code></p>
  <img src="{pixel_url}" width="1" height="1" style="display:none" alt="">
</div>
"""

print(f"SMTP Host: {SMTP_HOST}:{SMTP_PORT}")
print(f"SMTP User: {SMTP_USER}")
print(f"From Name: {SMTP_FROM_NAME}")
print(f"Sending to: {TO_EMAIL}")
print(f"Tracking ID: {tracking_id}")
print()

if not SMTP_USER or not SMTP_PASSWORD or "your" in SMTP_PASSWORD.lower():
    print("ERROR: SMTP_PASSWORD not set in .env")
    sys.exit(1)

try:
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_USER}>"
    msg["To"] = TO_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    print("Connecting to SMTP server...")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        print("TLS established. Logging in...")
        server.login(SMTP_USER, SMTP_PASSWORD)
        print("Login successful. Sending email...")
        server.send_message(msg)
        print()
        print("=" * 50)
        print("SUCCESS! Email sent to", TO_EMAIL)
        print("=" * 50)
        print()
        print("Now check your Gmail inbox for the test email.")
        print("Open it — the tracking pixel will fire to Supabase.")
        print(f"Then check: {SUPABASE_URL}/rest/v1/email_tracking")

except Exception as e:
    print(f"FAILED: {e}")
    sys.exit(1)
