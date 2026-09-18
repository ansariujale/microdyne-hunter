"""
MicrodyneHunter v2 — Configuration
All API keys, thresholds, and settings in one place.
"""

import os
import base64
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════
# API KEYS & CREDENTIALS
# ═══════════════════════════════════════════════════════════════

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")  # service_role key

APIFY_API_KEY = os.getenv("APIFY_API_KEY", "")

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")

INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY", "")
INSTANTLY_WORKSPACE_ID = os.getenv("INSTANTLY_WORKSPACE_ID", "")

# SMTP (Gmail) — fallback when Instantly is not configured
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "M. Marediya")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# Keyword generation runs on its own key so chat traffic and keyword traffic
# bill and rate-limit separately. Falls back to the key above when unset.
OPENROUTER_API_KEY_KEYWORDS = os.getenv("OPENROUTER_API_KEY_KEYWORDS", "")
# OpenRouter model id. Override in .env if the default is ever retired.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ═══════════════════════════════════════════════════════════════
# MICRODYNE ENGINEERING BUSINESS INFO (used in emails & form fills)
# ═══════════════════════════════════════════════════════════════

MICRODYNE = {
    "company_name": "Microdyne Engineering",
    "contact_name": os.getenv("CONTACT_NAME", "M. Marediya"),
    "contact_email": "sales@microdyneengineering.com",
    "sales_email": "sales@microdyneengineering.com",
    "website": os.getenv("COMPANY_WEBSITE", "https://www.microdyneengineering.com"),
    "phone": os.getenv("COMPANY_PHONE", "+91-9082121601"),
    "address": os.getenv("COMPANY_ADDRESS", "Mumbai, Maharashtra, India"),
    "gst": "27BWWPM3354A1ZC",
    "products": [
        "CNC Turning Job Work (screws, nuts, sleeves, bushings, custom turned parts)",
        "Precision Turned Components — brass, SS, mild steel",
        "Mechanical Seals (Cartridge, Spring, Bellow, Teflon Bellow, Conical Spring)",
        "Subcontract / Contract Manufacturing Turning Capacity",
    ],
    "materials": "Brass, SS304/SS316, Mild Steel, EN8/EN24, PTFE (Teflon), EPDM/Viton (for seals)",
    "coverage": "Manufacturer based in Mumbai, India — established 2021",
    "usp": "In-house CNC turning capacity available for job-work partnerships — precision turned components, screws, nuts & sleeves — backed by our own mechanical seal manufacturing since 2021",
}

# Backward-compatible aliases
ROZPER = MICRODYNE
FLOWLOCK = MICRODYNE

# ═══════════════════════════════════════════════════════════════
# SCRAPING SETTINGS
# ═══════════════════════════════════════════════════════════════

# Max new leads stored per extraction run. Extraction only runs when no
# un-emailed and no un-submitted leads are left in the database.
DAILY_LEAD_TARGET = int(os.getenv("DAILY_LEAD_TARGET", "50"))

# Target buyer types — manufacturers that consume turned components or
# mechanical seals and may outsource CNC turning job work.
LEAD_TYPES = [
    "pump_valve_manufacturer",
    "hydraulic_pneumatic_manufacturer",
    "automotive_component_manufacturer",
    "machinery_equipment_manufacturer",
    "process_equipment_manufacturer",
    "electrical_equipment_manufacturer",
    "compressor_blower_manufacturer",
    "general_engineering",
]

import json

# Target countries (priority order — high-converting industrial markets first)
_tc_env = os.getenv("TARGET_COUNTRIES", "")
if _tc_env:
    try:
        TARGET_COUNTRIES = json.loads(_tc_env)
    except:
        TARGET_COUNTRIES = [c.strip() for c in _tc_env.split(",") if c.strip()]
else:
    TARGET_COUNTRIES = [
        "India", "UAE", "Saudi Arabia", "US", "UK", "Germany", "Netherlands",
        "South Africa", "Nigeria", "Kenya", "Singapore", "Malaysia",
        "Turkey", "Egypt", "Brazil", "Mexico", "Italy", "France",
        "Australia", "Indonesia", "Thailand", "Vietnam", "Qatar", "Oman",
    ]

# ═══════════════════════════════════════════════════════════════
# COUNTRY ROTATION (auto-selection for the next campaign)
# ═══════════════════════════════════════════════════════════════

# Gulf markets are never picked automatically, whatever the history says.
# Listed by every spelling the AI or an admin might type.
GULF_COUNTRY_ALIASES = {
    "uae": "United Arab Emirates",
    "u.a.e.": "United Arab Emirates",
    "u.a.e": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates",
    "dubai": "United Arab Emirates",
    "abu dhabi": "United Arab Emirates",
    "sharjah": "United Arab Emirates",
    "ksa": "Saudi Arabia",
    "saudi": "Saudi Arabia",
    "saudi arabia": "Saudi Arabia",
    "kingdom of saudi arabia": "Saudi Arabia",
    "qatar": "Qatar",
    "state of qatar": "Qatar",
    "doha": "Qatar",
    "kuwait": "Kuwait",
    "state of kuwait": "Kuwait",
    "bahrain": "Bahrain",
    "kingdom of bahrain": "Bahrain",
    "oman": "Oman",
    "sultanate of oman": "Oman",
    "muscat": "Oman",
}

GULF_COUNTRIES = [
    "United Arab Emirates", "Saudi Arabia", "Qatar", "Kuwait", "Bahrain", "Oman",
]


def is_gulf_country(name: str) -> bool:
    """True for any spelling of a Gulf market, so it can be filtered out."""
    key = " ".join(str(name or "").strip().lower().replace(".", ". ").split())
    if key in GULF_COUNTRY_ALIASES:
        return True
    return " ".join(str(name or "").strip().lower().split()) in GULF_COUNTRY_ALIASES


# How long a country sits out before it can be picked again.
COUNTRY_COOLDOWN_DAYS = int(os.getenv("COUNTRY_COOLDOWN_DAYS", "14"))

# Markets the auto-selector may choose from. Industrial economies that buy
# turned components and mechanical seals; no Gulf entries by construction.
COUNTRY_POOL = [
    "India", "United States", "United Kingdom", "Germany", "Netherlands",
    "Italy", "France", "Spain", "Poland", "Czech Republic", "Sweden",
    "Turkey", "Egypt", "South Africa", "Nigeria", "Kenya", "Morocco",
    "Singapore", "Malaysia", "Indonesia", "Thailand", "Vietnam",
    "Philippines", "South Korea", "Japan", "Taiwan", "Australia",
    "New Zealand", "Canada", "Mexico", "Brazil", "Chile", "Colombia",
    "Argentina", "Bangladesh", "Sri Lanka", "Israel", "Portugal", "Romania",
]
COUNTRY_POOL = [c for c in COUNTRY_POOL if not is_gulf_country(c)]


# Preserved best keyword set — always searched, cannot be removed from the admin
# panel. These target BUYERS of turned parts / mechanical seals (OEMs), not other
# CNC job shops: searching "CNC turning job work" would return competitors.
# The target country is appended at search time.
BEST_SEARCH_KEYWORDS = [
    "pump manufacturer",
    "valve manufacturer",
    "hydraulic cylinder manufacturer",
    "auto components manufacturer",
    "compressor manufacturer",
    "gearbox manufacturer",
    "electric motor manufacturer",
    "agitator mixer manufacturer",
    "process equipment manufacturer",
    "packaging machine manufacturer",
    "textile machinery manufacturer",
    "agricultural machinery manufacturer",
    "food processing machinery manufacturer",
    "pharmaceutical machinery manufacturer",
    "special purpose machine manufacturer",
]


def normalize_keyword(keyword: str) -> str:
    return " ".join(str(keyword or "").replace("{country}", " ").split())


def merge_keywords(extra) -> list[str]:
    """Best set first, then any extra admin keywords (deduped, case-insensitive)."""
    merged, seen = [], set()
    for kw in list(BEST_SEARCH_KEYWORDS) + list(extra or []):
        kw = normalize_keyword(kw)
        if kw and kw.lower() not in seen:
            seen.add(kw.lower())
            merged.append(kw)
    return merged


_sk_env = os.getenv("SEARCH_KEYWORDS", "")
_sk_extra = []
if _sk_env:
    try:
        _sk_extra = json.loads(_sk_env)
    except Exception:
        _sk_extra = [k.strip() for k in _sk_env.split(",") if k.strip()]
SEARCH_KEYWORDS = merge_keywords(_sk_extra)

# Apollo.io job titles to search
APOLLO_JOB_TITLES = [
    "Procurement Manager", "Purchase Head", "Sourcing Manager",
    "Production Manager", "Manufacturing Manager", "Plant Manager",
    "Supply Chain Manager", "Operations Head",
    "Director of Engineering", "General Manager",
]

# Apollo.io industry keywords
APOLLO_INDUSTRIES = [
    "Industrial Machinery Manufacturing", "Automotive", "Machinery",
    "Mechanical Or Industrial Engineering", "Electrical & Electronic Manufacturing",
    "Industrial Automation", "Manufacturing",
]

# ═══════════════════════════════════════════════════════════════
# EMAIL SETTINGS (Instantly.dev)
# ═══════════════════════════════════════════════════════════════

# Strict daily outreach caps. Env vars can only LOWER these, never raise them.
MAX_EMAILS_PER_DAY = 10
MAX_FORMS_PER_DAY = 100
DAILY_EMAIL_LIMIT = max(0, min(int(os.getenv("DAILY_EMAIL_LIMIT", str(MAX_EMAILS_PER_DAY))), MAX_EMAILS_PER_DAY))
DAILY_FORM_LIMIT = max(0, min(int(os.getenv("DAILY_FORM_LIMIT", str(MAX_FORMS_PER_DAY))), MAX_FORMS_PER_DAY))

# The day window used for the caps starts at this local hour (matches dashboard "today").
BUSINESS_DAY_RESET_HOUR = 11

# The server auto-runs the daily cycle once per day after this local time.
AUTO_DAILY_RUN = os.getenv("AUTO_DAILY_RUN", "true").strip().lower() != "false"
DAILY_RUN_HOUR = 11
DAILY_RUN_MINUTE = 5

DAILY_EMAIL_TARGET = DAILY_EMAIL_LIMIT  # legacy name
EMAILS_PER_DOMAIN = min(int(os.getenv("EMAILS_PER_DOMAIN", "40")), DAILY_EMAIL_LIMIT)

# Email subject line (editable from admin panel)
EMAIL_SUBJECT = os.getenv("EMAIL_SUBJECT", "CNC Turning Job Work Partnership — Microdyne Engineering")

# Email body content (editable from admin panel — supports <b>bold</b> tags)
# NOTE: Do NOT put this in .env — multiline values break dotenv parsing
_DEFAULT_EMAIL_BODY = (
    "Dear Sir/Madam,\n\n"
    "I'm writing from <b>Microdyne Engineering</b>, a Mumbai-based manufacturing company. Since 2021 we've run our own "
    "line of mechanical seals, and alongside that we operate in-house <b>CNC turning</b> capacity for job work — "
    "screws, nuts, sleeves, bushings, and other turned components.\n\n"
    "We're looking to partner with manufacturers who need reliable turning job-work support, especially when your own "
    "shop floor is running at capacity. If that's something you outsource, we'd welcome the chance to quote a trial "
    "batch and show you our quality and turnaround.\n\n"
    "Would you be open to a short call to see if there's a fit for your production needs?"
)

_email_body_b64 = os.getenv("EMAIL_BODY_B64", "")
_email_body_env = os.getenv("EMAIL_BODY", "")
EMAIL_BODY = _DEFAULT_EMAIL_BODY
if _email_body_b64:
    try:
        EMAIL_BODY = base64.b64decode(_email_body_b64.encode("ascii")).decode("utf-8")
    except Exception:
        EMAIL_BODY = _DEFAULT_EMAIL_BODY
elif _email_body_env:
    EMAIL_BODY = _email_body_env.replace("\\n", "\n")

# Sending email accounts (emails are sent FROM these — 65 emails/day each)
_sending_emails_b64 = os.getenv("SENDING_EMAILS_B64", "")
if _sending_emails_b64:
    try:
        _decoded = base64.b64decode(_sending_emails_b64.encode("ascii")).decode("utf-8")
        SENDING_EMAILS = [l.strip() for l in _decoded.splitlines() if l.strip()]
    except Exception:
        SENDING_EMAILS = []
else:
    SENDING_EMAILS = [
        "sales@microdyneengineering.com",
    ]

# Backward-compat: domains list (derived)
SENDING_DOMAINS = list(set(e.split("@")[-1] for e in SENDING_EMAILS if "@" in e))

# Follow-up sequence timing (days after initial email)
FOLLOWUP_SCHEDULE = {
    1: "initial",       # Day 1: Intro + turning job-work partnership ask
    3: "quality",       # Day 3: Quality angle — trial batch, turnaround
    7: "social_proof",  # Day 7: Credibility — manufacturing since 2021, own seal line
    14: "breakup",      # Day 14: Breakup — "No pressure, offer open"
}

# Email open tracking — pixel served by the "email-open" Supabase Edge Function
# (source in supabase/functions/email-open). TRACKING_PIXEL_BASE overrides the
# whole URL if the pixel is ever hosted somewhere else.
TRACKING_FUNCTION = os.getenv("TRACKING_FUNCTION", "email-open")
TRACKING_PIXEL_BASE = os.getenv("TRACKING_PIXEL_BASE", "")

# ═══════════════════════════════════════════════════════════════
# FORM FILLING SETTINGS
# ═══════════════════════════════════════════════════════════════

DAILY_FORM_TARGET = DAILY_FORM_LIMIT  # legacy name

# Contact data used when filling website forms
FORM_FILL_DATA = {
    'name': os.getenv('FORM_NAME', 'Microdyne Engineering'),
    'first_name': os.getenv('FORM_FIRST_NAME', 'Microdyne'),
    'last_name': os.getenv('FORM_LAST_NAME', 'Engineering'),
    'company': os.getenv('FORM_COMPANY', 'Microdyne Engineering'),
    'email': os.getenv('FORM_EMAIL', 'sales@microdyneengineering.com'),
    'phone': os.getenv('FORM_PHONE', '+91-9082121601'),
    'subject': os.getenv('FORM_SUBJECT', 'We are Microdyne Engineering, a Mumbai-based manufacturer offering CNC turning job work — screws, nuts, sleeves, bushings and custom turned components — alongside our own line of mechanical seals. If you outsource any turning or job work, get in touch at sales@microdyneengineering.com.').replace('\\n', '\n'),
    'message': os.getenv('FORM_MESSAGE', (
        'We are Microdyne Engineering, a Mumbai-based manufacturer. Since 2021 we have run '
        'our own line of mechanical seals, and alongside that we operate in-house CNC turning '
        'capacity for job work — screws, nuts, sleeves, bushings, and other turned components '
        'in brass, SS, and mild steel. '
        'We are looking to partner with manufacturers who need reliable turning job-work support. '
        'If that is something you outsource, we would welcome the chance to quote a trial batch. '
        'Contact us at sales@microdyneengineering.com to discuss.'
    )).replace('\\n', '\n'),
}

FORM_PATHS_TO_TRY = [
    "/contact", "/contact-us", "/inquiry", "/get-quote",
    "/partnership", "/partners", "/get-in-touch",
    "/request-quote", "/reach-us", "/talk-to-sales",
    "/sales", "/connect", "/enquiry",
]

FORM_MESSAGE_TEMPLATE = (
    "Hi, I'm {contact_name} from {company_name}. "
    "We're a Mumbai-based manufacturer offering CNC turning job work — screws, nuts, sleeves, "
    "bushings, and custom turned components — alongside our own mechanical seal manufacturing. "
    "If you outsource any turning or job work, we'd welcome the chance to quote a trial batch. "
    "Happy to share more details — just let me know your requirements. "
    "Best regards, {contact_name}"
)

# ═══════════════════════════════════════════════════════════════
# LEAD SCORING THRESHOLDS
# ═══════════════════════════════════════════════════════════════

SCORE_THRESHOLDS = {
    "min_qualify": int(os.getenv("MIN_QUALIFY_SCORE", "60")),  # minimum score to store/contact a lead
    "high_priority": 70,      # high-priority leads
    "skip_below": 20,         # auto-skip leads below this score
}

# ═══════════════════════════════════════════════════════════════
# AUTO-EXCLUSION RULES (Intelligence System)
# ═══════════════════════════════════════════════════════════════

AUTO_EXCLUSION = {
    "country_pause_after_leads": 200,       # pause country if 0 closes after N leads
    "lead_type_min_close_rate": 0.005,      # 0.5% — deprioritize below this
    "lead_type_min_sample": 500,            # need N leads before judging
    "source_min_reply_rate": 0.01,          # 1% — reduce volume below this
    "source_min_sample": 1000,              # need N sends before judging
    "email_domain_min_open_rate": 0.10,     # rotate domain if open rate < 10%
}

# ═══════════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════════

WEEKLY_REPORT_DAY = "sunday"  # day of week to generate intelligence report
NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL", "sales@microdyneengineering.com")

# Current sending email (for testing)
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "sales@microdyneengineering.com")

# ═══════════════════════════════════════════════════════════════
# RATE LIMITS & SAFETY
# ═══════════════════════════════════════════════════════════════

SCRAPE_DELAY_SECONDS = 2        # delay between web scraping requests
FORM_FILL_DELAY_SECONDS = 5     # delay between form submissions
MAX_RETRIES = 3                 # max retries per operation
REQUEST_TIMEOUT = 30            # HTTP request timeout in seconds

# ═══════════════════════════════════════════════════════════════
# EMAIL VARIANT GENERATION & SCORING
# ═══════════════════════════════════════════════════════════════

VARIANTS_PER_LEAD = 1           # send only 1 email per lead — no variants

# Warmup schedule: day number → max emails per domain
WARMUP_SCHEDULE = {
    1: 5, 2: 8, 3: 12, 4: 18, 5: 25,
    6: 30, 7: 35, 8: 40, 9: 45, 10: 50,
    11: 52, 12: 54, 13: 56, 14: 58,
    15: 60, 16: 61, 17: 62, 18: 63, 19: 64, 20: 64, 21: 65,
}  # after day 21: EMAILS_PER_DOMAIN (65)

# Email scoring weights (each dimension 0-25, total 0-100)
EMAIL_SCORING_WEIGHTS = {
    "subject": 25,
    "personalization": 25,
    "cta": 25,
    "spam_safety": 25,
}

# Spam trigger words — presence in subject/body reduces spam_safety score
SPAM_TRIGGERS = {
    "act now", "limited time", "click here", "buy now", "order now",
    "urgent", "congratulations", "winner", "guarantee", "no obligation",
    "risk-free", "special promotion", "exclusive deal", "100%", "amazing",
    "incredible offer", "lowest price", "earn money", "cash bonus",
    "double your", "apply now", "sign up free", "subscribe now",
    "no cost", "no fees", "once in a lifetime", "don't miss",
    "for free", "zero risk",
}

# Junk email domains (personal emails, not company — skip these leads)
JUNK_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "mail.com", "protonmail.com", "zoho.com", "yandex.com",
    "live.com", "msn.com", "inbox.com", "gmx.com",
}

# Email send delay range (seconds) — random between min and max to mimic human
EMAIL_SEND_DELAY = (2, 8)
