"""
Tesla Jobs Scraper
==================
Fetches Tesla's public job catalog via the internal CUA API endpoint:

    GET https://www.tesla.com/cua-api/apps/careers/state

Returns all ~6,000 global jobs in one JSON response (no pagination).
Uses Playwright (headless Chromium) to navigate tesla.com/careers/search
and intercept the XHR response for /cua-api/apps/careers/state — the same
technique used for TikTok/Bloomberg. Playwright executes Akamai's JS
challenge natively, which plain HTTP requests cannot.

Response shape:
  listings[]:  each entry has id, t (title), dp (dept_id), l (loc_id), y (type_id)
  lookup.departments:  {dept_id -> display name}
  lookup.locations:    {loc_id -> display string, e.g. "Austin, Texas, United States"}
  lookup.types:        {type_id -> "fulltime" / "intern" / ...}

US filter: lookup.locations[l] must contain "United States".
Recency proxy: listings are sorted by job ID descending (higher = newer).

Run: python tesla_scraper.py
"""

import asyncio
import json
import os
import re
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("playwright not installed — run: pip install playwright && playwright install chromium")
    sys.exit(1)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
TARGET_EMAIL    = os.environ.get("EMAIL_TO", "")
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SMTP_SERVER     = "smtp.gmail.com"
SMTP_PORT       = 465

BASE_DIR     = Path(__file__).parent
SEEN_FILE    = BASE_DIR / "json" / "tesla_seen_jobs.json"

CAREERS_URL  = "https://www.tesla.com/careers/search/?site=US"
STATE_PATH   = "cua-api/apps/careers/state"
USER_AGENT   = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
NAV_TIMEOUT  = 60_000   # ms
WAIT_TIMEOUT = 35       # seconds to wait for state XHR after page load

TARGET_ROLES = [
    "data engineer",
    "analytics engineer",
    "data analyst",
    "business analyst",
    "business intelligence",
    "software engineer",
    "software developer",
    "machine learning engineer",
    "ml engineer",
    "ai engineer",
    "backend engineer",
    "full stack engineer",
    "fullstack engineer",
]

EXCLUDE_LEVELS = [
    "senior", "sr.", " sr ", "lead", "staff", "principal",
    "manager", "director", "vp ", "vice president", "head of",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return {str(x) for x in data}
    return set()


def save_seen(ids: set[str]) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(ids)), encoding="utf-8")


def is_target(title: str) -> bool:
    t = title.lower()
    if any(lvl in t for lvl in EXCLUDE_LEVELS):
        return False
    return any(role in t for role in TARGET_ROLES)


def is_us(location_str: str) -> bool:
    if not location_str:
        return False
    return "united states" in location_str.lower()


def make_url(title: str, job_id) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"https://www.tesla.com/careers/search/job/{slug}-{job_id}"


# ── Fetch ─────────────────────────────────────────────────────────────────────

async def _fetch_state_async() -> dict:
    captured: dict = {}
    ready = asyncio.Event()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = await ctx.new_page()

        async def on_response(response):
            if STATE_PATH in response.url and not ready.is_set():
                print(f"  [intercept] {response.status} {response.url[:120]}")
                if response.status == 200:
                    try:
                        captured.update(await response.json())
                        ready.set()
                    except Exception as e:
                        print(f"  [warn] JSON parse failed: {e}")

        page.on("response", on_response)

        print(f"  [browser] navigating to {CAREERS_URL}")
        try:
            await page.goto(CAREERS_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except PlaywrightTimeout:
            print("  [warn] Page load timed out — checking if state XHR was captured anyway")
        except Exception as e:
            print(f"  [warn] Navigation error: {e}")

        # Wait for the state XHR (it fires shortly after page JS initialises)
        try:
            await asyncio.wait_for(ready.wait(), timeout=WAIT_TIMEOUT)
            print("  [ok] State XHR captured.")
        except asyncio.TimeoutError:
            print("  [warn] Timed out waiting for state XHR — Akamai may have blocked the page load")

        await browser.close()

    return captured


def fetch_state() -> dict:
    return asyncio.run(_fetch_state_async())


# ── Parse ─────────────────────────────────────────────────────────────────────

def parse_jobs(state: dict) -> list[dict]:
    listings     = state.get("listings") or []
    lookup       = state.get("lookup") or {}
    lookup_locs  = lookup.get("locations") or {}
    lookup_depts = lookup.get("departments") or {}

    matched  = []
    seen_ids = set()

    for item in listings:
        job_id = str(item.get("id", ""))
        title  = (item.get("t") or "").strip()
        if not title or not job_id or job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        if not is_target(title):
            continue

        loc_key  = item.get("l")
        location = lookup_locs.get(loc_key) or lookup_locs.get(str(loc_key)) or ""
        if not is_us(location):
            continue

        dept_key = item.get("dp")
        dept     = lookup_depts.get(dept_key) or lookup_depts.get(str(dept_key)) or ""

        matched.append({
            "job_id":     job_id,
            "title":      title,
            "location":   location,
            "department": dept,
            "url":        make_url(title, job_id),
        })
        print(f"  MATCH: {title}  [{location}]")

    # Higher job IDs are newer postings
    matched.sort(key=lambda j: int(j["job_id"]) if j["job_id"].isdigit() else 0, reverse=True)
    return matched


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count = sum(1 for j in jobs if j["job_id"] not in previously_seen)
    count     = len(jobs)
    subject   = f"Tesla Jobs Alert — {count} Role(s) Found ({new_count} NEW)"

    NEW_BADGE = (
        '<span style="background:#cc0000;color:#fff;font-size:11px;'
        'font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
    )

    if not jobs:
        html  = "<p>No matching Tesla jobs found.</p>"
        plain = "No matching Tesla jobs found."
    else:
        rows = []
        for j in jobs:
            is_new = j["job_id"] not in previously_seen
            row_bg = "background:#fff5f5;" if is_new else ""
            badge  = NEW_BADGE if is_new else ""
            rows.append(
                f'<tr style="{row_bg}">'
                f'<td style="padding:8px;border:1px solid #ddd;">{badge}{j["title"]}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location","")}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">{j.get("department","")}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">'
                f'<a href="{j["url"]}" style="color:#cc0000">{j["url"]}</a></td>'
                f"</tr>"
            )
        role_labels = " | ".join(r.title() for r in TARGET_ROLES)
        html = f"""
        <html><body style="font-family:-apple-system,BlinkMacSystemFont,Arial,sans-serif;color:#000">
        <h2 style="color:#cc0000">Tesla Jobs — {count} Matching US Role(s)</h2>
        <p>Roles: <em>{role_labels}</em></p>
        <table style="border-collapse:collapse;width:100%;max-width:1200px">
          <tr style="background:#cc0000;color:#fff">
            <th style="padding:10px;text-align:left;width:30%">Role</th>
            <th style="padding:10px;text-align:left;width:20%">Location</th>
            <th style="padding:10px;text-align:left;width:15%">Department</th>
            <th style="padding:10px;text-align:left">Link</th>
          </tr>
          {"".join(rows)}
        </table>
        <p style="font-size:12px;color:#666;margin-top:20px">
          Source: tesla.com/cua-api/apps/careers/state · Sorted by Job ID (newest first)
        </p>
        </body></html>
        """
        plain = f"Found {count} matching role(s) ({new_count} NEW):\n\n" + "\n".join(
            f"- {'[NEW] ' if j['job_id'] not in previously_seen else ''}"
            f"{j['title']} — {j.get('location','location unknown')}\n"
            f"  {j['url']}"
            for j in jobs
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = TARGET_EMAIL
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, TARGET_EMAIL, msg.as_string())
    print(f"[email] Sent to {TARGET_EMAIL} — {count} job(s).")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Tesla Jobs Scraper — curl_cffi + CUA state API")
    print("=" * 60)

    t0 = time.time()

    print("[1] Fetching job catalog...")
    try:
        state = fetch_state()
    except Exception as exc:
        print(f"[error] Failed to fetch state: {exc}")
        sys.exit(1)

    total = len(state.get("listings") or [])
    print(f"  Total listings in catalog: {total:,}")

    print("[2] Filtering by target roles (US only, non-senior)...")
    jobs    = parse_jobs(state)
    elapsed = time.time() - t0

    print(f"\n{'=' * 60}")
    print(f"Total matches: {len(jobs)} | elapsed: {elapsed:.1f}s")
    for j in jobs:
        print(f"  • {j['title']}  [{j['location']}]")
        print(f"    {j['url']}")
    print("=" * 60)

    previously_seen = load_seen()
    new_jobs = [j for j in jobs if j["job_id"] not in previously_seen]
    print(f"New roles (not seen before): {len(new_jobs)}")

    save_seen(previously_seen | {j["job_id"] for j in jobs})

    if not new_jobs:
        print("No new roles — skipping email.")
    elif not TARGET_EMAIL:
        print("[warn] EMAIL_TO not set — skipping email.")
    else:
        print(f"\nSending email ({len(new_jobs)} new role(s))...")
        send_email(jobs, previously_seen)
    print("Done.")


if __name__ == "__main__":
    main()
