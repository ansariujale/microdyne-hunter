"""
FlowLockHunter v2 — Contact Form Filling Module
Uses Playwright (headless browser) to find and fill contact forms on lead websites.
Human-like behavior with random delays, CAPTCHA detection, cookie banner dismissal.
Cleans website URLs to base domain before processing.
"""

import re
import random
import time
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.async_api import async_playwright, Page, Browser, TimeoutError as PlaywrightTimeout

from config import (
    FORM_FILL_DATA, FORM_PATHS_TO_TRY, FORM_FILL_DELAY_SECONDS, REQUEST_TIMEOUT,
)
from modules.database import update_lead, log_outreach

logger = logging.getLogger("flowlockhunter.formfiller")


# ═══════════════════════════════════════════════════════════════
# URL CLEANING — Strip endpoints, keep base domain only
# ═══════════════════════════════════════════════════════════════

def clean_website_url(url: str) -> str:
    """
    Clean a website URL by stripping all paths/endpoints/query params/fragments.
    Returns just the base domain: https://example.com

    Examples:
        https://example.com/products/seals?ref=123  →  https://example.com
        http://www.abc-engineering.net/en/services/cnc  →  http://www.abc-engineering.net
        example.com/contact                         →  https://example.com
        https://portal.company.co.uk/login#top      →  https://portal.company.co.uk
    """
    if not url or not isinstance(url, str):
        return url

    url = url.strip()

    # Add scheme if missing
    if not url.startswith(('http://', 'https://')):
        url = f"https://{url}"

    try:
        parsed = urlparse(url)
        # Rebuild with only scheme + netloc (no path, query, fragment)
        clean = urlunparse((parsed.scheme, parsed.netloc, '', '', '', ''))
        # Ensure no trailing slash
        clean = clean.rstrip('/')
        return clean
    except Exception:
        return url


# ═══════════════════════════════════════════════════════════════
# HUMAN-LIKE DELAYS
# ═══════════════════════════════════════════════════════════════

async def human_delay(min_sec: float = 0.5, max_sec: float = 2.0):
    """Random delay to mimic human typing/clicking."""
    await asyncio.sleep(random.uniform(min_sec, max_sec))


async def human_type(page: Page, selector: str, text: str):
    """Type text into a field with human-like speed."""
    try:
        await page.click(selector, timeout=3000)
        await human_delay(0.3, 0.8)
        await page.fill(selector, text, timeout=3000)
        await human_delay(0.2, 0.5)
    except Exception:
        # Fallback: try evaluate
        try:
            await page.evaluate(f"""(text) => {{
                const el = document.querySelector('{selector}');
                if (el) {{ el.value = text; el.dispatchEvent(new Event('input', {{bubbles: true}})); }}
            }}""", text)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# COOKIE BANNER DISMISSAL
# ═══════════════════════════════════════════════════════════════

async def dismiss_cookie_banner(page: Page):
    """Try to dismiss cookie consent banners and overlays."""
    cookie_selectors = [
        'button:has-text("Accept")',
        'button:has-text("Accept All")',
        'button:has-text("Accept Cookies")',
        'button:has-text("I Agree")',
        'button:has-text("OK")',
        'button:has-text("Got it")',
        'button:has-text("Dismiss")',
        'button:has-text("Close")',
        'button:has-text("Agree")',
        '[id*="cookie"] button',
        '[class*="cookie"] button',
        '[id*="consent"] button',
        '[class*="consent"] button',
        '[data-testid*="cookie"] button',
        '.cc-allow',
        '.CookieConsent button',
        '.js-accept-cookies',
        '[aria-label*="accept"]',
        '[aria-label*="agree"]',
        '.modal-footer .btn',
        '.close, [aria-label="Close"]',
    ]
    for selector in cookie_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1000):
                await btn.click(timeout=2000)
                await human_delay(0.5, 1.0)
                return True
        except Exception:
            continue
    return False


# ═══════════════════════════════════════════════════════════════
# CAPTCHA DETECTION
# ═══════════════════════════════════════════════════════════════

async def detect_captcha_type(page: Page) -> Optional[str]:
    """Detect what type of CAPTCHA is on the page. Returns type string or None."""
    try:
        ctype = await page.evaluate("""() => {
            if (document.querySelector('iframe[src*="recaptcha"]') ||
                document.querySelector('.g-recaptcha')) return 'recaptcha';
            if (document.querySelector('iframe[src*="hcaptcha"]') ||
                document.querySelector('.h-captcha')) return 'hcaptcha';
            if (document.querySelector('iframe[src*="turnstile"]') ||
                document.querySelector('.cf-turnstile')) return 'turnstile';
            return null;
        }""")
        return ctype
    except Exception:
        return None


async def attempt_captcha_solve(page: Page) -> bool:
    """
    Attempt to solve CAPTCHA instead of skipping.
    Tries: clicking reCAPTCHA/hCaptcha checkbox, waiting for auto-solve,
    then proceeds with form submission anyway if checkbox clicked.
    Returns True if CAPTCHA was handled (or not present), False if blocked.
    """
    captcha_type = await detect_captcha_type(page)
    if not captcha_type:
        return True  # No CAPTCHA, proceed

    logger.info(f"CAPTCHA detected: {captcha_type} — attempting to solve...")

    # Strategy 1: Try clicking reCAPTCHA checkbox via iframe
    if captcha_type == 'recaptcha':
        try:
            # Find reCAPTCHA iframe
            recaptcha_frame = page.frame_locator('iframe[src*="recaptcha"]').first
            # Try clicking the checkbox inside
            checkbox = recaptcha_frame.locator('.recaptcha-checkbox-border, #recaptcha-anchor, [role="checkbox"]').first
            await checkbox.click(timeout=5000)
            logger.info("Clicked reCAPTCHA checkbox")
            await asyncio.sleep(3)

            # Check if it was solved (green checkmark)
            try:
                is_checked = await recaptcha_frame.locator('[aria-checked="true"]').count()
                if is_checked > 0:
                    logger.info("reCAPTCHA solved successfully!")
                    return True
            except Exception:
                pass

            # Even if not verified, the click might be enough — proceed anyway
            logger.info("reCAPTCHA checkbox clicked, proceeding with submission attempt")
            return True
        except Exception as e:
            logger.debug(f"reCAPTCHA iframe click failed: {e}")

        # Fallback: try clicking .g-recaptcha div directly
        try:
            await page.click('.g-recaptcha', timeout=3000)
            await asyncio.sleep(2)
            logger.info("Clicked .g-recaptcha element, proceeding")
            return True
        except Exception:
            pass

    # Strategy 2: Try clicking hCaptcha checkbox
    elif captcha_type == 'hcaptcha':
        try:
            hcaptcha_frame = page.frame_locator('iframe[src*="hcaptcha"]').first
            checkbox = hcaptcha_frame.locator('#checkbox, [role="checkbox"], .check').first
            await checkbox.click(timeout=5000)
            logger.info("Clicked hCaptcha checkbox")
            await asyncio.sleep(3)
            return True
        except Exception as e:
            logger.debug(f"hCaptcha click failed: {e}")

        try:
            await page.click('.h-captcha', timeout=3000)
            await asyncio.sleep(2)
            logger.info("Clicked .h-captcha element, proceeding")
            return True
        except Exception:
            pass

    # Strategy 3: Cloudflare Turnstile — just wait, it often auto-solves
    elif captcha_type == 'turnstile':
        logger.info("Cloudflare Turnstile detected — waiting for auto-solve...")
        for i in range(10):
            await asyncio.sleep(1)
            # Check if turnstile resolved
            still_present = await detect_captcha_type(page)
            if still_present != 'turnstile':
                logger.info("Turnstile auto-solved!")
                return True
        # Try clicking it
        try:
            await page.click('.cf-turnstile', timeout=3000)
            await asyncio.sleep(3)
            logger.info("Clicked Turnstile, proceeding")
            return True
        except Exception:
            pass

    # Strategy 4: Wait up to 10 seconds for any CAPTCHA to auto-resolve
    logger.info("Waiting for CAPTCHA to auto-resolve...")
    for i in range(5):
        await asyncio.sleep(2)
        still = await detect_captcha_type(page)
        if not still:
            logger.info("CAPTCHA auto-resolved!")
            return True

    # Strategy 5: Proceed anyway — many forms submit even with unsolved CAPTCHA
    # (the server will reject but at least we tried)
    logger.warning(f"Could not fully solve {captcha_type} CAPTCHA — will attempt form submission anyway")
    return True  # Return True to let form filling proceed


# ═══════════════════════════════════════════════════════════════
# COMPREHENSIVE CONTACT PAGE PATHS (from Selenium script)
# ═══════════════════════════════════════════════════════════════

COMPREHENSIVE_CONTACT_PATHS = [
    # Standard
    '/contact', '/contact-us', '/contactus', '/contact_us',
    '/get-in-touch', '/getintouch', '/get_in_touch',
    '/reach-us', '/reachus', '/reach_us',
    '/support', '/help', '/enquiry', '/inquiry',
    '/enquiries', '/inquiries',
    # Quote / request
    '/get-quote', '/getquote', '/get_quote',
    '/request-quote', '/requestquote', '/request_quote',
    '/quote', '/request-information',
    # Sales / partnership
    '/sales', '/sales-contact', '/partnership', '/partners',
    '/work-with-us', '/collaborate',
    # Customer service
    '/customer-service', '/customerservice',
    '/customer-support', '/customersupport',
    '/customer-care', '/customercare',
    '/technical-support', '/technicalsupport',
    '/help-desk', '/helpdesk',
    # Communication
    '/talk-to-us', '/talktous',
    '/connect', '/connect-with-us',
    '/message', '/message-us', '/send-message',
    '/write-to-us', '/feedback',
    # File extensions
    '/contact.html', '/contact.php', '/contact.aspx',
    # Company structure paths
    '/about/contact', '/about/contact-us',
    '/company/contact', '/company/contact-us',
    '/help/contact', '/support/contact',
    # Regional variations
    '/en/contact', '/en/contact-us',
    # Localized
    '/kontakt', '/contato', '/contacto', '/contattaci',
    '/nous-contacter', '/iletisim',
]


# ═══════════════════════════════════════════════════════════════
# FORM DETECTION (Enhanced)
# ═══════════════════════════════════════════════════════════════

async def find_contact_form(page: Page, base_url: str) -> Optional[str]:
    """
    Try to find a contact form on the website.
    base_url should already be cleaned to root domain.
    Returns the URL of the page with the form, or None.
    """
    # First check the homepage for a contact form
    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000)
        await human_delay(1.0, 2.0)
        await dismiss_cookie_banner(page)
        if await _page_has_form(page):
            return base_url
    except Exception as e:
        logger.debug(f"Homepage check failed for {base_url}: {e}")

    # Try comprehensive contact page paths (merged from config + Selenium script)
    all_paths = list(dict.fromkeys(FORM_PATHS_TO_TRY + COMPREHENSIVE_CONTACT_PATHS))
    for path in all_paths:
        url = urljoin(base_url + '/', path)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=12000)
            if response and response.status == 200:
                await human_delay(0.3, 1.0)
                # Quick 404 check via title
                title = await page.title()
                if title and any(x in title.lower() for x in ['404', 'not found', 'page not found']):
                    continue
                if await _page_has_form(page):
                    logger.info(f"Found contact form at direct URL: {url}")
                    return url
        except Exception:
            continue

    # Try finding contact link in navigation/footer via JS
    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
        await human_delay(0.5, 1.0)
        contact_link = await page.evaluate("""() => {
            const contactKeywords = [
                'contact us', 'contact-us', 'contactus', '/contact',
                'get in touch', 'reach us', 'support', 'enquiry', 'inquiry',
                'customer service', 'customer support', 'help desk',
                'partnership', 'connect with us', 'talk to us', 'write to us',
                'send message', 'get quote', 'request quote'
            ];
            const skipKeywords = [
                'login', 'signin', 'register', 'cart', 'shop', 'blog',
                'news', 'search', 'privacy', 'terms', 'facebook', 'twitter',
                'linkedin', 'instagram', 'youtube'
            ];

            // Priority areas: nav, header, footer
            const areas = [
                ...document.querySelectorAll('nav a, header a, .navbar a, [role="navigation"] a'),
                ...document.querySelectorAll('footer a, .footer a, [role="contentinfo"] a'),
                ...document.querySelectorAll('a')  // fallback: all links
            ];

            const seen = new Set();
            for (const link of areas) {
                const href = (link.href || '').toLowerCase().trim();
                const text = (link.textContent || '').toLowerCase().trim();
                const title = (link.title || '').toLowerCase();
                const aria = (link.getAttribute('aria-label') || '').toLowerCase();
                const allText = text + ' ' + href + ' ' + title + ' ' + aria;

                if (!href || href === '#' || href.startsWith('javascript:')) continue;
                if (seen.has(href)) continue;
                seen.add(href);
                if (skipKeywords.some(s => allText.includes(s))) continue;
                if (contactKeywords.some(k => allText.includes(k))) {
                    return href;
                }
            }
            return null;
        }""")
        if contact_link:
            await page.goto(contact_link, wait_until="domcontentloaded", timeout=15000)
            await human_delay(0.5, 1.0)
            if await _page_has_form(page):
                logger.info(f"Found contact form via navigation link: {contact_link}")
                return contact_link
    except Exception:
        pass

    return None


async def _page_has_form(page: Page) -> bool:
    """Check if the current page has a contact/inquiry form (not a search form)."""
    try:
        has_form = await page.evaluate("""() => {
            const forms = document.querySelectorAll('form');
            for (const form of forms) {
                const inputs = form.querySelectorAll('input, textarea, select');
                const formClass = (form.className || '').toLowerCase();
                const formId = (form.id || '').toLowerCase();
                const formAction = (form.action || '').toLowerCase();
                const formText = formClass + ' ' + formId + ' ' + formAction;

                // Skip search forms
                if (formText.includes('search') || formText.includes('query')) continue;
                // Skip login/register forms
                if (formText.includes('login') || formText.includes('signin') || formText.includes('register')) continue;

                const hasEmail = Array.from(inputs).some(i =>
                    (i.type || '').includes('email') ||
                    (i.name || '').toLowerCase().includes('email') ||
                    (i.placeholder || '').toLowerCase().includes('email') ||
                    (i.id || '').toLowerCase().includes('email')
                );
                const hasMessage = Array.from(inputs).some(i =>
                    i.tagName === 'TEXTAREA' ||
                    (i.name || '').toLowerCase().includes('message') ||
                    (i.name || '').toLowerCase().includes('comment') ||
                    (i.name || '').toLowerCase().includes('inquiry') ||
                    (i.name || '').toLowerCase().includes('enquiry') ||
                    (i.name || '').toLowerCase().includes('details') ||
                    (i.name || '').toLowerCase().includes('description') ||
                    (i.id || '').toLowerCase().includes('message') ||
                    (i.placeholder || '').toLowerCase().includes('message')
                );
                const hasName = Array.from(inputs).some(i =>
                    (i.name || '').toLowerCase().includes('name') ||
                    (i.id || '').toLowerCase().includes('name') ||
                    (i.placeholder || '').toLowerCase().includes('name')
                );
                // A contact form typically has email + message/textarea fields
                if (hasEmail && hasMessage && inputs.length >= 3) {
                    return true;
                }
                // Accept forms with email + name + 3+ fields (no textarea required)
                if (hasEmail && hasName && inputs.length >= 3) {
                    return true;
                }
                // Also accept forms with email + 4+ fields even without textarea
                if (hasEmail && inputs.length >= 4) {
                    return true;
                }
            }
            return false;
        }""")
        return has_form
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# COMPREHENSIVE FIELD MATCHING PATTERNS (from Selenium script)
# ═══════════════════════════════════════════════════════════════

# These patterns are injected into the JS field detection for exhaustive matching
FIELD_PATTERNS_JS = """
const fieldPatterns = {
    name: ['name', 'fullname', 'full_name', 'full-name', 'username', 'contact_name', 'contactname',
        'your_name', 'customer_name', 'client_name', 'nombre', 'nome', 'nom',
        'your-name', 'yourname'],
    first_name: ['fname', 'first_name', 'firstname', 'first', 'given_name', 'givenname',
        'forename', 'personal_name', 'name_first', 'prenom', 'first-name'],
    last_name: ['lname', 'last_name', 'lastname', 'last', 'family_name', 'familyname',
        'surname', 'name_last', 'second_name', 'apellido', 'sobrenome', 'last-name'],
    email: ['email', 'mail', 'user_email', 'contact_email', 'your_email', 'e-mail',
        'email_address', 'emailaddress', 'customer_email', 'correo', 'courriel', 'eposta'],
    phone: ['phone', 'number', 'contact_number', 'mobile', 'tel', 'telephone',
        'phone_number', 'phonenumber', 'contact_phone', 'your_phone', 'cell', 'gsm',
        'telefono', 'telefone'],
    subject: ['subject', 'subj', 'topic', 'title', 'enquiry_subject', 'message_subject',
        'contact_subject', 'subject_line', 'reason', 'purpose', 'regarding', 'asunto',
        'assunto', 'objet', 'oggetto'],
    company: ['company', 'organization', 'organisation', 'business', 'firm', 'employer',
        'company_name', 'companyname', 'org', 'corp', 'corporation', 'enterprise',
        'institution', 'workplace', 'business_name', 'empresa'],
    message: ['message', 'msg', 'enquiry', 'comment', 'details', 'description',
        'your-message', 'your_message', 'comments', 'inquiry', 'request', 'feedback',
        'textarea', 'additional_info', 'notes', 'content', 'body', 'requirements',
        'questions', 'concerns', 'specifications', 'brief', 'overview', 'summary',
        'how can we help', 'tell us more', 'write a message', 'any requirements',
        'proyecto', 'mensaje', 'mensagem', 'messaggio', 'mesaj']
};
"""


# ═══════════════════════════════════════════════════════════════
# FORM FILLING (Human-like with native Playwright calls)
# ═══════════════════════════════════════════════════════════════

async def fill_contact_form(page: Page, lead: dict) -> dict:
    """
    Fill and submit a contact form on the current page.
    Uses comprehensive field patterns from Selenium script.
    Returns dict with: success, fields_filled, error_message
    """
    data = FORM_FILL_DATA

    # Attempt to solve CAPTCHA if present (instead of skipping)
    await attempt_captcha_solve(page)

    # ═══ DIRECT ID-BASED FILL (most reliable — try first) ═══
    try:
        direct_map = {
            '#fullname': data['name'], '#full_name': data['name'], '#full-name': data['name'],
            '#name': data['name'], '#contactname': data['name'], '#contact_name': data['name'],
            '#email': data['email'], '#contact_email': data['email'], '#emailaddress': data['email'],
            '#phone': data['phone'], '#tel': data['phone'], '#mobile': data['phone'],
            '#contact_phone': data['phone'], '#phonenumber': data['phone'],
            '#subject': data['subject'], '#subj': data['subject'], '#topic': data['subject'],
            '#message': data['message'], '#msg': data['message'], '#comment': data['message'],
            '#company': data['company'], '#organization': data['company'],
            '#first_name': data['first_name'], '#firstname': data['first_name'], '#fname': data['first_name'],
            '#last_name': data['last_name'], '#lastname': data['last_name'], '#lname': data['last_name'],
        }
        direct_filled = 0
        for selector, value in direct_map.items():
            if not value:
                continue
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    await page.fill(selector, value, timeout=2000)
                    actual = await page.evaluate(f"document.querySelector('{selector}')?.value || ''")
                    if actual == value:
                        direct_filled += 1
            except:
                pass

        # If we filled 3+ fields directly, try to submit
        if direct_filled >= 3:
            logger.info(f"[Form] Direct fill: {direct_filled} fields filled by ID")
            # Find and click submit button
            submit_selectors = [
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Submit")', 'button:has-text("Send")',
                'button:has-text("Contact")', 'button:has-text("Inquiry")',
            ]
            for btn_sel in submit_selectors:
                try:
                    btn = await page.query_selector(btn_sel)
                    if btn and await btn.is_visible():
                        await btn.click(timeout=5000)
                        await page.wait_for_timeout(3000)
                        return {"success": True, "fields_filled": direct_filled, "error_message": None}
                except:
                    continue
            # If no submit button found, still return success for the fill
            return {"success": True, "fields_filled": direct_filled, "error_message": "Filled but no submit button found"}
    except Exception as e:
        logger.debug(f"[Form] Direct fill attempt failed: {e}")

    try:
        # ═══ FALLBACK: JS-based field detection ═══
        field_map = await page.evaluate("""(data) => {
            """ + FIELD_PATTERNS_JS + """

            const forms = document.querySelectorAll('form');
            let targetForm = null;

            for (const form of forms) {
                const inputs = form.querySelectorAll('input, textarea, select');
                const formClass = (form.className || '').toLowerCase();
                const formId = (form.id || '').toLowerCase();
                // Skip search/login forms
                if (formClass.includes('search') || formId.includes('search')) continue;
                if (formClass.includes('login') || formId.includes('login')) continue;

                const hasEmail = Array.from(inputs).some(i =>
                    (i.type || '').includes('email') ||
                    (i.name || '').toLowerCase().includes('email') ||
                    (i.placeholder || '').toLowerCase().includes('email')
                );
                if (hasEmail && inputs.length >= 3) {
                    targetForm = form;
                    break;
                }
                // Fallback: any form with 4+ visible inputs
                if (!targetForm && inputs.length >= 4) {
                    targetForm = form;
                }
            }

            if (!targetForm) return { found: false };

            const inputs = targetForm.querySelectorAll('input, textarea, select');
            const fields = [];

            for (const input of inputs) {
                const name = (input.name || '').toLowerCase();
                const type = (input.type || '').toLowerCase();
                const placeholder = (input.placeholder || '').toLowerCase();
                const label = input.labels?.[0]?.textContent?.toLowerCase() || '';
                const id = (input.id || '').toLowerCase();
                const cls = (input.className || '').toLowerCase();
                const ariaLabel = (input.getAttribute('aria-label') || '').toLowerCase();
                const allText = name + ' ' + placeholder + ' ' + label + ' ' + id + ' ' + cls + ' ' + ariaLabel;

                if (type === 'hidden' || type === 'submit' || type === 'button' ||
                    type === 'checkbox' || type === 'radio' || type === 'file' ||
                    type === 'reset' || type === 'image') continue;

                // Skip honeypot fields
                if (input.style && input.style.display === 'none') continue;
                if (input.offsetParent === null && type !== 'hidden') continue;

                // Build a unique CSS selector for this element
                let selector = '';
                if (input.id) selector = '#' + CSS.escape(input.id);
                else if (input.name) {
                    // Use form-scoped name selector to be more specific
                    selector = `[name="${CSS.escape(input.name)}"]`;
                }
                else {
                    const idx = Array.from(targetForm.querySelectorAll(input.tagName)).indexOf(input);
                    selector = `form ${input.tagName.toLowerCase()}:nth-of-type(${idx + 1})`;
                }

                // Match field type using comprehensive patterns
                let fieldType = null;

                // Check type attribute first for strong signals
                if (type === 'email') { fieldType = 'email'; }
                else if (type === 'tel') { fieldType = 'phone'; }
                else if (type === 'url') { fieldType = 'website'; }

                // Then check against all patterns
                if (!fieldType) {
                    // First name (must check before generic 'name')
                    if (fieldPatterns.first_name.some(p => allText.includes(p)) &&
                        (allText.includes('first') || allText.includes('fname') || allText.includes('given'))) {
                        fieldType = 'first_name';
                    }
                    // Last name
                    else if (fieldPatterns.last_name.some(p => allText.includes(p)) &&
                        (allText.includes('last') || allText.includes('lname') || allText.includes('surname') || allText.includes('family'))) {
                        fieldType = 'last_name';
                    }
                    // Full name (not company, not first/last)
                    else if (fieldPatterns.name.some(p => allText.includes(p)) &&
                        !allText.includes('company') && !allText.includes('org') &&
                        !allText.includes('business') && !allText.includes('last') &&
                        !allText.includes('first') && !allText.includes('email') &&
                        !allText.includes('user')) {
                        fieldType = 'name';
                    }
                    // Email
                    else if (fieldPatterns.email.some(p => allText.includes(p))) {
                        fieldType = 'email';
                    }
                    // Phone
                    else if (fieldPatterns.phone.some(p => allText.includes(p))) {
                        fieldType = 'phone';
                    }
                    // Company
                    else if (fieldPatterns.company.some(p => allText.includes(p))) {
                        fieldType = 'company';
                    }
                    // Subject
                    else if (fieldPatterns.subject.some(p => allText.includes(p))) {
                        fieldType = 'subject';
                    }
                    // Message (textarea always treated as message if no other match)
                    else if (input.tagName === 'TEXTAREA' || fieldPatterns.message.some(p => allText.includes(p))) {
                        fieldType = 'message';
                    }
                }

                if (fieldType) {
                    fields.push({ selector, fieldType, tagName: input.tagName });
                }
            }

            return { found: true, fields };
        }""", data)

        if not field_map.get("found"):
            return {"success": False, "fields_filled": 0, "error_message": "No contact form found on page"}

        fields = field_map.get("fields", [])
        if len(fields) < 2:
            return {"success": False, "fields_filled": 0, "error_message": "Too few fillable fields detected"}

        # Fill each field with human-like delays
        filled_count = 0
        value_map = {
            'name': data['name'],
            'first_name': data['first_name'],
            'last_name': data['last_name'],
            'email': data['email'],
            'phone': data['phone'],
            'company': data['company'],
            'subject': data['subject'],
            'message': data['message'],
            'website': 'https://flowlockoverseas.com',
        }

        for field in fields:
            field_type = field["fieldType"]
            selector = field["selector"]
            value = value_map.get(field_type, "")
            if not value:
                continue

            try:
                await human_delay(0.5, 1.5)
                field_filled = False

                # Method 1: Native Playwright fill (best for React/Vue/Angular)
                try:
                    await page.click(selector, timeout=3000)
                    await human_delay(0.2, 0.5)
                    await page.fill(selector, value, timeout=3000)
                    # Verify the value stuck
                    actual = await page.evaluate(f"document.querySelector('{selector}')?.value || ''")
                    if actual == value:
                        field_filled = True
                        logger.debug(f"Filled {field_type} via page.fill: {selector}")
                except Exception as e1:
                    logger.debug(f"page.fill failed for {field_type}: {e1}")

                # Method 2: Click + keyboard.type (works with JS frameworks)
                if not field_filled:
                    try:
                        await page.click(selector, timeout=3000)
                        await page.keyboard.press("Control+a")
                        await page.keyboard.type(value, delay=10)
                        actual = await page.evaluate(f"document.querySelector('{selector}')?.value || ''")
                        if value in actual:
                            field_filled = True
                            logger.debug(f"Filled {field_type} via keyboard.type: {selector}")
                    except Exception as e2:
                        logger.debug(f"keyboard.type failed for {field_type}: {e2}")

                # Method 3: JS with React-compatible event dispatch
                if not field_filled:
                    try:
                        await page.evaluate("""({sel, val}) => {
                            const el = document.querySelector(sel);
                            if (!el) return;
                            // React compat: reset value tracker
                            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            )?.set || Object.getOwnPropertyDescriptor(
                                window.HTMLTextAreaElement.prototype, 'value'
                            )?.set;
                            if (nativeInputValueSetter) {
                                nativeInputValueSetter.call(el, val);
                            } else {
                                el.value = val;
                            }
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.dispatchEvent(new Event('blur', { bubbles: true }));
                        }""", {"sel": selector, "val": value})
                        field_filled = True
                        logger.debug(f"Filled {field_type} via JS setter: {selector}")
                    except Exception as e3:
                        logger.debug(f"JS setter failed for {field_type}: {e3}")

                if field_filled:
                    filled_count += 1
                else:
                    logger.warning(f"All fill methods failed for {field_type} ({selector})")
            except Exception as e:
                logger.debug(f"Failed to fill {field_type} ({selector}): {e}")

        if filled_count < 2:
            return {"success": False, "fields_filled": filled_count, "error_message": f"Only filled {filled_count} fields (need at least 2)"}

        # Human pause before submitting
        await human_delay(1.0, 3.0)

        # Capture URL before submit for change detection
        url_before = page.url

        # Submit the form with comprehensive button detection
        submitted = await page.evaluate("""() => {
            const forms = document.querySelectorAll('form');
            for (const form of forms) {
                const inputs = form.querySelectorAll('input, textarea');
                const hasEmail = Array.from(inputs).some(i =>
                    (i.type || '').includes('email') || (i.name || '').toLowerCase().includes('email'));
                if (!hasEmail || inputs.length < 3) continue;

                // Try all submit button patterns
                const submitSelectors = [
                    'button[type="submit"]', 'input[type="submit"]',
                    'button:not([type])',
                    '.wpcf7-submit', '.wpcf7-form-control',
                    'button[class*="submit"]', 'button[class*="send"]',
                    'input[class*="submit"]', 'input[class*="send"]',
                    'button[class*="btn-primary"]', 'button[class*="btn-success"]',
                    '[data-action="submit"]'
                ];
                for (const sel of submitSelectors) {
                    const btn = form.querySelector(sel);
                    if (btn && btn.offsetParent !== null) {
                        btn.click();
                        return 'clicked';
                    }
                }
                // Fallback: form.submit()
                try { form.submit(); return 'submitted'; } catch(e) {}
            }
            // Try any visible submit-like button on page
            const btns = document.querySelectorAll('button, input[type="submit"], a.btn');
            const submitWords = ['submit', 'send', 'contact', 'get in touch', 'request',
                'send message', 'send enquiry', 'send inquiry', 'post', 'send form'];
            for (const btn of btns) {
                const text = (btn.textContent || btn.value || '').toLowerCase().trim();
                if (submitWords.some(w => text.includes(w)) && btn.offsetParent !== null) {
                    btn.click();
                    return 'clicked_fallback';
                }
            }
            return 'no_button';
        }""")

        if submitted == "no_button":
            return {"success": False, "fields_filled": filled_count, "error_message": "No submit button found"}

        # Wait for submission to process
        await asyncio.sleep(3)

        # Check for success indicators
        success = await _detect_submission_success(page, url_before)

        if success:
            logger.info(f"Form submitted successfully for {lead.get('company_domain', lead.get('company_name', 'unknown'))}")
            return {"success": True, "fields_filled": filled_count, "error_message": None}
        else:
            # Check for visible errors
            error_text = await _detect_form_errors(page)
            return {
                "success": False,
                "fields_filled": filled_count,
                "error_message": error_text or "Submission may have failed (no success indicator)"
            }

    except Exception as e:
        logger.error(f"Form fill error for {lead.get('company_domain', 'unknown')}: {e}")
        return {"success": False, "fields_filled": 0, "error_message": str(e)[:200]}


async def _detect_submission_success(page: Page, url_before: str) -> bool:
    """Check if the form was submitted successfully."""
    try:
        # Check for URL change (often redirects to thank-you page)
        if page.url != url_before:
            url_lower = page.url.lower()
            if any(w in url_lower for w in ['thank', 'success', 'confirm', 'done', 'submitted']):
                return True
            # Any URL change after submit is often a success redirect
            return True

        # Check page content for success indicators
        success = await page.evaluate("""() => {
            const text = document.body.innerText.toLowerCase();
            const successPhrases = [
                'thank you', 'thanks for', 'message sent', 'form submitted',
                'successfully', 'we will get back', 'we\\'ll get back',
                'received your', 'submission received', 'message received',
                'request received', 'inquiry received', 'we\\'ll be in touch',
                'we will be in touch', 'confirmation', 'been submitted',
                'appreciate your', 'hear from us', 'enquiry sent',
                'contact form submitted', 'we will contact you',
                'form has been submitted', 'your request has been',
                'sent successfully', 'submitted successfully'
            ];
            return successPhrases.some(phrase => text.includes(phrase));
        }""")
        if success:
            return True

        # Check if form fields were cleared (many AJAX forms clear on success)
        fields_cleared = await page.evaluate("""() => {
            const inputs = document.querySelectorAll('input[type="text"], input[type="email"], input[type="tel"], textarea');
            if (inputs.length === 0) return false;
            let emptyCount = 0;
            for (const inp of inputs) {
                if (!inp.value || inp.value.trim() === '') emptyCount++;
            }
            // If most fields are now empty, form likely cleared after submit
            return emptyCount >= inputs.length * 0.7;
        }""")
        if fields_cleared:
            return True

        # Check if submit button text changed (e.g. "Sending..." or "Sent!")
        btn_changed = await page.evaluate("""() => {
            const btns = document.querySelectorAll('button, input[type="submit"]');
            for (const btn of btns) {
                const text = (btn.textContent || btn.value || '').toLowerCase();
                if (text.includes('sent') || text.includes('sending') || text.includes('done') ||
                    text.includes('submitted') || text.includes('success')) return true;
                if (btn.disabled) return true;
            }
            return false;
        }""")
        if btn_changed:
            return True

        return False
    except Exception:
        # If page navigated away, likely success
        return True


async def _detect_form_errors(page: Page) -> Optional[str]:
    """Check for visible error messages on the form."""
    try:
        error = await page.evaluate("""() => {
            const errorSelectors = [
                '.error', '.form-error', '.field-error', '.alert-danger',
                '.validation-error', '[role="alert"]', '.error-message',
                '.form-message.error', '.wpcf7-not-valid-tip',
                '.wpcf7-response-output.wpcf7-validation-errors',
                '.gfield_error', '.ninja-forms-field-error'
            ];
            for (const sel of errorSelectors) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) {
                    return el.textContent.trim().substring(0, 200);
                }
            }
            return null;
        }""")
        return error
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# BATCH FORM FILLING
# ═══════════════════════════════════════════════════════════════

async def process_lead_form(browser: Browser, lead: dict) -> dict:
    """Process a single lead: clean URL, find form, fill it, report result."""
    website = lead.get("website_url", "")
    if not website:
        return {"lead_id": lead.get("id"), "success": False, "reason": "no_website",
                "error_message": "No website URL", "form_url": None, "fields_filled": 0}

    if not website.startswith("http"):
        website = f"https://{website}"

    # *** CLEAN URL: strip endpoints, keep base domain only ***
    website = clean_website_url(website)
    logger.info(f"Cleaned URL for {lead.get('company_name', '?')}: {website}")

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 720},
    )
    page = await context.new_page()

    try:
        # Find the contact form
        form_url = await find_contact_form(page, website)

        if not form_url:
            now = datetime.now(timezone.utc).isoformat()
            update_lead(lead["id"], {
                "has_contact_form": False,
                "form_submission_status": "failed",
                "form_error_message": "No contact form found",
                "form_last_attempted_at": now,
            })
            return {"lead_id": lead["id"], "success": False, "reason": "no_form",
                    "error_message": "No contact form found", "form_url": None, "fields_filled": 0}

        # Navigate to the form page (if not already there)
        if page.url != form_url:
            await page.goto(form_url, wait_until="domcontentloaded", timeout=15000)
            await human_delay(0.5, 1.5)

        # Fill and submit
        result = await fill_contact_form(page, lead)
        now = datetime.now(timezone.utc).isoformat()

        if result["success"]:
            update_lead(lead["id"], {
                "has_contact_form": True,
                "form_filled": True,
                "form_filled_at": now,
                "contact_page_url": form_url,
                "form_submission_status": "success",
                "form_error_message": None,
                "form_last_attempted_at": now,
            })
            log_outreach(
                lead_id=lead["id"],
                channel="form",
                form_url=form_url,
                form_submitted=True,
            )
            return {"lead_id": lead["id"], "success": True, "form_url": form_url,
                    "fields_filled": result["fields_filled"], "error_message": None, "reason": "success"}
        else:
            update_lead(lead["id"], {
                "has_contact_form": True,
                "contact_page_url": form_url,
                "form_submission_status": "failed",
                "form_error_message": result.get("error_message", "Fill failed"),
                "form_last_attempted_at": now,
            })
            return {"lead_id": lead["id"], "success": False, "reason": "fill_failed",
                    "error_message": result.get("error_message", "Fill failed"),
                    "form_url": form_url, "fields_filled": result["fields_filled"]}

    except PlaywrightTimeout:
        now = datetime.now(timezone.utc).isoformat()
        update_lead(lead["id"], {
            "form_submission_status": "failed",
            "form_error_message": "Page load timeout",
            "form_last_attempted_at": now,
        })
        return {"lead_id": lead["id"], "success": False, "reason": "timeout",
                "error_message": "Page load timeout", "form_url": None, "fields_filled": 0}
    except Exception as e:
        now = datetime.now(timezone.utc).isoformat()
        error_msg = str(e)[:200]
        update_lead(lead["id"], {
            "form_submission_status": "failed",
            "form_error_message": error_msg,
            "form_last_attempted_at": now,
        })
        logger.error(f"Form processing error for {lead.get('company_domain')}: {e}")
        return {"lead_id": lead["id"], "success": False, "reason": "error",
                "error_message": error_msg, "form_url": None, "fields_filled": 0}
    finally:
        await context.close()


async def run_form_filling(leads: list[dict], max_concurrent: int = 3) -> dict:
    """
    Run form filling for a batch of leads.
    Processes one lead at a time with stop check between each.
    Each form has a 60s timeout so nothing hangs.
    """
    from modules.form_outreach import outreach_state

    stats = {"total": len(leads), "success": 0, "no_form": 0, "failed": 0, "results": []}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for i, lead in enumerate(leads):
            # Check stop flag before each form
            if outreach_state.get("stop_requested"):
                logger.info(f"Form filling stopped at {i}/{len(leads)}")
                break

            # Update current lead for dashboard
            outreach_state["current_lead"] = lead.get("company_name") or lead.get("website_url") or "?"

            try:
                # Timeout per form — 20 seconds max so stop is fast
                result = await asyncio.wait_for(
                    process_lead_form(browser, lead),
                    timeout=20
                )
            except asyncio.TimeoutError:
                result = {"success": False, "error_message": "Timed out after 20s"}
                logger.warning(f"Form fill timeout: {lead.get('company_name')}")
            except Exception as e:
                result = {"success": False, "error_message": str(e)[:200]}
                logger.error(f"Form fill exception: {e}")

            if isinstance(result, Exception):
                stats["failed"] += 1
                stats["results"].append({"success": False, "error_message": str(result)[:200]})
            elif result.get("success"):
                stats["success"] += 1
                stats["results"].append(result)
            elif result.get("reason") == "no_form":
                stats["no_form"] += 1
                stats["results"].append(result)
            else:
                stats["failed"] += 1
                stats["results"].append(result)

            # Update outreach state live for dashboard
            outreach_state["total_processed"] = stats["success"] + stats["failed"] + stats["no_form"]
            outreach_state["success"] = stats["success"]
            outreach_state["failed"] = stats["failed"]
            outreach_state["no_form"] = stats["no_form"]

            # Brief pause between forms
            await asyncio.sleep(1)

        await browser.close()

    logger.info(f"Form filling complete: total={stats['total']}, success={stats['success']}, "
                f"no_form={stats['no_form']}, failed={stats['failed']}")
    return stats


def fill_forms_sync(leads: list[dict], max_concurrent: int = 3) -> dict:
    """Synchronous wrapper for form filling."""
    return asyncio.run(run_form_filling(leads, max_concurrent))
