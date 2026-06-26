"""
send_real_report.py
-------------------
Reads today's actually-sent contacts from email_rate_log.json + extracted_contacts.csv
and fires the real curated report to REPORT_EMAILS.
"""
import sys, os, json
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
load_dotenv()

# pyrefly: ignore [missing-import]
from main import send_curated_sheet_report

BASE_DIR   = Path(__file__).parent
RATELOG    = BASE_DIR / os.getenv("RATELOG_FILE", "data/email_rate_log.json")
CONTACTS   = BASE_DIR / os.getenv("CONTACTS_LOG_FILE", "data/extracted_contacts.csv")
TODAY      = str(date.today())

# ── Load today's sent entries from rate log ────────────────────────────────────
try:
    rate_data = json.loads(RATELOG.read_text())
except Exception as e:
    print(f"❌ Could not read rate log: {e}")
    sys.exit(1)

today_emails = {}
for entry in rate_data:
    sent_at = entry.get("sent_at", "")
    if TODAY in sent_at:
        today_emails[entry["email"].lower()] = entry

print(f"📅 Today ({TODAY}): {len(today_emails)} emails sent")

# ── Match with CSV to get JD summaries ────────────────────────────────────────
import csv
today_contacts = []
try:
    with open(CONTACTS, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row.get("Email", "").strip().lower()
            if email in today_emails:
                today_contacts.append({
                    "email":      row.get("Email", "").strip(),
                    "name":       row.get("Name", ""),
                    "company":    row.get("Company", ""),
                    "jd_summary": row.get("Post Snippet", "")[:500],
                    "status":     "SENT",
                })
except Exception as e:
    print(f"⚠️  Could not read contacts CSV: {e}")
    # Fall back to just emails from rate log
    for email, entry in today_emails.items():
        today_contacts.append({
            "email":      entry["email"],
            "name":       "",
            "company":    entry.get("company", ""),
            "jd_summary": f"Subject sent: {entry.get('subject', 'N/A')}",
            "status":     "SENT",
        })

print(f"✅ Matched {len(today_contacts)} contacts with JD data")

# ── Send the real curated report ───────────────────────────────────────────────
report_emails = os.getenv("REPORT_EMAILS", os.getenv("NOTIFICATION_EMAIL", ""))
if not report_emails:
    print("❌ No REPORT_EMAILS configured in .env")
    sys.exit(1)

print(f"📧 Sending real curated report to: {report_emails}")
send_curated_sheet_report(recipient_email=report_emails, today_contacts=today_contacts)
print("✅ Done!")
