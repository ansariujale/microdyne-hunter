"""
MicrodyneHunter v2 — Email Outreach Module
Sends personalized cold emails via Instantly.dev API.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from config import (
    INSTANTLY_API_KEY, ANTHROPIC_API_KEY, ROZPER,
    SENDING_DOMAINS, EMAILS_PER_DOMAIN, FOLLOWUP_SCHEDULE,
)
from modules.database import update_lead, log_outreach

logger = logging.getLogger("microdynehunter.emailer")

ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
http = httpx.Client(timeout=30)

INSTANTLY_BASE = "https://api.instantly.ai/api/v1"


# ═══════════════════════════════════════════════════════════════
# EMAIL GENERATION WITH AI
# ═══════════════════════════════════════════════════════════════

EMAIL_TEMPLATES = {
    "initial": {
        "subject": "Mechanical seals & CNC parts for {region} — free consultation for {company}",
        "prompt": """Write a short, personalized cold email (max 100 words) from {sender_name} at Microdyne Engineering to {contact_name} at {company_name}.

Microdyne Engineering manufactures mechanical seals and CNC precision components for industrial applications.
The lead is a {lead_type} in {country}.
Offer: Free consultation and sample to demonstrate quality.

Tone: Professional but conversational. No fluff. Direct value proposition.
End with a clear CTA asking if they'd like a free consultation or sample for their equipment.

Return ONLY the email body (no subject line, no greeting "Hi Name" — that's added automatically).""",
    },
    "quality": {
        "subject": "Precision-engineered seals for {region} — Microdyne Engineering",
        "prompt": """Write a follow-up email #2 (max 80 words) from {sender_name} at Microdyne Engineering.
This is the SECOND email to {contact_name} at {company_name}, a {lead_type} in {country}.
They didn't reply to the first email about free consultation and sample.

Angle: Quality — mention API 682 compliance, tight tolerances, premium materials, extended MTBF.
Don't repeat the first email. Add new value.
Tone: Helpful, not pushy.

Return ONLY the email body.""",
    },
    "social_proof": {
        "subject": "Serving top plants across {region} — Microdyne Engineering",
        "prompt": """Write follow-up email #3 (max 80 words) from {sender_name} at Microdyne Engineering.
Third email to {contact_name} at {company_name}, a {lead_type} in {country}.
No reply to 2 previous emails.

Angle: Social proof — mention Microdyne Engineering serves major chemical plants, refineries, and pharma facilities across India and globally.
Make them feel they're missing out on a reliable manufacturing partner.
Tone: Confident but not arrogant.

Return ONLY the email body.""",
    },
    "breakup": {
        "subject": "Last note from Microdyne Engineering — offer stays open, {contact_name}",
        "prompt": """Write a final breakup email #4 (max 60 words) from {sender_name} at Microdyne Engineering.
Fourth and last email to {contact_name} at {company_name}.
No reply to 3 previous emails.

Angle: Breakup — no pressure, door stays open, wish them well.
Short and graceful. Make them feel respected, not spammed.

Return ONLY the email body.""",
    },
}


def generate_email(lead: dict, stage: str) -> dict:
    """Generate a personalized email using AI or templates."""
    template = EMAIL_TEMPLATES.get(stage, EMAIL_TEMPLATES["initial"])
    region = lead.get("country", "your region")
    company = lead.get("company_name", "your company")
    contact = lead.get("contact_name", "there")

    subject = template["subject"].format(
        region=region, company=company, contact_name=contact,
    )

    if ai_client:
        try:
            prompt = template["prompt"].format(
                sender_name=ROZPER["contact_name"],
                contact_name=contact,
                company_name=company,
                lead_type=lead.get("lead_type", "industrial company"),
                country=region,
            )
            response = ai_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                messages=[{"role": "user", "content": prompt}],
            )
            body = response.content[0].text.strip()
        except Exception as e:
            logger.error(f"AI email gen error: {e}")
            body = _fallback_body(lead, stage)
    else:
        body = _fallback_body(lead, stage)

    # Add greeting
    first_name = contact.split()[0] if contact and contact != "there" else "there"
    full_body = f"Hi {first_name},\n\n{body}\n\nBest,\n{ROZPER['contact_name']}\n{ROZPER['company_name']}\n{ROZPER['website']}"

    return {"subject": subject, "body": full_body}


def _fallback_body(lead: dict, stage: str) -> str:
    """Fallback email body when AI is unavailable."""
    country = lead.get("country", "your region")
    bodies = {
        "initial": (
            f"We manufacture mechanical seals and CNC precision components for industrial "
            f"applications across {country} and globally.\n\n"
            f"Would you be open to a free consultation to discuss your sealing requirements? "
            f"No commitment — just share your equipment details and we'll recommend the right solution."
        ),
        "quality": (
            f"Quick follow-up — Microdyne Engineering maintains API 682 compliance and tight "
            f"tolerances on all our mechanical seals and CNC components.\n\n"
            f"Happy to send a sample if you'd like to verify our quality. "
            f"What are your main equipment types?"
        ),
        "social_proof": (
            f"Microdyne Engineering currently serves major chemical plants, refineries, "
            f"and pharmaceutical facilities across India and globally.\n\n"
            f"We'd love to add {lead.get('company_name', 'your company')} to our client network. "
            f"Free consultation and sample available — interested?"
        ),
        "breakup": (
            f"This is my last note — I understand timing might not be right. "
            f"If you ever need mechanical seals or precision components, the offer for a free consultation and sample stays open.\n\n"
            f"Wishing you all the best."
        ),
    }
    return bodies.get(stage, bodies["initial"])


# ═══════════════════════════════════════════════════════════════
# INSTANTLY.DEV API INTEGRATION
# ═══════════════════════════════════════════════════════════════

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def add_lead_to_instantly(email: str, first_name: str, last_name: str,
                          company: str, campaign_id: str) -> bool:
    """Add a lead to an Instantly campaign."""
    url = f"{INSTANTLY_BASE}/lead/add"
    payload = {
        "api_key": INSTANTLY_API_KEY,
        "campaign_id": campaign_id,
        "skip_if_in_workspace": True,
        "leads": [{
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "company_name": company,
        }],
    }
    try:
        resp = http.post(url, json=payload)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Instantly add lead error: {e}")
        return False


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def send_email_via_instantly(to_email: str, subject: str, body: str,
                             from_email: str, campaign_id: str = None) -> bool:
    """Send a single email through Instantly."""
    url = f"{INSTANTLY_BASE}/unibox/emails/send"
    payload = {
        "api_key": INSTANTLY_API_KEY,
        "from_email": from_email,
        "to_email": to_email,
        "subject": subject,
        "body": body.replace("\n", "<br>"),
    }
    if campaign_id:
        payload["campaign_id"] = campaign_id

    try:
        resp = http.post(url, json=payload)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Instantly send error to {to_email}: {e}")
        return False


def get_instantly_campaigns() -> list[dict]:
    """List all Instantly campaigns."""
    url = f"{INSTANTLY_BASE}/campaign/list"
    params = {"api_key": INSTANTLY_API_KEY}
    try:
        resp = http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Instantly list campaigns error: {e}")
        return []


def get_instantly_analytics(campaign_id: str) -> dict:
    """Get analytics for an Instantly campaign."""
    url = f"{INSTANTLY_BASE}/analytics/campaign/summary"
    params = {"api_key": INSTANTLY_API_KEY, "campaign_id": campaign_id}
    try:
        resp = http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Instantly analytics error: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════
# DOMAIN ROTATION
# ═══════════════════════════════════════════════════════════════

class DomainRotator:
    """Rotate sending domains to stay within safe limits."""

    def __init__(self):
        self.domain_counts: dict[str, int] = {d: 0 for d in SENDING_DOMAINS}

    def get_next_domain(self) -> Optional[str]:
        """Get the next available sending domain (least used today)."""
        available = {
            d: c for d, c in self.domain_counts.items()
            if c < EMAILS_PER_DOMAIN
        }
        if not available:
            return None
        return min(available, key=available.get)

    def record_send(self, domain: str):
        """Record that an email was sent from this domain."""
        self.domain_counts[domain] = self.domain_counts.get(domain, 0) + 1

    def get_from_email(self, domain: str) -> str:
        """Get the from email address for a domain."""
        return f"info@{domain}"

    def remaining_capacity(self) -> int:
        """How many more emails can be sent today across all domains."""
        return sum(EMAILS_PER_DOMAIN - c for c in self.domain_counts.values())


# ═══════════════════════════════════════════════════════════════
# BATCH EMAIL SENDING
# ═══════════════════════════════════════════════════════════════

def send_initial_emails(leads: list[dict], campaign_id: str = None) -> int:
    """
    Send initial cold emails to a batch of leads using the variant engine.
    Generates 5 variants per lead, scores them, sends the winner.
    Returns count of emails sent/recorded.
    """
    from modules.email_queue import process_lead_email
    sent = 0

    for lead in leads:
        email = lead.get("contact_email")
        if not email:
            continue

        success = process_lead_email(lead)
        if success:
            sent += 1

        if sent % 50 == 0 and sent > 0:
            logger.info(f"Sent {sent}/{len(leads)} initial emails")

    logger.info(f"Initial email batch complete: {sent} sent/recorded")
    return sent


def send_followup_emails(leads: list[dict], campaign_id: str = None) -> int:
    """
    Send follow-up emails using the variant engine.
    Returns count of follow-ups sent/recorded.
    """
    from modules.email_queue import process_followups
    return process_followups()
