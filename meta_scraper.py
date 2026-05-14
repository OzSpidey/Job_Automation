"""
Meta Jobs Scraper
=================
Uses Playwright to intercept Meta's internal GraphQL response from:

    https://www.metacareers.com/jobs/

Meta's careers site is a Relay/GraphQL SPA. Every job-search page load
fires a POST to https://www.metacareers.com/graphql that carries the full
job listing in a clean JSON payload (data.job_search_with_featured_jobs).
We intercept that network response instead of scraping DOM — so we get the
same structured data the SPA uses, without parsing HTML.

Posting dates are fetched from each matched job's detail page
(profile/job_details/{id}) which is server-rendered and embeds a
JSON-LD JobPosting block with a datePosted field. All detail pages are
fetched concurrently with httpx.

Why not a direct POST to /graphql?
  Meta's GraphQL endpoint requires fb_dtsg (a per-session CSRF token) and
  doc_id (a persisted-query numeric ID baked into the JS bundle). Both
  rotate on deploys, making a raw requests approach fragile. Playwright
  lets the browser handle the token handshake automatically.

Run: python meta_scraper.py
"""

import asyncio
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
TARGET_EMAIL    = os.environ.get("EMAIL_TO", "")
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SMTP_SERVER     = "smtp.gmail.com"
SMTP_PORT       = 465

JOBS_URL        = "https://www.metacareers.com/jobs/"
GRAPHQL_HOST    = "metacareers.com/graphql"
DETAIL_URL      = "https://www.metacareers.com/profile/job_details/{id}"
PAGE_TIMEOUT_MS = 60_000
SEEN_JOBS_FILE  = os.path.join(os.path.dirname(__file__), "json", "meta_api_seen_jobs.json")
USER_AGENT      = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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
    "new grad",
    "university graduate",
    "early career",
]

EXCLUDE_LEVELS = ["senior", "principal", "lead", "staff", "manager", "director"]

US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}

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


def _is_us_location(loc: str) -> bool:
    if not loc:
        return False
    if "Remote, US" in loc or "United States" in loc:
        return True
    # "City, ST" — last segment is a 2-letter US state code
    parts = loc.rsplit(",", 1)
    if len(parts) == 2 and parts[1].strip() in US_STATES:
        return True
    return False


def has_us_location(job: dict) -> bool:
    return any(
        _is_us_location(loc if isinstance(loc, str) else (loc.get("name") or ""))
        for loc in (job.get("locations") or [])
    )


def format_locations(job: dict) -> str:
    locs = job.get("locations") or []
    seen: set[str] = set()
    parts = []
    for loc in locs:
        s = loc if isinstance(loc, str) else (loc.get("name") or "")
        if s and s not in seen:
            seen.add(s)
            parts.append(s)
    return " / ".join(parts)


def format_teams(job: dict) -> str:
    teams = job.get("teams") or []
    parts = []
    for t in teams:
        s = t if isinstance(t, str) else (t.get("name") or "")
        if s:
            parts.append(s)
    return ", ".join(parts)


def job_url(job: dict) -> str:
    return f"https://www.metacareers.com/jobs/{job['id']}/"


def parse_iso(s: str) -> datetime:
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def format_date(iso: str) -> str:
    dt = parse_iso(iso)
    if dt == datetime.min.replace(tzinfo=timezone.utc):
        return ""
    return dt.strftime("%b %d, %Y")


# ──────────────────────────────────────────────────────────────────────────────
# PLAYWRIGHT FETCH (job list)
# ──────────────────────────────────────────────────────────────────────────────

async def _fetch_jobs_playwright() -> list[dict]:
    all_jobs: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page    = await context.new_page()

        async def on_response(resp):
            if GRAPHQL_HOST not in resp.url:
                return
            try:
                body = await resp.json()
            except Exception:
                return
            js   = (body.get("data") or {}).get("job_search_with_featured_jobs") or {}
            jobs = js.get("all_jobs") or []
            if jobs:
                all_jobs.extend(jobs)
                print(f"  [graphql] captured {len(jobs)} jobs (total so far: {len(all_jobs)})")

        page.on("response", on_response)
        print(f"  [browser] navigating to {JOBS_URL} ...")
        await page.goto(JOBS_URL, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        await browser.close()

    return all_jobs


def fetch_all_jobs() -> list[dict]:
    return asyncio.run(_fetch_jobs_playwright())


# ──────────────────────────────────────────────────────────────────────────────
# HTTPX FETCH (posting dates — concurrent)
# ──────────────────────────────────────────────────────────────────────────────

async def _fetch_date(client: httpx.AsyncClient, job_id: str) -> str:
    url = DETAIL_URL.format(id=job_id)
    try:
        r = await client.get(url, timeout=15)
        m = re.search(r'"datePosted"\s*:\s*"([^"]+)"', r.text)
        return m.group(1) if m else ""
    except Exception:
        return ""


async def _fetch_all_dates(job_ids: list[str]) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        results = await asyncio.gather(*[_fetch_date(client, jid) for jid in job_ids])
    return dict(zip(job_ids, results))


def fetch_posting_dates(job_ids: list[str]) -> dict[str, str]:
    return asyncio.run(_fetch_all_dates(job_ids))


# ──────────────────────────────────────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count = sum(1 for j in jobs if j["url"] not in previously_seen)
    count     = len(jobs)
    subject   = f"Meta Jobs Alert — {count} Role(s) Found ({new_count} NEW)"

    if not jobs:
        plain = "No matching jobs found."
        html  = "<p>No matching jobs found.</p>"
    else:
        NEW_BADGE = (
            '<span style="background:#0866ff;color:#fff;font-size:11px;'
            'font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
        )
        rows = []
        for j in jobs:
            is_new = j["url"] not in previously_seen
            row_bg = "background:#eef2ff;" if is_new else ""
            badge  = NEW_BADGE if is_new else ""
            rows.append(
                f'<tr style="{row_bg}">'
                f'<td style="padding:8px;border:1px solid #ddd;">{badge}{j["title"]}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">{j.get("team", "")}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location", "")}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;">'
                f'<a href="{j["url"]}" style="color:#0866ff">{j["url"]}</a></td>'
                f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{j.get("date", "")}</td>'
                f"</tr>"
            )
        role_labels = " &nbsp;|&nbsp; ".join(r.title() for r in TARGET_ROLES)
        html = f"""
        <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;color:#1c1e21">
        <h2 style="color:#0866ff">Meta Jobs — Matching Roles (US Only)</h2>
        <p>Found <strong>{count}</strong> US role(s) matching: <em>{role_labels}</em></p>
        <table style="border-collapse:collapse;width:100%;max-width:1200px">
          <tr style="background:#0866ff;color:#fff">
            <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:28%">Role</th>
            <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:15%">Team</th>
            <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:18%">Location</th>
            <th style="padding:10px;border:1px solid #1877f2;text-align:left">Link</th>
            <th style="padding:10px;border:1px solid #1877f2;text-align:left;width:12%">Date Posted</th>
          </tr>
          {chr(10).join(rows)}
        </table>
        <p style="font-size:12px;color:#65676b;margin-top:20px">
          Source: metacareers.com · United States · Sorted by Most Recent
        </p>
        </body></html>
        """
        plain = f"Found {count} US role(s) ({new_count} NEW):\n\n" + "\n".join(
            f"- {'[NEW] ' if j['url'] not in previously_seen else ''}"
            f"{j['title']} | {j.get('team', '')} | {j.get('location', '')}\n"
            f"  Posted: {j.get('date', 'unknown')}\n"
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
    print("[1] Launching browser and intercepting Meta GraphQL ...")
    raw = fetch_all_jobs()
    print(f"  Total raw jobs captured: {len(raw)}")

    if not raw:
        print("  [warn] No jobs captured — metacareers.com may have changed its response structure.")
        return []

    print("[2] Filtering: US only, target roles, excluding senior/principal/lead/staff/manager/director ...")
    pre_matched: list[dict] = []
    seen_urls: set[str]     = set()
    for j in raw:
        title = j.get("title") or ""
        if not is_target_role(title):
            continue
        if not has_us_location(j):
            continue
        url = job_url(j)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        pre_matched.append({
            "id":       j["id"],
            "title":    title,
            "url":      url,
            "location": format_locations(j),
            "team":     format_teams(j),
        })
        print(f"  MATCH: {title}  [{pre_matched[-1]['location']}]")

    if not pre_matched:
        return []

    print(f"[3] Fetching posting dates for {len(pre_matched)} matched job(s) ...")
    dates = fetch_posting_dates([j["id"] for j in pre_matched])
    print(f"  Dates fetched.")

    matched: list[dict] = []
    for j in pre_matched:
        iso  = dates.get(j["id"], "")
        matched.append({**j, "date": format_date(iso), "raw_date": iso})
        print(f"  {j['title']}  →  {format_date(iso) or 'date unknown'}")

    matched.sort(key=lambda j: parse_iso(j["raw_date"]), reverse=True)
    return matched


def main():
    print("=" * 60)
    print("Meta Jobs Scraper — Playwright GraphQL Intercept")
    print("=" * 60)

    t0      = time.time()
    jobs    = scan()
    elapsed = time.time() - t0

    print("\n" + "=" * 60)
    print(f"Total matches: {len(jobs)} | elapsed: {elapsed:.1f}s")
    for j in jobs:
        print(f"  • {j['title']}  [{j.get('date', '')}]")
        print(f"    {j['url']}")
    print("=" * 60)

    previously_seen = load_seen_urls()
    new_jobs = [j for j in jobs if j["url"] not in previously_seen]
    print(f"New roles (not seen before): {len(new_jobs)}")

    save_seen_urls(previously_seen | {j["url"] for j in jobs})

    if not new_jobs:
        print("No new roles — skipping email.")
    elif not TARGET_EMAIL:
        print("[warn] EMAIL_TO not configured — skipping email.")
    else:
        print(f"\nSending email ({len(new_jobs)} new role(s))...")
        send_email(jobs, previously_seen)
    print("Done.")


if __name__ == "__main__":
    main()
