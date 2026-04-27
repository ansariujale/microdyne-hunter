"""
FlowLockHunter v2 — Configuration
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
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "H. Khorajiya")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ═══════════════════════════════════════════════════════════════
# FLOWLOCK OVERSEAS BUSINESS INFO (used in emails & form fills)
# ═══════════════════════════════════════════════════════════════

FLOWLOCK = {
    "company_name": "FlowLock Overseas",
    "contact_name": "H. Khorajiya",
    "contact_email": "info@flowlockoverseas.com",
    "sales_email": "sales@flowlockoverseas.com",
    "website": "https://www.flowlockoverseas.com",
    "phone": "+91-9082717763",
    "address": "Unit 2, 1st Floor, Dawood Baug, Rani Sati Road, Mumbai 400097, Maharashtra, India",
    "gst": "27ISYPK7898N1ZX",
    "products": [
        "Mechanical Seals (Cartridge, Spring, Bellow, Agitator)",
        "Hydraulic Fittings (NPT, JIC, ORFS, BSP, Compression)",
        "Hydraulic Couplings (Quick Release, Flat Face)",
        "Seal Repair & Refurbishment Services",
    ],
    "materials": "SS316, Hastelloy, Alloy 20, Silicon Carbide, Tungsten Carbide, PTFE, Viton",
    "coverage": "Global export from India — serving 50+ countries",
    "usp": "Reliable mechanical seal solutions & hydraulic fittings — reduce maintenance costs 30-50% with expert refurbishment",
}

# Backward-compatible alias
ROZPER = FLOWLOCK

# ═══════════════════════════════════════════════════════════════
# SCRAPING SETTINGS
# ═══════════════════════════════════════════════════════════════

DAILY_LEAD_TARGET = int(os.getenv("DAILY_LEAD_TARGET", "120"))

# Target buyer types (industrial sectors)
LEAD_TYPES = [
    "chemical_plant",
    "pharmaceutical",
    "oil_gas",
    "water_treatment",
    "power_generation",
    "oem_pump_manufacturer",
    "food_processing",
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

# Search keywords templates (combined with country)
_sk_env = os.getenv("SEARCH_KEYWORDS", "")
if _sk_env:
    try:
        SEARCH_KEYWORDS = json.loads(_sk_env)
    except:
        SEARCH_KEYWORDS = [k.strip() for k in _sk_env.split(",") if k.strip()]
else:
    SEARCH_KEYWORDS = [
        "mechanical seal supplier {country}",
        "hydraulic fittings wholesaler {country}",
        "industrial seal manufacturer {country}",
        "pump seal supplier {country}",
        "mechanical seal distributor {country}",
        "hydraulic tube fittings {country}",
        "seal refurbishment service {country}",
        "cartridge seal supplier {country}",
        "bellow seal manufacturer {country}",
        "hydraulic couplings supplier {country}",
    ]

# Apollo.io job titles to search
APOLLO_JOB_TITLES = [
    "Plant Manager", "Maintenance Manager", "Procurement Manager",
    "Head of Maintenance", "VP Operations", "VP Procurement",
    "Director of Engineering", "Chief Engineer",
    "Purchase Head", "General Manager",
]

# Apollo.io industry keywords
APOLLO_INDUSTRIES = [
    "Chemical Manufacturing", "Pharmaceutical", "Oil & Gas",
    "Water Treatment", "Power Generation", "Industrial Manufacturing",
    "Food Processing", "Pump Manufacturing",
]

# ═══════════════════════════════════════════════════════════════
# EMAIL SETTINGS (Instantly.dev)
# ═══════════════════════════════════════════════════════════════

DAILY_EMAIL_TARGET = int(os.getenv("DAILY_EMAIL_TARGET", "40"))
EMAILS_PER_DOMAIN = int(os.getenv("EMAILS_PER_DOMAIN", "40"))  # safe limit per sending domain

# Email subject line (editable from admin panel)
EMAIL_SUBJECT = os.getenv("EMAIL_SUBJECT", "Mechanical Seals & Hydraulic Fittings — FlowLock Overseas")

# Email body content (editable from admin panel — supports <b>bold</b> tags)
# NOTE: Do NOT put this in .env — multiline values break dotenv parsing
_DEFAULT_EMAIL_BODY = (
    "Dear Sir/Madam,\n\n"
    "We are <b>FlowLock Overseas</b>, a leading supplier of <b>mechanical seals</b> and <b>hydraulic fittings</b> from Mumbai, India.\n\n"
    "Our product range includes <b>cartridge seals</b>, <b>spring seals</b>, <b>bellow seals</b>, agitator seals, "
    "and hydraulic tube fittings in <b>SS316</b>, <b>Hastelloy</b>, and <b>Silicon Carbide</b>.\n\n"
    "Would you be open to a <b>free consultation</b> to discuss your sealing requirements? "
    "We can also send a sample for quality evaluation at no cost."
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
        "flowlockoverseas@gmail.com",
        "sales@flowlockoverseas.com",
    ]

# Backward-compat: domains list (derived)
SENDING_DOMAINS = list(set(e.split("@")[-1] for e in SENDING_EMAILS if "@" in e))

# Follow-up sequence timing (days after initial email)
FOLLOWUP_SCHEDULE = {
    1: "initial",       # Day 1: Intro + USP + free consultation
    3: "quality",       # Day 3: Quality angle — "Premium seals in SS316/SiC"
    7: "social_proof",  # Day 7: Social proof — "Trusted by 100+ plants globally"
    14: "breakup",      # Day 14: Breakup — "No pressure, offer open"
}

# ═══════════════════════════════════════════════════════════════
# FORM FILLING SETTINGS
# ═══════════════════════════════════════════════════════════════

DAILY_FORM_TARGET = 130

# Contact data used when filling website forms
FORM_FILL_DATA = {
    'name': os.getenv('FORM_NAME', 'FlowLock Overseas'),
    'first_name': os.getenv('FORM_FIRST_NAME', 'FlowLock'),
    'last_name': os.getenv('FORM_LAST_NAME', 'Overseas'),
    'company': os.getenv('FORM_COMPANY', 'FlowLock Overseas'),
    'email': os.getenv('FORM_EMAIL', 'sales@flowlockoverseas.com'),
    'phone': os.getenv('FORM_PHONE', '+91-9082717763'),
    'subject': os.getenv('FORM_SUBJECT', 'We are FlowLock Overseas, a leading supplier of mechanical seals and hydraulic fittings from Mumbai, India. Our product range includes cartridge seals, spring seals, bellow seals, agitator seals, and hydraulic tube fittings in SS316, Hastelloy, and Silicon Carbide. Contact us at sales@flowlockoverseas.com or call +91-9082717763 for a free consultation.').replace('\\n', '\n'),
    'message': os.getenv('FORM_MESSAGE', (
        'We are FlowLock Overseas, a leading supplier of mechanical seals and '
        'hydraulic fittings from Mumbai, India. We offer high-quality cartridge seals, '
        'spring seals, bellow seals, agitator seals, and hydraulic tube fittings in '
        'SS316, Hastelloy, Alloy 20, Silicon Carbide, and Tungsten Carbide. '
        'We also provide seal repair and refurbishment services that can reduce '
        'your maintenance costs by 30-50%. '
        'Contact us at sales@flowlockoverseas.com or call +91-9082717763 '
        'for a free consultation and sample.'
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
    "We supply premium mechanical seals and hydraulic fittings globally from India. "
    "Our products include cartridge seals, spring seals, bellow seals, and hydraulic fittings "
    "in materials like SS316, Hastelloy, and Silicon Carbide. "
    "Would you be interested in a free consultation or sample to evaluate our quality? "
    "Happy to share our product catalog — just let me know your requirements. "
    "Best regards, {contact_name}"
)

# ═══════════════════════════════════════════════════════════════
# LEAD SCORING THRESHOLDS
# ═══════════════════════════════════════════════════════════════

SCORE_THRESHOLDS = {
    "min_qualify": int(os.getenv("MIN_QUALIFY_SCORE", "40")),  # minimum score to qualify a lead
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
NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL", "info@flowlockoverseas.com")

# Current sending email (for testing)
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "info@flowlockoverseas.com")

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
