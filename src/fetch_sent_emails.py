import os
import imaplib
import email
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
import csv
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

def decode_mime_words(s):
    if not s:
        return ""
    decoded_words = decode_header(s)
    out = []
    for word, charset in decoded_words:
        if isinstance(word, bytes):
            try:
                out.append(word.decode(charset or 'utf-8'))
            except Exception:
                out.append(word.decode('latin1', errors='replace'))
        else:
            out.append(word)
    return "".join(out)

def fetch_from_folder(mail, folder_names):
    selected = False
    for folder in folder_names:
        status, _ = mail.select(folder)
        if status == 'OK':
            selected = True
            print(f"Selected folder: {folder}", flush=True)
            break
            
    if not selected:
        print(f"Could not select any of folders {folder_names}", flush=True)
        return []

    status, messages = mail.search(None, "ALL")
    if status != 'OK' or not messages[0]:
        print("No emails found or could not search.", flush=True)
        return []

    email_ids = messages[0].split()
    total_found = len(email_ids)
    
    print(f"Found {total_found} emails in folder. Extracting headers...", flush=True)
    
    results = []
    
    # Process in chunks of 100 to avoid IMAP socket drops from too many commands
    chunk_size = 100
    for i in range(0, total_found, chunk_size):
        chunk = email_ids[i:i+chunk_size]
        print(f"Processed {min(i+chunk_size, total_found)}/{total_found} in folder...", flush=True)
        
        # Join the IDs with commas
        id_str = b",".join(chunk).decode('ascii')
        try:
            status, msg_data = mail.fetch(id_str, '(BODY.PEEK[HEADER.FIELDS (TO SUBJECT DATE)])')
            if status != 'OK':
                continue
                
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    to_header = msg.get("To", "")
                    if not to_header:
                        continue
                    
                    addresses = to_header.split(',')
                    for addr_str in addresses:
                        name, email_address = parseaddr(addr_str)
                        email_address = email_address.lower().strip()
                        
                        if not email_address or '@' not in email_address:
                            continue
                            
                        subject = decode_mime_words(msg.get("Subject", ""))
                        
                        date_header = msg.get("Date", "")
                        try:
                            date_obj = parsedate_to_datetime(date_header)
                            date_str = date_obj.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            date_str = date_header

                        results.append({
                            "Email Address": email_address,
                            "Name": decode_mime_words(name),
                            "Subject": subject,
                            "Date": date_str
                        })
        except Exception as e:
            print(f"Error fetching chunk: {e}", flush=True)
            # Reconnect if needed, or just skip chunk (simplification)
            pass
            
    # Reverse results so newest is first
    results.reverse()
    return results

def get_all_emails():
    GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
    GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("Error: GMAIL_ADDRESS or GMAIL_APP_PASSWORD missing from .env")
        return []

    print(f"Logging in as {GMAIL_ADDRESS}...", flush=True)
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    except Exception as e:
        print(f"Login failed: {e}")
        return []
    
    # 1. Fetch Sent Emails
    print("--- Fetching Sent Emails ---", flush=True)
    sent_emails = fetch_from_folder(mail, ['"[Gmail]/Sent Mail"', '"Sent"'])
    
    # 2. Fetch Drafts
    print("--- Fetching Drafts ---", flush=True)
    draft_emails = fetch_from_folder(mail, ['"[Gmail]/Drafts"', '"Drafts"'])
    
    # Try closing the mailbox gracefully, though it might error if no mailbox is selected
    try:
        mail.close()
    except Exception:
        pass
    mail.logout()
    
    # 3. Combine and Deduplicate
    unique_emails = {}
    
    # Process Drafts first
    for item in draft_emails:
        item["Source Folder"] = "Draft"
        unique_emails[item["Email Address"]] = item
        
    # Process Sent. If email exists, it overrides the draft entry.
    for item in sent_emails:
        if item["Email Address"] in unique_emails:
            item["Source Folder"] = "Both (Sent & Draft)"
        else:
            item["Source Folder"] = "Sent"
        unique_emails[item["Email Address"]] = item
        
    return list(unique_emails.values())

def main():
    load_dotenv()
    records = get_all_emails()
    
    if not records:
        print("No records extracted.", flush=True)
        return
        
    output_file = "data/all_emails_extracted.csv"
    with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["Email Address", "Name", "Subject", "Date", "Source Folder"])
        writer.writeheader()
        writer.writerows(records)
        
    print(f"Successfully extracted {len(records)} unique email addresses.", flush=True)
    print(f"Saved to {output_file} (You can open this directly in Excel).", flush=True)

if __name__ == "__main__":
    main()
