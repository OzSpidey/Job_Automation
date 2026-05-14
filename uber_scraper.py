"""
uber_scraper.py
---------------
Uses Playwright to intercept Uber's internal jobs API from:

    https://jobs.uber.com/en/

jobs.uber.com is a HappyDance-powered SPA. On page load it fires JSON
API requests to fetch listings. We intercept every JSON response and
pick out whichever one carries a jobs array — so we get structured
data without parsing HTML and without needing to know the exact endpoint
in advance (HappyDance can change routes without breaking this).

Run: python uber_scraper.py
"""

import asyncio
import csv
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright

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

BASE_DIR        = Path(__file__).parent
SEEN_FILE       = BASE_DIR / "json" / "uber_seen_jobs.json"
CSV_FILE        = BASE_DIR / "csv"  / "uber_jobs.csv"

JOBS_URL        = "https://jobs.uber.com/en/"
PAGE_TIMEOUT_MS = 60_000

TARGET_ROLES = [
    "data engineer",
    "analytics engineer",
    "data analyst",
    "business analyst",
    "business intelligence",
    "data scientist",
    "software engineer",
    "software developer",
    "machine learning engineer",
    "ml engineer",
    "ai engineer",
    "backend engineer",
    "full stack engineer",
    "fullstack engineer",
]

EXCLUDE_LEVELS = ["senior", "sr.", "lead", "staff", "principal", "manager",
                  "director", "vp ", "vice president", "head of"]

_NON_US_RE = re.compile(
    r'\b(india|canada|united\s+kingdom|\buk\b|australia|germany|france|'
    r'netherlands|singapore|japan|china|brazil|mexico|ireland|poland|'
    r'london|amsterdam|berlin|toronto|sydney|bangalore|bengaluru|delhi|'
    r'hyderabad|mumbai|tel\s+aviv|warsaw|dublin|stockholm)\b',
    re.I,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(ids: set[str]) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(ids)), encoding="utf-8")


def is_target(title: str) -> bool:
    t = title.lower()
    if any(lvl in t for lvl in EXCLUDE_LEVELS):
        return False
    return any(role in t for role in TARGET_ROLES)


def is_us(location: str) -> bool:
    if not location:
        return True
    if _NON_US_RE.search(location):
        return False
    return True


def _extract_jobs_from_payload(body: dict | list) -> list[dict]:
    """
    Walk the response payload looking for a list that contains job objects.
    Returns a flat list of raw job dicts.
    """
    candidates: list[dict] = []

    def _walk(node):
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict) and (
                    "title" in item or "jobTitle" in item or "name" in item
                ):
                    candidates.append(item)
                else:
                    _walk(item)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)

    _walk(body)
    return candidates


def _normalise(raw: dict) -> dict | None:
    """Pull consistent fields out of whatever shape the Uber API returns."""
    title = (
        raw.get("title")
        or raw.get("jobTitle")
        or raw.get("name")
        or ""
    ).strip()
    if not title:
        return None

    job_id = str(
        raw.get("id")
        or raw.get("jobId")
        or raw.get("requisitionId")
        or ""
    )

    # URL: prefer absolute_url, then build from id
    url = (
        raw.get("absolute_url")
        or raw.get("url")
        or raw.get("applyUrl")
        or raw.get("jobUrl")
        or ""
    )
    if not url and job_id:
        url = f"https://jobs.uber.com/en/job/{job_id}"

    # Location: string or nested object
    loc_raw = raw.get("location") or raw.get("locationName") or raw.get("city") or ""
    if isinstance(loc_raw, dict):
        loc = loc_raw.get("name") or loc_raw.get("city") or ""
    elif isinstance(loc_raw, list):
        loc = ", ".join(
            (item.get("name") or item.get("city") or item) if isinstance(item, dict) else str(item)
            for item in loc_raw
        )
    else:
        loc = str(loc_raw)

    # Posted date
    posted_raw = (
        raw.get("postedDate")
        or raw.get("firstPublished")
        or raw.get("createdAt")
        or raw.get("publishedAt")
        or raw.get("updatedAt")
        or ""
    )
    try:
        posted = datetime.fromisoformat(
            str(posted_raw).replace("Z", "+00:00")
        ).strftime("%Y-%m-%d") if posted_raw else ""
    except Exception:
        posted = str(posted_raw)[:10] if posted_raw else ""

    return {
        "job_id":   job_id,
        "title":    title,
        "company":  "Uber",
        "location": loc.strip(),
        "url":      url,
        "posted":   posted,
    }


# ── Playwright fetch ──────────────────────────────────────────────────────────

async def _fetch_jobs() -> list[dict]:
    all_raw: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        async def on_response(resp):
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await resp.json()
            except Exception:
                return
            found = _extract_jobs_from_payload(body)
            if found:
                all_raw.extend(found)
                print(f"  [intercept] {resp.url[:80]}  → {len(found)} job objects")

        page.on("response", on_response)

        print(f"  [browser] loading {JOBS_URL} ...")
        try:
            await page.goto(JOBS_URL, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        except Exception as exc:
            print(f"  [warn] page load: {exc}")

        # Extra wait to let any lazy-loaded requests finish
        await asyncio.sleep(3)
        await browser.close()

    return all_raw


def fetch_all_jobs() -> list[dict]:
    return asyncio.run(_fetch_jobs())


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count = sum(1 for j in jobs if j["job_id"] not in previously_seen)
    count     = len(jobs)
    subject   = f"Uber Jobs Alert — {count} Role(s) Found ({new_count} NEW)"

    NEW_BADGE = (
        '<span style="background:#000;color:#fff;font-size:11px;'
        'font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
    )
    rows = []
    for j in jobs:
        is_new = j["job_id"] not in previously_seen
        row_bg = "background:#f5f5f5;" if is_new else ""
        badge  = NEW_BADGE if is_new else ""
        rows.append(
            f'<tr style="{row_bg}">'
            f'<td style="padding:8px;border:1px solid #ddd;">{badge}{j["title"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location","")}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">'
            f'<a href="{j["url"]}" style="color:#000">{j["url"]}</a></td>'
            f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{j.get("posted","")}</td>'
            f"</tr>"
        )

    role_labels = " | ".join(r.title() for r in TARGET_ROLES)
    html = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,Arial,sans-serif;color:#000">
    <h2>Uber Jobs — {count} Matching Role(s)</h2>
    <p>Roles: <em>{role_labels}</em></p>
    <table style="border-collapse:collapse;width:100%;max-width:1100px">
      <tr style="background:#000;color:#fff">
        <th style="padding:10px;text-align:left;width:30%">Role</th>
        <th style="padding:10px;text-align:left;width:20%">Location</th>
        <th style="padding:10px;text-align:left">Link</th>
        <th style="padding:10px;text-align:left;width:12%">Posted</th>
      </tr>
      {"".join(rows)}
    </table>
    <p style="font-size:12px;color:#666;margin-top:20px">Source: jobs.uber.com</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = TARGET_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, TARGET_EMAIL, msg.as_string())
    print(f"[email] Sent to {TARGET_EMAIL} — {count} job(s).")


# ── Main ──────────────────────────────────────────────────────────────────────

def scan() -> list[dict]:
    print("[1] Launching Playwright and intercepting Uber job responses...")
    raw = fetch_all_jobs()
    print(f"  Raw job objects captured: {len(raw)}")

    if not raw:
        print("  [warn] No job data captured — jobs.uber.com may have changed its API structure.")
        return []

    print(f"  [debug] first raw object keys: {list(raw[0].keys())}")

    print("[2] Normalising and filtering...")
    matched: list[dict] = []
    seen_ids: set[str] = set()
    for r in raw:
        job = _normalise(r)
        if not job:
            continue
        if job["job_id"] in seen_ids:
            continue
        seen_ids.add(job["job_id"])
        if not is_target(job["title"]):
            continue
        if not is_us(job["location"]):
            continue
        matched.append(job)
        print(f"  MATCH: {job['title']}  [{job['location']}]")

    matched.sort(key=lambda j: j.get("posted") or "", reverse=True)
    return matched


def main():
    print("=" * 60)
    print("Uber Jobs Scraper — Playwright intercept")
    print("=" * 60)

    t0      = time.time()
    jobs    = scan()
    elapsed = time.time() - t0

    print("\n" + "=" * 60)
    print(f"Total matches: {len(jobs)} | elapsed: {elapsed:.1f}s")
    for j in jobs:
        print(f"  • {j['title']}  [{j['location']}]")
        print(f"    {j['url']}")
    print("=" * 60)

    previously_seen = load_seen()
    new_jobs = [j for j in jobs if j["job_id"] not in previously_seen]
    print(f"New roles (not seen before): {len(new_jobs)}")

    # Append new jobs to CSV
    if new_jobs:
        CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_header = not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0
        fieldnames = ["job_id", "title", "company", "location", "posted", "url", "found_at"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            for j in new_jobs:
                writer.writerow({**j, "found_at": now})
        print(f"[+] Appended {len(new_jobs)} row(s) to {CSV_FILE}")

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
