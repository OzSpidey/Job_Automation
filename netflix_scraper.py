"""
Netflix Jobs Scraper
====================
Netflix's careers site (explore.jobs.netflix.net) is a Phenom JS SPA.
Uses Playwright to render the page, then extracts all positions from the
embedded JSON in a <script> tag under the key "positions".

Run: python netflix_scraper.py
"""

import asyncio
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────────────────────────────
TARGET_EMAIL    = os.environ.get("EMAIL_TO", "")
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

CAREERS_URL    = "https://explore.jobs.netflix.net/careers?domain=netflix.com"
SEEN_JOBS_FILE = os.path.join(os.path.dirname(__file__), "json", "netflix_seen_jobs.json")

TARGET_ROLES = [
    "data engineer",
    "data analyst",
    "analytics engineer",
    "analytics",
    "business intelligence",
    "machine learning",
    "software engineer",
    "software developer",
    "new grad",
    "university graduate",
    "early career",
]

AI_REGEX = re.compile(r"\bai\b", re.I)

EXCLUDE_LEVELS = [
    "senior", "sr.", "principal", "lead", "staff",
    "manager", "director", "head", "vp", "architect",
]

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_seen() -> set:
    if not os.path.exists(SEEN_JOBS_FILE):
        return set()
    with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen(seen: set) -> None:
    os.makedirs(os.path.dirname(SEEN_JOBS_FILE), exist_ok=True)
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)


def is_target_role(title: str) -> bool:
    t = title.lower()
    if any(level in t for level in EXCLUDE_LEVELS):
        return False
    if any(role in t for role in TARGET_ROLES):
        return True
    return bool(AI_REGEX.search(title))


def job_url(posting: dict) -> str:
    job_id = posting.get("id") or ""
    return f"https://explore.jobs.netflix.net/careers/job/{job_id}"


def parse_timestamp(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


# ── Fetch ──────────────────────────────────────────────────────────────────────
async def fetch_all_jobs_async() -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        print(f"  [browser] navigating to {CAREERS_URL}")
        await page.goto(CAREERS_URL, wait_until="networkidle", timeout=60_000)

        html = await page.content()
        await browser.close()

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script"):
        text = (tag.string or "").strip()
        if '"positions"' not in text:
            continue
        try:
            data      = json.loads(text)
            positions = data.get("positions")
            if isinstance(positions, list) and positions:
                print(f"  [page] extracted {len(positions)} positions")
                return positions
        except (json.JSONDecodeError, AttributeError):
            pass

    print("  [!] Could not find positions in rendered page")
    return []


def fetch_all_jobs() -> list[dict]:
    return asyncio.run(fetch_all_jobs_async())


# ── Email ──────────────────────────────────────────────────────────────────────
def send_email(jobs: list[dict], previously_seen: set) -> None:
    new_count = sum(1 for j in jobs if j["url"] not in previously_seen)
    count     = len(jobs)
    subject   = f"Netflix Jobs Alert — {count} Role(s) Found ({new_count} NEW)"

    NEW_BADGE = (
        '<span style="background:#e50914;color:#fff;font-size:11px;'
        'font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
    )
    rows = []
    for j in jobs:
        is_new = j["url"] not in previously_seen
        row_bg = "background:#fff5f5;" if is_new else ""
        badge  = NEW_BADGE if is_new else ""
        rows.append(
            f'<tr style="{row_bg}">'
            f'<td style="padding:8px;border:1px solid #ddd;">{badge}{j["title"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location", "")}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">'
            f'<a href="{j["url"]}" style="color:#e50914">{j["url"]}</a></td>'
            f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{j.get("date", "")}</td>'
            f"</tr>"
        )

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#221f1f">
    <h2 style="color:#e50914">Netflix Jobs — Matching Roles</h2>
    <p>Found <strong>{count}</strong> role(s) | <strong>{new_count}</strong> new</p>
    <table style="border-collapse:collapse;width:100%;max-width:1100px">
      <tr style="background:#221f1f;color:#fff">
        <th style="padding:10px;border:1px solid #333;text-align:left;width:35%">Role</th>
        <th style="padding:10px;border:1px solid #333;text-align:left;width:20%">Location</th>
        <th style="padding:10px;border:1px solid #333;text-align:left">Link</th>
        <th style="padding:10px;border:1px solid #333;text-align:left;width:12%">Date</th>
      </tr>
      {"".join(rows)}
    </table>
    <p style="font-size:12px;color:#888;margin-top:20px">
      Source: explore.jobs.netflix.net
    </p>
    </body></html>
    """
    plain = f"Found {count} role(s) ({new_count} NEW):\n\n" + "\n".join(
        f"- {'[NEW] ' if j['url'] not in previously_seen else ''}{j['title']} — {j.get('location', '')}\n  {j['url']}"
        for j in jobs
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = TARGET_EMAIL
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, TARGET_EMAIL, msg.as_string())

    print(f"[email] Sent to {TARGET_EMAIL} — {count} job(s).")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("Netflix Jobs Scraper")
    print("=" * 60)

    print("[1] Fetching jobs via Playwright...")
    raw = fetch_all_jobs()
    print(f"  Total raw jobs fetched: {len(raw)}")

    print("[2] Filtering by target roles...")
    previously_seen = load_seen()
    matched   = []
    seen_urls: set = set()

    for posting in raw:
        title = posting.get("name", "")
        if not is_target_role(title):
            continue
        url = job_url(posting)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        matched.append({
            "title":    title,
            "url":      url,
            "location": posting.get("location", ""),
            "date":     parse_timestamp(posting.get("t_create")),
        })
        print(f"  MATCH: {title}  [{posting.get('location', '')}]")

    print(f"\n{'='*60}")
    print(f"Total matches: {len(matched)}")
    new_jobs = [j for j in matched if j["url"] not in previously_seen]
    print(f"New roles (not seen before): {len(new_jobs)}")

    save_seen(previously_seen | seen_urls)

    if not new_jobs:
        print("No new roles — skipping email.")
    else:
        print(f"\nSending email ({len(new_jobs)} new role(s))...")
        send_email(matched, previously_seen)
    print("Done.")


if __name__ == "__main__":
    main()
