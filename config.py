"""
MicrodyneHunter v2 — Configuration
All API keys, thresholds, and settings in one place.
"""

import os
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
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Shohail Maredia")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ═══════════════════════════════════════════════════════════════
# MICRODYNE ENGINEERING BUSINESS INFO (used in emails & form fills)
# ═══════════════════════════════════════════════════════════════

MICRODYNE = {
    "company_name": "Microdyne Engineering",
    "contact_name": "Shohail Maredia",
    "contact_email": "sales@microdyneengineering.com",
    "sales_email": "sales@microdyneengineering.com",
    "website": "https://www.microdyneengineering.com",
    "phone": "+91-XXXXXXXXXX",
    "address": "Mumbai 400097, Maharashtra, India",
    "gst": "27BWWPM3354A1ZC",
    "products": [
        "Mechanical Seals (Cartridge, Single Spring, Multi Spring, Bellow)",
        "CNC Machined Components & Precision Turned Parts",
        "Pump Seals & Agitator Seals",
        "Seal Repair & Refurbishment Services",
    ],
    "materials": "SS316, SS304, Hastelloy, Silicon Carbide, Tungsten Carbide, Carbon, PTFE, Viton",
    "coverage": "Pan-India & global export — serving industrial clients worldwide",
    "usp": "Precision-engineered mechanical seals & CNC components — manufacturer-direct pricing with guaranteed quality",
    "hook": "Free consultation & sample — prove quality before any commitment",
}

# Backward-compatible alias so modules referencing ROZPER still work
ROZPER = MICRODYNE

# ═══════════════════════════════════════════════════════════════
# SCRAPING SETTINGS
# ═══════════════════════════════════════════════════════════════

DAILY_LEAD_TARGET = 1000

# Target buyer types (industrial sectors that need mechanical seals & CNC parts)
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

# Target countries (priority order — high-converting industrial markets first)
TARGET_COUNTRIES = [
    "India", "UAE", "Saudi Arabia", "US", "UK", "Germany", "Netherlands",
    "South Africa", "Nigeria", "Kenya", "Singapore", "Malaysia",
    "Turkey", "Egypt", "Brazil", "Mexico", "Italy", "France",
    "Australia", "Indonesia", "Thailand", "Vietnam", "Qatar", "Oman",
]

# Search keywords templates (combined with country)
SEARCH_KEYWORDS = [
    "mechanical seal supplier {country}",
    "CNC machining services {country}",
    "pump seal manufacturer {country}",
    "precision turned parts {country}",
    "mechanical seal distributor {country}",
    "industrial seal manufacturer {country}",
    "cartridge seal supplier {country}",
    "bellow seal manufacturer {country}",
    "seal refurbishment service {country}",
    "CNC precision components {country}",
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

DAILY_EMAIL_TARGET = 1000
EMAILS_PER_DOMAIN = 65  # safe limit per sending domain

# Sending domains (only active domains)
SENDING_DOMAINS = [
    "gmail.com",
    "microdyneengineering.com",
]

# Follow-up sequence timing (days after initial email)
FOLLOWUP_SCHEDULE = {
    1: "initial",       # Day 1: Intro + USP + free consultation
    3: "quality",       # Day 3: Quality angle — "Precision-engineered in SS316/SiC"
    7: "social_proof",  # Day 7: Social proof — "Trusted by leading plants across India"
    14: "breakup",      # Day 14: Breakup — "No pressure, offer open"
}

# ═══════════════════════════════════════════════════════════════
# FORM FILLING SETTINGS
# ═══════════════════════════════════════════════════════════════

DAILY_FORM_TARGET = 1000

# Contact data used when filling website forms
FORM_FILL_DATA = {
    'name': 'Microdyne Engineering',
    'first_name': 'Microdyne',
    'last_name': 'Engineering',
    'company': 'Microdyne Engineering',
    'email': 'sales@microdyneengineering.com',
    'phone': '+91-9082121601',
    'subject': 'Manufacturer of Mechanical Seals, CNC Components & Precision Turned Parts',
    'message': (
        'We are Microdyne Engineering, a Mumbai-based manufacturer specializing in '
        'mechanical seals, CNC machined components, and precision turned parts. '
        'Our product range includes cartridge seals, single & multi spring seals, '
        'bellow seals (rubber, metal, PTFE), pump seals, and agitator seals — '
        'manufactured in SS316, SS304, Hastelloy, Silicon Carbide, Tungsten Carbide, '
        'Carbon, PTFE, and Viton. We also offer CNC machines and precision turned parts '
        'for industrial applications. Our seal repair and refurbishment services help '
        'reduce maintenance costs significantly. '
        'Contact us at sales@microdyneengineering.com or call +91-9082121601 '
        'for a free consultation and sample.'
    ),
}

FORM_PATHS_TO_TRY = [
    "/contact", "/contact-us", "/inquiry", "/get-quote",
    "/partnership", "/partners", "/get-in-touch",
    "/request-quote", "/reach-us", "/talk-to-sales",
    "/sales", "/connect", "/enquiry",
]

FORM_MESSAGE_TEMPLATE = (
    "Hi, I'm {contact_name} from {company_name}. "
    "We are a Mumbai-based manufacturer of precision mechanical seals and CNC machined components. "
    "Our product range includes cartridge seals, spring seals, bellow seals, and custom turned parts "
    "in materials like SS316, Hastelloy, Silicon Carbide, and Tungsten Carbide. "
    "Would you be interested in a free consultation or sample to evaluate our quality? "
    "Happy to share our product catalog — just let me know your requirements. "
    "Best regards, {contact_name}"
)

# ═══════════════════════════════════════════════════════════════
# LEAD SCORING THRESHOLDS
# ═══════════════════════════════════════════════════════════════

SCORE_THRESHOLDS = {
    "min_qualify": 40,        # minimum score to qualify a lead
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

VARIANTS_PER_LEAD = 5           # number of AI-generated email variants per lead

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
