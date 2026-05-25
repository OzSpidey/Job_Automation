"""
Netflix Jobs Scraper
====================
Hits Netflix's public JSON search endpoint:

    GET https://jobs.netflix.com/api/search?page={n}

No auth required. Pages through results, filters by title against TARGET_ROLES,
and emails new matches.

Run: python netflix_scraper.py
"""

import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

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

API_URL         = "https://jobs.netflix.com/api/search"
MAX_PAGES       = 50
REQUEST_DELAY_S = 0.5
SEEN_JOBS_FILE  = os.path.join(os.path.dirname(__file__), "json", "netflix_seen_jobs.json")

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

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
    ext_id = posting.get("external_id") or posting.get("id") or ""
    return f"https://jobs.netflix.com/jobs/{ext_id}"


def parse_date(s: str) -> str:
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return s[:10] if len(s) >= 10 else s


# ── Fetch ──────────────────────────────────────────────────────────────────────
def fetch_page(session: requests.Session, page: int) -> list[dict]:
    try:
        resp = session.get(API_URL, params={"page": page}, headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            print(f"  [!] HTTP {resp.status_code} at page {page}")
            return []
        data = resp.json()
        return data.get("records", {}).get("postings", [])
    except Exception as e:
        print(f"  [!] Request failed at page {page}: {e}")
        return []


def fetch_all_jobs() -> list[dict]:
    session = requests.Session()
    results = []
    for page in range(1, MAX_PAGES + 1):
        postings = fetch_page(session, page)
        if not postings:
            print(f"  [api] page={page} returned 0 — stopping.")
            break
        results.extend(postings)
        print(f"  [api] page={page:2d}  fetched={len(postings):3d}  cumulative={len(results)}")
        if page < MAX_PAGES:
            time.sleep(REQUEST_DELAY_S)
    return results


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
      Source: jobs.netflix.com/api/search
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

    print("[1] Fetching jobs from Netflix API...")
    raw = fetch_all_jobs()
    print(f"  Total raw jobs fetched: {len(raw)}")

    print("[2] Filtering by target roles...")
    previously_seen = load_seen()
    matched = []
    seen_urls: set = set()

    for posting in raw:
        title = posting.get("text", "")
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
            "date":     parse_date(posting.get("created_at", "")),
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
