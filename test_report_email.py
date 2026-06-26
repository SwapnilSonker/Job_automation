"""
test_report_email.py
--------------------
Sends BOTH sample reports so you can preview the new email designs.
  1. Notification report  → swapnilsonker04@gmail.com  (NOTIFICATION_EMAIL)
  2. Curated leads report → REPORT_EMAILS recipients
Run: python test_report_email.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# pyrefly: ignore [missing-import]
from mail_agent import send_run_report
# pyrefly: ignore [missing-import]
from main import send_curated_sheet_report

# ── Dummy contacts (simulating today's run) ────────────────────────────────────
SAMPLE_CONTACTS = [
    {
        "name":       "Priya Sharma",
        "company":    "Razorpay",
        "email":      "priya.sharma@razorpay.com",
        "subject":    "AI Engineer Application — Razorpay",
        "snippet":    "We are hiring an AI Engineer to build RAG-based financial document pipelines. Experience with LangChain, FastAPI, and GCP is required.",
        "jd_summary": "Razorpay is hiring an AI Engineer to build RAG-based financial document pipelines using LangChain and FastAPI on GCP.",
        "status":     "SENT",
    },
    {
        "name":       "Rahul Mehta",
        "company":    "Swiggy",
        "email":      "r.mehta@swiggy.in",
        "subject":    "GenAI Developer Application — Swiggy",
        "snippet":    "Looking for a GenAI Developer with experience in multi-agent systems and real-time inference. Must have worked with vector databases.",
        "jd_summary": "Swiggy is hiring a GenAI Developer with multi-agent systems experience and knowledge of vector databases like Pinecone.",
        "status":     "SENT",
    },
    {
        "name":       "Anjali Verma",
        "company":    "PhonePe",
        "email":      "anjali.v@phonepe.com",
        "subject":    "ML Engineer Application — PhonePe",
        "snippet":    "Hiring for ML Engineer role focused on fraud detection using transformer models and gradient boosting.",
        "jd_summary": "PhonePe is hiring an ML Engineer for fraud detection using transformer models and gradient boosting frameworks.",
        "status":     "FAILED",
    },
    {
        "name":       "Hiring Team",
        "company":    "Zepto",
        "email":      "careers@zepto.com",
        "subject":    "AI Engineer Application — Zepto",
        "snippet":    "Seeking a passionate AI Engineer to work on LLM fine-tuning and real-time recommendation systems at scale.",
        "jd_summary": "Zepto is hiring an AI Engineer to work on LLM fine-tuning and real-time recommendation systems at scale.",
        "status":     "SENT",
    },
]

if __name__ == "__main__":
    print("\n1️⃣  Sending notification report to swapnilsonker04@gmail.com ...")
    send_run_report(
        contacts_processed=SAMPLE_CONTACTS,
        sent_count=3,
        failed_count=1,
        dry_run=False,
    )

    print("\n2️⃣  Sending curated leads report to REPORT_EMAILS ...")
    send_curated_sheet_report(
        recipient_email=os.getenv("REPORT_EMAILS", ""),
        today_contacts=SAMPLE_CONTACTS,
    )

    print("\n✅ Done — check both inboxes!")
