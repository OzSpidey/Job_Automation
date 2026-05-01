"""
Lever Jobs Scanner
------------------
Scans verified Lever companies for:
  Data Engineer, Data Analyst  (exact title, no senior/lead/manager variants)

Filter logic:
  - US location (or remote / blank)
  - Not seen in a previous run  ← primary dedup (no date filter; Lever jobs stay
                                   live for months, so "new to us" is what matters)

First run:  emails ALL current matching roles across all companies.
Subsequent: emails only newly added postings since the last run.

Run: python lever_jobs_scanner.py
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
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────────
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENTS      = [e.strip() for e in os.environ.get("EMAIL_TO", "").split(",") if e.strip()]

SEEN_FILE    = Path(__file__).parent / "lever_seen_jobs.json"
CONCURRENCY  = 15
MAX_AGE_DAYS = 3

ALLOWED_TITLES = re.compile(r"^(data\s+engineer|data\s+analyst)$", re.I)
SKIP_TITLE_RE  = re.compile(
    r"\b(senior|sr\.?|lead|manager|principal|staff|head|director|vp|ii|iii|iv)\b", re.I
)

_US_STATES = (
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    r"MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
US_LOCATION_RE = re.compile(
    rf"\b(united\s+states|usa|u\.s\.a?\.?|remote|{_US_STATES})\b", re.I
)

# ── Verified Lever companies ─────────────────────────────────────────────────────
# Slugs confirmed live via API — add more as you discover them at jobs.lever.co/<slug>
COMPANIES = [
    # Data / Analytics
    "hevo", "matillion", "acceldata", "logrocket",
    # Tech / SaaS
    "outreach", "pipedrive", "clari", "contentsquare", "algolia",
    "intercom", "typeform", "contentful", "sanity", "prismic",
    "metabase", "linear", "postman", "sentry", "launchdarkly",
    "toptal", "gohighlevel", "attentive", "activecampaign",
    "bazaarvoice", "okendo", "entrata", "agiloft", "regrello",
    "conversica", "secureframe", "jobvite", "angellist", "findem",
    "skillshare", "brilliant",
    # Cloud / Infra / Security
    "anyscale", "neon", "cockroachlabs", "temporal", "netlify",
    "tailscale", "render", "replicate", "jumpcloud", "sysdig",
    "verygoodsecurity", "evidentid",
    # Fintech / Crypto
    "plaid", "anchorage", "zerion", "ledger", "remitly", "relay",
    "clearco", "pipe", "fundrise",
    # Healthcare / Wellness / Bio
    "ro", "lyrahealth", "color", "tempus", "benchsci", "modernhealth",
    "headspace", "forward", "helix", "insitro", "zocdoc", "veeva",
    # Media / Consumer / Gaming
    "spotify", "theathletic", "medium", "patreon", "rover",
    "gopuff", "turo", "kabam", "whereby",
    # Logistics / Supply Chain
    "loadsmart",
    # Autonomous / Deep Tech
    "weride", "hermeus", "rigetti", "voltus", "tamr", "toku",
    # AI / ML
    "mistral", "palantir", "whoop",
    # Fintech (additional)
    "wealthfront",
    # HR / People Ops
    "15five", "trinet", "justworks", "bamboohr", "leapsome", "linkedin", "cornerstone",
    # Security (additional)
    "sophos",
    # Data / Streaming
    "snowplow", "zilliz",
    # Marketing / E-commerce
    "omnisend",
    # Other
    "lever", "greenhouse",
]

COMPANIES = list(dict.fromkeys(COMPANIES))  # dedupe, preserve order


# ── Persistence ──────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(ids: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


# ── Filters ───────────────────────────────────────────────────────────────────────

def is_allowed_title(title: str) -> bool:
    title = title.strip()
    if SKIP_TITLE_RE.search(title):
        return False
    return bool(ALLOWED_TITLES.match(title))


def is_us_location(location: str) -> bool:
    if not location.strip():
        return True
    return bool(US_LOCATION_RE.search(location))


def is_recent(created_at_ms: int) -> bool:
    posted = datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc)
    return (datetime.now(timezone.utc) - posted).days <= MAX_AGE_DAYS


def posted_label(created_at_ms: int) -> str:
    """Human-readable age string for the email."""
    days = (datetime.now(timezone.utc) - datetime.fromtimestamp(
        created_at_ms / 1000, tz=timezone.utc
    )).days
    if days == 0:
        return "Today"
    if days == 1:
        return "1 day ago"
    if days < 7:
        return f"{days} days ago"
    if days < 14:
        return "1 week ago"
    return f"{days // 7} weeks ago"


# ── Fetching ──────────────────────────────────────────────────────────────────────

async def fetch_company(client: httpx.AsyncClient, company: str, sem: asyncio.Semaphore) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    async with sem:
        try:
            resp = await client.get(url, timeout=10)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []


async def fetch_all() -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; job-scanner/1.0)"}

    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        results = await asyncio.gather(
            *[fetch_company(client, c, sem) for c in COMPANIES]
        )

    jobs = []
    for company, postings in zip(COMPANIES, results):
        for p in postings:
            jobs.append({**p, "_company": company})
    return jobs


# ── Email ─────────────────────────────────────────────────────────────────────────

def send_email(jobs: list[dict]) -> None:
    count   = len(jobs)
    subject = f"[Lever Scanner] {count} Matching Role(s) Found"

    rows_html = []
    for j in jobs:
        cats = j.get("categories", {})
        rows_html.append(
            f'<tr>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j["text"]}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j["_company"].replace("-"," ").title()}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{cats.get("location","")}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{cats.get("team","")}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{posted_label(j.get("createdAt",0))}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">'
            f'<a href="{j.get("hostedUrl","")}">View</a> &nbsp;'
            f'<a href="{j.get("applyUrl","")}">Apply</a></td>'
            f'</tr>'
        )

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333">
    <h2 style="color:#4a4a4a">Lever Jobs — New Matches</h2>
    <p>Found <strong>{count}</strong> new role(s) &nbsp;|&nbsp;
       Data Engineer &nbsp;|&nbsp; Data Analyst &nbsp;|&nbsp; US / Remote</p>
    <table style="border-collapse:collapse;width:100%;max-width:1300px">
      <tr style="background:#4a4a4a;color:#fff">
        <th style="padding:10px;border:1px solid #555;text-align:left;">Role</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Company</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Location</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Team</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Posted</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Links</th>
      </tr>
      {"".join(rows_html)}
    </table>
    <p style="font-size:12px;color:#888;margin-top:20px">
      Source: Lever ATS · {len(COMPANIES)} companies scanned
    </p>
    </body></html>
    """
    plain = f"Lever Jobs — {count} new match(es):\n\n" + "\n".join(
        f"- {j['text']} @ {j['_company'].replace('-',' ').title()} "
        f"| {j.get('categories',{}).get('location','')} "
        f"| {posted_label(j.get('createdAt',0))}\n  {j.get('hostedUrl','')}"
        for j in jobs
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, RECIPIENTS, msg.as_string())

    print(f"[email] Sent to {', '.join(RECIPIENTS)} — {count} job(s).")


# ── Main ──────────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 55)
    print("  Lever Jobs Scanner")
    print(f"  Scanning {len(COMPANIES)} companies...")
    print("=" * 55)

    all_postings    = await fetch_all()
    previously_seen = load_seen()

    print(f"\n  Total postings fetched: {len(all_postings)}")

    matched = []
    seen_ids: set = set()

    for p in all_postings:
        title    = p.get("text", "").strip()
        location = p.get("categories", {}).get("location", "")
        job_id   = p.get("id", "")

        if not is_allowed_title(title):
            continue
        if not is_recent(p.get("createdAt", 0)):
            continue
        if not is_us_location(location):
            continue
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        matched.append(p)

    new_jobs = [p for p in matched if p["id"] not in previously_seen]

    print(f"  Matched (title + US):          {len(matched)}")
    print(f"  New (not sent before):         {len(new_jobs)}")

    for j in new_jobs:
        cats = j.get("categories", {})
        print(f"\n  • {j['text']}")
        print(f"    Company:  {j['_company'].replace('-', ' ').title()}")
        print(f"    Location: {cats.get('location', '')}")
        print(f"    Team:     {cats.get('team', '')}")
        print(f"    Posted:   {posted_label(j.get('createdAt', 0))}")
        print(f"    URL:      {j.get('hostedUrl', '')}")

    if not new_jobs:
        print("\n  No new matching roles found.")
    else:
        print(f"\n  Sending email ({len(new_jobs)} new role(s))...")
        send_email(new_jobs)
        save_seen(previously_seen | {j["id"] for j in new_jobs})

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
