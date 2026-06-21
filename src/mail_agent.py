"""
mail_agent.py
=============
Standalone mailing agent — reads extracted_contacts.csv, deduplicates
against already-contacted emails (email_rate_log.json), drafts a
personalised outreach email for each new contact using Groq + the
resume_structure.md skeleton, attaches Swapnil_Sonker_s.pdf, and
fires it via Gmail SMTP.

Usage:
    python mail_agent.py            # live send
    python mail_agent.py --dry-run  # print emails, do NOT send
    python mail_agent.py --limit 5  # send at most 5 emails this run
"""

import os, sys, json, csv, re, time, smtplib, logging, argparse
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path

# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from groq import Groq

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()

# The groq SDK auto-reads GROQ_BASE_URL from the environment and then appends
# its own /openai/v1 path on top — causing a 404 "double path" bug.
# Pop it so the SDK uses its own default endpoint correctly.
os.environ.pop("GROQ_BASE_URL", None)

GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL        = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GMAIL_ADDRESS     = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD= os.getenv("GMAIL_APP_PASSWORD", "")
YOUR_NAME         = os.getenv("YOUR_NAME", "Swapnil Sonker")
YOUR_PHONE        = os.getenv("YOUR_PHONE", "+91-6392672691")
RESUME_FILE       = os.getenv("RESUME_FILE", "Swapnil_Sonker_s.pdf")
CONTACTS_LOG_FILE = os.getenv("CONTACTS_LOG_FILE", "extracted_contacts.csv")
RATELOG_FILE      = os.getenv("RATELOG_FILE", "email_rate_log.json")
DAILY_SEND_LIMIT  = int(os.getenv("DAILY_SEND_LIMIT", "40"))

BASE_DIR = Path(__file__).parent.parent

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mail_agent")

# ── Resume skeleton (the structure from resume_structure.md) ──────────────────
RESUME_SKELETON = """
Hi,

I am writing to express my interest in the {role} role, as my background in building production-grade Generative AI solutions aligns directly with your requirements for end-to-end ML system design and deployment. I am an AI Engineer at Occams Advisory, where I recently designed and shipped TaxVantage AI—a multi-agent OCR and document intelligence platform that utilises a production MCP server to process complex financial data.

My experience directly maps to your key responsibilities:

• GenAI & RAG Development: I have built and deployed RAG agents and form automation pipelines using LangChain, LangGraph, and FastAPI. I am proficient in extending these pipelines with vector databases and implementing complex reasoning flows.
• Fine-Tuning & Model Evaluation: I have hands-on experience fine-tuning Llama 3.2-1B for domain-specific tasks and am currently tuning transformer self-attention mechanisms and gradient-boosting models like CatBoost and XGBoost.
• Production ML & Cloud: I have experience deploying ML workloads across GCP, Firebase, and Cloudflare using Docker. My work includes engineering outbound Voice AI agents with real-time LLM reasoning and automated LinkedIn scraping workflows.
• Full-Stack AI Lifecycle: From transformer internals to cloud infrastructure, I manage the entire lifecycle from prototyping to production-level monitoring.

I operate effectively in ambiguity and move fast to build reliable systems. My resume is attached, and I am available for a technical conversation at your earliest convenience.

{your_name}
{your_phone}
""".strip()


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_already_contacted() -> set:
    """Return a set of email addresses we have already sent to."""
    path = BASE_DIR / RATELOG_FILE
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return {entry["email"].lower() for entry in data if "email" in entry}
    except Exception:
        return set()


def append_rate_log(email: str, company: str, subject: str) -> None:
    """Append a sent-email record to email_rate_log.json."""
    path = BASE_DIR / RATELOG_FILE
    try:
        data = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        data = []
    data.append({
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "email":   email,
        "company": company,
        "subject": subject,
        "replied": False,
    })
    path.write_text(json.dumps(data, indent=2))


def load_contacts(already_contacted: set) -> list[dict]:
    """
    Read extracted_contacts.csv, deduplicate by email, and skip
    any email already in the rate log. Returns unique new contacts.
    """
    path = BASE_DIR / CONTACTS_LOG_FILE
    if not path.exists():
        log.error(f"Contacts file not found: {path}")
        return []

    seen_emails: set = set()
    contacts: list[dict] = []

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row.get("Email", "").strip()
            if not email or not re.match(r"[^@]+@[^@]+\.[^@]+", email):
                continue                          # skip invalid / empty
            email_lower = email.lower()
            if email_lower in seen_emails:
                continue                          # deduplicate within CSV
            if email_lower in already_contacted:
                log.info(f"[SKIP] Already contacted: {email}")
                continue
            seen_emails.add(email_lower)

            # Use the JD summary if present (it's stored in Post Snippet column
            # by the scraper when it has a multi-line summary), otherwise fall
            # back to the raw post snippet.
            post_snippet = row.get("Post Snippet", "").strip()

            contacts.append({
                "email":   email,
                "name":    row.get("Name", "Hiring Manager").strip() or "Hiring Manager",
                "company": row.get("Company", "").strip(),
                "snippet": post_snippet[:800],    # cap at 800 chars for the LLM
            })

    log.info(f"[LOAD] {len(contacts)} new unique contacts to process")
    return contacts


def infer_role(snippet: str, company: str) -> str:
    """Quick heuristic to extract the role title from the post snippet."""
    patterns = [
        r"hiring\s+(?:an?\s+)?([A-Za-z /&+]+(?:Engineer|Developer|Architect|Lead|Manager|Scientist|Analyst|Researcher))",
        r"(?:role|position|opening)[:\s]+([A-Za-z /&+]+(?:Engineer|Developer|Architect|Lead|Manager|Scientist|Analyst|Researcher))",
    ]
    for pat in patterns:
        m = re.search(pat, snippet, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:60]
    return "AI Engineer"   # sensible default


def draft_email_with_llm(client: Groq, contact: dict) -> tuple[str, str]:
    """
    Use Groq to personalise the resume skeleton for this specific contact.
    Returns (subject, body).
    """
    role    = infer_role(contact["snippet"], contact["company"])
    company = contact["company"] or "your company"
    name    = contact["name"]

    # ── Edge Case: Empty JD Fallback ───────────────────────────────────────
    if len(contact["snippet"].strip()) < 10:
        log.info(f"[EMAIL] Empty JD for {contact.get('email', 'unknown')}. Bypassing AI and sending raw skeleton.")
        subject = f"{role} Application — {company}"
        body = RESUME_SKELETON.format(role=role, your_name=YOUR_NAME, your_phone=YOUR_PHONE)
        return subject, body

    system_prompt = (
        "You are an expert job-application email writer. "
        "Your task is to personalise the candidate's cover-letter template "
        "for a specific job posting. Output ONLY two sections separated by "
        "a line containing exactly '---BODY---'. The first section is the "
        "email subject line (no label prefix, just the text). "
        "The second section is the full email body. "
        "Keep the body tight, professional, and under 250 words. "
        "Do NOT add any extra commentary outside those two sections."
    )

    user_prompt = f"""
Job posting snippet:
\"\"\"{contact['snippet']}\"\"\"

Candidate template:
\"\"\"{RESUME_SKELETON.format(role=role, your_name=YOUR_NAME, your_phone=YOUR_PHONE)}\"\"\"

Addressee name : {name}
Company        : {company}
Role           : {role}

CRITICAL INSTRUCTIONS:
1. ONLY personalise the opening and closing paragraphs to reference specific skills or requirements mentioned in the job posting.
2. DO NOT alter, rewrite, or fabricate any of the 4 technical bullet points. They must remain exactly identical to the candidate template.
3. Keep {YOUR_NAME}'s credentials 100% honest and accurate.
Output the subject line first, then '---BODY---', then the email body.
"""

    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.4,
        max_tokens=600,
    )

    raw = resp.choices[0].message.content.strip()

    # Parse subject / body
    if "---BODY---" in raw:
        parts   = raw.split("---BODY---", 1)
        subject = parts[0].strip()
        body    = parts[1].strip()
    else:
        # Fallback: first line = subject, rest = body
        lines   = raw.splitlines()
        subject = lines[0].strip() if lines else f"{role} Application — {company}"
        body    = "\n".join(lines[1:]).strip() if len(lines) > 1 else raw

    # Safety net for subject
    if not subject or len(subject) > 120:
        subject = f"{role} Application — {company}"

    return subject, body


def validate_email_with_llm(client: Groq, subject: str, body: str) -> tuple[str, str]:
    """
    Validator Agent: Cross-checks the drafted email against the candidate's true constraints.
    Prevents hallucinating years of experience or false skills.
    """
    system_prompt = (
        "You are a strict QA Validator for job application emails. "
        "Your job is to read the drafted email below and ensure it does NOT hallucinate "
        "or lie about the candidate's years of experience. "
        "TRUE FACTS: The candidate is early-career (1-2 years of experience). They do NOT have 5+ years of experience. "
        "They are highly skilled in GenAI, RAG, and FastAPI (e.g., building TaxVantage AI). "
        "TASK: "
        "If the email falsely claims 5+ years of experience or senior status, REWRITE it to be 100% truthful "
        "but highly persuasive by pivoting the focus to the complex, production-level projects they have actually built. "
        "If the email is already truthful and does not lie about years of experience, return it exactly as is. "
        "Output ONLY two sections separated by a line containing exactly '---BODY---'. "
        "The first section is the subject line. The second section is the full email body."
    )

    user_prompt = f"Drafted Subject:\n{subject}\n\nDrafted Body:\n{body}"

    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=600,
    )

    raw = resp.choices[0].message.content.strip()

    if "---BODY---" in raw:
        parts   = raw.split("---BODY---", 1)
        new_subject = parts[0].strip()
        new_body    = parts[1].strip()
        return new_subject, new_body
    
    # Fallback to original if formatting fails
    return subject, body


def send_email(to_email: str, subject: str, body: str, dry_run: bool) -> bool:
    """Send the email via Gmail SMTP with resume attached. Returns success bool."""
    resume_path = BASE_DIR / RESUME_FILE
    if not resume_path.exists():
        log.error(f"[EMAIL] Resume file not found: {resume_path}")
        return False

    msg = MIMEMultipart()
    msg["From"]    = f"{YOUR_NAME} <{GMAIL_ADDRESS}>"
    msg["To"]      = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    # Attach resume PDF
    with open(resume_path, "rb") as f:
        part = MIMEApplication(f.read(), Name=resume_path.name)
        part["Content-Disposition"] = f'attachment; filename="{resume_path.name}"'
        msg.attach(part)

    if dry_run:
        log.info(f"[DRY-RUN] Would send to {to_email}")
        log.info(f"[DRY-RUN] Subject : {subject}")
        log.info(f"[DRY-RUN] Body    :\n{body}\n{'─'*60}")
        return True

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())
        log.info(f"[EMAIL] ✅ Sent → {to_email} | {subject}")
        return True
    except Exception as e:
        log.error(f"[EMAIL] ❌ Failed → {to_email} | {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, limit: int = DAILY_SEND_LIMIT) -> None:
    log.info("=" * 60)
    log.info("[MAIL AGENT] Starting standalone mailing agent")
    log.info(f"[MAIL AGENT] Mode: {'DRY-RUN' if dry_run else 'LIVE SEND'} | Limit: {limit}")
    log.info("=" * 60)

    # 1. Load already-contacted set
    already_contacted = load_already_contacted()
    log.info(f"[MAIL AGENT] Already contacted: {len(already_contacted)} emails")

    # 2. Load unique new contacts from CSV
    contacts = load_contacts(already_contacted)
    if not contacts:
        log.warning("[MAIL AGENT] No new contacts to process. Exiting.")
        return
            
    # 3. Apply per-run send limit
    if len(contacts) > limit:
        log.info(f"[MAIL AGENT] Capping to {limit} contacts (of {len(contacts)} found)")
        contacts = contacts[:limit]

    # 4. Initialise Groq client
    # NOTE: Do NOT pass base_url here. The groq SDK already has the correct
    # endpoint built-in. Passing GROQ_BASE_URL (https://api.groq.com/openai/v1)
    # causes the SDK to append /openai/v1 again → 404 double-path bug.
    client = Groq(api_key=GROQ_API_KEY)

    # 5. Process each contact
    sent_count   = 0
    failed_count = 0

    for i, contact in enumerate(contacts, start=1):
        log.info(f"[{i}/{len(contacts)}] Processing: {contact['email']} ({contact['company'] or 'unknown company'})")

        # Draft email via LLM
        try:
            subject, body = draft_email_with_llm(client, contact)
            # Validator Agent Pass
            subject, body = validate_email_with_llm(client, subject, body)
        except Exception as e:
            log.error(f"[LLM] Failed to draft email for {contact['email']}: {e}")
            failed_count += 1
            time.sleep(2)
            continue

        # Send (or dry-run)
        success = send_email(contact["email"], subject, body, dry_run)

        if success:
            sent_count += 1
            if not dry_run:
                append_rate_log(contact["email"], contact["company"], subject)
        else:
            failed_count += 1

        # Polite delay between sends to avoid Gmail rate limiting
        if i < len(contacts):
            pause = 3 if dry_run else 8
            time.sleep(pause)

    # 6. Summary
    log.info("=" * 60)
    log.info("[MAIL AGENT] Run complete")
    log.info(f"  Contacts processed : {len(contacts)}")
    log.info(f"  Emails sent        : {sent_count}")
    log.info(f"  Failed             : {failed_count}")
    log.info(f"  Mode               : {'DRY-RUN (nothing sent)' if dry_run else 'LIVE'}")
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone mailing agent for HR outreach")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print drafted emails to console, do NOT actually send anything",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DAILY_SEND_LIMIT,
        help=f"Max emails to send in this run (default: {DAILY_SEND_LIMIT})",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run, limit=args.limit)
