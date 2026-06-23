"""
suggest_searches.py
===================
Standalone script that analyzes the user's resume (assets/resume_structure.md)
using the Groq LLM and suggests optimal LinkedIn search queries for HR outreach.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from groq import Groq

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
os.environ.pop("GROQ_BASE_URL", None)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
BASE_DIR = Path(__file__).parent.parent
RESUME_FILE_PATH = BASE_DIR / "assets" / "resume_structure.md"

def suggest_search_queries():
    print("\n🔍 Analyzing your resume to find the best LinkedIn search queries...\n")
    
    if not GROQ_API_KEY:
        print("❌ Error: GROQ_API_KEY not found in .env")
        return
        
    if not RESUME_FILE_PATH.exists():
        print(f"❌ Error: Resume file not found at {RESUME_FILE_PATH}")
        return

    # Read the resume content
    with open(RESUME_FILE_PATH, "r", encoding="utf-8") as f:
        resume_text = f.read()

    # Initialize Groq Client
    client = Groq(api_key=GROQ_API_KEY)

    system_prompt = (
        "You are an expert tech recruiter and career strategist. "
        "Your task is to analyze the candidate's resume/profile and suggest 5 specific "
        "Job Titles that perfectly match their exact skill set."
    )

    user_prompt = f"""
Here is my resume/cover letter template containing my core skills and experience:

{resume_text}

Based on my specific experience (especially the projects and technologies I have worked with), 
suggest 5 different exact Job Titles I should use as my search queries on LinkedIn. 

IMPORTANT: Do NOT add 'hiring' or any other keywords. I want ONLY the raw job titles 
(e.g., 'Generative AI Engineer', 'Machine Learning Engineer').

Format the output cleanly as a numbered list with the Job Title in quotes, followed by a 1-sentence explanation of why it fits.
"""

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            max_tokens=800,
        )
        
        suggestions = response.choices[0].message.content.strip()
        print("🎯 Recommended LinkedIn Search Queries:\n")
        print(suggestions)
        print("\n" + "="*60 + "\n")
        
    except Exception as e:
        print(f"❌ Failed to get suggestions from Groq: {e}")

if __name__ == "__main__":
    suggest_search_queries()
