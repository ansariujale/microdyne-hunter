"""
MicrodyneHunter v2 — Lead Enrichment Module
Fetches company website HTML → extracts emails & phone numbers via regex.
Ported from the user's n8n JS extraction logic.
"""

import re
import json
import html
import logging
from typing import Optional
from urllib.parse import urljoin

import httpx

from config import REQUEST_TIMEOUT

logger = logging.getLogger("microdynehunter.enricher")

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Junk email domains to filter out
JUNK_EMAIL_DOMAINS = {
    "wixpress.com", "sentry.io", "codefusion.com", "example.com",
    "sentry-next.wixpress.com", "gravatar.com", "schema.org",
    "wordpress.org", "w3.org",
}

# File extensions that look like emails but aren't
JUNK_EMAIL_EXTENSIONS = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|ico|pdf|css|js)$", re.IGNORECASE
)

# Hex-hash local parts (tracking pixels etc.)
HEX_HASH_LOCAL = re.compile(r"^[a-f0-9]{8,}$", re.IGNORECASE)

# Repeated-digit garbage phone numbers
GARBAGE_PHONE = re.compile(
    r"^(0+|1{5,}|2{5,}|3{5,}|4{5,}|5{5,}|6{5,}|7{5,}|8{5,}|9{5,})$"
)


def _decode_html_entities(text: str) -> str:
    """Decode &#NNN; numeric entities and standard HTML entities."""
    return html.unescape(text)


def _uniq_case(items: list[str]) -> list[str]:
    """Deduplicate strings case-insensitively, preserving first occurrence."""
    seen = set()
    out = []
    for v in items:
        v = v.strip()
        if not v:
            continue
        k = v.lower()
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _normalize_phone(p: str) -> Optional[str]:
    """Normalize a phone string → digits only, 10-15 digits, or None."""
    if not p:
        return None
    s = re.sub(r"^tel:", "", p, flags=re.IGNORECASE)
    s = re.sub(r"&nbsp;", " ", s, flags=re.IGNORECASE)
    s = s.replace("(0)", "")  # remove optional (0)
    s = re.sub(r"\s+", " ", s).strip()
    # Keep digits only
    digits = re.sub(r"[^\d]", "", s)
    # Convert 00 prefix
    if digits.startswith("00"):
        digits = digits[2:]
    # Basic validity: 10–15 digits
    if len(digits) < 10 or len(digits) > 15:
        return None
    # Reject garbage repeated digits
    if GARBAGE_PHONE.match(digits):
        return None
    return digits


def _is_junk_email(email: str) -> bool:
    """Return True if email looks like junk/tracking/asset."""
    lower = email.lower()
    # File extension emails
    if JUNK_EMAIL_EXTENSIONS.search(lower):
        return True
    # Known junk domains
    domain = lower.split("@")[-1] if "@" in lower else ""
    if domain in JUNK_EMAIL_DOMAINS:
        return True
    # Hex-hash local part
    local = lower.split("@")[0] if "@" in lower else ""
    if HEX_HASH_LOCAL.match(local):
        return True
    return False


def _to_absolute_url(href: str, base_url: str) -> Optional[str]:
    """Convert a relative href to absolute URL."""
    if not href:
        return None
    if re.match(r"^\s*(javascript:|#)", href, re.IGNORECASE):
        return None
    try:
        return urljoin(base_url, href)
    except Exception:
        return href or None


# ═══════════════════════════════════════════════════════════════
# EXTRACTION FROM HTML
# ═══════════════════════════════════════════════════════════════

def extract_contacts_from_html(raw_html: str, base_url: str = "") -> dict:
    """
    Extract emails, phones, contact links, and social links from HTML.
    This is a direct port of the user's JS n8n extraction code.

    Returns:
        {
            "emails": ["info@company.com", ...],
            "phone_numbers": ["971441234567", ...],
            "contact_page_links": "https://example.com/contact, ...",
            "social_links": "https://linkedin.com/company/..., ...",
        }
    """
    decoded = _decode_html_entities(raw_html)

    # ── 1) Phones from tel: links ──────────────────────────────
    tel_links = re.findall(r'href=["\']tel:([^"\']+)["\']', decoded, re.IGNORECASE)

    # ── 2) Phones & emails from JSON-LD ────────────────────────
    ld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
        decoded, re.IGNORECASE,
    )

    schema_phones = []
    schema_emails = []

    def walk_json_ld(node):
        """Recursively walk JSON-LD to find telephone & email fields."""
        if node is None:
            return
        if isinstance(node, list):
            for item in node:
                walk_json_ld(item)
            return
        if isinstance(node, dict):
            for k, v in node.items():
                key = k.lower()
                if key == "telephone" and isinstance(v, str):
                    schema_phones.append(v)
                if key == "email" and isinstance(v, str):
                    schema_emails.append(v)
                if key in ("contactpoint", "contactpoints"):
                    walk_json_ld(v)
                elif isinstance(v, (dict, list)):
                    walk_json_ld(v)

    for block in ld_blocks:
        try:
            data = json.loads(block.strip())
            walk_json_ld(data)
        except (json.JSONDecodeError, ValueError):
            pass

    # ── 3) Phones from Microdata ───────────────────────────────
    microdata_phones = []
    # itemprop="telephone" content="..."
    microdata_phones.extend(
        re.findall(r'itemprop=["\']telephone["\'][^>]*content=["\']([^"\']+)["\']', decoded, re.IGNORECASE)
    )
    # itemprop="telephone">text<
    microdata_phones.extend(
        re.findall(r'itemprop=["\']telephone["\'][^>]*>([^<]+)<', decoded, re.IGNORECASE)
    )

    # ── Combine & normalize phones ─────────────────────────────
    phone_candidates = tel_links + schema_phones + microdata_phones
    phone_numbers = _uniq_case([p for p in (_normalize_phone(c) for c in phone_candidates) if p])

    # ── 4) Emails ──────────────────────────────────────────────
    # From mailto: links
    mailto_emails = [
        m.split("?")[0]
        for m in re.findall(r'href=["\']mailto:([^"\']+)["\']', decoded, re.IGNORECASE)
    ]

    # From regex scan of full HTML
    regex_emails = [
        e for e in re.findall(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}\b", decoded
        )
        if not _is_junk_email(e)
    ]

    emails = _uniq_case(mailto_emails + schema_emails + regex_emails)

    # ── 5) Contact page links ──────────────────────────────────
    contact_links = []
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', decoded, re.IGNORECASE):
        href = m.group(1) or ""
        text = re.sub(r"<[^>]*>", " ", m.group(2) or "").strip()
        href_l = href.lower()
        text_l = text.lower()
        if (
            "contact" in href_l
            or re.search(r"/(contact|support)(/|$)", href, re.IGNORECASE)
            or "contact" in text_l
            or "support" in text_l
            or "get in touch" in text_l
            or "reach us" in text_l
        ):
            abs_url = _to_absolute_url(href, base_url)
            if abs_url:
                contact_links.append(abs_url)

    # ── 6) Social links ───────────────────────────────────────
    social_links = re.findall(
        r'href=["\'](https?://(?:www\.)?(facebook|twitter|linkedin|instagram)[^"\']*)["\']',
        decoded, re.IGNORECASE,
    )
    social_urls = [s[0] for s in social_links]

    return {
        "emails": emails,
        "phone_numbers": phone_numbers,
        "contact_page_links": ", ".join(_uniq_case(contact_links)),
        "social_links": ", ".join(_uniq_case(social_urls)),
    }


# ═══════════════════════════════════════════════════════════════
# FETCH + EXTRACT (per lead)
# ═══════════════════════════════════════════════════════════════

def fetch_website_html(url: str, timeout: int = None) -> Optional[str]:
    """GET a website and return the HTML body, or None on failure."""
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    try:
        resp = httpx.get(
            url,
            headers=HEADERS,
            timeout=timeout or REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            return resp.text
        else:
            logger.debug(f"[Enricher] {url} returned {resp.status_code}")
            return None
    except httpx.TimeoutException:
        logger.debug(f"[Enricher] Timeout fetching {url}")
        return None
    except Exception as e:
        logger.debug(f"[Enricher] Error fetching {url}: {e}")
        return None


def enrich_lead(lead: dict) -> dict:
    """
    Enrich a single lead by fetching its website and extracting contacts.
    Clears any pre-existing Apify contact data — only website-extracted data counts.
    Returns the enriched lead.
    """
    website = lead.get("website_url") or lead.get("website") or ""
    domain = lead.get("company_domain", "")

    if not website and domain:
        website = f"https://{domain}"

    # Clear pre-existing contact data from Apify — we only want website-extracted data
    lead["contact_email"] = ""
    lead["contact_phone"] = ""

    logger.info(f"[Enricher] Fetching {website} ...")

    raw_html = fetch_website_html(website)
    if not raw_html:
        logger.info(f"[Enricher] No HTML from {website} — no contact info extracted")
        return lead

    contacts = extract_contacts_from_html(raw_html, base_url=website)

    # Set the first email found (if any)
    if contacts["emails"]:
        lead["contact_email"] = contacts["emails"][0]
        logger.info(f"[Enricher] ✓ Email found: {contacts['emails'][0]} ({domain})")

    # Set the first phone found (if any)
    if contacts["phone_numbers"]:
        lead["contact_phone"] = contacts["phone_numbers"][0]
        logger.info(f"[Enricher] ✓ Phone found: {contacts['phone_numbers'][0]} ({domain})")

    # Store contact page link if found
    if contacts["contact_page_links"]:
        lead["has_contact_form"] = True

    # Log if nothing found
    if not contacts["emails"] and not contacts["phone_numbers"]:
        logger.info(f"[Enricher] ✗ No email or phone found on {domain}")

    return lead


def score_lead(lead: dict) -> dict:
    """
    Score a lead using rule-based scoring.
    Called after enrichment, before DB insert.
    """
    score = 30  # base score
    reasons = []

    # Lead type scoring
    type_scores = {
        "chemical_plant": 30, "pharmaceutical": 25, "oil_gas": 25, "water_treatment": 25,
        "oem_pump_manufacturer": 20, "power_generation": 20, "food_processing": 15, "general_engineering": 10, "other": 0,
    }
    type_bonus = type_scores.get(lead.get("lead_type", "other"), 0)
    score += type_bonus
    if type_bonus > 0:
        reasons.append(f"Relevant type: {lead['lead_type']}")

    # Has email = better
    if lead.get("contact_email"):
        score += 10
        reasons.append("Has email")

    # Has phone = better
    if lead.get("contact_phone"):
        score += 5
        reasons.append("Has phone")

    # Decision-maker title
    title = (lead.get("contact_title") or "").lower()
    if any(t in title for t in ["ceo", "cto", "vp", "director", "head", "manager"]):
        score += 10
        reasons.append("Decision-maker contact")

    # Country priority
    high_priority = ["India", "UAE", "US", "Germany", "Saudi Arabia", "Singapore"]
    if lead.get("country") in high_priority:
        score += 10
        reasons.append(f"Priority country: {lead['country']}")

    # Has website
    if lead.get("website_url"):
        score += 5

    score = max(0, min(100, score))
    lead["score"] = score
    lead["score_reason"] = "; ".join(reasons) if reasons else "Base score"

    logger.info(f"[Enricher] Score: {score} for {lead.get('company_domain', '?')} ({'; '.join(reasons)})")
    return lead


def enrich_leads(leads: list[dict], insert_immediately: bool = True) -> list[dict]:
    """
    Enrich a batch of leads. Only keeps leads that have at least
    one email OR one phone number after website extraction.
    Scores each lead and inserts into Supabase immediately (one by one).
    """
    if not leads:
        return []

    from modules.database import insert_lead, domain_exists

    logger.info(f"[Enricher] Starting enrichment for {len(leads)} leads...")

    enriched = []
    skipped = 0
    inserted = 0

    for i, lead in enumerate(leads, 1):
        domain = lead.get("company_domain", "?")
        logger.info(f"[Enricher] [{i}/{len(leads)}] Enriching: {domain}")

        # Check if already in DB (skip duplicates early)
        if domain_exists(domain):
            skipped += 1
            logger.info(f"[Enricher] [{i}/{len(leads)}] ⊘ DUPLICATE {domain} — already in DB")
            continue

        enriched_lead = enrich_lead(lead)

        has_email = bool(enriched_lead.get("contact_email"))
        has_phone = bool(enriched_lead.get("contact_phone"))

        if has_email or has_phone:
            # Score the lead
            enriched_lead = score_lead(enriched_lead)
            enriched.append(enriched_lead)

            # Insert into Supabase immediately
            if insert_immediately:
                result = insert_lead(enriched_lead)
                if result:
                    inserted += 1
                    # Store the DB-assigned ID back on the lead
                    if isinstance(result, dict) and "id" in result:
                        enriched_lead["id"] = result["id"]
                    logger.info(
                        f"[Enricher] [{i}/{len(leads)}] ✓ SAVED {domain} → Supabase "
                        f"(email: {enriched_lead.get('contact_email','')}, "
                        f"phone: {enriched_lead.get('contact_phone','')}, "
                        f"score: {enriched_lead['score']})"
                    )
                    # Email worker will pick this up from DB automatically
                    # (no in-memory queue — email worker polls for New leads)
                else:
                    logger.info(f"[Enricher] [{i}/{len(leads)}] ⊘ DUPLICATE {domain} (DB conflict)")
            else:
                logger.info(
                    f"[Enricher] [{i}/{len(leads)}] ✓ KEPT {domain} "
                    f"(email: {has_email}, phone: {has_phone}, score: {enriched_lead['score']})"
                )
        else:
            skipped += 1
            logger.info(
                f"[Enricher] [{i}/{len(leads)}] ✗ SKIPPED {domain} "
                f"(no email or phone on website)"
            )

    logger.info(
        f"[Enricher] Done: {inserted} saved to DB, {skipped} skipped "
        f"(from {len(leads)} total)"
    )
    return enriched
