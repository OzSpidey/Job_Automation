"""
Naukri Jobs Scanner
-------------------
Searches Naukri.com for the following roles with 1 year experience:
  C++, C, Python, Software Engineer

Scrapes first 10 pages per search.
Only collects jobs posted less than 1 day ago.
Skips jobs already seen in previous runs.

Run: python naukri_jobs_scanner.py
"""

import asyncio
import json
import os
import re
import smtplib
import sys
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENTS      = [e.strip() for e in os.environ.get("EMAIL_TO_INDIA", "").split(",") if e.strip()]
SMTP_SERVER     = "smtp.gmail.com"
SMTP_PORT       = 465

SEEN_JOBS_FILE  = os.path.join(os.path.dirname(__file__), "naukri_seen_jobs.json")
PAGES_TO_SCRAPE = 1

# (keyword, direct search URL with experience=1 filter)
TARGET_SEARCHES = [
    ("C++",             "https://www.naukri.com/c-plus-plus-jobs?k=c%2B%2B&experience=1"),
    ("Python",          "https://www.naukri.com/python-jobs?k=python&experience=1"),
    ("Software Engineer", "https://www.naukri.com/software-engineer-jobs?k=software+engineer&experience=1"),
]

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def load_seen_urls() -> set[str]:
    if not os.path.exists(SEEN_JOBS_FILE):
        return set()
    with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_urls(urls: set[str]) -> None:
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(urls), f, indent=2)


def is_recent(posted_text: str) -> bool:
    """Return True only if posted less than 1 day ago."""
    t = posted_text.lower().strip()
    if not t:
        return False
    return any(word in t for word in ["just now", "minute", "hour", "few", "today"])


def make_paged_url(base_url: str, page_num: int) -> str:
    """
    Naukri pagination uses both a path suffix (-jobs-N) and pageNo= query param.
    e.g. /python-jobs-1?... → /python-jobs-2?...&pageNo=2
    """
    parsed = urllib.parse.urlparse(base_url)
    path   = re.sub(r'-(\d+)$', f'-{page_num}', parsed.path)

    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    params["pageNo"] = [str(page_num)]
    new_query = urllib.parse.urlencode(params, doseq=True)

    return urllib.parse.urlunparse(parsed._replace(path=path, query=new_query))


def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count = sum(1 for j in jobs if j["url"] not in previously_seen)
    count     = len(jobs)
    subject   = f"Naukri Jobs Scraper — {count} Matching Role(s) Found ({new_count} NEW)"

    NEW_BADGE = '<span style="background:#4a90d9;color:#fff;font-size:11px;font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
    rows_list = []
    for j in jobs:
        is_new = j["url"] not in previously_seen
        row_bg = 'background:#f0f7ff;' if is_new else ''
        badge  = NEW_BADGE if is_new else ''
        rows_list.append(
            f'<tr style="{row_bg}">'
            f'<td style="padding:8px;border:1px solid #ddd;">{badge}{j["title"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j["company"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j["location"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{j["experience"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{j["posted"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;"><a href="{j["url"]}">Apply</a></td>'
            f'</tr>'
        )
    rows = "\n".join(rows_list)
    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333">
    <h2 style="color:#4a4a4a">Naukri Jobs — Recent Roles</h2>
    <p>Found <strong>{count}</strong> role(s) posted today — C++ &nbsp;|&nbsp; Python &nbsp;|&nbsp; Software Engineer</p>
    <table style="border-collapse:collapse;width:100%;max-width:1200px">
      <tr style="background:#4a4a4a;color:#fff">
        <th style="padding:10px;border:1px solid #555;text-align:left;">Role</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Company</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Location</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Experience</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Posted</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Link</th>
      </tr>
      {rows}
    </table>
    <p style="font-size:12px;color:#888;margin-top:20px">Source: naukri.com · Experience: 1 year · Most Recent</p>
    </body></html>
    """
    plain = f"Found {count} role(s) ({new_count} NEW):\n\n" + "\n".join(
        f"- {'[NEW] ' if j['url'] not in previously_seen else ''}{j['title']} @ {j['company']} | {j['location']} | {j['experience']} | {j['posted']}\n  {j['url']}"
        for j in jobs
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, RECIPIENTS, msg.as_string())

    print(f"[email] Sent to {', '.join(RECIPIENTS)} — {count} job(s).")


async def collect_page_jobs(page) -> list[dict]:
    """Extract all job cards from the current results page."""
    jobs = []
    seen_hrefs: set[str] = set()

    await page.wait_for_timeout(2_000)

    cards = await page.locator(
        'article.jobTuple, div.jobTuple, div[class*="job-tuple"], '
        'div[class*="srp-jobtuple"], li.jobTuple'
    ).all()

    # Fallback: grab all title links and walk up to the card
    if not cards:
        cards = await page.locator('a.title[href*="naukri.com/job-listings"]').all()

    for card in cards:
        try:
            # ── Title & URL ──────────────────────────────────────────────────
            link_el = card.locator('a.title').first
            if await link_el.count() == 0:
                link_el = card.locator('a[href*="job-listings"]').first
            if await link_el.count() == 0:
                continue

            title = (await link_el.inner_text()).strip()
            href  = (await link_el.get_attribute("href") or "").strip()
            if not title or not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            # ── Company ──────────────────────────────────────────────────────
            company = ""
            for sel in ['a.comp-name', 'span.comp-name', '[class*="comp-name"]']:
                el = card.locator(sel).first
                if await el.count() > 0:
                    company = (await el.inner_text()).strip()
                    break

            # ── Location ─────────────────────────────────────────────────────
            location = ""
            el = card.locator('span.locWdth').first
            if await el.count() > 0:
                location = (await el.get_attribute("title") or await el.inner_text()).strip()

            # ── Experience ───────────────────────────────────────────────────
            experience = ""
            el = card.locator('span.expwdth').first
            if await el.count() > 0:
                experience = (await el.get_attribute("title") or await el.inner_text()).strip()

            # ── Posted date ──────────────────────────────────────────────────
            posted = ""
            el = card.locator('span.job-post-day').first
            if await el.count() > 0:
                posted = (await el.inner_text()).strip()

            jobs.append({
                "title":      title,
                "company":    company,
                "location":   location,
                "experience": experience,
                "posted":     posted,
                "url":        href,
            })

        except Exception:
            continue

    return jobs


async def scrape_keyword(page, keyword: str, search_url: str) -> list[dict]:
    """Navigate directly to search results URL and scrape page 1."""
    matched: list[dict] = []
    seen_urls_this_run: set[str] = set()

    print(f"\n{'=' * 55}")
    print(f"  Searching: {keyword}")
    print(f"{'=' * 55}")

    # Navigate directly to pre-built search URL — bypasses the search form entirely
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=35_000)
        await page.wait_for_timeout(3_500)
        await page.wait_for_selector('a.title', timeout=15_000)
        print(f"  URL: {page.url}")
    except PlaywrightTimeout:
        print("  No results found — skipping keyword.")
        return matched
    except Exception as e:
        print(f"  Failed to load results: {e}")
        return matched

    page_jobs = await collect_page_jobs(page)
    print(f"  Jobs found on page: {len(page_jobs)}\n")

    for job in page_jobs:
        if job["url"] in seen_urls_this_run:
            continue
        seen_urls_this_run.add(job["url"])

        if is_recent(job["posted"]):
            matched.append(job)
            # print(f"  ✔  KEPT    [{job['posted']:>15}]  {job['title']} @ {job['company']} — {job['location']}")
        # else:
            # print(f"  ✘  SKIPPED [{job['posted']:>15}]  {job['title']} @ {job['company']} — {job['location']}")

    print(f"\n  Recent matches: {len(matched)}")
    return matched


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 55)
    print("  Naukri Jobs Scanner — Recent Roles (< 1 day old)")
    print("=" * 55)

    all_matched: list[dict] = []

    async with async_playwright() as pw:
        for i, (keyword, search_url) in enumerate(TARGET_SEARCHES):
            # Fresh browser per keyword — avoids crashes carrying over between searches
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = await ctx.new_page()
            try:
                results = await scrape_keyword(page, keyword, search_url)
                all_matched.extend(results)
            except Exception as e:
                print(f"  [ERROR] Keyword '{keyword}' failed: {e}")
            finally:
                await browser.close()

            if i < len(TARGET_SEARCHES) - 1:
                print(f"\n  Cooling down 5s before next search...")
                await asyncio.sleep(5)

    # De-duplicate across searches by URL
    seen_hrefs: set[str] = set()
    deduped = []
    for job in all_matched:
        if job["url"] not in seen_hrefs:
            seen_hrefs.add(job["url"])
            deduped.append(job)

    print("\n" + "=" * 55)
    print(f"  Total recent matches (across all searches): {len(deduped)}")
    print("=" * 55)

    previously_seen = load_seen_urls()
    new_jobs = [j for j in deduped if j["url"] not in previously_seen]
    print(f"  New (not sent before): {len(new_jobs)}")

    for j in new_jobs:
        print(f"\n  • {j['title']}")
        print(f"    Company:    {j['company']}")
        print(f"    Location:   {j['location']}")
        print(f"    Experience: {j['experience']}")
        print(f"    Posted:     {j['posted']}")
        print(f"    URL:        {j['url']}")

    if not new_jobs:
        print("\n  No new recent roles found.")
    else:
        print(f"\n  Sending email ({len(new_jobs)} new role(s))...")
        send_email(new_jobs, previously_seen)
        save_seen_urls(previously_seen | {j["url"] for j in new_jobs})

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
