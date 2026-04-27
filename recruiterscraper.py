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
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ─────────────────────────────────────────────
# STEP 1: Paste your LinkedIn session cookie here
# How to get it:
#   1. Log into LinkedIn in Chrome
#   2. Press F12 → Application → Cookies → https://www.linkedin.com
#   3. Find the cookie named "li_at" and copy its value
# ─────────────────────────────────────────────
LINKEDIN_SESSION_COOKIE = "PASTE_YOUR_li_at_COOKIE_VALUE_HERE"


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

    # Try common patterns for company name on Jobright
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

    print(f"           ✓ Company detected: {company}")
    return company


def build_linkedin_search_url(company: str) -> str:
    """Build a LinkedIn people search URL for recruiters at the company."""
    keywords = f"recruiter OR \"talent acquisition\" OR \"HR\" {company}"
    encoded = requests.utils.quote(keywords)
    # LinkedIn people search
    return (
        f"https://www.linkedin.com/search/results/people/"
        f"?keywords={encoded}"
        f"&origin=GLOBAL_SEARCH_HEADER"
    )


def setup_driver(cookie_value: str) -> webdriver.Chrome:
    """Launch Chrome with the LinkedIn session cookie pre-loaded."""
    print("\n[2/3] Launching browser with your LinkedIn session...")
    options = Options()
    # Comment out headless if you want to watch it work:
    # options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    # Set cookie on LinkedIn domain
    driver.get("https://www.linkedin.com")
    driver.add_cookie({
        "name": "li_at",
        "value": cookie_value,
        "domain": ".linkedin.com"
    })
    return driver


def scrape_recruiters(driver: webdriver.Chrome, company: str) -> list[dict]:
    """Search LinkedIn and scrape recruiter profiles."""
    search_url = build_linkedin_search_url(company)
    print(f"\n[3/3] Searching LinkedIn for recruiters at '{company}'...")
    print(f"           URL: {search_url}\n")

    driver.get(search_url)
    time.sleep(3)  # Let the page load

    recruiters = []
    page = 1
    max_pages = 3  # Scrape up to 3 pages (30 results)

    while page <= max_pages:
        print(f"  → Scraping page {page}...")

        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "li.reusable-search__result-container")
                )
            )
        except Exception:
            print("  [!] Could not find results on this page. LinkedIn may be blocking or no more results.")
            break

        cards = driver.find_elements(By.CSS_SELECTOR, "li.reusable-search__result-container")

        if not cards:
            print("  [!] No result cards found.")
            break

        for card in cards:
            try:
                # Name
                name_el = card.find_element(By.CSS_SELECTOR, "span[aria-hidden='true']")
                name = name_el.text.strip()

                # LinkedIn profile URL
                link_el = card.find_element(By.CSS_SELECTOR, "a.app-aware-link")
                profile_url = link_el.get_attribute("href").split("?")[0]  # Clean URL

                # Title/Headline
                try:
                    title_el = card.find_element(By.CSS_SELECTOR, ".entity-result__primary-subtitle")
                    title = title_el.text.strip()
                except Exception:
                    title = "N/A"

                # Only include if title looks recruiter-related
                recruiter_keywords = ["recruit", "talent", "hr ", "human resources", "hiring", "people ops"]
                if any(kw in title.lower() for kw in recruiter_keywords):
                    recruiters.append({
                        "name": name,
                        "title": title,
                        "linkedin_url": profile_url
                    })

            except Exception:
                continue

        # Try to go to next page
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
    print("=" * 60)

    # Validate cookie
    if LINKEDIN_SESSION_COOKIE in ("PASTE_YOUR_li_at_COOKIE_VALUE_HERE", ""):
        print("\n[ERROR] You need to paste your LinkedIn 'li_at' cookie at the top of this script.")
        print("        Steps:")
        print("        1. Log into LinkedIn in Chrome")
        print("        2. Press F12 → Application tab → Cookies → https://www.linkedin.com")
        print("        3. Find 'li_at' → copy the Value")
        print("        4. Paste it into LINKEDIN_SESSION_COOKIE at the top of recruiter_finder.py\n")
        sys.exit(1)

    jobright_url = input("\nPaste the Jobright job link: ").strip()
    if not jobright_url.startswith("http"):
        print("[ERROR] That doesn't look like a valid URL.")
        sys.exit(1)

    # Run the pipeline
    company = get_company_from_jobright(jobright_url)
    driver = setup_driver(LINKEDIN_SESSION_COOKIE)

    try:
        recruiters = scrape_recruiters(driver, company)
        print_results(recruiters, company)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()