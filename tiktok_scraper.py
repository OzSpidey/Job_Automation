"""
TikTok / ByteDance Jobs Scraper
================================
lifeattiktok.com is a fully client-side Next.js app with no publicly
accessible API. This scraper uses Playwright (headless Chromium) to:

  1. Navigate to the search page for each target keyword
  2. Intercept the JSON API response the browser fetches internally
     to get clean structured job data
  3. Fall back to DOM link extraction if the API shape changes
  4. Filter by role / seniority, diff against seen-jobs, email new matches

Run: python tiktok_scraper.py
"""

import asyncio
import json
import os
import re
import smtplib
import sys
import urllib.parse
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("playwright not installed — run: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Config ─────────────────────────────────────────────────────────────────────
TARGET_EMAIL    = os.environ.get("EMAIL_TO", "")
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SMTP_SERVER     = "smtp.gmail.com"
SMTP_PORT       = 465

BASE_URL        = "https://lifeattiktok.com/search"
LOCATION_FILTER = "United States"
SEEN_JOBS_FILE  = Path(__file__).parent / "json" / "tiktok_seen_jobs.json"

SEARCH_KEYWORDS = ["data", "analytics"]

TARGET_ROLES = [
    "data engineer",
    "data analyst",
    "data scientist",
    "analytics engineer",
    "machine learning",
    "ml engineer",
    "ai engineer",
    "business intelligence",
    "analyst",
    "analytics",
    "data",
]

EXCLUDE_LEVELS = [
    "senior", "sr.", " sr ", "staff", "lead", "principal",
    "manager", "director", "head of", "vp", "vice president",
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if SEEN_JOBS_FILE.exists():
        return set(json.loads(SEEN_JOBS_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(ids: set[str]) -> None:
    SEEN_JOBS_FILE.parent.mkdir(exist_ok=True)
    SEEN_JOBS_FILE.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


def is_target(title: str) -> bool:
    t = title.lower()
    if any(lvl in t for lvl in EXCLUDE_LEVELS):
        return False
    return any(role in t for role in TARGET_ROLES)


def extract_id(url: str) -> str:
    """Pull the numeric job ID out of a TikTok careers URL."""
    m = re.search(r"/(\d{10,})", url)
    return m.group(1) if m else ""


def job_url(job_id: str) -> str:
    return f"https://lifeattiktok.com/search/{job_id}"


def parse_api_response(data) -> list[dict]:
    """
    Flexibly extract job records from an intercepted API response.
    TikTok's internal API shape is undocumented — we probe common keys.
    """
    jobs = []

    # Unwrap one level of nesting to find a list
    candidates = []
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        for key in ("data", "jobs", "positions", "items", "results", "list",
                    "jobList", "positionList", "job_list"):
            val = data.get(key)
            if isinstance(val, list):
                candidates = val
                break
            if isinstance(val, dict):
                for sub in ("list", "items", "jobs", "positions", "results"):
                    s = val.get(sub)
                    if isinstance(s, list):
                        candidates = s
                        break
                if candidates:
                    break

    for item in candidates:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or item.get("positionName") or
                 item.get("jobName") or item.get("name") or "")
        if not title:
            continue
        job_id = str(item.get("id") or item.get("jobId") or
                     item.get("positionId") or "")
        loc = (item.get("location") or item.get("locationName") or
               item.get("city") or item.get("area") or "")
        if isinstance(loc, list):
            loc = ", ".join(str(x) for x in loc if x)
        posted = (item.get("postDate") or item.get("publishTime") or
                  item.get("createTime") or item.get("postedDate") or "")
        url = (item.get("url") or item.get("detailUrl") or
               (job_url(job_id) if job_id else ""))
        jobs.append({
            "id":       job_id,
            "title":    str(title).strip(),
            "location": str(loc).strip(),
            "posted":   str(posted).strip(),
            "url":      url,
        })
    return jobs


# ── Core scraper ───────────────────────────────────────────────────────────────

async def scrape_jobs() -> list[dict]:
    collected: dict[str, dict] = {}   # job_id → job

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await ctx.new_page()

        # ── Intercept API responses ──────────────────────────────────────────
        async def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct:
                    return
                if response.status != 200:
                    return
                data = await response.json()
                api_jobs = parse_api_response(data)
                for j in api_jobs:
                    if j["id"] and j["id"] not in collected:
                        collected[j["id"]] = j
                        print(f"  [api] {j['title']} ({j['location']})")
            except Exception:
                pass

        page.on("response", on_response)

        # ── Search each keyword ──────────────────────────────────────────────
        for keyword in SEARCH_KEYWORDS:
            params = {"keyword": keyword, "location": LOCATION_FILTER}
            url = BASE_URL + "?" + urllib.parse.urlencode(params)
            print(f"\n[search] {keyword!r} → {url}")

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(3_000)
            except Exception as e:
                print(f"  Navigation error: {e}")
                continue

            # Accept cookie/consent banner if present
            try:
                btn = page.locator(
                    'button:has-text("Accept"), button:has-text("Got it"), '
                    'button:has-text("I agree"), button[id*="accept"], '
                    'button[id*="cookie"]'
                ).first
                if await btn.count():
                    await btn.click(timeout=3_000)
                    await page.wait_for_timeout(800)
            except Exception:
                pass

            # Wait for job cards
            try:
                await page.wait_for_selector(
                    'a[href*="/search/"], a[href*="/position/"]',
                    timeout=15_000,
                )
            except PlaywrightTimeout:
                print("  No job cards rendered.")
                continue

            # Scroll to trigger lazy-loaded cards
            for _ in range(4):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(1_000)

            await page.wait_for_timeout(2_000)   # let API calls settle

            # ── DOM fallback: scrape link elements ───────────────────────────
            links = await page.locator(
                'a[href*="/search/"], a[href*="/position/"]'
            ).all()
            dom_count = 0
            for link in links:
                try:
                    href = (await link.get_attribute("href") or "").strip()
                    if not href:
                        continue
                    if not href.startswith("http"):
                        href = "https://lifeattiktok.com" + href
                    job_id = extract_id(href)
                    if not job_id or job_id in collected:
                        continue

                    lines = [
                        l.strip()
                        for l in (await link.inner_text()).splitlines()
                        if l.strip()
                    ]
                    if not lines:
                        continue

                    title    = lines[0]
                    location = lines[1] if len(lines) > 1 else ""
                    posted   = next(
                        (l for l in lines
                         if re.search(r"\d{4}|\bday\b|\bhour\b|\bweek\b", l, re.I)),
                        "",
                    )
                    collected[job_id] = {
                        "id":       job_id,
                        "title":    title,
                        "location": location,
                        "posted":   posted,
                        "url":      href,
                    }
                    dom_count += 1
                except Exception:
                    continue
            if dom_count:
                print(f"  [dom] +{dom_count} jobs from link scraping")

        await browser.close()

    # ── Filter by role ─────────────────────────────────────────────────────────
    matched = [j for j in collected.values() if is_target(j["title"])]
    matched.sort(key=lambda j: j.get("posted", ""), reverse=True)
    return matched


# ── Email ──────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict], prev_seen: set[str]) -> None:
    new_jobs = [j for j in jobs if j["id"] not in prev_seen]
    count     = len(jobs)
    new_count = len(new_jobs)
    subject   = f"TikTok Jobs Alert — {count} Role(s) Found ({new_count} NEW)"

    BRAND    = "#010101"
    NEW_BADGE = (
        '<span style="background:#1a7f37;color:#fff;font-size:11px;'
        'font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
    )

    rows = "\n".join(
        f'<tr style="{"background:#e6f4ea;" if j["id"] not in prev_seen else ""}">'
        f'<td style="padding:8px;border:1px solid #ddd;">'
        f'{"" if j["id"] in prev_seen else NEW_BADGE}{j["title"]}</td>'
        f'<td style="padding:8px;border:1px solid #ddd;">'
        f'<a href="{j["url"]}">{j["url"]}</a></td>'
        f'<td style="padding:8px;border:1px solid #ddd;">{j["location"]}</td>'
        f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{j["posted"]}</td>'
        f'</tr>'
        for j in jobs
    )

    html = f"""<html><body style="font-family:Arial,sans-serif;color:#333">
<h2 style="color:{BRAND}">TikTok / ByteDance Jobs — Matching Roles</h2>
<p>Found <strong>{count}</strong> role(s) · <strong>{new_count}</strong> new</p>
<table style="border-collapse:collapse;width:100%;max-width:1200px">
  <tr style="background:{BRAND};color:#fff">
    <th style="padding:10px;border:1px solid #000;text-align:left;width:30%">Role</th>
    <th style="padding:10px;border:1px solid #000;text-align:left">Link</th>
    <th style="padding:10px;border:1px solid #000;text-align:left;width:20%">Location</th>
    <th style="padding:10px;border:1px solid #000;text-align:left;width:12%">Posted</th>
  </tr>
  {rows}
</table>
<p style="font-size:12px;color:#888;margin-top:20px">
  Source: lifeattiktok.com · Keywords: {", ".join(SEARCH_KEYWORDS)}
</p>
</body></html>"""

    plain = f"TikTok Jobs — {count} role(s), {new_count} new:\n\n" + "\n".join(
        f"{'[NEW] ' if j['id'] not in prev_seen else ''}{j['title']} | "
        f"{j['location']} | {j['posted']}\n  {j['url']}"
        for j in jobs
    )

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = TARGET_EMAIL
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, TARGET_EMAIL, msg.as_string())
    print(f"[email] Sent to {TARGET_EMAIL} — {count} job(s), {new_count} new.")


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("TikTok / ByteDance Jobs Scraper")
    print("=" * 60)

    jobs = await scrape_jobs()

    print(f"\nMatched roles: {len(jobs)}")
    for j in jobs:
        print(f"  • {j['title']} | {j['location']}")
        print(f"    {j['url']}")

    prev_seen = load_seen()
    new_jobs  = [j for j in jobs if j["id"] not in prev_seen]
    print(f"\nNew (unseen): {len(new_jobs)}")

    save_seen(prev_seen | {j["id"] for j in jobs})

    if not new_jobs:
        print("No new roles — skipping email.")
        return

    print(f"Sending email ({len(new_jobs)} new role(s))...")
    send_email(jobs, prev_seen)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
