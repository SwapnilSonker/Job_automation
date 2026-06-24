"""
test_report_email.py
--------------------
Sends a sample HTML notification report to NOTIFICATION_EMAIL
so you can preview the new email design.
Run: python test_report_email.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# pyrefly: ignore [missing-import]
from mail_agent import send_run_report

# ── Dummy lead data ────────────────────────────────────────────────────────────
SAMPLE_CONTACTS = [
    {
        "name":    "Priya Sharma",
        "company": "Razorpay",
        "email":   "priya.sharma@razorpay.com",
        "subject": "AI Engineer Application — Razorpay",
        "snippet": "We are hiring an AI Engineer to build RAG-based financial document pipelines. "
                   "Experience with LangChain, FastAPI, and GCP is required. You will work "
                   "on automating complex document workflows using LLMs.",
        "status":  "SENT",
    },
    {
        "name":    "Rahul Mehta",
        "company": "Swiggy",
        "email":   "r.mehta@swiggy.in",
        "subject": "GenAI Developer Application — Swiggy",
        "snippet": "Looking for a GenAI Developer with experience in multi-agent systems and "
                   "real-time inference. Must have worked with vector databases like Pinecone or Weaviate.",
        "status":  "SENT",
    },
    {
        "name":    "Anjali Verma",
        "company": "PhonePe",
        "email":   "anjali.v@phonepe.com",
        "subject": "ML Engineer Application — PhonePe",
        "snippet": "Hiring for ML Engineer role focused on fraud detection using transformer models "
                   "and gradient boosting. Strong Python and cloud deployment skills needed.",
        "status":  "FAILED",
    },
    {
        "name":    "Hiring Team",
        "company": "Zepto",
        "email":   "careers@zepto.com",
        "subject": "AI Engineer Application — Zepto",
        "snippet": "Seeking a passionate AI Engineer to join our fast-growing team. "
                   "Work on LLM fine-tuning and real-time recommendation systems at scale.",
        "status":  "SENT",
    },
]

if __name__ == "__main__":
    print("📧 Sending test HTML report email...")
    send_run_report(
        contacts_processed=SAMPLE_CONTACTS,
        sent_count=3,
        failed_count=1,
        dry_run=False,
    )
    print("✅ Done — check your inbox!")
