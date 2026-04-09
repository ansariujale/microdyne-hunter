"""
FlowLockHunter v2 — Notification Module
Sends alerts for hot leads and weekly reports.
"""

import logging
from typing import Optional

import httpx

from config import INSTANTLY_API_KEY, NOTIFICATION_EMAIL, ROZPER

logger = logging.getLogger("flowlockhunter.notifier")


def send_hot_lead_alert(lead: dict) -> bool:
    """
    Send an instant notification when a lead replies/shows interest.
    Uses Instantly's email API to send notification.
    """
    subject = f"🔥 HOT LEAD: {lead.get('company_name', 'Unknown')} ({lead.get('country', '')})"

    body = f"""<h2>New Hot Lead — Reply Received!</h2>
<table style="border-collapse:collapse;font-family:sans-serif;">
<tr><td style="padding:6px 12px;font-weight:bold;">Company:</td><td style="padding:6px 12px;">{lead.get('company_name', 'N/A')}</td></tr>
<tr><td style="padding:6px 12px;font-weight:bold;">Domain:</td><td style="padding:6px 12px;">{lead.get('company_domain', 'N/A')}</td></tr>
<tr><td style="padding:6px 12px;font-weight:bold;">Contact:</td><td style="padding:6px 12px;">{lead.get('contact_name', 'N/A')} ({lead.get('contact_title', '')})</td></tr>
<tr><td style="padding:6px 12px;font-weight:bold;">Email:</td><td style="padding:6px 12px;">{lead.get('contact_email', 'N/A')}</td></tr>
<tr><td style="padding:6px 12px;font-weight:bold;">Phone:</td><td style="padding:6px 12px;">{lead.get('contact_phone', 'N/A')}</td></tr>
<tr><td style="padding:6px 12px;font-weight:bold;">Country:</td><td style="padding:6px 12px;">{lead.get('country', 'N/A')}</td></tr>
<tr><td style="padding:6px 12px;font-weight:bold;">Type:</td><td style="padding:6px 12px;">{lead.get('lead_type', 'N/A')}</td></tr>
<tr><td style="padding:6px 12px;font-weight:bold;">Score:</td><td style="padding:6px 12px;">{lead.get('score', 'N/A')}/100</td></tr>
<tr><td style="padding:6px 12px;font-weight:bold;">Channel:</td><td style="padding:6px 12px;">{lead.get('reply_channel', 'N/A')}</td></tr>
<tr><td style="padding:6px 12px;font-weight:bold;">Website:</td><td style="padding:6px 12px;"><a href="{lead.get('website_url', '#')}">{lead.get('website_url', 'N/A')}</a></td></tr>
</table>
<br>
<p><strong>Action needed:</strong> Follow up with this lead ASAP to close the deal.</p>
<p style="color:#888;font-size:12px;">— FlowLockHunter v2 Agent</p>"""

    return _send_notification(subject, body)


def send_weekly_report_email(report_text: str) -> bool:
    """Send the weekly intelligence report via email."""
    subject = "📊 FlowLockHunter Weekly Report"
    body = f"<pre style='font-family:monospace;font-size:13px;line-height:1.6;'>{report_text}</pre>"
    return _send_notification(subject, body)


def send_daily_summary(stats: dict) -> bool:
    """Send a brief daily summary."""
    subject = f"📈 Daily: {stats.get('leads_added', 0)} leads, {stats.get('emails_sent', 0)} emails, {stats.get('forms_filled', 0)} forms"
    body = f"""<h3>FlowLockHunter Daily Summary</h3>
<ul>
<li>Leads scraped: <strong>{stats.get('leads_added', 0)}</strong></li>
<li>Emails sent: <strong>{stats.get('emails_sent', 0)}</strong></li>
<li>Forms filled: <strong>{stats.get('forms_filled', 0)}</strong></li>
<li>Follow-ups sent: <strong>{stats.get('followups_sent', 0)}</strong></li>
</ul>
<p style="color:#888;font-size:12px;">— FlowLockHunter v2 Agent</p>"""
    return _send_notification(subject, body)


def _send_notification(subject: str, body: str) -> bool:
    """Send a notification email using Instantly API."""
    if not INSTANTLY_API_KEY:
        logger.warning("Instantly API key not set — cannot send notification")
        logger.info(f"NOTIFICATION: {subject}")
        return False

    try:
        url = "https://api.instantly.ai/api/v1/unibox/emails/send"
        payload = {
            "api_key": INSTANTLY_API_KEY,
            "from_email": ROZPER["contact_email"],
            "to_email": NOTIFICATION_EMAIL,
            "subject": subject,
            "body": body,
        }
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        logger.info(f"Notification sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"Notification send error: {e}")
        # Fallback: just log it
        logger.info(f"NOTIFICATION (fallback log): {subject}")
        return False
