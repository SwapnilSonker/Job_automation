"""
role_agent.py
=============
Stateful agent that reads the user's resume and suggests highly optimized 
LinkedIn job titles. Saves a prioritized queue of roles to a config file
so the main scraper can auto-pivot if the primary role is a dry well.
"""

import os
import sys
import json
import select
from pathlib import Path
from dotenv import load_dotenv
from groq import Groq

load_dotenv()
os.environ.pop("GROQ_BASE_URL", None)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
BASE_DIR = Path(__file__).parent.parent
RESUME_FILE_PATH = BASE_DIR / "assets" / "resume_structure.md"
CONFIG_FILE_PATH = BASE_DIR / "config" / "active_search_roles.json"

def _input_with_timeout(prompt: str, timeout: int = 10) -> str:
    """Prompt for input with a timeout (works on Mac/Linux)."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        return sys.stdin.readline().strip()
    print("\n[Timeout reached. Auto-continuing...]")
    return ""

def _fetch_new_roles_from_llm() -> list:
    """Read resume and use Groq to generate 5 tailored job titles."""
    print("\n🔍 Analyzing your resume to find the best LinkedIn job titles...")
    
    if not GROQ_API_KEY:
        print("❌ Error: GROQ_API_KEY not found in .env")
        return []
        
    if not RESUME_FILE_PATH.exists():
        print(f"❌ Error: Resume file not found at {RESUME_FILE_PATH}")
        return []

    with open(RESUME_FILE_PATH, "r", encoding="utf-8") as f:
        resume_text = f.read()

    client = Groq(api_key=GROQ_API_KEY)

    system_prompt = (
        "You are an expert tech recruiter and career strategist. "
        "Your task is to analyze the candidate's resume/profile and suggest 5 specific "
        "Job Titles that perfectly match their exact skill set."
    )

    user_prompt = f"""
Here is my resume/cover letter template containing my core skills and experience:

{resume_text}

Based on my specific experience, suggest 5 different exact Job Titles I should use as my search queries on LinkedIn. 
IMPORTANT: Do NOT add 'hiring' or any other keywords. I want ONLY the raw job titles.

Return the output as a raw JSON object containing a single key "roles" mapping to a list of strings.
Example format:
{{
  "roles": ["Generative AI Engineer", "Machine Learning Engineer", "Python Developer"]
}}
"""

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            max_tokens=800,
        )
        
        raw_json = response.choices[0].message.content.strip()
        data = json.loads(raw_json)
        return data.get("roles", [])
    except Exception as e:
        print(f"❌ Failed to get suggestions from Groq: {e}")
        return []

def get_or_update_roles() -> list:
    """
    Main entrypoint: Check saved roles, optionally fetch new ones, and 
    return a prioritized list of job titles to search for.
    """
    saved_roles = []
    if CONFIG_FILE_PATH.exists():
        try:
            with open(CONFIG_FILE_PATH, "r") as f:
                saved_roles = json.load(f)
        except Exception:
            pass

    if saved_roles:
        print(f"\n📂 Currently targeting priority queue: {saved_roles}")
        print(f"   Primary Role: >> {saved_roles[0]} <<")
        ans = _input_with_timeout("Press ENTER to keep these, or type 'new' for fresh suggestions (10s timeout): ", 10)
        if ans.lower().strip() != 'new':
            return saved_roles
            
    # We need new roles
    new_roles = _fetch_new_roles_from_llm()
    if not new_roles:
        # Fallback if LLM failed
        return saved_roles if saved_roles else ["AI Engineer", "Machine Learning Engineer"]

    print("\n🎯 Recommended Job Titles:")
    for i, role in enumerate(new_roles, 1):
        print(f"  {i}. {role}")
        
    choice_str = _input_with_timeout("\nEnter the NUMBER of your primary choice (1-5) (10s timeout defaults to 1): ", 10)
    
    try:
        idx = int(choice_str) - 1
        if idx < 0 or idx >= len(new_roles):
            idx = 0
    except ValueError:
        idx = 0

    # Reorder queue so chosen is first
    primary = new_roles.pop(idx)
    final_queue = [primary] + new_roles
    
    # Save to config
    os.makedirs(CONFIG_FILE_PATH.parent, exist_ok=True)
    with open(CONFIG_FILE_PATH, "w") as f:
        json.dump(final_queue, f, indent=2)
        
    print(f"\n✅ Saved queue. Primary target is: {primary}\n")
    return final_queue

if __name__ == "__main__":
    get_or_update_roles()
