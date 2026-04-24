"""
Microsoft Jobs Scanner
-----------------------
Flow (exactly as specified):
  1. Go to https://careers.microsoft.com/v2/global/en/home.html
  2. Click "Find jobs" button
  3. Sort by Latest
  4. Scrape first 5 pages for matching roles
  5. Email results to lopes.o@northeastern.edu and srishti77@gmail.com

Target roles: Software Engineer, Data Engineer, Data Analyst, Business Intelligence Analyst

Run: python microsoft_jobs_scanner.py
"""

import asyncio
import json
import os
import smtplib
import sys
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
TARGET_EMAILS   = os.environ.get("MS_TARGET_EMAILS", "").split(",")
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SMTP_SERVER     = "smtp.gmail.com"
SMTP_PORT       = 465

MICROSOFT_URL   = "https://careers.microsoft.com/v2/global/en/home.html"
PAGES_TO_SCRAPE = 10
SEEN_JOBS_FILE  = os.path.join(os.path.dirname(__file__), "microsoft_seen_jobs.json")

TARGET_ROLES = [
    "software engineer",
    "data engineer",
    "data analyst",
    "business intelligence analyst",
    "bi analyst",
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


def is_target_role(title: str) -> bool:
    t = title.lower()
    return any(role in t for role in TARGET_ROLES)


def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count   = sum(1 for j in jobs if j["url"] not in previously_seen)
    count       = len(jobs)
    subject     = f"Microsoft Jobs Alert — {count} Role(s) Found ({new_count} NEW)"

    if not jobs:
        plain = "No matching jobs found (Software Engineer / Data Engineer / Data Analyst / BI Analyst, Latest)."
        html  = "<p>No matching jobs found.</p>"
    else:
        NEW_BADGE = '<span style="background:#1a7f37;color:#fff;font-size:11px;font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
        rows_list = []
        for j in jobs:
            is_new   = j["url"] not in previously_seen
            row_bg   = 'background:#e6f4ea;' if is_new else ''
            badge    = NEW_BADGE if is_new else ''
            rows_list.append(
                f'<tr style="{row_bg}">'
                f'<td style="padding:8px;border:1px solid #ddd;">{badge}{j["title"]}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;"><a href="{j["url"]}">{j["url"]}</a></td>'
                f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location", "")}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{j.get("date", "")}</td>'
                f'</tr>'
            )
        rows = "\n".join(rows_list)
        html = f"""
        <html><body style="font-family:Arial,sans-serif;color:#333">
        <h2 style="color:#0078D4">Microsoft Jobs — Matching Roles</h2>
        <p>Found <strong>{count}</strong> role(s) matching:
           <em>Software Engineer &nbsp;|&nbsp; Data Engineer &nbsp;|&nbsp;
           Data Analyst &nbsp;|&nbsp; Business Intelligence Analyst</em>
        </p>
        <table style="border-collapse:collapse;width:100%;max-width:1200px">
          <tr style="background:#0078D4;color:#fff">
            <th style="padding:10px;border:1px solid #005a9e;text-align:left;width:30%">Role</th>
            <th style="padding:10px;border:1px solid #005a9e;text-align:left">Link</th>
            <th style="padding:10px;border:1px solid #005a9e;text-align:left;width:20%">Location</th>
            <th style="padding:10px;border:1px solid #005a9e;text-align:left;width:15%">Date Posted</th>
          </tr>
          {rows}
        </table>
        <p style="font-size:12px;color:#888;margin-top:20px">
          Source: careers.microsoft.com · Sorted by Latest · 10 Pages
        </p>
        </body></html>
        """
        plain = f"Found {count} matching role(s) ({new_count} NEW):\n\n" + "\n".join(
            f"- {'[NEW] ' if j['url'] not in previously_seen else ''}{j['title']} | {j.get('location', 'location unknown')} ({j.get('date', 'date unknown')})\n  {j['url']}" for j in jobs
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = SENDER_EMAIL
    msg["Bcc"]     = ", ".join(TARGET_EMAILS)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, TARGET_EMAILS, msg.as_string())

    print(f"[email] BCC'd to {', '.join(TARGET_EMAILS)} — {count} job(s).")


async def collect_page_jobs(page) -> list[dict]:
    """Collect job title, URL, and posting date from the current results page.

    Microsoft job links contain multi-line inner text:
      Line 0 — job title
      Line 1 — location
      Line 2 — "Posted X hours/days ago"
    """
    seen: set[str] = set()
    results = []

    candidates = await page.locator('a[href*="/job/"]').all()
    for link in candidates:
        try:
            full_text = (await link.inner_text()).strip()
            href      = (await link.get_attribute("href") or "").strip()

            if not full_text or not href:
                continue
            if not href.startswith("http"):
                href = "https://careers.microsoft.com" + href
            if href in seen:
                continue
            seen.add(href)

            # Split multi-line text; first non-empty line is the job title
            lines = [l.strip() for l in full_text.splitlines() if l.strip()]
            title = lines[0] if lines else ""
            if not title or len(title) < 5:
                continue

            # Find the line that starts with "Posted"
            date = next((l for l in lines if l.lower().startswith("posted")), "")

            # Location is the line between title and the "Posted" line
            location = ""
            for l in lines[1:]:
                if not l.lower().startswith("posted"):
                    location = l
                    break

            results.append({"title": title, "url": href, "date": date, "location": location})
        except Exception:
            continue

    return results


# ──────────────────────────────────────────────────────────────────────────────
# MAIN SCRAPER
# ──────────────────────────────────────────────────────────────────────────────

async def scrape_jobs() -> list[dict]:
    matched: list[dict] = []
    seen_urls: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx     = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()

        # ── 1. Load Microsoft Careers home ───────────────────────────────────
        print("[1] Loading Microsoft Careers home...")
        await page.goto(MICROSOFT_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3_000)

        # Accept cookie banner if present
        try:
            btn = page.locator(
                'button:has-text("Accept all"), button:has-text("Accept All"), '
                'button#onetrust-accept-btn-handler'
            ).first
            if await btn.count() > 0:
                await btn.click(timeout=4_000)
                await page.wait_for_timeout(800)
                print("  [cookies] Accepted.")
        except Exception:
            pass

        # ── 2. Click "Find jobs" ──────────────────────────────────────────────
        print("[2] Clicking 'Find jobs'...")
        try:
            find_btn = page.locator('button#find-jobs-btn, button.find-jobs-btn').first
            await find_btn.wait_for(state="visible", timeout=10_000)
            await find_btn.click()
            print("  [find-jobs] Clicked.")
        except Exception as e:
            print(f"  [find-jobs] Error: {e}")

        # Wait for results page to load
        await page.wait_for_timeout(4_000)
        print(f"  URL: {page.url}")

        # Wait for job listings to appear
        try:
            await page.wait_for_selector(
                'a[href*="/job/"], [class*="job-card"], [class*="JobCard"]',
                timeout=15_000
            )
        except PlaywrightTimeout:
            await page.screenshot(path="msft_debug_after_find.png")
            print("  No jobs appeared after Find Jobs. Saved msft_debug_after_find.png")
            await browser.close()
            return matched

        # ── 3. Sort by Latest ─────────────────────────────────────────────────
        print("[3] Sorting by Latest...")
        try:
            # Click the sort button — may show "Sort: Relevance" or "Sort: Latest"
            sort_btn = page.locator(
                'button:has([class*="button-text-small"]):has-text("Sort"), '
                'button:has-text("Sort")'
            ).first
            await sort_btn.wait_for(state="visible", timeout=8_000)
            await sort_btn.click()
            await page.wait_for_timeout(800)

            # Select "Latest" from the dropdown
            latest_opt = page.locator(
                'button:has-text("Latest"), '
                '[role="option"]:has-text("Latest"), '
                'li:has-text("Latest"), '
                'a:has-text("Latest")'
            ).first
            await latest_opt.click(timeout=5_000)
            await page.wait_for_timeout(3_000)
            print("  [sort] Latest selected.")
        except Exception as e:
            print(f"  [sort] Skipped: {e}")

        # ── 4. Capture base URL for pagination ────────────────────────────────
        base_results_url = page.url
        print(f"  Base results URL: {base_results_url}")

        # ── 5. Scrape 10 pages ─────────────────────────────────────────────────
        for page_num in range(1, PAGES_TO_SCRAPE + 1):
            print(f"\n[page {page_num} of {PAGES_TO_SCRAPE}]")

            if page_num > 1:
                # Microsoft uses start= for pagination (start=0,10,20,30,40…)
                parsed = urllib.parse.urlparse(base_results_url)
                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                params["start"] = [str((page_num - 1) * 10)]
                params.pop("pg", None)
                paged_url = urllib.parse.urlunparse(
                    parsed._replace(query=urllib.parse.urlencode(params, doseq=True))
                )
                print(f"  URL: {paged_url}")
                try:
                    await page.goto(paged_url, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(4_000)
                    try:
                        await page.wait_for_selector(
                            'a[href*="/job/"], [class*="job-card"]',
                            timeout=10_000
                        )
                    except PlaywrightTimeout:
                        # Fallback: try Next button on current page
                        try:
                            next_btn = page.locator(
                                'button[aria-label="Next page"], button:has-text("Next"), '
                                'a[aria-label="Next"], [class*="next"]'
                            ).first
                            if await next_btn.count() == 0:
                                print("  No more pages — done.")
                                break
                            await next_btn.click()
                            await page.wait_for_selector('a[href*="/job/"]', timeout=10_000)
                        except Exception:
                            print("  No more pages — done.")
                            break
                except Exception as e:
                    print(f"  Could not load page {page_num}: {e}")
                    break
            else:
                await page.wait_for_timeout(1_500)

            jobs_on_page = await collect_page_jobs(page)
            print(f"  Links found: {len(jobs_on_page)}")

            if not jobs_on_page:
                print("  Empty page — done.")
                break

            page_matches = 0
            for job in jobs_on_page:
                if job["url"] in seen_urls:
                    continue
                seen_urls.add(job["url"])
                if is_target_role(job["title"]):
                    matched.append(job)
                    page_matches += 1
                    print(f"  MATCH: {job['title']}")

            print(f"  Matches this page: {page_matches}")

        await browser.close()

    return matched


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("Microsoft Jobs Scanner — Latest · 10 Pages")
    print("=" * 60)
    print(f"Target roles: {', '.join(TARGET_ROLES)}")

    jobs = await scrape_jobs()

    print("\n" + "=" * 60)
    print(f"Total matches: {len(jobs)}")
    for j in jobs:
        print(f"  • {j['title']} | {j.get('location', '')} ({j.get('date', '')})")
        print(f"    {j['url']}")
    print("=" * 60)

    previously_seen = load_seen_urls()
    new_jobs = [j for j in jobs if j["url"] not in previously_seen]
    print(f"New roles (not seen before): {len(new_jobs)}")

    save_seen_urls(previously_seen | {j["url"] for j in jobs})

    print("\nSending email...")
    send_email(jobs, previously_seen)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
