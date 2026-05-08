"""
Microsoft Jobs Scraper
======================
Hits Microsoft careers' public JSON search endpoint directly:

    https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com&query=&start=N&num=20

The API returns jobs sorted newest-first by default and gives us
`postedTs` (Unix epoch) on every record — no need to parse "Posted X
days ago" relative date strings. We page through the most recent
postings, filter by title against TARGET_ROLES (and exclude senior /
principal levels), then sort the matches most-recent-first for email.

Why this exists (replacing the older Playwright scraper):
The UI scraper drove a Chromium browser through careers.microsoft.com,
clicked "Find jobs" and "Sort by Latest", then scraped DOM cards. Slow,
fragile when MS reshuffles their SPA, and limited to whatever the SPA
chose to render. This script hits the same endpoint the SPA uses
internally, so we get clean structured data in ~5–10s.

Run: python microsoft_scraper.py
"""

import json
import os
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
TARGET_EMAILS   = [e.strip() for e in os.environ.get("MS_TARGET_EMAILS", "").split(",") if e.strip()]
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SMTP_SERVER     = "smtp.gmail.com"
SMTP_PORT       = 465

API_URL         = "https://apply.careers.microsoft.com/api/pcsx/search"
DOMAIN          = "microsoft.com"
PAGE_SIZE       = 10         # API caps at 10 per call regardless of `num` — must match to avoid gaps
MAX_JOBS        = 500        # was effectively ~100 in the UI scanner; this is 5x deeper
REQUEST_DELAY_S = 0.3
NEW_MAX_DAYS    = 2          # mark as NEW only if postedTs < 2 days old (matches old scanner)
SEEN_JOBS_FILE  = os.path.join(os.path.dirname(__file__), "json", "microsoft_api_seen_jobs.json")
USER_AGENT      = "Mozilla/5.0 (compatible; MicrosoftJobsScanner/1.0)"

TARGET_ROLES = [
    "software engineer",
    "data engineer",
    "data analyst",
    "business intelligence analyst",
    "bi analyst",
]

EXCLUDE_LEVELS = ["senior", "principal"]

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def load_seen_urls() -> set[str]:
    if not os.path.exists(SEEN_JOBS_FILE):
        return set()
    with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_urls(urls: set[str]) -> None:
    os.makedirs(os.path.dirname(SEEN_JOBS_FILE), exist_ok=True)
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(urls), f, indent=2)


def is_target_role(title: str) -> bool:
    t = title.lower()
    if any(level in t for level in EXCLUDE_LEVELS):
        return False
    return any(role in t for role in TARGET_ROLES)


def is_recent(posted_ts: int, max_days: int = NEW_MAX_DAYS) -> bool:
    """True if postedTs is within max_days of now."""
    if not posted_ts:
        return False
    age_days = (time.time() - posted_ts) / 86400
    return age_days < max_days


def format_age(posted_ts: int) -> str:
    """Render postedTs as 'Posted N day(s) ago' for email display."""
    if not posted_ts:
        return ""
    age_seconds = time.time() - posted_ts
    age_days = age_seconds / 86400
    if age_days < 1:
        hrs = int(age_seconds / 3600)
        if hrs <= 1:
            return "Posted just now"
        return f"Posted {hrs} hours ago"
    days = int(age_days)
    return f"Posted {days} day{'s' if days != 1 else ''} ago"


def position_url(pos: dict) -> str:
    """Canonical URL for a Microsoft job posting."""
    pid = pos.get("id")
    if pid:
        return f"https://jobs.careers.microsoft.com/global/en/job/{pid}"
    rel = pos.get("positionUrl") or ""
    if rel.startswith("/"):
        return "https://apply.careers.microsoft.com" + rel
    return rel


def format_locations(pos: dict) -> str:
    """Render multi-location postings as 'Redmond, WA, US / Mountain View, CA, US'."""
    locs = pos.get("standardizedLocations") or pos.get("locations") or []
    seen = []
    for l in locs:
        if l and l not in seen:
            seen.append(l)
    return " / ".join(seen)


# ──────────────────────────────────────────────────────────────────────────────
# API FETCH
# ──────────────────────────────────────────────────────────────────────────────

def fetch_recent_jobs(max_total: int = MAX_JOBS) -> list[dict]:
    """Page through pcsx/search with empty query (returns all jobs by date desc)."""
    results = []
    start = 0
    while start < max_total:
        params = {
            "domain":   DOMAIN,
            "query":    "",
            "location": "",
            "start":    str(start),
            "num":      str(PAGE_SIZE),
        }
        url = API_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept":     "application/json",
            "Referer":    "https://jobs.careers.microsoft.com/",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        positions = (data.get("data") or {}).get("positions") or []
        if not positions:
            print(f"  [api] start={start} returned 0 — stopping.")
            break
        results.extend(positions)
        print(f"  [api] start={start:4d}  fetched={len(positions)}  cumulative={len(results)}")
        start += PAGE_SIZE
        if start < max_total:
            time.sleep(REQUEST_DELAY_S)
    return results


# ──────────────────────────────────────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    def _is_new(j: dict) -> bool:
        return j["url"] not in previously_seen and is_recent(j["posted_ts"])

    new_count = sum(1 for j in jobs if _is_new(j))
    count     = len(jobs)
    subject   = f"Microsoft Jobs Alert — {count} Role(s) Found ({new_count} NEW)"

    if not jobs:
        plain = "No matching jobs found."
        html  = "<p>No matching jobs found.</p>"
    else:
        NEW_BADGE = '<span style="background:#1a7f37;color:#fff;font-size:11px;font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
        rows_list = []
        for j in jobs:
            is_new = _is_new(j)
            row_bg = 'background:#e6f4ea;' if is_new else ''
            badge  = NEW_BADGE if is_new else ''
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
           (excluding senior &amp; principal levels)
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
          Source: apply.careers.microsoft.com/api/pcsx/search · Sorted by Most Recent
        </p>
        </body></html>
        """
        plain = f"Found {count} matching role(s) ({new_count} NEW):\n\n" + "\n".join(
            f"- {'[NEW] ' if _is_new(j) else ''}{j['title']} | {j.get('location', 'location unknown')} ({j.get('date', 'date unknown')})\n  {j['url']}"
            for j in jobs
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


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def scan() -> list[dict]:
    print(f"[1] Fetching up to {MAX_JOBS} most-recent jobs from pcsx/search...")
    raw = fetch_recent_jobs(MAX_JOBS)
    print(f"  Total raw jobs: {len(raw)}")

    print("[2] Filtering by target role title (excluding senior/principal)...")
    matched = []
    seen_urls = set()
    for pos in raw:
        title = pos.get("name") or ""
        if not is_target_role(title):
            continue
        url = position_url(pos)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        posted_ts = pos.get("postedTs") or 0
        matched.append({
            "title":     title,
            "url":       url,
            "location":  format_locations(pos),
            "date":      format_age(posted_ts),
            "posted_ts": posted_ts,
        })
        print(f"  MATCH: {title}  [{matched[-1]['location']}]")

    matched.sort(key=lambda j: j["posted_ts"], reverse=True)
    return matched


def main():
    print("=" * 60)
    print("Microsoft Jobs Scraper — Most Recent")
    print("=" * 60)

    t0 = time.time()
    jobs = scan()
    elapsed = time.time() - t0

    print("\n" + "=" * 60)
    print(f"Total matches: {len(jobs)} | elapsed: {elapsed:.1f}s")
    for j in jobs:
        print(f"  • {j['title']}")
        print(f"    {j['url']}")
    print("=" * 60)

    previously_seen = load_seen_urls()
    new_jobs = [j for j in jobs if j["url"] not in previously_seen and is_recent(j["posted_ts"])]
    print(f"New roles (not seen before, posted < {NEW_MAX_DAYS} days ago): {len(new_jobs)}")

    save_seen_urls(previously_seen | {j["url"] for j in jobs})

    if not new_jobs:
        print("No new roles — skipping email.")
    elif not TARGET_EMAILS:
        print("[warn] MS_TARGET_EMAILS not configured — skipping email.")
    else:
        print(f"\nSending email ({len(new_jobs)} new role(s))...")
        send_email(jobs, previously_seen)
    print("Done.")


if __name__ == "__main__":
    main()
