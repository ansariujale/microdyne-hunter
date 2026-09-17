"""
MicrodyneHunter v2 — Lead Qualification & Scoring Module
Uses Claude AI to score leads and tag them with metadata.
"""

import json
import logging
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from config import (
    ROZPER, SCORE_THRESHOLDS, TARGET_COUNTRIES,
)
from modules.database import update_lead, is_segment_paused
from modules.ai_client import ai_generate, is_ai_available

logger = logging.getLogger("microdynehunter.qualifier")

# ═══════════════════════════════════════════════════════════════
# AI SCORING
# ═══════════════════════════════════════════════════════════════

SCORING_PROMPT = """You are a B2B lead scoring expert for Microdyne Engineering, a Mumbai-based manufacturer offering CNC turning job work and its own line of mechanical seals.

Microdyne Engineering is looking for:
- Manufacturers who outsource CNC turning / job work (screws, nuts, sleeves, bushings, custom turned parts)
- Companies that buy precision turned components in brass, SS, or mild steel
- Buyers of mechanical seals (cartridge, spring, bellow, teflon bellow)

Target buyers: OEM manufacturers that consume turned parts or mechanical seals — pump/valve, hydraulic cylinder, compressor/blower, gearbox, electric motor, agitator/mixer and process equipment, automotive component, and machinery (packaging, textile, agricultural, food, pharma, special purpose) manufacturers.
NOT targets: other CNC job shops or turning/machining service providers — they are competitors (score 0-10), and B2B directories/marketplaces.

Score this lead from 0-100 based on how likely they are to outsource CNC turning job work to Microdyne Engineering or buy their mechanical seals:

Company: {company_name}
Domain: {company_domain}
Contact: {contact_name} ({contact_title})
Country: {country}
City: {city}
Type: {lead_type}
Description: {description}

Consider:
1. Does their business need CNC turned components, job-work capacity, or mechanical seals? (component manufacturer = high, restaurant = 0)
2. Are they a manufacturer likely to subcontract machining work when their own shop floor is at capacity?
3. Is the contact person a decision-maker (Procurement Manager, Production Manager, Plant Manager)?
4. Company size/relevance to industrial manufacturing

Return ONLY valid JSON:
{{
    "score": <0-100>,
    "reason": "<one sentence why>",
    "company_size": "<small|medium|enterprise>",
    "likely_products": ["<what Microdyne Engineering could offer them>"],
    "priority": "<high|medium|low>"
}}"""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def score_lead_with_ai(lead: dict) -> dict:
    """Score a single lead using AI (Gemini or Anthropic)."""
    if not is_ai_available():
        return score_lead_rules(lead)

    prompt = SCORING_PROMPT.format(
        company_name=lead.get("company_name", ""),
        company_domain=lead.get("company_domain", ""),
        contact_name=lead.get("contact_name", ""),
        contact_title=lead.get("contact_title", ""),
        country=lead.get("country", ""),
        city=lead.get("city", ""),
        lead_type=lead.get("lead_type", ""),
        description=lead.get("keyword_used", ""),
    )

    try:
        text = ai_generate(prompt, max_tokens=300)
        if not text:
            return score_lead_rules(lead)

        # Parse JSON from response
        import re
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            result = json.loads(match.group())
        else:
            result = json.loads(text)

        return {
            "score": max(0, min(100, int(result.get("score", 0)))),
            "score_reason": result.get("reason", ""),
            "company_size": result.get("company_size", "small"),
        }
    except json.JSONDecodeError:
        logger.warning(f"AI returned non-JSON for {lead.get('company_domain', '?')}, falling back to rules")
        return score_lead_rules(lead)
    except Exception as e:
        logger.error(f"AI scoring error: {e}")
        return score_lead_rules(lead)


def score_lead_rules(lead: dict) -> dict:
    """Rule-based fallback scoring when AI is unavailable."""
    score = 30  # base score
    reasons = []

    # Lead type scoring
    type_scores = {
        "pump_valve_manufacturer": 25, "process_equipment_manufacturer": 25,
        "hydraulic_pneumatic_manufacturer": 25, "automotive_component_manufacturer": 20,
        "machinery_equipment_manufacturer": 20, "compressor_blower_manufacturer": 20,
        "electrical_equipment_manufacturer": 15, "general_engineering": 10, "other": 0,
    }
    type_bonus = type_scores.get(lead.get("lead_type", "other"), 0)
    score += type_bonus
    if type_bonus > 0:
        reasons.append(f"Relevant lead type: {lead['lead_type']}")

    # Has email = better
    if lead.get("contact_email"):
        score += 10
        reasons.append("Has contact email")

    # Has decision-maker title
    title = (lead.get("contact_title") or "").lower()
    if any(t in title for t in ["ceo", "cto", "vp", "director", "head", "manager", "plant manager", "maintenance", "procurement", "chief engineer"]):
        score += 10
        reasons.append("Decision-maker contact")

    # Country priority (top-converting countries get bonus)
    high_priority = ["India", "UAE", "US", "Germany", "Saudi Arabia", "Singapore"]
    if lead.get("country") in high_priority:
        score += 10
        reasons.append(f"High-priority country: {lead['country']}")

    # Has website (needed for form filling)
    if lead.get("website_url"):
        score += 5

    score = max(0, min(100, score))
    return {
        "score": score,
        "score_reason": "; ".join(reasons) if reasons else "Base score",
        "company_size": "small",
    }


# ═══════════════════════════════════════════════════════════════
# BATCH QUALIFICATION
# ═══════════════════════════════════════════════════════════════

def qualify_leads(leads: list[dict], use_ai: bool = True) -> list[dict]:
    """
    Score and qualify a batch of leads.
    Returns only leads that pass the minimum score threshold.
    Skips leads from paused segments.
    """
    qualified = []
    skipped_segment = 0
    skipped_score = 0

    for lead in leads:
        # Check if segment is paused
        if is_segment_paused("country", lead.get("country", "")):
            skipped_segment += 1
            continue
        if is_segment_paused("lead_type", lead.get("lead_type", "")):
            skipped_segment += 1
            continue

        # Score the lead
        if use_ai and is_ai_available():
            scoring = score_lead_with_ai(lead)
        else:
            scoring = score_lead_rules(lead)

        lead["score"] = scoring["score"]
        lead["score_reason"] = scoring["score_reason"]
        lead["company_size"] = scoring.get("company_size", "small")

        # Apply threshold
        if lead["score"] < SCORE_THRESHOLDS["skip_below"]:
            skipped_score += 1
            continue

        if lead["score"] >= SCORE_THRESHOLDS["min_qualify"]:
            qualified.append(lead)
        else:
            skipped_score += 1

    logger.info(
        f"Qualification: {len(qualified)} qualified, "
        f"{skipped_score} low-score, {skipped_segment} paused-segment"
    )
    return qualified


def re_score_lead(lead_id: str, lead: dict) -> None:
    """Re-score an existing lead (used during weekly re-evaluation)."""
    scoring = score_lead_with_ai(lead) if is_ai_available() else score_lead_rules(lead)
    update_lead(lead_id, {
        "score": scoring["score"],
        "score_reason": scoring["score_reason"],
        "company_size": scoring.get("company_size", "small"),
    })
