

import os
import re
import sys
import asyncio
import sqlite3
import requests
from pathlib import Path
from dotenv import load_dotenv
from whatsapp_bot_proto import init_db, is_already_sent, mark_as_sent, send_via_twilio, send_via_ultramsg

load_dotenv()

# Setup UTF-8 for Windows output console
import io
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', write_through=True)

# Setup paths
IMAGE_DIR = Path("downloaded_images")
IMAGE_DIR.mkdir(exist_ok=True)
SESSION_FILE = Path("linkedin_session.json")

# Config options from .env
TARGET_URL = [ os.getenv("SCRAPE_TARGET_URL", 
                         "https://www.linkedin.com/company/infed/posts/?feedView=all", 
                         "https://www.startupindia.gov.in/content/sih/en/ams-application/application-listing.html", 
                         "https://www.microsoft.com/en-us/startups", 
                         "https://aws.amazon.com/startups/", )
             ]
TARGET_PHONE = os.getenv("TARGET_PHONE", "7798582017")
KEYWORDS = [k.strip().lower() for k in os.getenv("TARGET_KEYWORDS", "register").split(",")]

# LinkedIn login credentials (optional)
LINKEDIN_EMAIL = os.getenv("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD", "")

async def login_to_linkedin(page):
    """Handles automated or manual login to LinkedIn and saves session state."""
    print("[*] Detecting login or sign-up wall...")
    
    # If on sign-up page, redirect directly to the login URL
    if "signup" in page.url or await page.query_selector("text=Join LinkedIn") or await page.query_selector("text=Already on LinkedIn?"):
        print("[!] Detected sign-up/join wall. Redirecting directly to login URL...")
        try:
            await page.goto("https://www.linkedin.com/login")
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"[-] Redirect to sign-in page failed: {e}")
            
    # Check if we are on login screen
    if "login" in page.url or "signin" in page.url or await page.query_selector("input#username") or await page.query_selector("input[name='session_key']"):
        print("[!] Login wall detected!")
        
        if LINKEDIN_EMAIL and LINKEDIN_PASSWORD:
            print("[*] Attempting automated login with credentials in .env...")
            try:
                # Enter credentials
                username_field = await page.query_selector("input#username, input[name='session_key']")
                password_field = await page.query_selector("input#password, input[name='session_password']")
                if username_field and password_field:
                    await username_field.fill(LINKEDIN_EMAIL)
                    await password_field.fill(LINKEDIN_PASSWORD)
                    submit_btn = await page.query_selector("button[type='submit'], button.btn__primary--large")
                    if submit_btn:
                        await submit_btn.click()
                        await page.wait_for_timeout(5000)
                
                # Check if we bypassed login
                if "login" not in page.url and "signin" not in page.url:
                    print("[+] Automated login successful.")
                    return True
                else:
                    await page.screenshot(path="login_failed.png")
                    print("[-] Automated login failed (incorrect credentials or security challenge). Saved screenshot to login_failed.png")
            except Exception as e:
                await page.screenshot(path="login_failed.png")
                print(f"[-] Automated login error: {e}. Saved screenshot to login_failed.png")
        
        # If automated fails or no credentials, instruct manual intervention
        print("\n" + "="*70)
        print(" [Action Required] LinkedIn login required.")
        print(" Since automated login is gated, we will launch a visible browser window.")
        print(" Please log in manually inside the browser window.")
        print(" The bot will automatically save your session when you are finished.")
        print("="*70 + "\n")
        return False
    
    return True

async def get_post_age_in_days(card):
    """
    Extracts the relative age of the post from LinkedIn actor subtexts/time tags
    and returns the age in days. Returns None if it cannot be determined.
    """
    subtext_selectors = [
        "span.update-components-actor__subtext",
        "span.feed-shared-actor__sub-text",
        "span.feed-shared-actor__meta",
        "time",
        ".update-components-actor__subtext-item"
    ]
    for sel in subtext_selectors:
        elements = await card.query_selector_all(sel)
        for el in elements:
            text = await el.inner_text()
            text = text.replace("•", "").strip().lower()
            if not text:
                continue
                
            # Regex to match: "1d ago", "20h ago", "3d ago", "1w ago", "1mo ago", "12 hours ago", "2 days ago", "1 yr ago"
            match = re.search(r'(\d+)\s*(s|m|h|d|w|mo|yr|second|minute|hour|day|week|month|year)s?\b', text)
            if match:
                value = int(match.group(1))
                unit = match.group(2)
                if unit in ('s', 'second'):
                    return 0
                elif unit in ('m', 'minute'):
                    return 0
                elif unit in ('h', 'hour'):
                    return 0
                elif unit in ('d', 'day'):
                    return value
                elif unit in ('w', 'week'):
                    return value * 7
                elif unit in ('mo', 'month'):
                    return value * 30
                elif unit in ('yr', 'year'):
                    return value * 365
            if "yesterday" in text:
                return 1
            if "just now" in text or "now" in text:
                return 0
    return None

async def scrape_page_and_send():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[!] Playwright is not installed. Please run: pip install playwright")
        return

    init_db()

    if not TARGET_PHONE:
        print("[!] TARGET_PHONE is missing. Cannot send WhatsApp alerts.")
        return

    print(f"[*] Starting Scraper for target: {TARGET_URL}")
    print(f"[*] Looking for keywords: {KEYWORDS}")
    print(f"[*] Sending to: {TARGET_PHONE}")

    async with async_playwright() as p:
        # Determine if we have a saved session
        session_exists = SESSION_FILE.exists()
        
        # If credentials are provided, we can run headlessly. If not, open browser to log in manually.
        headless_mode = bool(LINKEDIN_EMAIL) or session_exists
        if not session_exists and not LINKEDIN_EMAIL:
            headless_mode = False # Force headful mode to let user log in manually
            
        print(f"[*] Launching Chromium (Headless: {headless_mode})...")
        
        launch_args = {
            "headless": headless_mode,
        }
        
        browser = await p.chromium.launch(**launch_args)
        
        # Setup context arguments
        context_args = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "viewport": {"width": 1280, "height": 800}
        }
        
        if session_exists:
            print(f"[+] Loading existing session state from {SESSION_FILE}")
            context_args["storage_state"] = str(SESSION_FILE)
            
        context = await browser.new_context(**context_args)
        page = await context.new_page()

        try:
            # Go to target URL (LinkedIn Posts)
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(5000)

            # Check if logged in
            logged_in = await login_to_linkedin(page)
            
            if not logged_in:
                if headless_mode:
                    print("[*] Re-launching browser in visible (headful) mode for manual login/verification...")
                    # Close current headless browser
                    await browser.close()
                    
                    # Launch headfully
                    headless_mode = False
                    launch_args["headless"] = False
                    browser = await p.chromium.launch(**launch_args)
                    context = await browser.new_context(**context_args)
                    page = await context.new_page()
                    
                    # Go back to page
                    await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(5000)
                    
                    # Call login check again to print manual login prompt
                    logged_in = await login_to_linkedin(page)
                
                if not logged_in:
                    # Wait for user to log in manually and navigate to feed
                    print("[*] Waiting for you to log in manually in the browser window...")
                    # We wait for the feed to load, or user to navigate back to target URL after login
                    for i in range(120): # Wait up to 2 minutes
                        await page.wait_for_timeout(1000)
                        if "company/infed" in page.url and not (await page.query_selector("input#username")):
                            print("[+] Detected manual login & page load complete!")
                            logged_in = True
                            # Save state
                            await context.storage_state(path=str(SESSION_FILE))
                            print(f"[+] Saved login state to {SESSION_FILE}")
                            break
                    
                    if not logged_in:
                        print("[-] Timeout waiting for manual login. Exiting.")
                        return

            # Re-verify page is loaded
            if "company/infed" not in page.url:
                await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(5000)

            # Save screenshot for debug
            screenshot_path = "linkedin_infed_page.png"
            await page.screenshot(path=screenshot_path)
            print(f"[i] Saved page screenshot to {screenshot_path}")

            # Locate post elements. In logged-in view, selectors can be different:
            post_selectors = [
                "div.feed-shared-update-v2", 
                "div.org-update",
                ".org-updates-activity-card",
                "article"
            ]
            
            cards = []
            for sel in post_selectors:
                cards = await page.query_selector_all(sel)
                if cards:
                    print(f"[+] Found {len(cards)} post cards using selector '{sel}'")
                    break

            if not cards:
                cards = await page.query_selector_all("[data-urn]")
                print(f"[i] Fallback found {len(cards)} elements")

            matched_posts = []
            
            for index, card in enumerate(cards):
                # 1. Check relative age first
                age_days = await get_post_age_in_days(card)
                if age_days is not None and age_days > 2:
                    print(f"Post {index+1}: Skipped (Too old: {age_days} days).")
                    continue

                text = await card.inner_text()
                text_lower = text.lower()
                
                # Check for keywords
                has_keyword = any(kw in text_lower for kw in KEYWORDS)
                
                # Try to extract the post ID/urn
                urn = await card.get_attribute("data-urn")
                if not urn:
                    # Look inside elements
                    urn = f"infed_post_{abs(hash(text[:150]))}"

                # Extract post image URL
                img_url = ""
                # Selector strategies for logged-in user feed images
                img_selectors = [
                    "div.update-components-image__container img", 
                    "div.feed-shared-image__container img", 
                    "img.update-components-image__image"
                ]
                
                for img_sel in img_selectors:
                    img_el = await card.query_selector(img_sel)
                    if img_el:
                        src = await img_el.get_attribute("src")
                        if src and src.startswith("http"):
                            img_url = src
                            break
                            
                # Fallback: check any image
                if not img_url:
                    all_imgs = await card.query_selector_all("img")
                    for img in all_imgs:
                        src = await img.get_attribute("src")
                        if src and src.startswith("http"):
                            alt = await img.get_attribute("alt") or ""
                            # Skip avatars or icon images
                            if "avatar" in alt.lower() or "logo" in alt.lower() or "member" in alt.lower():
                                continue
                            img_url = src
                            break

                # Extract links from the post
                post_links = []
                link_elements = await card.query_selector_all("a[href]")
                for link_el in link_elements:
                    href = await link_el.get_attribute("href")
                    if href and href.startswith("http") and "linkedin.com" not in href:
                        # Avoid LinkedIn internal links, capture external links
                        if href not in post_links:
                            post_links.append(href)

                matched_posts.append({
                    "index": index + 1,
                    "id": urn,
                    "description": text.strip(),
                    "image_url": img_url,
                    "links": post_links,
                    "has_keyword": has_keyword
                })
                
                age_str = f"{age_days} days ago" if age_days is not None else "Unknown age"
                print(f"Post {index+1}: Snippet: {repr(text[:60])} | Age: {age_str} | Keyword: {has_keyword} | Img: {'Yes' if img_url else 'No'}")


            print(f"\n[i] Scrape completed. Processing results:")
            
            # The user requested: "try to fetch the second post of this whre the keyword is register"
            register_posts = [p for p in matched_posts if p["has_keyword"]]
            print(f"[i] Found {len(register_posts)} posts with keyword 'register'.")
            
            target_post = None
            if len(register_posts) >= 2:
                target_post = register_posts[1] # The second post of this where the keyword is register
                print(f"\n[+] Selected second post containing 'register': Post #{target_post['index']}")
            elif len(register_posts) == 1:
                target_post = register_posts[0]
                print(f"\n[!] Only 1 post containing 'register' was found. Using Post #{target_post['index']}")
            elif len(matched_posts) >= 2:
                target_post = matched_posts[1]
                print(f"\n[!] No post containing 'register' found. Defaulting to second post overall: Post #{target_post['index']}")
            elif matched_posts:
                target_post = matched_posts[0]
                print(f"\n[!] Defaulting to first post overall: Post #{target_post['index']}")
            else:
                print("[-] No posts found at all on the page. Check screenshot / selector structures.")

            if target_post:
                post_id = target_post["id"]
                description = target_post["description"]
                image_url = target_post["image_url"]
                links = target_post.get("links", [])

                print(f"\nTarget Post Details:")
                print(f"ID: {post_id}")
                print(f"Image Link: {image_url}")
                print(f"Description (first 300 chars):\n{description[:300]}...\n")
                if links:
                    print(f"Found {len(links)} link(s) in post:")
                    for link in links:
                        print(f"  - {link}")

                if is_already_sent(post_id):
                    print(f"[-] Target post {post_id} has already been sent previously. Skipping.")
                else:
                    # Download image
                    local_img = None
                    if image_url:
                        local_img = download_image(image_url, post_id)

                    # Append links to the message
                    message_text = description
                    if links:
                        message_text += "\n\n🔗 Links:\n" + "\n".join(links)

                    # Send message
                    sent = False
                    if os.getenv("TWILIO_ACCOUNT_SID"):
                        print("[*] Sending via Twilio...")
                        sent = send_via_twilio(TARGET_PHONE, message_text, image_url)
                    elif os.getenv("ULTRAMSG_INSTANCE_ID"):
                        print("[*] Sending via UltraMsg...")
                        sent = send_via_ultramsg(TARGET_PHONE, message_text, image_url)
                    else:
                        print("[i] No WhatsApp API credentials configured in .env. Running Simulation Mode.")
                        print(f"[DEMO ONLY] Image: {local_img or image_url}")
                        print(f"[DEMO ONLY] Message:\n{message_text}")
                        sent = True

                    if sent:
                        mark_as_sent(post_id)
                        print(f"[+] Successfully sent and marked post {post_id}.")

        except Exception as e:
            print(f"[-] Error during scraping workflow: {e}")
        finally:
            # Save storage state for future headless runs
            try:
                await context.storage_state(path=str(SESSION_FILE))
                print(f"[+] Storage state saved to {SESSION_FILE}")
            except Exception:
                pass
            
            await browser.close()

def download_image(img_url, post_id):
    """Downloads post image locally."""
    try:
        # Clean image name
        safe_post_id = re.sub(r'[^a-zA-Z0-9]', '_', post_id)
        img_path = IMAGE_DIR / f"{safe_post_id}.jpg"
        
        response = requests.get(img_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code == 200:
            with open(img_path, 'wb') as f:
                f.write(response.content)
            print(f"[+] Downloaded image to {img_path}")
            return str(img_path)
    except Exception as e:
        print(f"[-] Image download failed: {e}")
    return None

if __name__ == "__main__":
    asyncio.run(scrape_page_and_send())
