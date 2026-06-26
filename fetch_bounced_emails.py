"""
fetch_bounced_emails.py
-----------------------
Connects to Gmail via IMAP, searches for Mail Delivery Subsystem bounce
emails, and extracts every bounced email address.
"""
import imaplib, email, re, os
from dotenv import load_dotenv

load_dotenv()

GMAIL   = os.getenv("GMAIL_ADDRESS", "")
APP_PWD = os.getenv("GMAIL_APP_PASSWORD", "")

# ── Connect ────────────────────────────────────────────────────────────────────
print(f"🔌 Connecting to Gmail as {GMAIL} ...")
mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
mail.login(GMAIL, APP_PWD)
mail.select("INBOX")

# ── Search for bounce/delivery-failure emails ──────────────────────────────────
# Gmail's Mail Delivery Subsystem always comes from mailer-daemon@googlemail.com
_, msg_ids = mail.search(None, 'FROM "mailer-daemon@googlemail.com"')

ids = msg_ids[0].split()
print(f"📬 Found {len(ids)} bounce notification(s)\n")

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

bounced = set()

for mid in ids:
    _, data = mail.fetch(mid, "(RFC822)")
    raw = data[0][1]
    msg = email.message_from_bytes(raw)

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("text/plain", "text/html"):
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

    # Look for "wasn't delivered to X" or "address ... not found" patterns
    # Also do a broad email scan on the body and match against known sent list
    found = EMAIL_REGEX.findall(body)
    for addr in found:
        lo = addr.lower()
        # Skip Gmail's own infra addresses
        if any(x in lo for x in ["googlemail", "gmail.com", "google.com",
                                   "gstatic", "googleapis", "postmaster"]):
            continue
        bounced.add(addr)

mail.logout()

# ── Print results ──────────────────────────────────────────────────────────────
print(f"{'─'*50}")
print(f"❌ Bounced / undeliverable addresses ({len(bounced)} found):")
print(f"{'─'*50}")
for addr in sorted(bounced):
    print(addr)
