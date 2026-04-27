"""
LinkedIn People Search Scraper
- Injects li_at session cookie via CDP
- Navigates directly to People + United States filtered URL (no UI clicking needed)
- Collects all profile URLs across MAX_PAGES pages, saves to a .txt file
"""

import os
import time
import re
import sys
import subprocess
from urllib.parse import quote

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────
COMPANY   = "The RIght CLick"                          # CHANGE: target company
ROLES     = ["Recruiter", "Analytics Manager"]   # CHANGE: list of roles to search
MAX_PAGES = 1                                  # Each page has ~10 profiles

# LinkedIn geoUrn for United States = 103644278
# To find other countries: search LinkedIn, apply location filter, copy geoUrn from URL
GEO_URN = "103644278"  # United States

# ── LinkedIn session cookie ───────────────────────────────────────────────────
# How to refresh when expired:
#   1. Open Chrome, go to linkedin.com, log in
#   2. Press F12 → Application → Cookies → https://www.linkedin.com
#   3. Find "li_at" → copy its Value → paste below
LINKEDIN_COOKIE = os.environ.get("LINKEDIN_COOKIE", "PASTE_li_at_HERE")
# ─────────────────────────────────────────────────────────────────────────────


def build_search_url(query: str, page: int = 1) -> str:
    """Build a LinkedIn People search URL with US location filter."""
    geo = f'["{GEO_URN}"]'
    url = (
        f"https://www.linkedin.com/search/results/people/"
        f"?keywords={quote(query)}"
        f"&geoUrn={quote(geo)}"
        f"&origin=GLOBAL_SEARCH_HEADER"
    )
    if page > 1:
        url += f"&page={page}"
    return url


def launch_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )


def inject_cookie(driver: webdriver.Chrome, cookie_value: str):
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setCookie", {
        "name":     "li_at",
        "value":    cookie_value,
        "domain":   ".linkedin.com",
        "path":     "/",
        "secure":   True,
        "httpOnly": True,
        "sameSite": "None",
    })


def scroll_and_extract(driver: webdriver.Chrome) -> set:
    """Scroll page to load lazy-rendered cards, then extract profile URLs."""
    for _ in range(8):
        driver.execute_script("window.scrollBy(0, 700)")
        time.sleep(0.6)
    driver.execute_script("window.scrollTo(0, 0)")
    time.sleep(0.5)

    profiles = set()

    container = driver.find_elements(By.CSS_SELECTOR, "main")
    scope = container[0] if container else driver

    _bad_prefixes = ("status is", "provides services", "view ", "open to", "following", "connect")

    for a in scope.find_elements(By.CSS_SELECTOR, "a[href*='/in/']"):
        href = a.get_attribute("href") or ""
        m = re.search(r"linkedin\.com/in/([a-z0-9][a-z0-9_-]+)", href)
        if not m:
            continue
        url = f"https://www.linkedin.com/in/{m.group(1)}"
        if url in profiles:
            continue

        name = a.text.strip().split("\n")[0]
        if not name or len(name) < 3:
            continue
        if any(name.lower().startswith(p) for p in _bad_prefixes):
            continue

        profiles.add(url)
        print(f"    {name}  →  {url}")

    return profiles


def click(driver: webdriver.Chrome, el):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.3)
    driver.execute_script("arguments[0].click();", el)


def next_page_via_button(driver: webdriver.Chrome) -> bool:
    """Click the Next pagination button. Returns True if clicked."""
    for sel in [
        "button[aria-label='Next']",
        "button[aria-label='next']",
        "//button[contains(@aria-label,'Next')]",
    ]:
        try:
            by = By.CSS_SELECTOR if not sel.startswith("//") else By.XPATH
            el = WebDriverWait(driver, 6).until(EC.element_to_be_clickable((by, sel)))
            if el.is_enabled():
                click(driver, el)
                time.sleep(5)
                return True
        except Exception:
            continue
    return False


def scrape_role(driver: webdriver.Chrome, query: str) -> tuple[int, set]:
    """Scrape all pages for a single query and return (count, profiles)."""
    print(f"\n{'=' * 55}")
    print(f"  Query    : {query}")
    print("=" * 55)

    url = build_search_url(query, page=1)
    print(f"  Loading: {url}")
    driver.get(url)
    time.sleep(6)

    if "authwall" in driver.current_url or "login" in driver.current_url:
        driver.save_screenshot("debug_auth.png")
        print("[ERROR] Cookie not accepted (may be expired). Screenshot saved.")
        return 0, set()

    print(f"  Page title: {driver.title}")

    all_profiles: set = set()

    for page in range(1, MAX_PAGES + 1):
        print(f"  -> Page {page}: ", end="", flush=True)

        new = scroll_and_extract(driver) - all_profiles
        all_profiles.update(new)
        print(f"{len(new)} new  |  total {len(all_profiles)}")

        if not new and page > 1:
            print("  -> No new profiles — end of results.")
            break

        if page == MAX_PAGES:
            break

        clicked = next_page_via_button(driver)
        if not clicked:
            next_url = build_search_url(query, page=page + 1)
            print(f"  -> Navigating to page {page + 1} via URL")
            driver.get(next_url)
            time.sleep(6)

    return len(all_profiles), all_profiles


def main():
    li_at = LINKEDIN_COOKIE
    if not li_at or li_at == "PASTE_li_at_HERE":
        print("[ERROR] Set LINKEDIN_COOKIE at the top of this file.")
        sys.exit(1)
    print(f"[Auth] Cookie ready (length={len(li_at)})")

    subprocess.run(["taskkill", "/F", "/IM", "chromedriver.exe", "/T"], capture_output=True)
    time.sleep(1)

    print("[Setup] Launching Chrome...")
    driver = launch_driver()

    print("[Auth] Injecting session cookie via CDP...")
    driver.get("https://www.linkedin.com")
    time.sleep(2)
    inject_cookie(driver, li_at)

    results: dict[str, set] = {}
    combined: set = set()
    for role in ROLES:
        query = f"{COMPANY} {role}"
        count, profiles = scrape_role(driver, query)
        results[role] = profiles
        combined.update(profiles)

    driver.quit()

    with open("linkedin_urls.txt", "w", encoding="utf-8") as f:
        for u in sorted(combined):
            f.write(u + "\n")

    print(f"\n{'=' * 55}")
    print("  FINAL RESULTS")
    print("=" * 55)
    for role, profiles in results.items():
        print(f"\n  {role.upper()} ({len(profiles)}):")
        for u in sorted(profiles):
            print(f"    {u}")
    print(f"\n  Total: {len(combined)} profiles saved to linkedin_urls.txt")


if __name__ == "__main__":
    main()
