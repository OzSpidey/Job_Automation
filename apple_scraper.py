"""
Apple Jobs Scraper
==================
Hits Apple's internal JSON search endpoint directly:

    POST https://jobs.apple.com/api/v1/search

Requires a session cookie from the search page before the API responds.
Passes sort=newest + locations=postLocation-USA so we get the most-recent
US postings first. Pages through up to MAX_PAGES * 20 jobs, filters by
title against TARGET_ROLES, and emails new matches sorted most-recent-first.

Run: python apple_scraper.py
"""

import json
import os
import smtplib
import sys
import time
import http.cookiejar
import urllib.request
from datetime import datetime, timezone
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

API_URL         = "https://jobs.apple.com/api/v1/search"
SEARCH_PAGE_URL = "https://jobs.apple.com/en-us/search"
PAGE_SIZE       = 20        # Apple returns exactly 20 per page
MAX_PAGES       = 50        # 50 * 20 = 1 000 most-recent jobs scanned; 3 days fits well within this
MAX_AGE_DAYS    = 3         # ignore jobs posted more than 3 days ago
REQUEST_DELAY_S = 0.4
SEEN_JOBS_FILE  = os.path.join(os.path.dirname(__file__), "json", "apple_api_seen_jobs.json")
USER_AGENT      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

TARGET_ROLES = [
    "data engineer",
    "business intelligence engineer",
    "business analyst",
    "bi engineer",
    "data analyst",
    "software engineer",
    "ai engineer",
    "software developer",
    "machine learning engineer",
]

EXCLUDE_LEVELS = ["senior", "principal", "lead", "staff", "manager"]

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


def parse_gmt_date(s: str) -> datetime:
    """Parse Apple's postDateInGMT (nanosecond ISO string like '2026-05-14T18:01:47.961313540Z')."""
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    # Truncate sub-microsecond precision Python can't handle (>6 decimal digits)
    # e.g. '2026-05-14T18:01:47.961313540Z' -> '2026-05-14T18:01:47.961313+00:00'
    s = s.rstrip("Z")
    if "." in s:
        base, frac = s.split(".", 1)
        s = base + "." + frac[:6]
    try:
        return datetime.fromisoformat(s + "+00:00")
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def is_within_max_age(gmt_str: str) -> bool:
    dt = parse_gmt_date(gmt_str)
    if dt == datetime.min.replace(tzinfo=timezone.utc):
        return True  # keep if date unknown
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    return age_days <= MAX_AGE_DAYS


def format_locations(job: dict) -> str:
    locs = job.get("locations") or []
    parts = []
    seen: set[str] = set()
    for loc in locs:
        city    = loc.get("city") or ""
        state   = loc.get("stateProvince") or ""
        country = loc.get("countryName") or ""
        label   = ", ".join(filter(None, [city, state, country]))
        if label and label not in seen:
            seen.add(label)
            parts.append(label)
    return " / ".join(parts) if parts else ""


def job_url(job: dict) -> str:
    pos_id   = job.get("positionId") or ""
    slug     = job.get("transformedPostingTitle") or ""
    team_code = (job.get("team") or {}).get("teamCode") or ""
    url = f"https://jobs.apple.com/en-us/details/{pos_id}/{slug}"
    if team_code:
        url += f"?team={team_code}"
    return url


# ──────────────────────────────────────────────────────────────────────────────
# SESSION + API FETCH
# ──────────────────────────────────────────────────────────────────────────────

def make_opener() -> urllib.request.OpenerDirector:
    """Return an opener with a cookie jar (session required by Apple's API)."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    # Seed the session by visiting the search page
    req = urllib.request.Request(SEARCH_PAGE_URL,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with opener.open(req, timeout=20):
        pass
    return opener


def fetch_page(opener: urllib.request.OpenerDirector, page: int) -> dict:
    payload = json.dumps({
        "query":   "",
        "filters": {"locations": ["postLocation-USA"]},
        "page":    page,
        "locale":  "en-us",
        "sort":    "newest",
        "format":  {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "User-Agent":   USER_AGENT,
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "Referer":      SEARCH_PAGE_URL,
            "Origin":       "https://jobs.apple.com",
        },
        method="POST",
    )
    with opener.open(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_all_jobs(max_pages: int = MAX_PAGES) -> list[dict]:
    opener = make_opener()
    results: list[dict] = []
    total_known: int | None = None

    for page in range(1, max_pages + 1):
        data = fetch_page(opener, page)
        res  = data.get("res", {})

        if total_known is None:
            total_known = res.get("totalRecords")

        jobs = res.get("searchResults") or []
        if not jobs:
            print(f"  [api] page={page} returned 0 — stopping.")
            break

        results.extend(jobs)
        print(
            f"  [api] page={page:3d}  fetched={len(jobs):3d}"
            f"  cumulative={len(results)}"
            + (f"  total={total_known}" if total_known else "")
        )

        if len(jobs) < PAGE_SIZE:
            print("  [api] partial page — reached end.")
            break
        if total_known and len(results) >= total_known:
            print(f"  [api] fetched all {total_known} records.")
            break
        if page < max_pages:
            time.sleep(REQUEST_DELAY_S)

    return results


# ──────────────────────────────────────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count = sum(1 for j in jobs if j["url"] not in previously_seen)
    count     = len(jobs)
    subject   = f"Apple Jobs Alert — {count} Role(s) Found ({new_count} NEW)"

    if not jobs:
        plain = "No matching jobs found."
        html  = "<p>No matching jobs found.</p>"
    else:
        NEW_BADGE = (
            '<span style="background:#0071e3;color:#fff;font-size:11px;'
            'font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
        )
        rows = []
        for j in jobs:
            is_new = j["url"] not in previously_seen
            row_bg = "background:#f0f7ff;" if is_new else ""
            badge  = NEW_BADGE if is_new else ""
            rows.append(
                f'<tr style="{row_bg}">'
                f'<td style="padding:8px;border:1px solid #d2d2d7;">{badge}{j["title"]}</td>'
                f'<td style="padding:8px;border:1px solid #d2d2d7;">{j.get("location", "")}</td>'
                f'<td style="padding:8px;border:1px solid #d2d2d7;">'
                f'<a href="{j["url"]}" style="color:#0071e3">{j["url"]}</a></td>'
                f'<td style="padding:8px;border:1px solid #d2d2d7;white-space:nowrap;">{j.get("date", "")}</td>'
                f"</tr>"
            )
        role_labels = " &nbsp;|&nbsp; ".join(r.title() for r in TARGET_ROLES)
        html = f"""
        <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;color:#1d1d1f">
        <h2 style="color:#1d1d1f"> Apple Jobs — Matching Roles</h2>
        <p>Found <strong>{count}</strong> role(s) matching: <em>{role_labels}</em></p>
        <table style="border-collapse:collapse;width:100%;max-width:1100px">
          <tr style="background:#1d1d1f;color:#f5f5f7">
            <th style="padding:10px;border:1px solid #424245;text-align:left;width:30%">Role</th>
            <th style="padding:10px;border:1px solid #424245;text-align:left;width:20%">Location</th>
            <th style="padding:10px;border:1px solid #424245;text-align:left">Link</th>
            <th style="padding:10px;border:1px solid #424245;text-align:left;width:12%">Date Posted</th>
          </tr>
          {chr(10).join(rows)}
        </table>
        <p style="font-size:12px;color:#86868b;margin-top:20px">
          Source: jobs.apple.com/api/v1/search · United States · Newest First
        </p>
        </body></html>
        """
        plain = f"Found {count} matching role(s) ({new_count} NEW):\n\n" + "\n".join(
            f"- {'[NEW] ' if j['url'] not in previously_seen else ''}"
            f"{j['title']} — {j.get('location', 'location unknown')}\n"
            f"  {j.get('date', '')}\n"
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


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def scan() -> list[dict]:
    print(f"[1] Fetching US jobs from Apple API (up to {MAX_PAGES} pages × {PAGE_SIZE}, newest first, last {MAX_AGE_DAYS} days)...")
    raw = fetch_all_jobs(MAX_PAGES)
    print(f"  Total raw jobs fetched: {len(raw)}")

    print(f"[2] Filtering by date (<= {MAX_AGE_DAYS} days) and target role title...")
    matched: list[dict] = []
    seen_urls: set[str] = set()
    too_old = 0
    for j in raw:
        gmt = j.get("postDateInGMT") or ""
        if not is_within_max_age(gmt):
            too_old += 1
            # Results are newest-first; once we see 5 consecutive old jobs stop scanning
            if too_old >= 5:
                print(f"  [filter] 5+ jobs older than {MAX_AGE_DAYS} days — stopping early.")
                break
            continue
        too_old = 0  # reset streak on any recent job
        title = j.get("postingTitle") or ""
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
            "date":     j.get("postingDate") or "",   # already formatted: "May 14, 2026"
            "gmt":      gmt,
        })
        print(f"  MATCH: {title}  [{matched[-1]['location']}]")

    matched.sort(key=lambda j: parse_gmt_date(j["gmt"]), reverse=True)
    return matched


def main():
    print("=" * 60)
    print("Apple Jobs Scraper — US · Newest First")
    print("=" * 60)

    t0   = time.time()
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

    # New jobs first, then date desc within each group
    jobs.sort(key=lambda j: parse_gmt_date(j["gmt"]), reverse=True)
    jobs.sort(key=lambda j: j["url"] in previously_seen)

    save_seen_urls(previously_seen | {j["url"] for j in jobs})

    if not new_jobs:
        print("No new roles — skipping email.")
    else:
        print(f"\nSending email ({len(new_jobs)} new role(s))...")
        send_email(jobs, previously_seen)
    print("Done.")


if __name__ == "__main__":
    main()
