"""
Recruiter Finder Script
=======================
1. Paste a Jobright job link
2. Script extracts the company name
3. Searches LinkedIn for recruiters at that company using your session cookie
4. Prints recruiter names + LinkedIn profile URLs to terminal

Requirements:
    pip install requests beautifulsoup4 selenium webdriver-manager

Usage:
    python recruiter_finder.py
"""

import time
import re
import sys
import os
import json
import sqlite3
import shutil
import tempfile
import base64
import requests
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ─────────────────────────────────────────────
# STEP 1: Paste your LinkedIn session cookie here
# How to get it:
#   1. Log into LinkedIn in Chrome (as 8405.osborne.beit@gmail.com)
#   2. Press F12 → Application → Cookies → https://www.linkedin.com
#   3. Find the cookie named "li_at" and copy its value
# ─────────────────────────────────────────────
LINKEDIN_SESSION_COOKIE = os.environ.get("LINKEDIN_COOKIE", "PASTE_li_at_HERE")

# Chrome profile associated with 8405.osborne.beit@gmail.com
# ─────────────────────────────────────────────
# How to find your Chrome profile path:
#   1. Open Chrome as 8405.osborne.beit@gmail.com
#   2. Go to chrome://version
#   3. Copy the "Profile Path" value and paste it below
# ─────────────────────────────────────────────
CHROME_PROFILE_PATH = "C:\\Users\\Client\\AppData\\Local\\Google\\Chrome\\User Data"
CHROME_PROFILE_DIR = "Profile 2"


def get_company_from_jobright(jobright_url: str) -> str:
    """Fetch the Jobright job page and extract the company name."""
    print(f"\n[1/3] Fetching job details from Jobright...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(jobright_url, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Could not fetch Jobright page: {e}")
        sys.exit(1)

    soup = BeautifulSoup(resp.text, "html.parser")

    company = None

    # Pattern 1: og:site_name or og:title meta tags
    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("name", "")
        if "title" in prop.lower():
            content = meta.get("content", "")
            if " at " in content:
                company = content.split(" at ")[-1].strip()
                break

    # Pattern 2: Look for a link or element with company name
    if not company:
        for tag in soup.find_all(["a", "span", "h2", "h3"], class_=re.compile(r"company|employer", re.I)):
            text = tag.get_text(strip=True)
            if text:
                company = text
                break

    # Pattern 3: Page title fallback
    if not company and soup.title:
        title = soup.title.string or ""
        if " at " in title:
            company = title.split(" at ")[-1].strip()
        elif "|" in title:
            company = title.split("|")[-1].strip()

    if not company:
        print("[WARNING] Could not auto-detect company name from Jobright page.")
        company = input("         Please enter the company name manually: ").strip()

    # Strip trailing noise like "· 21 hours ago", "- Full-time", etc.
    company = re.split(r"\s*[·\-\|]\s*\d", company)[0].strip()
    company = re.sub(r"\s+(ago|hours|days|posted|full.time|part.time).*", "", company, flags=re.I).strip()

    print(f"           [OK] Company detected: {company}")
    return company


def build_linkedin_search_url(company: str) -> str:
    """Build a LinkedIn people search URL for recruiters at the company."""
    keywords = f"recruiter OR \"talent acquisition\" OR \"HR\" {company}"
    encoded = requests.utils.quote(keywords)
    return (
        f"https://www.linkedin.com/search/results/people/"
        f"?keywords={encoded}"
        f"&origin=GLOBAL_SEARCH_HEADER"
    )


def get_chrome_li_at_cookie() -> str:
    """Extract the li_at LinkedIn session cookie directly from Chrome's cookie database."""
    try:
        import win32crypt
        from Crypto.Cipher import AES

        local_state_path = os.path.join(CHROME_PROFILE_PATH, "Local State")
        cookies_path = os.path.join(CHROME_PROFILE_PATH, CHROME_PROFILE_DIR, "Network", "Cookies")
        if not os.path.exists(cookies_path):
            cookies_path = os.path.join(CHROME_PROFILE_PATH, CHROME_PROFILE_DIR, "Cookies")

        # Get Chrome's AES encryption key from Local State
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
        encrypted_key = encrypted_key[5:]  # Strip "DPAPI" prefix
        aes_key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]

        # Open the Cookies DB read-only with immutable flag to bypass Chrome's lock
        uri = "file:{}?mode=ro&immutable=1".format(
            cookies_path.replace("\\", "/").replace(" ", "%20")
        )
        conn = sqlite3.connect(uri, uri=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT encrypted_value FROM cookies WHERE host_key LIKE '%linkedin.com%' AND name='li_at'"
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        enc_val = row[0]
        # v10/v20 encrypted value: 3 bytes version + 12 bytes nonce + ciphertext + 16 bytes tag
        nonce = enc_val[3:15]
        ciphertext = enc_val[15:-16]
        tag = enc_val[-16:]
        cipher = AES.new(aes_key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")

    except Exception as e:
        print(f"           [!] Could not auto-extract cookie: {e}")
        return None


def launch_with_profile() -> uc.Chrome:
    """Launch Chrome using the saved user profile (requires Chrome to be closed first)."""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager

    options = Options()
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_PATH}")
    options.add_argument(f"--profile-directory={CHROME_PROFILE_DIR}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )
    return driver


def setup_driver(cookie_value: str):
    """Launch Chrome with the saved profile (most reliable) or fall back to cookie auth."""
    print("\n[2/3] Launching Chrome...")

    import subprocess
    chrome_running = bool(subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
        capture_output=True, text=True
    ).stdout.strip())

    if chrome_running:
        print("  Chrome is currently open. To use your saved LinkedIn session,")
        print("  please CLOSE all Chrome windows and press Enter.")
        print("  (Or press Ctrl+C to cancel and use a cookie instead)\n")
        try:
            input("  Press Enter after closing Chrome: ")
        except EOFError:
            pass

    # Try launching with profile
    try:
        driver = launch_with_profile()
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(5)
        title = driver.title.lower()
        url = driver.current_url.lower()
        if title != "www.linkedin.com" and "login" not in url and "authwall" not in url:
            print("           [OK] Logged into LinkedIn via Chrome profile")
            return driver
        driver.quit()
    except Exception as e:
        print(f"           [!] Profile launch failed: {e}")

    # Fallback: cookie auth
    if cookie_value == "PASTE_YOUR_li_at_COOKIE_VALUE_HERE":
        print("\n  Could not use Chrome profile. Please provide your li_at cookie:")
        print("    1. Open Chrome, go to linkedin.com, press F12")
        print("    2. Application -> Cookies -> https://www.linkedin.com -> copy 'li_at' value\n")
        try:
            cookie_value = input("  Paste li_at cookie: ").strip()
        except EOFError:
            print("  [ERROR] No cookie provided.")
            sys.exit(1)

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    driver = uc.Chrome(options=options, version_main=146)

    driver.get("https://www.linkedin.com/login")
    time.sleep(3)
    driver.add_cookie({
        "name": "li_at",
        "value": cookie_value,
        "domain": ".linkedin.com",
        "path": "/",
        "secure": True,
    })
    driver.get("https://www.linkedin.com/feed/")
    time.sleep(5)

    title = driver.title.lower()
    url = driver.current_url.lower()
    # Error pages have title "www.linkedin.com"; real feed/search pages have longer titles
    if title == "www.linkedin.com" or "login" in url or "authwall" in url:
        print("  [!] Could not authenticate. The cookie may be expired.")
        print("      Close Chrome and run the script again to use your saved session.")
        driver.quit()
        sys.exit(1)

    print("           [OK] Logged into LinkedIn")
    return driver


def scrape_recruiters(driver, company: str) -> list[dict]:
    """Search LinkedIn and scrape recruiter profiles."""
    search_url = build_linkedin_search_url(company)
    print(f"\n[3/3] Searching LinkedIn for recruiters at '{company}'...")
    print(f"           URL: {search_url}\n")

    # Navigate via JS to avoid redirect loops that driver.get() can trigger on LinkedIn
    driver.execute_script(f"window.location.href = '{search_url}'")
    time.sleep(6)

    recruiters = []
    page = 1
    max_pages = 3

    # LinkedIn uses different selectors depending on their current UI version
    CARD_SELECTORS = [
        "li.reusable-search__result-container",
        "li[class*='result-container']",
        "div.entity-result",
        "li[data-occludable-job-id]",
        "div[data-view-name='search-entity-result-universal-template']",
    ]

    while page <= max_pages:
        print(f"  -> Scraping page {page}...")

        # Wait for any known result container to appear
        cards = []
        for sel in CARD_SELECTORS:
            try:
                WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                cards = driver.find_elements(By.CSS_SELECTOR, sel)
                if cards:
                    break
            except Exception:
                continue

        if not cards:
            # Dump page source snippet for debugging
            src = driver.page_source[:500]
            print(f"  [!] No result cards found. Page title: {driver.title!r}")
            print(f"      Page snippet: {src[:200]}")
            break

        for card in cards:
            try:
                # Name: prefer aria-hidden span (display name), fall back to visible text
                try:
                    name_el = card.find_element(By.CSS_SELECTOR, "span[aria-hidden='true']")
                    name = name_el.text.strip()
                except Exception:
                    name_el = card.find_element(By.CSS_SELECTOR, "a[href*='/in/']")
                    name = name_el.text.strip().split("\n")[0]

                if not name:
                    continue

                # Profile URL
                try:
                    link_el = card.find_element(By.CSS_SELECTOR, "a[href*='/in/']")
                    profile_url = link_el.get_attribute("href").split("?")[0]
                except Exception:
                    profile_url = "N/A"

                # Title / subtitle
                title = "N/A"
                for title_sel in [
                    ".entity-result__primary-subtitle",
                    ".entity-result__summary",
                    "div[class*='subtitle']",
                    "div[class*='primary-subtitle']",
                    "span[class*='subtitle']",
                ]:
                    try:
                        title_el = card.find_element(By.CSS_SELECTOR, title_sel)
                        title = title_el.text.strip()
                        if title:
                            break
                    except Exception:
                        continue

                recruiter_keywords = ["recruit", "talent", "hr ", "human resources", "hiring", "people ops", "people partner"]
                if any(kw in title.lower() for kw in recruiter_keywords):
                    recruiters.append({
                        "name": name,
                        "title": title,
                        "linkedin_url": profile_url
                    })

            except Exception:
                continue

        try:
            next_btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Next']")
            if next_btn.is_enabled():
                next_btn.click()
                time.sleep(3)
                page += 1
            else:
                break
        except Exception:
            break

    return recruiters


def print_results(recruiters: list[dict], company: str):
    """Print the recruiter list to terminal."""
    print("\n" + "=" * 60)
    print(f"  RECRUITERS FOUND AT: {company.upper()}")
    print("=" * 60)

    if not recruiters:
        print("  No recruiters found. Try adjusting keywords or check your cookie.")
    else:
        for i, r in enumerate(recruiters, 1):
            print(f"\n  [{i}] {r['name']}")
            print(f"       Title   : {r['title']}")
            print(f"       LinkedIn: {r['linkedin_url']}")

    print("\n" + "=" * 60)
    print(f"  Total found: {len(recruiters)}")
    print("=" * 60 + "\n")


def main():
    print("=" * 60)
    print("  LinkedIn Recruiter Finder")
    print("  Account: 8405.osborne.beit@gmail.com")
    print("=" * 60)

    # Warn if neither profile path nor cookie is configured
    profile_set = True
    cookie_set = LINKEDIN_SESSION_COOKIE != "PASTE_YOUR_li_at_COOKIE_VALUE_HERE"

    if not profile_set and not cookie_set:
        print("\n[ERROR] You need to configure at least one of the following:")
        print("        OPTION A (recommended) — Chrome Profile Path:")
        print("          1. Open Chrome as 8405.osborne.beit@gmail.com")
        print("          2. Go to chrome://version")
        print("          3. Copy 'Profile Path' and paste into CHROME_PROFILE_PATH")
        print()
        print("        OPTION B — LinkedIn Session Cookie:")
        print("          1. Log into LinkedIn as 8405.osborne.beit@gmail.com")
        print("          2. Press F12 → Application → Cookies → https://www.linkedin.com")
        print("          3. Copy 'li_at' value and paste into LINKEDIN_SESSION_COOKIE\n")
        sys.exit(1)

    jobright_url = input("\nPaste the Jobright job link: ").strip()
    if not jobright_url.startswith("http"):
        print("[ERROR] That doesn't look like a valid URL.")
        sys.exit(1)

    company = get_company_from_jobright(jobright_url)
    driver = setup_driver(LINKEDIN_SESSION_COOKIE)

    try:
        recruiters = scrape_recruiters(driver, company)
        print_results(recruiters, company)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()