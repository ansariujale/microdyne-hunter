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


# ═══════════════════════════════════════════════════════════════
# QUALITY GATE — only genuine, relevant buyers get stored
# ═══════════════════════════════════════════════════════════════

# B2B directories / marketplaces list other companies — they are not the company itself.
DIRECTORY_DOMAINS = (
    "indiamart.com", "justdial.com", "tradeindia.com", "exportersindia.com", "alibaba.com",
    "made-in-china.com", "sulekha.com", "yellowpages", "dial4trade.com", "go4worldbusiness.com",
    "kompass.com", "europages", "thomasnet.com", "zaubacorp.com", "tofler.in", "globalsources.com",
    "ec21.com", "tradekey.com", "amazon.", "flipkart.com", "yelp.", "manta.com", "hotfrog.",
    "microdyneengineering.com",
)

# Other CNC job shops are competitors, not buyers.
COMPETITOR_PHRASES = (
    "job work", "jobwork", "turning works", "turned component", "turned parts", "cnc works",
    "cnc machining services", "machine shop", "precision turned",
)

RELEVANCE_SIGNALS = (
    "manufactur", "oem", "factory", "machinery", "machine", "pump", "valve", "cylinder",
    "hydraulic", "pneumatic", "gear", "compressor", "motor", "blower", "agitator", "mixer",
    "automotive", "auto component", "equipment", "industrial", "engineering",
)

UNUSABLE_EMAIL_LOCALPARTS = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "career", "jobs", "hr", "recruit",
    "resume", "cv", "webmaster", "privacy", "abuse", "postmaster", "press", "media",
)

PREFERRED_EMAIL_LOCALPARTS = (
    "sales", "purchase", "procurement", "enquiry", "inquiry", "info", "contact", "marketing",
    "business", "export",
)


def _brand_label(domain: str) -> str:
    return (domain or "").lower().replace("www.", "").split(".")[0]


def pick_business_email(emails: list[str], company_domain: str) -> str:
    """Best usable email on the company's own domain, preferring sales/purchase inboxes."""
    import config
    personal = {d.lower() for d in getattr(config, "JUNK_EMAIL_DOMAINS", set())}
    brand = _brand_label(company_domain)
    candidates = []
    for email in emails:
        email = email.strip().lower().rstrip(".")
        if "@" not in email or _is_junk_email(email):
            continue
        local, domain = email.split("@", 1)
        if domain in personal:
            continue
        if not (domain == company_domain or domain.endswith("." + company_domain)
                or company_domain.endswith("." + domain) or _brand_label(domain) == brand):
            continue
        if any(bad in local for bad in UNUSABLE_EMAIL_LOCALPARTS):
            continue
        rank = next((i for i, p in enumerate(PREFERRED_EMAIL_LOCALPARTS) if local.startswith(p)),
                    len(PREFERRED_EMAIL_LOCALPARTS))
        candidates.append((rank, email))
    return min(candidates)[1] if candidates else ""


def _page_summary(raw_html: str) -> dict:
    """Title, meta description, and lowercase visible text of a page."""
    title = re.search(r"<title[^>]*>([\s\S]*?)</title>", raw_html, re.IGNORECASE)
    meta = re.search(r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)', raw_html, re.IGNORECASE)
    text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return {
        "title": html.unescape(title.group(1)).strip() if title else "",
        "description": html.unescape(meta.group(1)).strip() if meta else "",
        "text": " ".join(html.unescape(text).split())[:40000].lower(),
    }


def assess_lead_quality(lead: dict) -> tuple[bool, str]:
    """Final go/no-go for storing a lead. Returns (passes, reason)."""
    import config
    domain = (lead.get("company_domain") or "").lower()
    if any(d in domain for d in DIRECTORY_DOMAINS):
        return False, "directory/marketplace site"
    if not lead.get("_site_reachable"):
        return False, "website not reachable"
    identity = f"{lead.get('company_name', '')} {lead.get('_page_title', '')}".lower()
    if any(p in identity for p in COMPETITOR_PHRASES):
        return False, "CNC job shop (competitor)"
    if lead.get("_relevance_hits", 0) < 2:
        return False, "website not relevant to manufacturing"
    if not lead.get("contact_email") and not lead.get("has_contact_form"):
        return False, "no business email or contact form"
    if lead.get("lead_type", "other") == "other":
        return False, "not a target industry"
    min_score = int(getattr(config, "SCORE_THRESHOLDS", {}).get("min_qualify", 60) or 60)
    if int(lead.get("score") or 0) < min_score:
        return False, f"score {lead.get('score')} below {min_score}"
    return True, "passed"


def enrich_lead(lead: dict) -> dict:
    """
    Fetch the company website (plus its contact page if needed), pick a business email
    on the company's own domain, detect a contact form, and classify relevance.
    Only website-extracted contact data is kept.
    """
    from modules.scraper import classify_lead_type

    website = lead.get("website_url") or lead.get("website") or ""
    domain = (lead.get("company_domain") or "").lower()
    if not website and domain:
        website = f"https://{domain}"

    lead["contact_email"] = ""
    lead["contact_phone"] = ""
    lead["_site_reachable"] = False
    lead["_relevance_hits"] = 0

    logger.info(f"[Enricher] Fetching {website} ...")
    raw_html = fetch_website_html(website)
    if not raw_html:
        logger.info(f"[Enricher] No HTML from {website}")
        return lead
    lead["_site_reachable"] = True

    contacts = extract_contacts_from_html(raw_html, base_url=website)
    emails = list(contacts["emails"])
    phones = list(contacts["phone_numbers"])
    contact_links = [u.strip() for u in contacts["contact_page_links"].split(",") if u.strip()]
    same_site_links = [u for u in contact_links if domain and domain in u.lower()]

    email = pick_business_email(emails, domain)
    if not email and same_site_links:
        contact_html = fetch_website_html(same_site_links[0])
        if contact_html:
            extra = extract_contacts_from_html(contact_html, base_url=same_site_links[0])
            emails += extra["emails"]
            phones += extra["phone_numbers"]
            email = pick_business_email(emails, domain)

    lead["contact_email"] = email
    lead["contact_phone"] = phones[0] if phones else ""
    lead["has_contact_form"] = bool(same_site_links)

    page = _page_summary(raw_html)
    lead["_page_title"] = page["title"]
    lead["_relevance_hits"] = sum(1 for s in RELEVANCE_SIGNALS if s in page["text"])
    lead["lead_type"] = classify_lead_type(
        f"{lead.get('company_name', '')} {page['title']}",
        f"{lead.get('description', '')} {page['description']}",
    )

    logger.info(
        f"[Enricher] {domain}: email={email or '-'} form={lead['has_contact_form']} "
        f"type={lead['lead_type']} relevance={lead['_relevance_hits']}"
    )
    return lead


def score_lead(lead: dict) -> dict:
    """Rule-based score used by the quality gate. Called after enrichment, before insert."""
    score = 30
    reasons = []

    type_scores = {
        "pump_valve_manufacturer": 25, "process_equipment_manufacturer": 25,
        "hydraulic_pneumatic_manufacturer": 25, "automotive_component_manufacturer": 20,
        "machinery_equipment_manufacturer": 20, "compressor_blower_manufacturer": 20,
        "electrical_equipment_manufacturer": 15, "general_engineering": 10, "other": 0,
    }
    type_bonus = type_scores.get(lead.get("lead_type", "other"), 0)
    score += type_bonus
    if type_bonus:
        reasons.append(f"Target industry: {lead['lead_type']}")

    email = lead.get("contact_email") or ""
    if email:
        score += 15
        reasons.append("Business email on company domain")
        if email.split("@")[0].startswith(("sales", "purchase", "procurement")):
            score += 5
            reasons.append("Sales/purchase inbox")

    if lead.get("has_contact_form"):
        score += 10
        reasons.append("Contact form")

    if lead.get("contact_phone"):
        score += 5
        reasons.append("Phone listed")

    if lead.get("_relevance_hits", 0) >= 4:
        score += 5
        reasons.append("Strong manufacturing signals on site")

    title = (lead.get("contact_title") or "").lower()
    if any(t in title for t in ["ceo", "director", "head", "manager", "purchase", "procurement"]):
        score += 10
        reasons.append("Decision-maker contact")

    if lead.get("country") in ["India", "UAE", "US", "Germany", "Saudi Arabia", "Singapore"]:
        score += 10
        reasons.append(f"Priority country: {lead['country']}")

    lead["score"] = max(0, min(100, score))
    lead["score_reason"] = "; ".join(reasons) if reasons else "Base score"
    return lead


def enrich_leads(leads: list[dict], insert_immediately: bool = True, max_keep: int = None) -> list[dict]:
    """
    Enrich, score, and quality-gate a batch of leads. Only leads passing
    assess_lead_quality are kept (and inserted immediately when requested).
    Stops once max_keep leads have been kept.
    """
    if not leads:
        return []

    from modules.database import insert_lead, domain_exists

    logger.info(f"[Enricher] Starting enrichment for {len(leads)} leads...")

    kept = []
    rejected = {}

    for i, lead in enumerate(leads, 1):
        if max_keep is not None and len(kept) >= max_keep:
            break
        domain = lead.get("company_domain", "?")

        if domain_exists(domain):
            rejected["duplicate"] = rejected.get("duplicate", 0) + 1
            continue

        lead = score_lead(enrich_lead(lead))
        passes, reason = assess_lead_quality(lead)
        if not passes:
            rejected[reason] = rejected.get(reason, 0) + 1
            logger.info(f"[Enricher] [{i}/{len(leads)}] ✗ REJECTED {domain} — {reason}")
            continue

        record = {k: v for k, v in lead.items() if not k.startswith("_") and k != "description"}
        if insert_immediately:
            result = insert_lead(record)
            if not result:
                rejected["duplicate"] = rejected.get("duplicate", 0) + 1
                continue
            if isinstance(result, dict) and "id" in result:
                record["id"] = result["id"]
        kept.append(record)
        logger.info(
            f"[Enricher] [{i}/{len(leads)}] ✓ KEPT {domain} (score {record['score']}, "
            f"email: {record.get('contact_email') or '-'}, form: {record.get('has_contact_form')})"
        )

    logger.info(f"[Enricher] Done: kept {len(kept)} of {len(leads)} — rejected: {rejected}")
    return kept
