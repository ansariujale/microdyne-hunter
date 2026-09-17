"""
MicrodyneHunter v2 — Email Variant Generation & Scoring Engine
Generates 5 AI email variants per lead, scores them, picks the winner.
"""

import json
import re
import logging
from typing import Optional

from config import (
    ROZPER, VARIANTS_PER_LEAD,
    SPAM_TRIGGERS, FOLLOWUP_SCHEDULE,
)
from modules.ai_client import ai_generate, is_ai_available

logger = logging.getLogger("microdynehunter.variants")

def _htmlish_to_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</?b\s*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"</?strong\s*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s

def _strip_tags(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"<[^>]+>", "", s).strip()


# ═══════════════════════════════════════════════════════════════
# STAGE CONFIGURATION
# ═══════════════════════════════════════════════════════════════

STAGE_CONFIG = {
    1: {
        "name": "initial",
        "description": "Introduce Microdyne Engineering, ask if they outsource CNC turning job work, offer a trial batch. Show you know their industry.",
        "word_limit": 100,
        "subject_hint": "mention their industry/equipment + value prop",
    },
    2: {
        "name": "quality",
        "description": "Reassurance angle — offer a small trial batch so they can judge quality and turnaround themselves. No unverified claims. Assume no reply to email #1.",
        "word_limit": 80,
        "subject_hint": "trial batch / turnaround angle",
    },
    3: {
        "name": "social_proof",
        "description": "Credibility — manufacturing mechanical seals in Mumbai since 2021, own in-house CNC turning capacity. Keep it modest and factual.",
        "word_limit": 80,
        "subject_hint": "credibility / experience angle",
    },
    4: {
        "name": "breakup",
        "description": "Breakup email — no pressure, door stays open, graceful exit.",
        "word_limit": 60,
        "subject_hint": "final / farewell angle, keep it friendly",
    },
}


# ═══════════════════════════════════════════════════════════════
# VARIANT GENERATION PROMPT
# ═══════════════════════════════════════════════════════════════

VARIANT_PROMPT = """You are a senior B2B cold email copywriter. You write like a peer, not a vendor.

ABOUT THE SENDER (Microdyne Engineering):
- Mumbai-based manufacturer, established 2021
- Own line of mechanical seals (cartridge, spring, bellow, teflon bellow) plus in-house CNC turning capacity for job work
- Job work: screws, nuts, sleeves, bushings, and custom turned parts in brass, SS, and mild steel
- Looking for manufacturers who outsource CNC turning / job work, especially when their own shop floor is at capacity
- USP: offer to quote a trial batch so they can judge quality and turnaround before committing to volume
- Do NOT invent compliance certifications, tolerance specs, or capabilities not stated here (e.g. no milling — turning only)

TARGET LEAD:
- Company: {company_name}
- Domain: {company_domain}
- Type: {lead_type}
- Country: {country}
- Email: {contact_email}

EMAIL STAGE: {stage_name} — {stage_description}

WRITING RULES:
1. Write EXACTLY {num_variants} different email variants
2. Each body must be under {word_limit} words
3. Each subject line under 50 characters
4. Each variant uses a DIFFERENT angle/hook
5. Sound human — peer-to-peer tone, casual-professional
6. Reference THEIR business or market specifically (not generic)
7. One clear, low-friction CTA per email (reply with equipment details, request a sample, ask for a quote)
8. NO greeting (no "Hi", "Dear", "Hello") — start with the hook
9. NO signature — end with the CTA
10. NO spam words: "act now", "limited time", "guaranteed", "click here"
11. NO ALL CAPS, NO exclamation marks, NO links, NO HTML
12. Subject hint: {subject_hint}

Return ONLY a valid JSON array with exactly {num_variants} objects:
[
  {{"subject": "...", "body": "...", "angle": "one-word-descriptor"}},
  ...
]"""


# ═══════════════════════════════════════════════════════════════
# GENERATE VARIANTS (single Claude call)
# ═══════════════════════════════════════════════════════════════

def generate_variants(lead: dict, sequence_stage: int = 1) -> list[dict]:
    """
    Generate N email variants for a lead using Claude Haiku.
    Returns list of dicts: [{subject, body, angle}, ...]
    Falls back to template if AI unavailable.
    """
    stage = STAGE_CONFIG.get(sequence_stage, STAGE_CONFIG[1])

    if sequence_stage == 1:
        import config as _cfg
        subject = _strip_tags(getattr(_cfg, "EMAIL_SUBJECT", ""))
        body = _htmlish_to_text(getattr(_cfg, "EMAIL_BODY", ""))
        if not subject:
            subject = "Mechanical Seals & Hydraulic Fittings — Microdyne Engineering"
        if not body:
            body = "Dear Sir/Madam,\n\nWe are Microdyne Engineering.\n\nWould you be open to a quick call?"
        return [{"subject": subject[:70], "body": body, "angle": "admin"}]

    if not is_ai_available():
        logger.info("[Variants] No AI provider configured — using template fallback")
        return _fallback_variants(lead, stage)

    prompt = VARIANT_PROMPT.format(
        company_name=lead.get("company_name", ""),
        company_domain=lead.get("company_domain", ""),
        lead_type=(lead.get("lead_type") or "industrial company").replace("_", " "),
        country=lead.get("country", ""),
        contact_email=lead.get("contact_email", ""),
        stage_name=stage["name"],
        stage_description=stage["description"],
        word_limit=stage["word_limit"],
        num_variants=VARIANTS_PER_LEAD,
        subject_hint=stage["subject_hint"],
    )

    for attempt in range(3):
        try:
            text = ai_generate(prompt, max_tokens=2000)
            if not text:
                logger.warning(f"[Variants] Attempt {attempt + 1}: empty AI response")
                continue

            # Extract JSON array from response
            variants = _parse_variants_json(text)
            if variants and len(variants) >= 1:
                logger.info(f"[Variants] Generated {len(variants)} variants for {lead.get('company_domain', '?')}")
                return variants[:VARIANTS_PER_LEAD]

            logger.warning(f"[Variants] Attempt {attempt + 1}: bad JSON, retrying")

        except json.JSONDecodeError:
            logger.warning(f"[Variants] Attempt {attempt + 1}: JSON parse error, retrying")
        except Exception as e:
            logger.error(f"[Variants] Attempt {attempt + 1}: {e}")

    logger.warning(f"[Variants] All retries failed for {lead.get('company_domain', '?')} — using fallback")
    return _fallback_variants(lead, stage)


def _parse_variants_json(text: str) -> list[dict] | None:
    """Try to parse JSON array from Claude's response."""
    # Try direct parse
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Try extracting JSON array from text
    match = re.search(r'\[[\s\S]*\]', text)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return None


def _fallback_variants(lead: dict, stage: dict) -> list[dict]:
    """Template-based fallback when AI is unavailable."""
    company = lead.get("company_name", "your company")
    country = lead.get("country", "your region")
    lead_type = (lead.get("lead_type") or "industrial").replace("_", " ")

    templates = {
        "initial": [
            {"subject": f"CNC turning job work — {company}", "body": f"We run in-house CNC turning capacity for job work — screws, nuts, sleeves, bushings, and custom turned parts. If {company} outsources any turning work in {country}, happy to quote a trial batch. What kind of components do you typically need machined?", "angle": "trial-batch"},
            {"subject": f"Turning job-work partner for {country}", "body": f"Microdyne Engineering is a Mumbai-based manufacturer with its own CNC turning capacity, alongside our mechanical seal line. If your {lead_type} operation ever needs extra turning capacity, we'd welcome the chance to quote a trial batch.", "angle": "capacity"},
        ],
        "quality": [
            {"subject": f"Trial batch — CNC turning, {company}", "body": f"Quick follow-up — happy to start with a small trial batch of turned components so you can judge our quality and turnaround yourselves before committing to any volume. Worth a try?", "angle": "trial"},
        ],
        "social_proof": [
            {"subject": f"Manufacturing since 2021 — Microdyne Engineering", "body": f"We've been manufacturing mechanical seals in Mumbai since 2021, and run our own CNC turning capacity for job work alongside it. We'd welcome the chance to become a turning job-work partner for {company}.", "angle": "credibility"},
        ],
        "breakup": [
            {"subject": f"Last note — offer stays open", "body": f"No pressure at all. If {company} ever needs CNC turning job work or mechanical seals, the offer to quote a trial batch stays open. Just reply whenever it makes sense.", "angle": "breakup"},
        ],
    }

    return templates.get(stage["name"], templates["initial"])


# ═══════════════════════════════════════════════════════════════
# VARIANT SCORING (rule-based, 0-100)
# ═══════════════════════════════════════════════════════════════

def score_variant(variant: dict, lead: dict) -> dict:
    """
    Score a single email variant across 4 dimensions (0-25 each, total 0-100).
    Returns the variant dict with score fields added.
    """
    subject = variant.get("subject", "")
    body = variant.get("body", "")
    company = lead.get("company_name", "")
    country = lead.get("country", "")
    lead_type = (lead.get("lead_type") or "").replace("_", " ")

    # ── Subject Quality (0-25) ────────────────────────────
    s_score = 0
    if 30 <= len(subject) <= 55:
        s_score += 8
    elif 20 <= len(subject) <= 60:
        s_score += 4

    if company.lower() in subject.lower() or country.lower() in subject.lower():
        s_score += 5

    if not re.search(r'[A-Z]{3,}', subject):  # no ALL CAPS words
        s_score += 4

    if not any(t in subject.lower() for t in SPAM_TRIGGERS):
        s_score += 4

    if re.search(r'\d', subject):  # has a number/specific detail
        s_score += 4

    s_score = min(25, s_score)

    # ── Personalization (0-25) ────────────────────────────
    p_score = 0
    body_lower = body.lower()

    if company.lower() in body_lower:
        p_score += 8

    if country.lower() in body_lower:
        p_score += 5

    if lead_type and lead_type.lower() in body_lower:
        p_score += 5

    # Penalize generic filler
    generic = ["your company", "your business", "your organization", "your firm"]
    if not any(g in body_lower for g in generic):
        p_score += 4

    if len(body.split()) > 20:  # has substance
        p_score += 3

    p_score = min(25, p_score)

    # ── CTA Strength (0-25) ──────────────────────────────
    c_score = 0

    if "?" in body:
        c_score += 5

    # Specific CTA keywords
    cta_specifics = ["sample", "catalogue", "equipment", "consultation", "quote",
                     "try", "test", "quick call", "reply"]
    if any(c in body_lower for c in cta_specifics):
        c_score += 8

    # Low-friction (no heavy asks)
    heavy_asks = ["schedule a demo", "book a call", "sign up", "register",
                  "fill out", "subscribe"]
    if not any(h in body_lower for h in heavy_asks):
        c_score += 5

    # CTA in last sentence
    sentences = [s.strip() for s in body.split('.') if s.strip()]
    if sentences:
        last = sentences[-1].lower()
        if any(c in last for c in ["?", "reply", "interested", "want", "send"]):
            c_score += 4

    # Single CTA (not multiple asks)
    question_count = body.count("?")
    if question_count <= 2:
        c_score += 3

    c_score = min(25, c_score)

    # ── Spam Safety (0-25) ────────────────────────────────
    sp_score = 0
    full_text = (subject + " " + body).lower()

    # No spam triggers
    spam_found = sum(1 for t in SPAM_TRIGGERS if t in full_text)
    if spam_found == 0:
        sp_score += 8
    elif spam_found <= 1:
        sp_score += 4

    # No excessive punctuation
    if not re.search(r'[!]{2,}|[?]{3,}|\.{4,}', body):
        sp_score += 4

    # No ALL CAPS words in body
    if not re.search(r'\b[A-Z]{4,}\b', body):
        sp_score += 4

    # Reasonable body length
    word_count = len(body.split())
    if 30 <= word_count <= 120:
        sp_score += 3

    # No URLs in body
    if not re.search(r'https?://', body):
        sp_score += 3

    # No HTML
    if not re.search(r'<[a-z]', body, re.IGNORECASE):
        sp_score += 3

    sp_score = min(25, sp_score)

    # ── Total ─────────────────────────────────────────────
    total = s_score + p_score + c_score + sp_score

    variant["score_subject"] = s_score
    variant["score_personalization"] = p_score
    variant["score_cta"] = c_score
    variant["score_spam_risk"] = sp_score
    variant["score_total"] = total

    return variant


def score_and_pick_winner(variants: list[dict], lead: dict) -> tuple[list[dict], dict]:
    """
    Score all variants and pick the winner.
    Returns (all_scored_variants, winner_variant).
    """
    scored = []
    for i, v in enumerate(variants, 1):
        v["variant_number"] = i
        scored_v = score_variant(v, lead)
        scored.append(scored_v)
        logger.debug(
            f"  Variant {i} ({v.get('angle', '?')}): "
            f"total={scored_v['score_total']} "
            f"[subj={scored_v['score_subject']}, pers={scored_v['score_personalization']}, "
            f"cta={scored_v['score_cta']}, spam={scored_v['score_spam_risk']}]"
        )

    # Pick highest scoring variant
    winner = max(scored, key=lambda v: v["score_total"])
    winner["is_winner"] = True

    domain = lead.get("company_domain", "?")
    logger.info(
        f"[Variants] Winner for {domain}: variant #{winner['variant_number']} "
        f"({winner.get('angle', '?')}) — score {winner['score_total']}/100 "
        f"— \"{winner['subject'][:50]}\""
    )

    return scored, winner


# ═══════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def generate_and_pick_winner(lead: dict, sequence_stage: int = 1) -> tuple[list[dict], dict]:
    """
    Full pipeline: generate variants → score → pick winner.
    Returns (all_variants_scored, winner).
    Never crashes — always returns at least a fallback template.
    """
    domain = lead.get("company_domain", "?")
    logger.info(f"[Variants] Generating {VARIANTS_PER_LEAD} variants for {domain} (stage {sequence_stage})")

    try:
        variants = generate_variants(lead, sequence_stage)
        if not variants:
            logger.warning(f"[Variants] No variants generated for {domain} — using fallback")
            stage = STAGE_CONFIG.get(sequence_stage, STAGE_CONFIG[1])
            variants = _fallback_variants(lead, stage)

        scored, winner = score_and_pick_winner(variants, lead)
        return scored, winner

    except Exception as e:
        logger.error(f"[Variants] Critical error for {domain}: {e}")
        # Ultimate fallback — simple template
        fallback = {
            "subject": f"Mechanical seals for {lead.get('country', 'your region')}",
            "body": f"Microdyne Engineering manufactures mechanical seals and CNC precision components for industrial applications. "
                    f"Happy to send a free sample for {lead.get('company_name', 'your company')} "
                    f"to evaluate our quality. Interested?",
            "angle": "fallback",
            "variant_number": 1,
            "score_total": 50,
            "score_subject": 12,
            "score_personalization": 12,
            "score_cta": 13,
            "score_spam_risk": 13,
            "is_winner": True,
        }
        return [fallback], fallback
