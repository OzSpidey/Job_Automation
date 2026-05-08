"""
Amazon Jobs Scraper
===================
Hits Amazon's public JSON search endpoint directly:

    https://www.amazon.jobs/en/search.json?sort=recent&result_limit=100&offset=N

Pages through up to MAX_JOBS most-recent postings, filters for US-located
roles in Python, then matches each title against TARGET_ROLES. Sorted
most-recent-first before email send.

Why this exists (replacing the older Playwright scraper):
The UI scraper relied on Amazon's `country=USA` URL filter, which silently
excludes postings whose top-level `country` field is null in Amazon's
index — a data quality bug on their side. Job 10411675 ("Data Engineer,
AIR DABI", Bellevue WA) is one example: every location field on the
record says USA, but the top-level `country` is None, so the URL filter
dropped it. This script reads `normalized_location` and per-location
`normalizedCountryCode`/`countryIso3a`/`countryIso2a`, so any of those
USA signals is enough to keep the record.

Run: python amazon_scraper.py
"""

import json
import os
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
TARGET_EMAIL    = os.environ.get("EMAIL_TO", "")
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SMTP_SERVER     = "smtp.gmail.com"
SMTP_PORT       = 465

API_URL         = "https://www.amazon.jobs/en/search.json"
PAGE_SIZE       = 100        # API max per request
MAX_JOBS        = 2000       # how deep into "most recent" to scan (was effectively 400 in UI scanner)
REQUEST_DELAY_S = 0.3        # polite pause between page fetches
SEEN_JOBS_FILE  = os.path.join(os.path.dirname(__file__), "json", "amazon_api_seen_jobs.json")
USER_AGENT      = "Mozilla/5.0 (compatible; AmazonJobsScanner/1.0)"

TARGET_ROLES = [
    "data engineer",
    "business intelligence engineer",
    "business analyst",
    "bi engineer",
    "data analyst",
    "early grad",
    "software engineer",
    "ai engineer",
    "software developer",
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
    os.makedirs(os.path.dirname(SEEN_JOBS_FILE), exist_ok=True)
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(urls), f, indent=2)


def is_target_role(title: str) -> bool:
    t = title.lower()
    return any(role in t for role in TARGET_ROLES)


def parse_posted_date(s: str) -> datetime:
    """Parse Amazon's 'Month  D, YYYY' (note the double space for single-digit days)."""
    if not s:
        return datetime.min
    s = " ".join(s.split())  # collapse runs of whitespace
    try:
        return datetime.strptime(s, "%B %d, %Y")
    except ValueError:
        return datetime.min


def is_us_job(job: dict) -> bool:
    """True if any of the job's locations is in the US.

    We deliberately do NOT trust the top-level `country` field — it is
    null for some valid US postings (e.g. job 10411675). Instead we check
    normalized_location plus the per-location ISO codes inside the
    `locations` list.
    """
    norm_loc = (job.get("normalized_location") or "").upper()
    if "USA" in norm_loc or norm_loc.endswith(", US"):
        return True

    # `locations` is a list of JSON-encoded strings, each describing one location
    for loc_str in (job.get("locations") or []):
        try:
            loc = json.loads(loc_str)
        except (json.JSONDecodeError, TypeError):
            continue
        if loc.get("normalizedCountryCode") == "USA":
            return True
        if loc.get("countryIso3a") == "USA":
            return True
        if loc.get("countryIso2a") == "US":
            return True

    loc_text = (job.get("location") or "").upper()
    if loc_text.startswith("US,") or ", USA" in loc_text:
        return True

    return False


def format_locations(job: dict) -> str:
    """Render a multi-city posting as 'Bellevue, WA, USA / Seattle, WA, USA'."""
    cities = []
    seen = set()
    for loc_str in (job.get("locations") or []):
        try:
            loc = json.loads(loc_str)
        except (json.JSONDecodeError, TypeError):
            continue
        nl = loc.get("normalizedLocation")
        if nl and nl not in seen:
            seen.add(nl)
            cities.append(nl)
    if cities:
        return " / ".join(cities)
    # fallbacks
    return job.get("normalized_location") or job.get("location") or ""


def job_url(job: dict) -> str:
    """Build a canonical jobs URL from the API record."""
    path = job.get("job_path") or ""
    if path.startswith("http"):
        return path
    if path:
        return "https://www.amazon.jobs" + path
    # last-ditch fallback
    job_id = job.get("id_icims") or job.get("id") or ""
    return f"https://www.amazon.jobs/en/jobs/{job_id}"


# ──────────────────────────────────────────────────────────────────────────────
# API FETCH
# ──────────────────────────────────────────────────────────────────────────────

def fetch_recent_jobs(max_total: int = MAX_JOBS) -> list[dict]:
    """Page through search.json sort=recent, no country filter.

    We don't pass country=USA at the API level so we don't lose records
    with null top-level country (see is_us_job for why). US-only filtering
    happens in Python.
    """
    results = []
    offset = 0
    while offset < max_total:
        params = {
            "sort": "recent",
            "result_limit": str(PAGE_SIZE),
            "offset": str(offset),
        }
        url = API_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        jobs = data.get("jobs", [])
        if not jobs:
            print(f"  [api] offset={offset} returned 0 — stopping.")
            break
        results.extend(jobs)
        print(f"  [api] offset={offset:4d}  fetched={len(jobs)}  cumulative={len(results)}")
        offset += PAGE_SIZE
        if offset < max_total:
            time.sleep(REQUEST_DELAY_S)
    return results


# ──────────────────────────────────────────────────────────────────────────────
# EMAIL (same shape as amazon_jobs_scanner.py)
# ──────────────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count = sum(1 for j in jobs if j["url"] not in previously_seen)
    count     = len(jobs)
    subject   = f"Amazon Jobs Scraper (API) — {count} Matching Role(s) Found ({new_count} NEW)"

    if not jobs:
        plain = "No matching jobs found."
        html  = "<p>No matching jobs found.</p>"
    else:
        NEW_BADGE = '<span style="background:#e47911;color:#fff;font-size:11px;font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
        rows = []
        for j in jobs:
            is_new = j["url"] not in previously_seen
            row_bg = 'background:#fef9f0;' if is_new else ''
            badge  = NEW_BADGE if is_new else ''
            rows.append(
                f'<tr style="{row_bg}">'
                f'<td style="padding:8px;border:1px solid #ddd;">{badge}{j["title"]}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location", "")}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;"><a href="{j["url"]}">{j["url"]}</a></td>'
                f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{j.get("date", "")}</td>'
                f'</tr>'
            )
        html = f"""
        <html><body style="font-family:Arial,sans-serif;color:#333">
        <h2 style="color:#232F3E">Amazon Jobs (API) — Matching Roles</h2>
        <p>Found <strong>{count}</strong> role(s) matching:
           <em>Data Engineer &nbsp;|&nbsp; Business Intelligence Engineer &nbsp;|&nbsp;
           Business Analyst &nbsp;|&nbsp; Data Analyst &nbsp;|&nbsp; Software Engineer &nbsp;|&nbsp; Early Grad</em>
        </p>
        <table style="border-collapse:collapse;width:100%;max-width:1100px">
          <tr style="background:#232F3E;color:#FF9900">
            <th style="padding:10px;border:1px solid #555;text-align:left;width:30%">Role</th>
            <th style="padding:10px;border:1px solid #555;text-align:left;width:20%">Location</th>
            <th style="padding:10px;border:1px solid #555;text-align:left">Link</th>
            <th style="padding:10px;border:1px solid #555;text-align:left;width:13%">Date Posted</th>
          </tr>
          {chr(10).join(rows)}
        </table>
        <p style="font-size:12px;color:#888;margin-top:20px">
          Source: amazon.jobs/en/search.json · United States · Most Recent
        </p>
        </body></html>
        """
        plain = f"Found {count} matching role(s) ({new_count} NEW):\n\n" + "\n".join(
            f"- {'[NEW] ' if j['url'] not in previously_seen else ''}{j['title']} — {j.get('location', 'location unknown')} ({j.get('date', 'date unknown')})\n  {j['url']}"
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


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def scan() -> list[dict]:
    print(f"[1] Fetching up to {MAX_JOBS} most-recent jobs from search.json...")
    raw = fetch_recent_jobs(MAX_JOBS)
    print(f"  Total raw jobs: {len(raw)}")

    print("[2] Filtering for US locations...")
    us_jobs = [j for j in raw if is_us_job(j)]
    print(f"  US jobs: {len(us_jobs)}")

    print("[3] Filtering by target role title...")
    matched = []
    seen_urls = set()
    for j in us_jobs:
        title = j.get("title") or ""
        if not is_target_role(title):
            continue
        url = job_url(j)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        matched.append({
            "title":    title,
            "url":      url,
            "location": format_locations(j),
            "date":     j.get("posted_date") or "",
        })
        print(f"  MATCH: {title}  [{matched[-1]['location']}]")

    matched.sort(key=lambda j: parse_posted_date(j["date"]), reverse=True)
    return matched


def main():
    print("=" * 60)
    print("Amazon Jobs Scanner (API) — US · Most Recent")
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
    new_jobs = [j for j in jobs if j["url"] not in previously_seen]
    print(f"New roles (not seen before): {len(new_jobs)}")

    save_seen_urls(previously_seen | {j["url"] for j in jobs})

    if not new_jobs:
        print("No new roles — skipping email.")
    else:
        print(f"\nSending email ({len(new_jobs)} new role(s))...")
        send_email(jobs, previously_seen)
    print("Done.")


if __name__ == "__main__":
    main()
