# C:\Users\awez7\OneDrive\Desktop\incubein\whatsapp_bot_proto.py

import os
import re
import json
import sqlite3
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

import sys
import io
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', write_through=True)

# Setup database for tracking sent posts

DB_FILE = "whatsapp_sent_posts.db"

def init_db():
    """Initializes the SQLite database to keep track of processed posts."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_posts (
            post_id TEXT PRIMARY KEY,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_already_sent(post_id):
    """Checks if a post has already been sent to WhatsApp."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_posts WHERE post_id = ?", (post_id,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def mark_as_sent(post_id):
    """Marks a post as sent in the database."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO sent_posts (post_id) VALUES (?)", (post_id,))
    conn.commit()
    conn.close()

# ── WhatsApp Senders ──────────────────────────────────────────────────

def send_via_twilio(to_number, body_text, media_url):
    """
    Sends WhatsApp message using Twilio's Official WhatsApp API / Sandbox.
    Requires: pip install twilio
    """
    try:
        from twilio.rest import Client
    except ImportError:
        print("[!] twilio library not installed. Run: pip install twilio")
        return False

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886") # Default Twilio Sandbox

    if not account_sid or not auth_token:
        print("[!] Twilio credentials missing in environment variables.")
        return False

    client = Client(account_sid, auth_token)
    try:
        message = client.messages.create(
            from_=from_number,
            body=body_text,
            media_url=[media_url] if media_url else None,
            to=f"whatsapp:{to_number}"
        )
        print(f"[+] Message sent via Twilio. SID: {message.sid}")
        return True
    except Exception as e:
        print(f"[-] Twilio Send Error: {e}")
        return False

def send_via_ultramsg(to_number, body_text, media_url):
    """
    Sends WhatsApp message using UltraMsg (unofficial API, paid/cheap instance).
    Very easy to set up with personal numbers.
    """
    instance_id = os.getenv("ULTRAMSG_INSTANCE_ID")
    token = os.getenv("ULTRAMSG_TOKEN")

    if not instance_id or not token:
        print("[!] UltraMsg credentials missing in environment variables.")
        return False

    url = f"https://api.ultramsg.com/{instance_id}/messages/image"
    
    # Truncate message to fit UltraMsg's 1024 character limit
    max_length = 500
    if len(body_text) > max_length:
        print(f"[i] Message too long ({len(body_text)} chars). Truncating to {max_length} chars...")
        body_text = body_text[:max_length] + "..."
    
    print(f"[i] Sending message with {len(body_text)} chars and image: {media_url[:50]}...")
    
    payload = {
        "token": token,
        "to": to_number,
        "image": media_url,
        "caption": body_text
    }
    headers = {'content-type': 'application/x-www-form-urlencoded'}

    try:
        response = requests.post(url, data=payload, headers=headers)
        res_data = response.json()
        if res_data.get("sent") == "true":
            print(f"[+] Message sent via UltraMsg. ID: {res_data.get('id')}")
            return True
        else:
            print(f"[-] UltraMsg Error Response: {res_data}")
            return False
    except Exception as e:
        print(f"[-] UltraMsg Send Error: {e}")
        return False

# ── Generic Web Scraper ───────────────────────────────────────────────

def download_image(img_url, post_id):
    """Downloads post image locally and returns local file path."""
    try:
        img_dir = Path("downloaded_images")
        img_dir.mkdir(exist_ok=True)
        
        # Clean image name
        safe_post_id = re.sub(r'[^a-zA-Z0-9]', '_', post_id)
        img_path = img_dir / f"{safe_post_id}.jpg"
        
        response = requests.get(img_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code == 200:
            with open(img_path, 'wb') as f:
                f.write(response.content)
            print(f"[+] Downloaded image to {img_path}")
            return str(img_path)
    except Exception as e:
        print(f"[-] Image download failed: {e}")
    return None

def check_keywords(text):
    """Helper to detect target action phrases."""
    keywords = ["register now", "apply now", "apply", "register today", "sign up"]
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)

# ── Demo / Prototype Workflow ─────────────────────────────────────────

def run_prototype_workflow(mock_posts):
    """
    Demonstrates scraping, filtering, downloading, and sending logic.
    For production, `mock_posts` would be replaced with dynamic results from Playwright.
    """
    init_db()
    target_phone = os.getenv("TARGET_PHONE")
    
    if not target_phone:
        print("[!] TARGET_PHONE not specified in .env. Please set it (e.g. +91XXXXXXXXXX).")
        return

    print(f"\n--- Starting Bot Run. Target Number: {target_phone} ---")

    for post in mock_posts:
        post_id = post["id"]
        post_url = post["url"]
        description = post["description"]
        image_url = post["image_url"]

        print(f"\nChecking Post {post_id}...")

        # 1. Deduplication
        if is_already_sent(post_id):
            print(f"[-] Post {post_id} already sent. Skipping.")
            continue

        # 2. Keyword Filter
        if not check_keywords(description):
            print(f"[-] Post {post_id} does not contain target keywords ('Apply' or 'Register Now'). Skipping.")
            continue

        print(f"[!] Match found in post description! Proceeding to process...")

        # 3. Download image
        local_img = download_image(image_url, post_id)

        # 4. Send Message (using either Twilio or UltraMsg)
        sent = False
        # Try Twilio first
        if os.getenv("TWILIO_ACCOUNT_SID"):
            print("[*] Sending via Twilio...")
            sent = send_via_twilio(target_phone, description, image_url)
        # Try UltraMsg second
        elif os.getenv("ULTRAMSG_INSTANCE_ID"):
            print("[*] Sending via UltraMsg...")
            sent = send_via_ultramsg(target_phone, description, image_url)
        else:
            print("[i] No WhatsApp API credentials configured in .env.")
            print(f"[DEMO ONLY] Image: {local_img or image_url}")
            print(f"[DEMO ONLY] Description:\n{description}")
            sent = True

        if sent:
            mark_as_sent(post_id)
            print(f"[+] Successfully processed and recorded post {post_id}.")

if __name__ == "__main__":
    # Mock data to simulate scrapped LinkedIn or Website posts
    demo_posts = [
        {
            "id": "lnk_post_001",
            "url": "https://www.linkedin.com/feed/update/urn:li:activity:111111",
            "description": "🚀 We are hosting a startup summit! Register now to reserve your spot! Limited tickets available.",
            "image_url": "https://images.unsplash.com/photo-1540575467063-178a50c2df87?w=800" # Demo event image
        },
        {
            "id": "lnk_post_002",
            "url": "https://www.linkedin.com/feed/update/urn:li:activity:222222",
            "description": "Just sharing some daily updates from our incubator headquarters. Happy Wednesday!",
            "image_url": "https://images.unsplash.com/photo-1497366216548-37526070297c?w=800"
        },
        {
            "id": "lnk_post_003",
            "url": "https://www.linkedin.com/feed/update/urn:li:activity:333333",
            "description": "Hiring Alert! We are looking for Full Stack Developers. Apply today at our careers portal.",
            "image_url": "https://images.unsplash.com/photo-1522071820081-009f0129c71c?w=800"
        }
    ]
    
    run_prototype_workflow(demo_posts)
