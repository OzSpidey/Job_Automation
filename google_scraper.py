"""
Google Jobs Scraper
===================
Hits Google's public careers JSON endpoint directly:

    https://careers.google.com/api/v3/search/?page=N&sort_by=date

Pages through up to MAX_JOBS most-recent postings, filters for US-located
roles, then matches each title against TARGET_ROLES. Sorted
most-recent-first before email send.

Mirrors amazon_scraper.py — same flow (fetch → filter → match → email),
just different field names. Notes on Google's schema:
  - `publish_date` is ISO (YYYY-MM-DD), not Amazon's "Month  D, YYYY".
  - `locations` is a list of dicts (city/state/country_code/display),
    not Amazon's list of JSON-encoded strings.
  - Page numbers are 1-indexed; response carries `next_page` and `count`.
  - We trust `country_code == "US"` per location; Google's data is
    cleaner than Amazon's here, so no equivalent of the null-country
    workaround is needed.

Run: python google_scraper.py
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

API_URL         = "https://careers.google.com/api/v3/search/"
PAGE_SIZE       = 100        # API default is 20; 100 is the documented max
MAX_JOBS        = 2000
REQUEST_DELAY_S = 0.3
SEEN_JOBS_FILE  = os.path.join(os.path.dirname(__file__), "json", "google_api_seen_jobs.json")
USER_AGENT      = "Mozilla/5.0 (compatible; GoogleJobsScanner/1.0)"

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
    """Parse Google's ISO date (YYYY-MM-DD). Some records include a time suffix."""
    if not s:
        return datetime.min
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return datetime.min


def is_us_job(job: dict) -> bool:
    """True if any of the job's locations is in the US."""
    for loc in (job.get("locations") or []):
        if (loc.get("country_code") or "").upper() == "US":
            return True
        if (loc.get("country") or "").upper() in ("UNITED STATES", "USA"):
            return True
        display = (loc.get("display") or "").upper()
        if display.endswith(", USA") or ", USA," in display:
            return True
    return False


def format_locations(job: dict) -> str:
    """Render a multi-city posting as 'Mountain View, CA, USA / New York, NY, USA'."""
    cities = []
    seen = set()
    for loc in (job.get("locations") or []):
        disp = loc.get("display")
        if not disp:
            city  = loc.get("city") or ""
            state = loc.get("state") or ""
            ctry  = loc.get("country") or ""
            disp  = ", ".join(p for p in (city, state, ctry) if p)
        if disp and disp not in seen:
            seen.add(disp)
            cities.append(disp)
    return " / ".join(cities)


def job_url(job: dict) -> str:
    """Build a canonical careers URL from the API record."""
    apply = job.get("apply_url") or ""
    if apply.startswith("http"):
        return apply
    job_id = job.get("id") or ""
    # Strip any "jobs/" prefix Google sometimes embeds in the id field
    if job_id.startswith("jobs/"):
        job_id = job_id[len("jobs/"):]
    return f"https://www.google.com/about/careers/applications/jobs/results/{job_id}"


# ──────────────────────────────────────────────────────────────────────────────
# API FETCH
# ──────────────────────────────────────────────────────────────────────────────

def fetch_recent_jobs(max_total: int = MAX_JOBS) -> list[dict]:
    """Page through Google careers search, sorted by date.

    Google uses 1-indexed pages with `page_size` items each. The response
    includes `next_page` (or `null` when there's no more), and `count` for
    the total result set.
    """
    results = []
    page = 1
    while len(results) < max_total:
        params = {
            "page":      str(page),
            "page_size": str(PAGE_SIZE),
            "sort_by":   "date",
        }
        url = API_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        jobs = data.get("jobs", [])
        if not jobs:
            print(f"  [api] page={page} returned 0 — stopping.")
            break
        results.extend(jobs)
        print(f"  [api] page={page:3d}  fetched={len(jobs)}  cumulative={len(results)}  total={data.get('count','?')}")
        if not data.get("next_page"):
            break
        page += 1
        if len(results) < max_total:
            time.sleep(REQUEST_DELAY_S)
    return results[:max_total]


# ──────────────────────────────────────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count = sum(1 for j in jobs if j["url"] not in previously_seen)
    count     = len(jobs)
    subject   = f"Google Jobs Scraper (API) — {count} Matching Role(s) Found ({new_count} NEW)"

    if not jobs:
        plain = "No matching jobs found."
        html  = "<p>No matching jobs found.</p>"
    else:
        NEW_BADGE = '<span style="background:#4285F4;color:#fff;font-size:11px;font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
        rows = []
        for j in jobs:
            is_new = j["url"] not in previously_seen
            row_bg = 'background:#f0f6ff;' if is_new else ''
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
        <h2 style="color:#202124">Google Jobs (API) — Matching Roles</h2>
        <p>Found <strong>{count}</strong> role(s) matching:
           <em>Data Engineer &nbsp;|&nbsp; Business Intelligence Engineer &nbsp;|&nbsp;
           Business Analyst &nbsp;|&nbsp; Data Analyst &nbsp;|&nbsp; Software Engineer &nbsp;|&nbsp; Early Grad</em>
        </p>
        <table style="border-collapse:collapse;width:100%;max-width:1100px">
          <tr style="background:#202124;color:#FBBC04">
            <th style="padding:10px;border:1px solid #555;text-align:left;width:30%">Role</th>
            <th style="padding:10px;border:1px solid #555;text-align:left;width:20%">Location</th>
            <th style="padding:10px;border:1px solid #555;text-align:left">Link</th>
            <th style="padding:10px;border:1px solid #555;text-align:left;width:13%">Date Posted</th>
          </tr>
          {chr(10).join(rows)}
        </table>
        <p style="font-size:12px;color:#888;margin-top:20px">
          Source: careers.google.com/api/v3/search · United States · Most Recent
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
    print(f"[1] Fetching up to {MAX_JOBS} most-recent jobs from careers.google.com...")
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
            "date":     j.get("publish_date") or "",
        })
        print(f"  MATCH: {title}  [{matched[-1]['location']}]")

    matched.sort(key=lambda j: parse_posted_date(j["date"]), reverse=True)
    return matched


def main():
    print("=" * 60)
    print("Google Jobs Scanner (API) — US · Most Recent")
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
