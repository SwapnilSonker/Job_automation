"""
AGENTIC HR Outreach Automation (Groq Tool-Calling + Playwright + LinkedIn)
==========================================================================

*** READ BEFORE USING ***
This script logs into LinkedIn with YOUR credentials/session, performs
automated searches, scrolls through results, and reads post content via
browser automation (Playwright). This is against LinkedIn's Terms of
Service and CAN RESULT IN A PERMANENT ACCOUNT BAN, even with stealth
techniques. There is no fully safe way to do this. You accepted this risk.

Architecture:
  - ReAct-style agent loop powered by Groq function-calling
  - LLM decides which tools to call, in what order, and whether a post
    is worth pursuing (selective filtering)
  - Self-healing: if scraping returns 0 posts, the agent inspects the live
    DOM, proposes CSS selector fixes, tests them sandboxed, and persists
    working selectors to selector_config.json automatically
  - Tools: scrape_linkedin_posts, extract_contact, check_already_contacted,
           send_outreach_email, run_followups, check_replies,
           inspect_page_dom, test_selector_patch, apply_selector_patch

Run modes:
  python main.py login      # Save LinkedIn session (run once)
  python main.py            # Full agentic pipeline
  python main.py followups  # Only follow-ups + reply check
"""

import re
import os
import sys
import time
import json
import random
import smtplib
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from openai import OpenAI
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# Load .env before reading any config
load_dotenv()

# ─────────────────────────────────────────
# CONFIG  (all values loaded from .env)
# ─────────────────────────────────────────

GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL       = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL          = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

GMAIL_ADDRESS       = os.getenv("GMAIL_ADDRESS",      "")
GMAIL_APP_PASSWORD  = os.getenv("GMAIL_APP_PASSWORD", "")

GOOGLE_CREDS_FILE   = os.getenv("GOOGLE_CREDS_FILE", "google_creds.json")
SHEET_NAME          = os.getenv("SHEET_NAME",        "HR Outreach Tracker")
SHEET_ID            = os.getenv("SHEET_ID",          "")   # preferred — from sheet URL

YOUR_NAME           = os.getenv("YOUR_NAME",   "Your Full Name")
YOUR_ROLE           = os.getenv("YOUR_ROLE",   "AI Engineer")
YOUR_SKILLS         = os.getenv("YOUR_SKILLS", "LLMs, Python, RAG pipelines, fine-tuning")
YOUR_PHONE          = os.getenv("YOUR_PHONE",  "+91-XXXXXXXXXX")

SEARCH_QUERY        = os.getenv("SEARCH_QUERY",       "hiring AI engineer")
MAX_POSTS_PER_RUN   = int(os.getenv("MAX_POSTS_PER_RUN",  "20"))
SCROLL_PAUSES       = (
    float(os.getenv("SCROLL_PAUSE_MIN", "2")),
    float(os.getenv("SCROLL_PAUSE_MAX", "5")),
)
DAILY_SEND_LIMIT    = int(os.getenv("DAILY_SEND_LIMIT",   "40"))
FOLLOWUP_DAY_1      = int(os.getenv("FOLLOWUP_DAY_1",     "3"))
FOLLOWUP_DAY_2      = int(os.getenv("FOLLOWUP_DAY_2",     "7"))
BROWSER_PROFILE_DIR  = os.getenv("BROWSER_PROFILE_DIR",    "linkedin_session")
MAX_AGENT_ITERATIONS = int(os.getenv("MAX_AGENT_ITERATIONS", "10"))
HEADLESS             = os.getenv("HEADLESS", "true").lower() == "true"
POST_MAX_AGE_DAYS    = int(os.getenv("POST_MAX_AGE_DAYS", "30"))
SEEN_POSTS_FILE      = os.getenv("SEEN_POSTS_FILE", "seen_posts.json")
RATELOG_FILE         = os.getenv("RATELOG_FILE", "email_rate_log.json")
CONTACTS_LOG_FILE    = os.getenv("CONTACTS_LOG_FILE", "extracted_contacts.csv")
RESUME_FILE          = os.getenv("RESUME_FILE", "Swapnil_Sonker_s.pdf")

# Fallback query expansion — used when primary query yields < 5 posts
QUERY_EXPANSIONS = [
    SEARCH_QUERY,
    "AI engineer hiring",
    "machine learning engineer job opening",
    "LLM engineer job",
    "deep learning engineer hiring",
]

# "See more" button selectors to expand truncated posts
SEE_MORE_SELECTORS = [
    "button.feed-shared-inline-show-more-text__see-more-less-toggle",
    "button[aria-label*='see more']",
    "span.see-more",
    "a[data-tracking-control-name*='see_more']",
    ".update-components-text a",
]

# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.FileHandler("logs/outreach.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Self-Healing Selector Config
# ─────────────────────────────────────────

SELECTOR_CONFIG_FILE = "config/selector_config.json"
SCREENSHOT_DIR       = "logs/screenshots"
# LinkedIn now uses hashed CSS class names — use stable data-attribute selectors instead
DEFAULT_SELECTORS = [
    "[data-urn*='activity']",
    "[data-view-name='search-entity-result-universal-template']",
    "li.reusable-search__result-container",
    "div.search-result__wrapper",
]

def load_selector_config() -> list:
    """Load CSS selectors from config file, falling back to defaults."""
    if os.path.exists(SELECTOR_CONFIG_FILE):
        try:
            with open(SELECTOR_CONFIG_FILE) as f:
                data = json.load(f)
                sels = data.get("post_selectors", DEFAULT_SELECTORS)
                if sels:
                    log.info(f"[HEAL] Loaded {len(sels)} selector(s) from {SELECTOR_CONFIG_FILE}")
                    return sels
        except Exception as e:
            log.warning(f"[HEAL] Could not read {SELECTOR_CONFIG_FILE}: {e} — using defaults")
    return list(DEFAULT_SELECTORS)

ACTIVE_SELECTORS: list = load_selector_config()

# ─────────────────────────────────────────
# Email-based deduplication (replaces text-fingerprint seen_posts.json)
# ─────────────────────────────────────────

def load_known_emails() -> set:
    """
    Build a set of email addresses we have already processed.
    Sources: extracted_contacts.csv + email_rate_log.json
    This replaces the old text-fingerprint seen_posts.json approach.
    A fresh scrape will always see ALL posts, but discard any whose
    email is already in our master contact list.
    """
    import csv
    known = set()

    # 1. Every email already in the contacts CSV
    if os.path.exists(CONTACTS_LOG_FILE):
        try:
            with open(CONTACTS_LOG_FILE, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    email = row.get("Email", "").strip().lower()
                    if email:
                        known.add(email)
        except Exception:
            pass

    # 2. Every email we have already sent an outreach to
    if os.path.exists(RATELOG_FILE):
        try:
            data = json.loads(Path(RATELOG_FILE).read_text())
            for entry in data:
                email = entry.get("email", "").strip().lower()
                if email:
                    known.add(email)
        except Exception:
            pass

    log.info(f"[DEDUP] Loaded {len(known)} known email(s) from contacts CSV + rate log")
    return known

# ─────────────────────────────────────────
# Email rate log (tracks sends for analytics)
# ─────────────────────────────────────────

def log_email_rate_entry(email: str, company: str, query: str, subject: str):
    """Append a send record to email_rate_log.json for later analytics."""
    entry = {
        "sent_at": datetime.now().isoformat(),
        "email": email,
        "company": company,
        "query": query,
        "subject": subject,
        "replied": False,
    }
    records = []
    if os.path.exists(RATELOG_FILE):
        try:
            with open(RATELOG_FILE) as f:
                records = json.load(f)
        except Exception:
            pass
    records.append(entry)
    try:
        with open(RATELOG_FILE, "w") as f:
            json.dump(records, f, indent=2)
    except Exception as e:
        log.warning(f"[RATELOG] Could not write {RATELOG_FILE}: {e}")


def _summarize_jd(post_text: str) -> str:
    """
    One-shot LLM call: summarize the job description from a LinkedIn post
    into 2-3 sentences capturing role, required skills, and company context.
    Returns an empty string on failure (so the email still sends).
    """
    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Summarize the following LinkedIn hiring post in 2-3 sentences. Include: the role title, key required skills, and company name if mentioned. Be concise."},
                {"role": "user",   "content": post_text[:1500]},
            ],
            max_tokens=120,
        )
        summary = resp.choices[0].message.content.strip()
        log.info(f"[JD] Summary: {summary[:80]}...")
        return summary
    except Exception as e:
        log.warning(f"[JD] Summarization failed: {e}")
        return ""


def _append_contact_log(email: str, name: str, company: str, post_snippet: str, query: str, jd_summary: str = ""):
    """
    Append an extracted contact to extracted_contacts.csv AND to the Google Sheet.
    Called inline during scraping — one row written per post as it is found.
    Sheet status is set to 'extracted'; updated to 'emailed' when email is sent.
    """
    import csv

    # ── 1. Write to CSV ──────────────────────────────────────
    file_exists = os.path.exists(CONTACTS_LOG_FILE)
    try:
        with open(CONTACTS_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Extracted At", "Email", "Name", "Company", "Query", "JD Summary", "Post Snippet"])
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                email,
                name,
                company,
                query,
                jd_summary,
                post_snippet[:200].replace("\n", " "),
            ])
        log.info(f"[CONTACTS] CSV logged: {email} ({company})")
    except Exception as e:
        log.warning(f"[CONTACTS] Could not write to {CONTACTS_LOG_FILE}: {e}")

    # ── 2. Write to Google Sheet (status = extracted) ────────
    try:
        sheet = get_sheet()
        # Skip if already in sheet (avoid duplicates across runs)
        existing = get_contacted_emails_set()
        if email.lower().strip() not in existing:
            sheet.append_row([name, email, company, "extracted", "", "", "", jd_summary[:200] if jd_summary else ""])
            log.info(f"[SHEET] Row added: {email} → status=extracted")
        else:
            log.info(f"[SHEET] Skipped (already in sheet): {email}")
    except Exception as e:
        log.warning(f"[CONTACTS] Could not write to Sheet: {e}")

# ─────────────────────────────────────────
# Groq client
# ─────────────────────────────────────────

groq_client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)

# ─────────────────────────────────────────
# Google Sheet helpers
# Columns: Name | Email | Company | Status | Emailed On | F1 Sent | F2 Sent | Replied
# ─────────────────────────────────────────

SHEET_COLS = {
    "name": 1, "email": 2, "company": 3, "status": 4,
    "emailed_on": 5, "f1_sent": 6, "f2_sent": 7, "replied": 8, "jd_summary": 9,
}

_sheet_cache = None  # simple module-level cache to avoid repeated auth

def get_sheet():
    global _sheet_cache
    if _sheet_cache is not None:
        return _sheet_cache
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)
    if SHEET_ID:
        _sheet_cache = client.open_by_key(SHEET_ID).sheet1
        log.info(f"[SHEET] Connected via SHEET_ID: {SHEET_ID[:20]}...")
    else:
        _sheet_cache = client.open(SHEET_NAME).sheet1
        log.info(f"[SHEET] Connected via SHEET_NAME: {SHEET_NAME}")
    return _sheet_cache

def get_contacted_emails_set():
    try:
        sheet = get_sheet()
        emails = sheet.col_values(SHEET_COLS["email"])
        return set(e.lower().strip() for e in emails[1:] if e)
    except Exception as e:
        log.error(f"Could not fetch contacted emails: {e}")
        return set()

def _log_contact_row(sheet, name, email, company):
    sheet.append_row([name, email, company, "pending", "", "", "", ""])

def _update_row(sheet, email, col_index, value):
    try:
        cell = sheet.find(email)
        sheet.update_cell(cell.row, col_index, value)
    except Exception as e:
        log.error(f"Could not update row for {email}: {e}")

# ─────────────────────────────────────────
# Extraction helpers
# ─────────────────────────────────────────

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

NAME_PATTERNS = [
    r'[Cc]ontact[:\s]+([A-Z][a-z]+ [A-Z][a-z]+)',
    r'[Rr]each out to ([A-Z][a-z]+ [A-Z][a-z]+)',
    r'[Cc]ontact ([A-Z][a-z]+ [A-Z][a-z]+) at',
    r'[Mm]ail ([A-Z][a-z]+ [A-Z][a-z]+)',
    r'[Ss]end.*?to ([A-Z][a-z]+ [A-Z][a-z]+)',
    r'[Cc][Vv].*?to ([A-Z][a-z]+ [A-Z][a-z]+)',
    r'-\s*([A-Z][a-z]+ [A-Z][a-z]+),?\s+HR',
    r'-\s*([A-Z][a-z]+ [A-Z][a-z]+),?\s+Recruiter',
    r'-\s*([A-Z][a-z]+ [A-Z][a-z]+),?\s+Talent',
]

COMPANY_PATTERNS = [
    r'at\s+([A-Z][A-Za-z0-9&.\- ]{1,30})\s*(?:is|are|hiring|looking)',
    r'(?:join|joining)\s+([A-Z][A-Za-z0-9&.\- ]{1,30})',
    r'([A-Z][A-Za-z0-9&.\- ]{1,30})\s+is hiring',
]

def _launch_browser(p):
    """Launch browser context respecting HEADLESS setting."""
    extra_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    if HEADLESS:
        extra_args += ["--disable-gpu", "--window-size=1280,900"]
    return p.chromium.launch_persistent_context(
        BROWSER_PROFILE_DIR,
        headless=HEADLESS,
        args=extra_args,
    )


def _click_see_more(page) -> int:
    """Click all visible 'See more' buttons to expand truncated posts. Returns count clicked."""
    clicked = 0
    for sel in SEE_MORE_SELECTORS:
        try:
            buttons = page.locator(sel).all()
            for btn in buttons:
                try:
                    if btn.is_visible(timeout=500):
                        btn.click(timeout=1000)
                        clicked += 1
                        time.sleep(0.6)
                except Exception:
                    pass
        except Exception:
            pass
    if clicked:
        log.info(f"[SCRAPE] Clicked {clicked} 'See more' button(s)")
    return clicked


def _is_post_recent(post_text: str) -> bool:
    """Rough age filter: reject posts that seem stale (contain old date references).
    LinkedIn posts rarely show explicit dates in text, so this is mostly a length/keyword check.
    Returns True if post appears recent/valid."""
    # Accept all for now — true age filtering requires parsing LinkedIn's timestamp elements
    # which varies by selector. Kept as hook for future enhancement.
    return True


def _click_load_more(page) -> bool:
    """Click 'Show more results' / pagination button. Returns True if found and clicked."""
    load_more_selectors = [
        "button.scaffold-finite-scroll__load-button",
        "button[aria-label*='Load more']",
        "button[aria-label*='Show more']",
        "div.search-results__cluster-bottom button",
    ]
    for sel in load_more_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000):
                btn.click(timeout=2000)
                log.info("[SCRAPE] Clicked 'Load more results'")
                time.sleep(random.uniform(2, 4))
                return True
        except Exception:
            pass
    return False


# ─────────────────────────────────────────
# Tool implementations
# ─────────────────────────────────────────

def _validate_and_heal_selectors(page) -> bool:
    """
    Validation Agent — runs on an already-open LinkedIn page BEFORE any scrolling.

    Step 1: Test every selector in ACTIVE_SELECTORS against the live page.
            A selector is considered 'working' only if at least one matched
            element has >= 80 chars of text AND contains a hiring keyword.
            If any selector passes → return True immediately (no heal needed).

    Step 2: If ALL selectors fail, inspect the live DOM to derive candidates:
            - [data-urn]          (post-level data attributes)
            - [data-view-name=X]  (view-name values found on page)
            - li.CLASSNAME        (li elements with substantial text)
            - [role='article']    (semantic role elements)
            - div.CLASSNAME       (hashed div classes with substantial text)

    Step 3: Test each candidate selector — same quality bar (text >= 80 chars
            + hiring keyword). Pick the one with the highest qualifying count.

    Step 4: If a winner is found → update ACTIVE_SELECTORS and persist to
            selector_config.json. Return True.
            If no winner → return False (caller will abort scrape cleanly).
    """
    global ACTIVE_SELECTORS

    HIRING_KWS = [
        "hiring", "looking for", "job", "engineer", "developer",
        "ai", "ml", "apply", "resume", "cv", "opening", "opportunity"
    ]

    def _count_real_posts(selector: str) -> int:
        """Count elements matching selector that look like real posts."""
        try:
            els = page.locator(selector).all()
            count = 0
            for el in els[:10]:
                try:
                    t = el.inner_text(timeout=1500).strip()
                    if len(t) >= 80 and any(kw in t.lower() for kw in HIRING_KWS):
                        count += 1
                except Exception:
                    pass
            return count
        except Exception:
            return 0

    # ── Step 1: Test current selectors ───────────────────────────────────────
    log.info("[VALIDATE] Testing current selectors against live page...")
    for sel in ACTIVE_SELECTORS:
        real_count = _count_real_posts(sel)
        log.info(f"[VALIDATE] '{sel}' → {real_count} real post elements")
        if real_count > 0:
            log.info(f"[VALIDATE] ✅ Selector '{sel}' is working. No heal needed.")
            return True

    log.warning("[VALIDATE] ⚠️  All current selectors returned 0 real posts. Starting DOM inspection...")

    # ── Step 2: Inspect live DOM to derive fresh candidates ──────────────────
    try:
        candidates = page.evaluate("""
            () => {
                const HIRING_KWS = ['hiring','job','engineer','developer','ai','ml',
                                    'apply','resume','cv','opening','opportunity'];
                const hasHiring = (text) => HIRING_KWS.some(kw => text.toLowerCase().includes(kw));

                const selectors = new Set();

                // data-urn attributes (most stable LinkedIn post identifiers)
                const urnEls = Array.from(document.querySelectorAll('[data-urn]'));
                const urnVals = [...new Set(urnEls.map(e => e.getAttribute('data-urn'))
                    .filter(v => v && v.includes('activity')))];
                if (urnVals.length > 0) selectors.add("[data-urn*='activity']");
                if (urnEls.length > 0)  selectors.add("[data-urn]");

                // data-view-name attributes
                const viewVals = [...new Set(
                    Array.from(document.querySelectorAll('[data-view-name]'))
                    .map(e => e.getAttribute('data-view-name')).filter(Boolean)
                )];
                for (const v of viewVals) {
                    const els = document.querySelectorAll(`[data-view-name='${v}']`);
                    const hasText = Array.from(els).some(e =>
                        (e.innerText || '').trim().length > 80 && hasHiring(e.innerText || ''));
                    if (hasText) selectors.add(`[data-view-name='${v}']`);
                }

                // li elements with hiring text → use their class name
                Array.from(document.querySelectorAll('li'))
                    .filter(li => (li.innerText || '').trim().length > 80
                                  && hasHiring(li.innerText || '')
                                  && li.className)
                    .slice(0, 5)
                    .forEach(li => {
                        li.className.trim().split(/\s+/).forEach(cls => {
                            if (cls.length > 3) selectors.add(`li.${cls}`);
                        });
                    });

                // role=article elements
                if (document.querySelectorAll('[role="article"]').length > 0)
                    selectors.add('[role="article"]');

                // div elements with substantial hiring text → use first class
                Array.from(document.querySelectorAll('div[class]'))
                    .filter(d => (d.innerText || '').trim().length > 80
                                 && hasHiring(d.innerText || ''))
                    .slice(0, 8)
                    .forEach(d => {
                        const cls = d.classList[0];
                        if (cls && cls.length > 3) selectors.add(`div.${cls}`);
                    });

                return Array.from(selectors).slice(0, 20);
            }
        """)
        log.info(f"[VALIDATE] DOM inspection found {len(candidates)} candidate selectors: {candidates}")
    except Exception as e:
        log.error(f"[VALIDATE] DOM inspection failed: {e}")
        return False

    if not candidates:
        log.error("[VALIDATE] DOM inspection returned no candidates. Cannot heal.")
        return False

    # ── Step 3: Test each candidate ───────────────────────────────────────────
    best_sel   = None
    best_count = 0
    for sel in candidates:
        real_count = _count_real_posts(sel)
        log.info(f"[VALIDATE] Candidate '{sel}' → {real_count} real posts")
        if real_count > best_count:
            best_count = real_count
            best_sel   = sel

    # ── Step 4: Apply winner or fail ─────────────────────────────────────────
    if best_sel and best_count > 0:
        new_selectors = [best_sel]
        # Also keep any other candidates that had > 0 real posts (multi-selector resilience)
        for sel in candidates:
            if sel != best_sel and _count_real_posts(sel) > 0:
                new_selectors.append(sel)
                if len(new_selectors) >= 3:
                    break

        config = {
            "post_selectors": new_selectors,
            "updated_at": datetime.now().isoformat(),
            "previous_selectors": ACTIVE_SELECTORS,
            "validated_match_count": best_count,
            "healed_by": "validate_and_heal_selectors",
        }
        try:
            with open(SELECTOR_CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            ACTIVE_SELECTORS = new_selectors
            log.info(f"[VALIDATE] ✅ Selectors healed → {new_selectors} (best real-post count: {best_count})")
            log.info(f"[VALIDATE] Saved to {SELECTOR_CONFIG_FILE}")
            return True
        except Exception as e:
            log.error(f"[VALIDATE] Could not write {SELECTOR_CONFIG_FILE}: {e}")
            # Still usable in-memory even if file write failed
            ACTIVE_SELECTORS = new_selectors
            return True
    else:
        log.error("[VALIDATE] ❌ No candidate selector matched real post content. Cannot proceed.")
        return False


def tool_scrape_linkedin_posts(query: str, max_posts: int) -> dict:
    """
    Tool: scrape_linkedin_posts
    Opens LinkedIn with saved session (headless), FIRST validates + heals selectors
    against the live page, then scrolls intelligently, expands truncated posts via
    'See more', and extracts email from each post immediately as it is found.
    """
    if not os.path.exists(BROWSER_PROFILE_DIR):
        return {"error": "No saved session found. Run: python main.py login", "posts": []}



    # Email-based dedup: load all emails we've already seen/contacted
    known_emails = load_known_emails()
    # Track emails found in THIS run to avoid intra-run duplicates
    this_run_emails: set = set()

    posts_text   = []
    search_url   = f"https://www.linkedin.com/search/results/content/?keywords={query.replace(' ', '%20')}"

    with sync_playwright() as p:
        context = _launch_browser(p)
        page = context.new_page()
        try:
            log.info(f"[SCRAPE] Navigating to LinkedIn search: '{query}' (headless={HEADLESS})")
            page.goto(search_url, timeout=60000)
            time.sleep(random.uniform(3, 6))

            # CAPTCHA / session check
            if "login" in page.url or "authwall" in page.url:
                context.close()
                return {"error": "Session expired. Run: python main.py login", "posts": []}
            if "checkpoint" in page.url or "challenge" in page.url:
                log.warning("[SCRAPE] LinkedIn challenge/CAPTCHA detected. Stopping.")
                context.close()
                return {"error": "LinkedIn CAPTCHA detected. Try again later or log in manually.", "posts": []}

            # ── Validation Agent: inspect + heal selectors BEFORE scrolling ──
            # This runs on the already-open page. If current selectors are stale
            # it derives new ones from the live DOM, tests them, and updates
            # selector_config.json — all before wasting any scroll attempts.
            selectors_ok = _validate_and_heal_selectors(page)
            if not selectors_ok:
                context.close()
                return {
                    "posts": [], "count": 0,
                    "error": (
                        "Validation Agent could not find any working selector on the live LinkedIn page. "
                        "LinkedIn may have changed their layout. Check screenshots/ for the current DOM, "
                        "or run 'python main.py login' to refresh the session."
                    ),
                }

            collected    = []          # ordered, for this run (email-deduped)
            scroll_attempts = 0
            max_scrolls = max(8, max_posts // 2)
            total_elements_found = 0   # Track total raw elements seen before dedup
            processed_elements_count = 0  # Post memorizer: how many elements we already processed


            while len(collected) < max_posts and scroll_attempts < max_scrolls:
                # 1. Expand any truncated posts on screen
                _click_see_more(page)

                # 2. Extract text from all visible post elements
                selector_str = ", ".join(ACTIVE_SELECTORS)
                post_elements = page.locator(selector_str).all()

                # ── Post Memorizer ──────────────────────────────────────────
                # LinkedIn always appends new posts at the bottom. We track
                # how many elements were already processed in previous scrolls
                # and only iterate the NEW tail slice. This prevents the
                # exponential re-reading that caused 40-minute runs.
                new_elements = post_elements[processed_elements_count:]
                total_elements_found += len(new_elements)
                log.info(
                    f"[SCRAPE] Scroll {scroll_attempts+1}: "
                    f"{len(post_elements)} total | {len(new_elements)} new (skipping {processed_elements_count} already seen)"
                )

                for el in new_elements:
                    try:
                        # Scroll element into view so 'see more' renders
                        el.scroll_into_view_if_needed(timeout=1500)
                        time.sleep(0.2)

                        # Click see-more inside this specific element
                        for sel in SEE_MORE_SELECTORS:
                            try:
                                inner_btn = el.locator(sel).first
                                if inner_btn.is_visible(timeout=300):
                                    inner_btn.click(timeout=800)
                                    time.sleep(0.5)
                            except Exception:
                                pass

                        text = el.inner_text(timeout=2000).strip()

                        # Quality filters
                        if len(text) < 100:
                            continue
                        if not any(kw in text.lower() for kw in [
                            "hiring", "looking for", "job", "engineer", "developer",
                            "AI", "ML", "apply", "resume", "cv", "opening", "opportunity"
                        ]):
                            continue

                        # ── Email-first filter: only keep posts that have a NEW email ──
                        # This is the core of the new dedup strategy. If the post has
                        # no email, or the email is already known, discard immediately.
                        emails_found = re.findall(EMAIL_REGEX, text)
                        if not emails_found:
                            continue  # no email in post — not useful

                        extracted_email = emails_found[0]
                        email_lower = extracted_email.lower()

                        if email_lower in known_emails or email_lower in this_run_emails:
                            continue  # already contacted or already found this run

                        # New email — accept this post
                        this_run_emails.add(email_lower)
                        collected.append(text)

                        # Quick name/company extraction
                        extracted_name = "Hiring Manager"
                        for pat in NAME_PATTERNS:
                            m = re.search(pat, text)
                            if m:
                                extracted_name = m.group(1)
                                break
                        extracted_company = ""
                        for pat in COMPANY_PATTERNS:
                            m = re.search(pat, text)
                            if m:
                                extracted_company = m.group(1).strip()
                                break

                        log.info(f"[SCRAPE] ✅ New contact found: {extracted_email}")

                        # Summarise the JD inline before logging — keeps the CSV/Sheet
                        # clean (3 dense sentences instead of raw truncated post text)
                        # and dramatically cuts token cost at email-drafting time.
                        log.info(f"[JD] Summarising post for {extracted_email}...")
                        jd_summary = _summarize_jd(text)

                        _append_contact_log(
                            email=extracted_email,
                            name=extracted_name,
                            company=extracted_company,
                            post_snippet=text,
                            query=query,
                            jd_summary=jd_summary,
                        )

                    except Exception:
                        continue

                # Advance the memorizer cursor to the end of the current list
                processed_elements_count = len(post_elements)

                # 3. Try to click "Load more results" button before scrolling
                _click_load_more(page)

                # 4. Scroll down — use JS scrollBy instead of mouse.wheel.
                # mouse.wheel only works if the cursor is hovering the right column,
                # which is unreliable in headless/headful mode both. window.scrollBy
                # always fires on the document itself, regardless of mouse position.
                scroll_px = random.randint(700, 1400)
                page.evaluate(f"window.scrollBy(0, {scroll_px})")
                time.sleep(random.uniform(*SCROLL_PAUSES))
                scroll_attempts += 1

            posts_text = collected[:max_posts]
            log.info(f"[SCRAPE] Collected {len(posts_text)} posts with new emails (known DB: {len(known_emails)} emails)")

            if len(posts_text) == 0:
                if total_elements_found == 0:
                    log.warning("[HEAL] 0 posts collected — selectors may be stale. Triggering self-heal.")
                    context.close()
                    return {
                        "posts": [], "count": 0,
                        "heal_hint": (
                            f"Scraping returned 0 posts using selectors: {ACTIVE_SELECTORS}. "
                            f"Call inspect_page_dom with url='{search_url}' to analyse the DOM, "
                            f"then test_selector_patch, then apply_selector_patch if they match."
                        ),
                        "search_url": search_url,
                    }
                else:
                    log.info(f"[SCRAPE] Found {total_elements_found} posts, but 0 NEW posts. Selectors are working perfectly.")
                    context.close()
                    return {
                        "posts": [], "count": 0,
                        "status": "no_new_posts",
                        "message": "Scraped successfully but all posts on the page were already seen from previous runs. There are no new openings right now. Tell the user to check back later."
                    }

        except Exception as e:
            log.error(f"Scraping error: {e}")
            return {"error": str(e), "posts": [], "heal_hint": f"Exception: {e}. Try inspect_page_dom."}
        finally:
            context.close()

    # Truncate posts to save tokens — 500 chars is enough to find email/name/company
    truncated = [p[:500] for p in posts_text]
    log.info(f"[SCRAPE] Returning {len(truncated)} posts (truncated to 500 chars each for token efficiency)")
    return {"posts": truncated, "count": len(truncated)}


def tool_extract_contact(post_text: str) -> dict:
    """
    Tool: extract_contact
    Parses post text via regex to find email, name, company.
    """
    emails = re.findall(EMAIL_REGEX, post_text)
    if not emails:
        return {"found": False, "reason": "No email address in post"}

    email = emails[0]
    for e in emails:
        if not any(d in e.lower() for d in ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com"]):
            email = e
            break

    name = "Hiring Manager"
    for pattern in NAME_PATTERNS:
        match = re.search(pattern, post_text)
        if match:
            name = match.group(1).strip()
            break

    company = ""
    for pattern in COMPANY_PATTERNS:
        match = re.search(pattern, post_text)
        if match:
            company = match.group(1).strip()
            break
    if not company:
        domain = email.split("@")[-1].split(".")[0]
        company = domain.capitalize()

    return {
        "found": True,
        "name": name,
        "email": email.lower().strip(),
        "company": company,
        "post_snippet": post_text.strip()[:500],
    }


def tool_check_already_contacted(email: str) -> dict:
    """
    Tool: check_already_contacted
    Returns whether the email has already been EMAILED (not just extracted).
    Contacts with status 'extracted' are treated as new (not yet emailed).
    """
    try:
        sheet = get_sheet()
        # Find the email in column 2
        try:
            cell = sheet.find(email.lower().strip())
            if cell:
                status = sheet.cell(cell.row, SHEET_COLS["status"]).value or ""
                # Only block if actually emailed or replied — not just extracted
                if status.lower() in {"emailed", "f1_sent", "f2_sent", "replied"}:
                    return {"email": email, "already_contacted": True}
        except Exception:
            pass  # cell not found = not in sheet
        return {"email": email, "already_contacted": False}
    except Exception as e:
        log.error(f"Could not check contacted status: {e}")
        return {"email": email, "already_contacted": False}


def tool_send_outreach_email(name: str, email: str, company: str, post_text: str) -> dict:
    """
    Tool: send_outreach_email
    Generates a personalized cold email via Groq and sends it via Gmail SMTP.
    Also logs the contact to Google Sheet.
    """
    # ── Guard: reject obviously fake / hallucinated emails ───────────────
    BLOCKED_DOMAINS = {"example.com", "test.com", "placeholder.com", "email.com",
                       "domain.com", "company.com", "yourcompany.com"}
    BLOCKED_USERS   = {"hr", "test", "user", "admin", "info", "email", "name"}
    email_lower = email.lower().strip()
    email_domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    email_user   = email_lower.split("@")[0]  if "@" in email_lower else ""
    if email_domain in BLOCKED_DOMAINS or email_user in BLOCKED_USERS:
        log.warning(f"[GUARD] Blocked fake/hallucinated email: {email}")
        return {"success": False, "reason": f"Blocked: {email} looks like a placeholder. Only use real emails extracted from actual posts.", "email": email}
    if not re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email_lower):
        log.warning(f"[GUARD] Invalid email format: {email}")
        return {"success": False, "reason": f"Invalid email format: {email}", "email": email}
    if len(post_text) < 80 or "Full text of" in post_text or "placeholder" in post_text.lower():
        log.warning(f"[GUARD] post_text looks like placeholder, not a real post.")
        return {"success": False, "reason": "post_text appears to be placeholder data, not a real LinkedIn post. Only use actual scraped post text.", "email": email}
    # ───────────────────────────────────────────────────
    sheet = get_sheet()

    # Look up JD summary from extracted_contacts.csv for this email
    jd_summary = ""
    try:
        import csv
        with open(CONTACTS_LOG_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("Email", "").lower().strip() == email.lower().strip():
                    jd_summary = row.get("JD Summary", "")
                    break
    except Exception:
        pass

    # Generate email content using fixed skeleton + LLM fills 2 personalized slots
    jd_context = jd_summary if jd_summary else post_text[:600]
    prompt = f"""You are filling in TWO blanks in a cold email template.

Job description context:
{jd_context}

Return ONLY valid JSON with exactly these two keys (no markdown):
{{
  "role_title": "<the exact role title from the JD, e.g. 'Senior AI Engineer'>",
  "jd_hook":    "<one phrase (10-15 words) naming 1-2 specific skills or requirements from the JD that match Swapnil's background in RAG, LangChain, LangGraph, FastAPI, fine-tuning, or cloud deployment>"
}}"""

    # Defaults if LLM fails
    role_title = "AI/ML Engineer"
    jd_hook    = "end-to-end ML system design and deployment"

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL, max_tokens=120,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = re.sub(r"```json|```", "", response.choices[0].message.content.strip()).strip()
        result = json.loads(raw)
        role_title = result.get("role_title", role_title).strip()
        jd_hook    = result.get("jd_hook",    jd_hook).strip()
    except Exception as e:
        log.warning(f"[EMAIL] Personalization LLM failed ({e}), using defaults.")

    subject = f"{role_title} Application — {company}"

    body = f"""Hi {name},

I am writing to express my interest in the {role_title} role, as my background in building production-grade Generative AI solutions aligns directly with your requirements for {jd_hook}. I am an AI Engineer at Occams Advisory, where I recently designed and shipped TaxVantage AI—a multi-agent OCR and document intelligence platform that utilizes a production MCP server to process complex financial data.

My experience directly maps to your key responsibilities:

\u2022 GenAI & RAG Development: I have built and deployed RAG agents and form automation pipelines using LangChain, LangGraph, and FastAPI. I am proficient in extending these pipelines with vector databases and implementing complex reasoning flows.
\u2022 Fine-Tuning & Model Evaluation: I have hands-on experience fine-tuning Llama 3.2-1B for domain-specific tasks and am currently tuning transformer self-attention mechanisms and gradient-boosting models like CatBoost and XGBoost.
\u2022 Production ML & Cloud: I have experience deploying ML workloads across GCP, Firebase, and Cloudflare using Docker. My work includes engineering outbound Voice AI agents with real-time LLM reasoning and automated LinkedIn scraping workflows.
\u2022 Full-Stack AI Lifecycle: From transformer internals to cloud infrastructure, I manage the entire lifecycle from prototyping to production-level monitoring.

I operate effectively in ambiguity and move fast to build reliable systems. My resume is attached, and I am available for a technical conversation at your earliest convenience.

Swapnil Sonker
+91-6392672691"""

    # Send email with resume attachment
    try:
        msg = MIMEMultipart("mixed")   # 'mixed' supports both text + attachments
        msg["Subject"] = subject
        msg["From"]    = f"{YOUR_NAME} <{GMAIL_ADDRESS}>"
        msg["To"]      = email
        msg.attach(MIMEText(body, "plain"))

        # ── Attach resume PDF ──────────────────────────────────
        if RESUME_FILE and os.path.exists(RESUME_FILE):
            from email.mime.base import MIMEBase
            from email import encoders as email_encoders
            with open(RESUME_FILE, "rb") as pdf:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(pdf.read())
            email_encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{os.path.basename(RESUME_FILE)}"',
            )
            msg.attach(part)
            log.info(f"[EMAIL] Resume attached: {RESUME_FILE}")
        else:
            log.warning(f"[EMAIL] Resume not found at '{RESUME_FILE}' — sending without attachment.")

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, email, msg.as_string())

        today_str = str(datetime.today().date())
        # Update existing 'extracted' row if present, else append new row
        try:
            cell = sheet.find(email.lower().strip())
            sheet.update_cell(cell.row, SHEET_COLS["status"],     "emailed")
            sheet.update_cell(cell.row, SHEET_COLS["emailed_on"], today_str)
        except Exception:
            _log_contact_row(sheet, name, email, company)
            _update_row(sheet, email, SHEET_COLS["status"],     "emailed")
            _update_row(sheet, email, SHEET_COLS["emailed_on"], today_str)

        log.info(f"[AGENT] Email sent to {name} <{email}> at {company}")
        log_email_rate_entry(email, company, SEARCH_QUERY, subject)   # H: rate log
        time.sleep(random.uniform(12, 20))  # rate-limit between sends
        return {"success": True, "email": email, "subject": subject}

    except smtplib.SMTPRecipientsRefused:
        log.warning(f"Bounced / refused: {email}")
        _log_contact_row(sheet, name, email, company)
        _update_row(sheet, email, SHEET_COLS["status"], "bounced")
        return {"success": False, "reason": "email bounced/refused", "email": email}
    except Exception as e:
        log.error(f"SMTP error for {email}: {e}")
        return {"success": False, "reason": str(e), "email": email}


def tool_run_followups() -> dict:
    """
    Tool: run_followups
    Sends follow-up emails on day 3 and day 7 to contacts who haven't replied.
    """
    sheet = get_sheet()
    records = sheet.get_all_records()
    today = datetime.today().date()
    sent_count = 0

    for row in records:
        if str(row.get("Replied", "")).lower() == "yes":
            continue
        if row.get("Status", "") != "emailed":
            continue

        email   = str(row.get("Email", "")).strip()
        name    = row.get("Name", "Hiring Manager")
        company = row.get("Company", "")

        def _send_followup(followup_num):
            nonlocal sent_count
            prompt = f"""Write a short follow-up cold email. This is follow-up #{followup_num}.

HR Name: {name}
Company: {company}
Sender: {YOUR_NAME}, {YOUR_ROLE}

Rules:
- {"Very brief, 2-3 lines, friendly nudge" if followup_num == 1 else "Final, 2 lines, no pressure, leave door open"}
- Refer to the previous email
- No desperation
- Sign off as {YOUR_NAME}, {YOUR_PHONE}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""
            try:
                response = groq_client.chat.completions.create(
                    model=GROQ_MODEL, max_tokens=300,
                    messages=[{"role": "user", "content": prompt}]
                )
                raw = re.sub(r"```json|```", "", response.choices[0].message.content.strip()).strip()
                result = json.loads(raw)
                subj, bod = result["subject"], result["body"]
            except Exception:
                subj = f"Re: {YOUR_ROLE} Application — {company}"
                bod = (
                    f"Hi {name},\n\nJust following up on my previous email. Still very keen — happy to share more details.\n\nBest,\n{YOUR_NAME}\n{YOUR_PHONE}"
                    if followup_num == 1 else
                    f"Hi {name},\n\nOne last nudge — if the role is still open, I'd love to connect. No worries if timing isn't right.\n\n{YOUR_NAME}\n{YOUR_PHONE}"
                )

            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subj
                msg["From"]    = f"{YOUR_NAME} <{GMAIL_ADDRESS}>"
                msg["To"]      = email
                msg.attach(MIMEText(bod, "plain"))
                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                    server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                    server.sendmail(GMAIL_ADDRESS, email, msg.as_string())
                col = SHEET_COLS["f1_sent"] if followup_num == 1 else SHEET_COLS["f2_sent"]
                _update_row(sheet, email, col, str(today))
                sent_count += 1
                time.sleep(10)
                log.info(f"[AGENT] Follow-up #{followup_num} sent to {email}")
            except Exception as e:
                log.error(f"Follow-up SMTP error for {email}: {e}")

        if not row.get("F1 Sent") and row.get("Emailed On"):
            try:
                emailed_on = datetime.strptime(row["Emailed On"], "%Y-%m-%d").date()
                if (today - emailed_on).days >= FOLLOWUP_DAY_1:
                    _send_followup(1)
            except Exception as e:
                log.error(f"F1 date error for {email}: {e}")

        elif row.get("F1 Sent") and not row.get("F2 Sent"):
            try:
                f1_date = datetime.strptime(row["F1 Sent"], "%Y-%m-%d").date()
                if (today - f1_date).days >= (FOLLOWUP_DAY_2 - FOLLOWUP_DAY_1):
                    _send_followup(2)
            except Exception as e:
                log.error(f"F2 date error for {email}: {e}")

    log.info(f"[AGENT] Follow-ups sent: {sent_count}")
    return {"followups_sent": sent_count}


def tool_check_replies() -> dict:
    """
    Tool: check_replies
    Reads Gmail inbox for replies from contacted HRs. Updates Sheet.
    """
    try:
        creds = Credentials.from_authorized_user_file("gmail_token.json")
        service = build("gmail", "v1", credentials=creds)

        results = service.users().messages().list(userId="me", q="in:inbox is:unread").execute()
        messages = results.get("messages", [])
        contacted = get_contacted_emails_set()
        replied_emails = set()
        sheet = get_sheet()

        for msg in messages:
            full = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata", metadataHeaders=["From"]
            ).execute()
            headers = full.get("payload", {}).get("headers", [])
            from_header = next((h["value"] for h in headers if h["name"] == "From"), "")
            match = re.search(EMAIL_REGEX, from_header)
            if match:
                sender_email = match.group(0).lower().strip()
                if sender_email in contacted:
                    replied_emails.add(sender_email)

        for email in replied_emails:
            _update_row(sheet, email, SHEET_COLS["replied"], "yes")
            _update_row(sheet, email, SHEET_COLS["status"], "replied")
            log.info(f"[AGENT] Reply detected from: {email} — sequence stopped")

        return {"replies_detected": list(replied_emails), "count": len(replied_emails)}

    except FileNotFoundError:
        log.warning("gmail_token.json not found — skipping reply detection.")
        return {"replies_detected": [], "count": 0, "note": "gmail_token.json not found"}
    except Exception as e:
        log.error(f"Reply check error: {e}")
        return {"error": str(e), "replies_detected": []}


# ─────────────────────────────────────────
# Self-Healing Tool implementations
# ─────────────────────────────────────────

# The ONLY valid URL for DOM inspection — the LLM must not override this.
_CONTENT_SEARCH_URL = "https://www.linkedin.com/search/results/content/?keywords=hiring%20AI%20engineer"

def tool_inspect_page_dom(url: str) -> dict:
    """
    Tool: inspect_page_dom
    Opens the LinkedIn content search page with the saved session, takes a screenshot,
    and returns visible div class names + data attributes for the agent to analyse.
    NOTE: The url parameter is IGNORED — we always inspect the content search page
    to avoid the LLM navigating to wrong/dead URLs.
    """
    if not os.path.exists(BROWSER_PROFILE_DIR):
        return {"error": "No saved session. Run: python main.py login"}
    # Always force the correct URL regardless of what the LLM passed
    url = _CONTENT_SEARCH_URL

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    screenshot_path = os.path.join(
        SCREENSHOT_DIR,
        f"heal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    )

    with sync_playwright() as p:
        context = _launch_browser(p)
        page = context.new_page()
        try:
            page.goto(url, timeout=60000)
            time.sleep(random.uniform(3, 5))

            if "login" in page.url or "authwall" in page.url:
                context.close()
                return {"error": "Session expired. Run: python main.py login"}

            # Screenshot
            page.screenshot(path=screenshot_path, full_page=False)
            log.info(f"[HEAL] Screenshot saved: {screenshot_path}")

            # Collect all div class names that contain substantial text
            visible_classes = page.evaluate("""
                () => {
                    const divs = Array.from(document.querySelectorAll('div[class]'));
                    const seen = new Set();
                    const results = [];
                    for (const div of divs) {
                        const text = div.innerText || '';
                        if (text.trim().length > 80) {
                            for (const cls of div.classList) {
                                if (!seen.has(cls)) {
                                    seen.add(cls);
                                    results.push(cls);
                                }
                            }
                        }
                    }
                    return results.slice(0, 60);
                }
            """)

            # Extract data-urn / data-view-name attributes (stable LinkedIn selectors)
            data_attrs = page.evaluate("""
                () => {
                    const results = {};
                    // data-urn values (post identifiers)
                    const urns = Array.from(document.querySelectorAll('[data-urn]'))
                        .map(el => el.getAttribute('data-urn')).filter(Boolean).slice(0, 10);
                    results['data_urn_sample'] = urns;

                    // data-view-name values
                    const views = [...new Set(
                        Array.from(document.querySelectorAll('[data-view-name]'))
                        .map(el => el.getAttribute('data-view-name')).filter(Boolean)
                    )].slice(0, 20);
                    results['data_view_name_values'] = views;

                    // li elements with substantial text
                    const lis = Array.from(document.querySelectorAll('li'))
                        .filter(li => (li.innerText || '').trim().length > 80)
                        .map(li => li.className || '(no class)').slice(0, 10);
                    results['li_classes_with_text'] = lis;

                    // article elements
                    const arts = Array.from(document.querySelectorAll('article'))
                        .map(a => a.className || '(no class)').slice(0, 5);
                    results['article_classes'] = arts;

                    // any element with role=article or role=listitem
                    const roles = Array.from(document.querySelectorAll('[role="article"],[role="listitem"]'))
                        .map(el => el.tagName.toLowerCase() + (el.className ? '.' + el.className.split(' ')[0] : ''))
                        .slice(0, 10);
                    results['role_article_elements'] = roles;

                    return results;
                }
            """)

            context.close()
            return {
                "screenshot_saved": screenshot_path,
                "visible_div_classes": visible_classes,
                "data_attrs": data_attrs,
                "instruction": (
                    "Use data_attrs to build CSS selectors. "
                    "If data_urn_sample is not empty, use \"[data-urn]\" as selector. "
                    "If data_view_name_values has useful names, use \"[data-view-name='NAME']\" selectors. "
                    "If li_classes_with_text has classes, use \"li.CLASSNAME\" selectors. "
                    "Then call test_selector_patch with your proposed selectors."
                ),
            }

        except Exception as e:
            log.error(f"[HEAL] inspect_page_dom error: {e}")
            context.close()
            return {"error": str(e)}


def tool_test_selector_patch(selectors: list, url: str) -> dict:
    """
    Tool: test_selector_patch
    Trial-runs a list of CSS selectors against the LinkedIn search page.
    Returns how many elements matched and sample text — purely diagnostic,
    no emails sent.
    """
    if not os.path.exists(BROWSER_PROFILE_DIR):
        return {"error": "No saved session. Run: python main.py login"}
    if not selectors:
        return {"error": "selectors list is empty"}

    results = []
    with sync_playwright() as p:
        context = _launch_browser(p)
        page = context.new_page()
        try:
            page.goto(url, timeout=60000)
            time.sleep(random.uniform(3, 5))

            if "login" in page.url or "authwall" in page.url:
                context.close()
                return {"error": "Session expired"}

            for sel in selectors:
                try:
                    elements = page.locator(sel).all()
                    samples = []
                    for el in elements[:3]:
                        try:
                            t = el.inner_text(timeout=1500)
                            if t and len(t) > 30:
                                samples.append(t[:200])
                        except Exception:
                            pass
                    results.append({
                        "selector": sel,
                        "matched_count": len(elements),
                        "sample_texts": samples,
                    })
                    log.info(f"[HEAL] Selector '{sel}' matched {len(elements)} elements")
                except Exception as e:
                    results.append({"selector": sel, "matched_count": 0, "error": str(e)})

        except Exception as e:
            log.error(f"[HEAL] test_selector_patch error: {e}")
        finally:
            context.close()

    best = max(results, key=lambda r: r.get("matched_count", 0), default={})
    return {
        "results": results,
        "best_selector": best.get("selector"),
        "best_match_count": best.get("matched_count", 0),
        "instruction": (
            "If best_match_count > 0, call apply_selector_patch with the working selectors. "
            "If all matched 0, inspect the html_snippet more carefully and try different class names."
        ),
    }


def tool_apply_selector_patch(selectors: list, min_match_count: int = 0) -> dict:
    """
    Tool: apply_selector_patch
    Persists a validated list of CSS selectors to selector_config.json.
    IMPORTANT: Only call this after test_selector_patch confirmed best_match_count > 0.
    This function re-verifies the selectors itself against the real LinkedIn content
    search page — it does NOT trust the min_match_count parameter from the LLM.
    """
    global ACTIVE_SELECTORS
    if not selectors:
        return {"error": "selectors list is empty — not applied"}

    # ── Self-verification: re-test the selectors on the real page ────────────
    # We do NOT trust min_match_count passed by the LLM — we verify ourselves.
    log.info(f"[HEAL] Re-verifying selectors before applying: {selectors}")
    actual_best_count = 0
    actual_best_selector = None
    try:
        with sync_playwright() as p:
            ctx = _launch_browser(p)
            pg = ctx.new_page()
            try:
                pg.goto(_CONTENT_SEARCH_URL, timeout=60000)
                time.sleep(4)
                if "login" not in pg.url and "authwall" not in pg.url:
                    for sel in selectors:
                        try:
                            els = pg.locator(sel).all()
                            count = len(els)
                            # Only count elements that actually have substantial text
                            real_count = 0
                            for el in els[:5]:
                                try:
                                    t = el.inner_text(timeout=1500).strip()
                                    if len(t) >= 80:
                                        real_count += 1
                                except Exception:
                                    pass
                            log.info(f"[HEAL] Re-verify '{sel}': {count} DOM matches, {real_count} with text>=80 chars")
                            if real_count > actual_best_count:
                                actual_best_count = real_count
                                actual_best_selector = sel
                        except Exception as sel_err:
                            log.warning(f"[HEAL] Selector '{sel}' error: {sel_err}")
            finally:
                ctx.close()
    except Exception as e:
        log.error(f"[HEAL] Re-verify failed: {e}")
        return {"error": f"Self-verification failed: {e}"}

    if actual_best_count <= 0:
        log.warning(f"[HEAL] ❌ Refused to apply selectors {selectors} — re-verification found 0 elements with real post text on the content search page. These selectors do not match LinkedIn posts.")
        return {
            "error": (
                f"Refused: selectors {selectors} were re-tested against the real LinkedIn content search page "
                f"and matched 0 elements with substantial text. Do NOT call apply_selector_patch with these. "
                f"Call inspect_page_dom first, find selectors with data-urn or data-view-name attributes, "
                f"test with test_selector_patch, and only apply if best_match_count > 5."
            ),
            "current_active_selectors": ACTIVE_SELECTORS,
        }

    config = {
        "post_selectors": selectors,
        "updated_at": datetime.now().isoformat(),
        "previous_selectors": ACTIVE_SELECTORS,
        "validated_match_count": actual_best_count,
        "verified_by": "apply_selector_patch_self_check",
    }
    try:
        with open(SELECTOR_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        ACTIVE_SELECTORS = selectors
        log.info(f"[HEAL] ✅ Selector patch applied (self-verified): {selectors} (real post matches: {actual_best_count})")
        log.info(f"[HEAL] Saved to {SELECTOR_CONFIG_FILE}")
        return {
            "success": True,
            "active_selectors": ACTIVE_SELECTORS,
            "verified_match_count": actual_best_count,
            "config_file": SELECTOR_CONFIG_FILE,
            "instruction": "Selectors verified and updated. Now retry scrape_linkedin_posts with the original query.",
        }
    except Exception as e:
        log.error(f"[HEAL] Could not write {SELECTOR_CONFIG_FILE}: {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────
# Tool Schemas (OpenAI function-calling format)
# ─────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "scrape_linkedin_posts",
            "description": "Scrape LinkedIn search results and return post texts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":     {"type": "string",  "description": "Search query"},
                    "max_posts": {"type": "integer", "description": "Max posts (<=20)"}
                },
                "required": ["query", "max_posts"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "extract_contact",
            "description": "Extract email, name, company from a post. Returns found=false if no email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "post_text": {"type": "string", "description": "Post text"}
                },
                "required": ["post_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_already_contacted",
            "description": "Check if email is already in the Google Sheet tracker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Email to check"}
                },
                "required": ["email"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_outreach_email",
            "description": "Generate and send a cold email, log contact to Sheet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":      {"type": "string", "description": "Recruiter name"},
                    "email":     {"type": "string", "description": "Recruiter email"},
                    "company":   {"type": "string", "description": "Company name"},
                    "post_text": {"type": "string", "description": "Original post text"}
                },
                "required": ["name", "email", "company", "post_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_followups",
            "description": "Send day-3 and day-7 follow-ups to non-replied contacts.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_replies",
            "description": "Check Gmail for replies from contacted HRs, update Sheet.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_page_dom",
            "description": "HEAL: Open LinkedIn URL, return CSS classes and data-attrs to find new selectors.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "LinkedIn search URL"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "test_selector_patch",
            "description": "HEAL: Test CSS selectors on page, return match counts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selectors": {"type": "array", "items": {"type": "string"}, "description": "CSS selectors to test"},
                    "url":       {"type": "string", "description": "LinkedIn search URL"}
                },
                "required": ["selectors", "url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "apply_selector_patch",
            "description": "HEAL: Save selectors permanently. Requires min_match_count>0 from test_selector_patch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selectors":      {"type": "array", "items": {"type": "string"}, "description": "Validated selectors"},
                    "min_match_count": {"type": "integer", "description": "best_match_count from test (must be >0)"}
                },
                "required": ["selectors", "min_match_count"]
            }
        }
    }
]

# ─────────────────────────────────────────
# Tool Dispatcher
# ─────────────────────────────────────────

TOOL_REGISTRY = {
    "scrape_linkedin_posts":   tool_scrape_linkedin_posts,
    "extract_contact":         tool_extract_contact,
    "check_already_contacted": tool_check_already_contacted,
    "send_outreach_email":     tool_send_outreach_email,
    "run_followups":           tool_run_followups,
    "check_replies":           tool_check_replies,
    # Self-healing tools
    "inspect_page_dom":        tool_inspect_page_dom,
    "test_selector_patch":     tool_test_selector_patch,
    "apply_selector_patch":    tool_apply_selector_patch,
}

def dispatch_tool(name: str, args: dict) -> str:
    """Execute a tool by name and return its result as a JSON string."""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name}"})
    # Guard: LLM sometimes passes null instead of {} for no-arg tools
    # (run_followups, check_replies). This caused a Python crash:
    # 'argument after ** must be a mapping, not NoneType'
    if args is None:
        args = {}
    try:
        result = fn(**args)
        return json.dumps(result)
    except Exception as e:
        log.error(f"Tool '{name}' raised an exception: {e}")
        return json.dumps({"error": str(e)})

# ─────────────────────────────────────────
# System Prompt
# ─────────────────────────────────────────

SYSTEM_PROMPT = f"""You are a LinkedIn scraping agent for {YOUR_NAME} ({YOUR_ROLE}).

Your ONLY job is PHASE 1 — SCRAPE. Do nothing else.

PHASE 1 — SCRAPE (do this EXACTLY ONCE):
- Call scrape_linkedin_posts ONCE with the query you were given.
- ANY count >= 1: report success and STOP. The email pipeline will be handled separately.
- If count = 0: go to SELF-HEAL below. You may retry scrape ONE time after a heal. That is the absolute maximum.
- CRITICAL: If scrape returns "fatal_zero_posts": true, STOP immediately and report failure.

AFTER SCRAPE:
- Once scrape returns count >= 1, output a short summary: how many contacts were found, and confirm done.
- Do NOT call any other tool after a successful scrape.

ABSOLUTE RULES:
- NEVER scrape more than once. The system will hard-block any second scrape attempt.
- NEVER call extract_contact, send_outreach_email, check_already_contacted, run_followups, or check_replies.
- NEVER call inspect_page_dom, test_selector_patch, or apply_selector_patch after a successful scrape.
- NEVER invent contacts, emails, or post text.
- NEVER call apply_selector_patch unless test_selector_patch returned best_match_count > 0.
  When calling apply_selector_patch, pass min_match_count = the best_match_count value.

SELF-HEAL (ONLY when scrape returns count=0):
1. Call inspect_page_dom (url is ignored — it always inspects the correct LinkedIn search page).
2. Call test_selector_patch with selectors based on data_attrs returned.
3. If best_match_count > 0: call apply_selector_patch(selectors=..., min_match_count=best_match_count).
4. Retry scrape ONCE. If still 0, STOP and report failure.
"""

# ─────────────────────────────────────────
# Agent Loop (ReAct)
# ─────────────────────────────────────────

def run_agent(task: str = None):
    """
    Main agentic loop. The LLM autonomously decides which tools to call
    until the task is complete or MAX_AGENT_ITERATIONS is reached.
    """
    if task is None:
        task = (
            f"Scrape LinkedIn for '{SEARCH_QUERY}'. "
            f"Collect up to {MAX_POSTS_PER_RUN} new contacts with emails. "
            f"Save them to the CSV. Report how many were found."
        )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": task},
    ]

    log.info("=" * 60)
    log.info("[AGENT] Starting agentic HR outreach pipeline")
    log.info(f"[AGENT] Task: {task}")
    log.info("=" * 60)

    consecutive_zero_scrapes = 0  # kill-switch: stop after 2 consecutive zero-post scrapes

    # ── Hard programmatic locks ─────────────────────────────────────────────
    # scrape_done: set to True after the first successful scrape (count > 0).
    #   Any subsequent attempt by the LLM to call scrape_linkedin_posts is
    #   intercepted here — before it reaches the tool — and returns an error.
    # heal_tools_allowed: set to False once scrape succeeds. Prevents the LLM
    #   from calling inspect_page_dom / test_selector_patch / apply_selector_patch
    #   after data has already been collected (seen in the wild as hallucination).
    scrape_done        = False
    heal_tools_allowed = True   # allowed only while scrape count == 0
    HEAL_TOOL_NAMES    = {"inspect_page_dom", "test_selector_patch", "apply_selector_patch"}

    # Build a mutable copy of TOOLS so we can strip tools at runtime
    active_tools = list(TOOLS)

    for iteration in range(1, MAX_AGENT_ITERATIONS + 1):
        log.info(f"[AGENT] Iteration {iteration}/{MAX_AGENT_ITERATIONS}")
        time.sleep(3)   # avoid 429 rate-limit on Groq free tier

        # ── API call with retry on tool_use_failed ──────────────────
        response = None
        for attempt in range(1, 4):   # up to 3 retries
            try:
                response = groq_client.chat.completions.create(
                    model=GROQ_MODEL,
                    tools=active_tools,
                    tool_choice="auto",
                    messages=messages,
                    max_tokens=4096,
                )
                break   # success
            except Exception as api_err:
                err_str = str(api_err)
                if "tool_use_failed" in err_str or ("400" in err_str and "tool" in err_str.lower()):
                    log.warning(f"[AGENT] tool_use_failed on attempt {attempt}/3 — retrying...")
                    if attempt == 3:
                        log.error("[AGENT] 3 retries exhausted. Stopping agent.")
                        return
                    time.sleep(2 * attempt)
                elif "429" in err_str and "tokens per day" in err_str.lower():
                    log.error("[AGENT] ❌ Groq daily token limit reached (100K free tier).")
                    log.error("[AGENT] Options: wait until tomorrow, or upgrade at https://console.groq.com/settings/billing")
                    log.error("[AGENT] Alternatively set GROQ_MODEL=llama-3.1-8b-instant in .env (500K daily limit).")
                    print("\n❌ Daily token limit hit. Wait ~2hrs or set GROQ_MODEL=llama-3.1-8b-instant in .env\n")
                    return
                else:
                    raise

        choice = response.choices[0]
        message = choice.message

        # Append assistant message to history
        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                }
                for tc in (message.tool_calls or [])
            ] if message.tool_calls else []
        })

        # If the LLM produced a final text response (no tool calls), we're done
        if not message.tool_calls:
            log.info("[AGENT] Agent finished — no more tool calls.")
            if message.content:
                log.info(f"[AGENT] Summary:\n{message.content}")
                print(f"\n{'='*60}\nAGENT SUMMARY:\n{message.content}\n{'='*60}\n")
            break

        # Execute each tool call
        for tool_call in message.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            # ── Lock 1: Hard scraper block ───────────────────────────────────
            # If scrape already succeeded this run, reject any further scrape
            # calls in Python — the LLM never even gets to call the tool.
            if fn_name == "scrape_linkedin_posts" and scrape_done:
                blocked_msg = json.dumps({
                    "error": "HARD BLOCK: scrape_linkedin_posts has already run successfully "
                             "this session. You are NOT allowed to scrape again. "
                             "Move to PHASE 2 (extract_contact) immediately."
                })
                log.warning("[AGENT] 🔒 Blocked second scrape attempt — scrape_done=True")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": blocked_msg,
                })
                continue

            # ── Lock 2: Hard self-heal block ─────────────────────────────────
            # Once a successful scrape has been done, self-heal tools are
            # stripped from active_tools so the LLM cannot call them at all.
            # If the LLM somehow still requests one (e.g. from context memory),
            # intercept it here and return an error.
            if fn_name in HEAL_TOOL_NAMES and not heal_tools_allowed:
                blocked_msg = json.dumps({
                    "error": f"HARD BLOCK: {fn_name} is not available after a successful scrape. "
                             "Self-heal tools are only active when scraping returns 0 posts. "
                             "Move to PHASE 2 (extract_contact) immediately."
                })
                log.warning(f"[AGENT] 🔒 Blocked heal tool '{fn_name}' — heal_tools_allowed=False")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": blocked_msg,
                })
                continue

            log.info(f"[AGENT] → Calling tool: {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:120]})")
            result_str = dispatch_tool(fn_name, fn_args)
            log.info(f"[AGENT] ← Result: {result_str[:200]}")

            # ── Post-execution state machine ─────────────────────────────────
            try:
                result_json = json.loads(result_str)
                if fn_name == "scrape_linkedin_posts":
                    if result_json.get("status") == "no_new_posts":
                        log.info("[AGENT] ✅ Clean exit: No new posts available to scrape.")
                        print("\n✅ SCRAPE COMPLETE: No new posts found today. All contacts already processed. Check back later!\n")
                        return True   # signal: scrape ran cleanly (even if 0 new)
                    elif result_json.get("count", 0) > 0:
                        # Successful scrape — engage both locks immediately
                        scrape_done        = True
                        heal_tools_allowed = False
                        # Strip ALL non-essential tools — only self-heal tools needed for
                        # the failure path, everything else is handled outside the agent.
                        active_tools = [
                            t for t in active_tools
                            if t["function"]["name"] not in HEAL_TOOL_NAMES
                            and t["function"]["name"] != "scrape_linkedin_posts"
                        ]
                        log.info(
                            f"[AGENT] 🔒 Scrape successful ({result_json['count']} contacts). "
                            "Locked: scraper + heal tools removed. Handing off to mail agent."
                        )
                        return True   # signal: scrape successful, ready for mail handoff
                    else:
                        # Zero posts — self-heal remains allowed
                        consecutive_zero_scrapes += 1
                        log.warning(f"[AGENT] Zero-posts scrape #{consecutive_zero_scrapes}")
                        if consecutive_zero_scrapes >= 2:
                            log.error("[AGENT] ❌ HARD STOP: 2 consecutive zero-post scrapes. Stopping.")
                            print("\n❌ SCRAPING FAILED: 0 posts found on 2 queries. "
                                  "Run the script again — the self-heal will attempt selector repair.\n")
                            return False
            except (json.JSONDecodeError, AttributeError):
                pass

            # Feed tool result back into conversation
            # Cap at 1200 chars to keep context from blowing up on large results
            result_for_history = result_str[:1200] + ("...[truncated]" if len(result_str) > 1200 else "")
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_for_history,
            })

    else:
        log.warning(f"[AGENT] Reached max iterations ({MAX_AGENT_ITERATIONS}). Stopping.")
        return False


# ─────────────────────────────────────────
# LinkedIn login (one-time setup)
# ─────────────────────────────────────────

def login_session():
    """Run once: opens a real browser, you log in manually, session is saved."""
    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            BROWSER_PROFILE_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        page.goto("https://www.linkedin.com/login")
        print("\n>>> Log into LinkedIn manually in the opened browser window.")
        print(">>> Complete any 2FA / captcha if asked.")
        print(">>> Once you see your LinkedIn feed, come back here and press ENTER.\n")
        input("Press ENTER once logged in...")
        context.close()
        print(f"Session saved to {BROWSER_PROFILE_DIR}/. You can now run: python main.py")


# ─────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login_session()
    elif len(sys.argv) > 1 and sys.argv[1] == "followups":
        # Standalone follow-up / reply-check run via mail_agent
        import mail_agent
        mail_agent.run(dry_run=False, limit=DAILY_SEND_LIMIT)
    else:
        # ── Phase 1: Scrape ──────────────────────────────────────────────────
        scrape_ok = run_agent()

        # ── Phase 2 + 3 + 4: Mail (only if scrape succeeded) ────────────────
        if scrape_ok:
            log.info("=" * 60)
            log.info("[PIPELINE] Scrape complete. Handing off to mail agent...")
            log.info("=" * 60)
            import mail_agent
            mail_agent.run(dry_run=False, limit=DAILY_SEND_LIMIT)
        else:
            log.warning("[PIPELINE] Scrape did not succeed — mail agent not triggered.")
