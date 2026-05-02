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

SEEN_FILE    = Path(__file__).parent / "json" / "lever_seen_jobs.json"
CONCURRENCY  = 15
MAX_AGE_DAYS = 7

ALLOWED_TITLES = re.compile(
    r"\b(data\s+engineer|data\s+analyst|analytics\s+engineer|analytics\s+analyst"
    r"|business\s+intelligence\s+analyst|machine\s+learning\s+engineer"
    r"|data\s+scientist|ai\s+engineer|software\s+developer|software\s+engineer)\b",
    re.I
)
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
    "tinybird", "qonto",
    # Tech / SaaS
    "outreach", "pipedrive", "clari", "contentsquare", "algolia",
    "intercom", "typeform", "contentful", "sanity", "prismic",
    "metabase", "linear", "postman", "sentry", "launchdarkly",
    "toptal", "gohighlevel", "attentive", "activecampaign",
    "bazaarvoice", "okendo", "entrata", "agiloft", "regrello",
    "conversica", "secureframe", "jobvite", "angellist", "findem",
    "skillshare", "brilliant",
    "freshworks", "houzz", "atlassian", "netflix",
    # Cloud / Infra / Security
    "anyscale", "neon", "cockroachlabs", "temporal", "netlify",
    "tailscale", "render", "replicate", "jumpcloud", "sysdig",
    "verygoodsecurity", "evidentid",
    "teleport", "saviynt", "sonatype", "logz",
    # Fintech / Crypto
    "plaid", "anchorage", "zerion", "ledger", "remitly", "relay",
    "clearco", "pipe", "fundrise",
    "kraken", "alloy", "sure", "better",
    # Healthcare / Wellness / Bio
    "ro", "lyrahealth", "color", "tempus", "benchsci", "modernhealth",
    "headspace", "forward", "helix", "insitro", "zocdoc", "veeva",
    "artera", "quantum-health", "nava",
    # Media / Consumer / Gaming
    "spotify", "theathletic", "medium", "patreon", "rover",
    "gopuff", "turo", "kabam", "whereby",
    # Logistics / Supply Chain
    "loadsmart",
    "duffel", "resilinc", "meroxa",
    # Autonomous / Deep Tech
    "weride", "hermeus", "rigetti", "voltus", "tamr", "toku",
    "robust-ai",
    # AI / ML
    "mistral", "palantir", "whoop",
    "labelbox", "beam", "humata",
    # Fintech (additional)
    "wealthfront",
    # HR / People Ops
    "15five", "trinet", "justworks", "bamboohr", "leapsome", "linkedin", "cornerstone",
    "achievers", "deputy",
    # Security (additional)
    "sophos",
    "accurate",
    # Data / Streaming
    "snowplow", "zilliz",
    # Marketing / E-commerce
    "omnisend",
    "kochava",
    # Climate / Clean Tech
    "arcadia", "pachama", "verdigris",
    # Legal Tech
    "filevine",
    # EdTech
    "bloom",
    # Other / Misc
    "lever", "greenhouse",
    "canarytechnologies", "hhaexchange", "BestEgg", "3pillarglobal",
    "integrate", "electricmind", "thinkahead", "insiderone",
    "adhoclabs", "startengine", "repurposeglobal", "venteur",
    "cloaked-app", "intersect", "noodle", "jiostar",
    "oowlish", "hatchit", "jobgether", "revefi",
    # Fintech / Payments (additional)
    "binance", "nium", "wealthsimple", "prosper",
    "rackspace", "spendesk", "ravio",
    # Sales / Revenue
    "reply", "stackadapt", "highspot", "mindtickle",
    # HR / People (additional)
    "humaans",
    # Marketing / Analytics
    "brightedge", "nielsen", "revinate",
    # Healthcare (additional)
    "aledade", "everlywell",
    # PropTech
    "belong", "lessen",
    # E-commerce / Retail
    "skio", "sugarcrm", "olo", "restaurant365", "revel",
    # Media / Consumer
    "restream", "playvs", "glide", "super", "thunkable",
    "buildium",
    # Learning / Education
    "360learning", "docebo", "instructure",
    # Logistics / Ops
    "getcircuit", "zoox",
    # Observability / DevTools
    "100ms", "conduktor", "flatfile", "snaplogic",
    "pivotal", "lemon",
    # Finance / Payments
    "factor",
    # Data / AI
    "appen", "superannotate", "aquarium",
    # Crypto / Web3
    "1inch", "safe",
    # Field Service / Hospitality
    "aircall", "boxcast",
    # Consulting / Services
    "bounteous", "cprime",
    # Hosting / Infra
    "hostinger", "kinsta", "siteground", "scaleway",
    # Identity / Verification
    "finch", "teller",
    # Misc
    "arable", "salesmsg", "mirror",
    "trustarc", "upguard",
    "caseware", "cority", "payactiv", "topdesk", "wonolo",
    "drivetrain",
    # Fintech / Healthcare (agent-verified)
    "zopa", "pointclickcare",
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
    return bool(ALLOWED_TITLES.search(title))


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

def send_email(all_jobs: list[dict], new_ids: set) -> None:
    new_count  = len(new_ids)
    seen_count = len(all_jobs) - new_count
    subject    = f"[Lever Scanner] {new_count} New Role(s) Found"

    # Sort newest first, new roles bubble to top within same date
    all_jobs = sorted(
        all_jobs,
        key=lambda j: (j["id"] in new_ids, j.get("createdAt", 0)),
        reverse=True,
    )

    rows_html = []
    for j in all_jobs:
        cats    = j.get("categories", {})
        is_new  = j["id"] in new_ids
        new_badge = (
            '<span style="background:#2ecc71;color:#fff;padding:2px 7px;'
            'border-radius:4px;font-size:11px;font-weight:bold;margin-left:6px;">NEW</span>'
            if is_new else ""
        )
        row_bg  = 'background:#f0fff4;' if is_new else ''
        rows_html.append(
            f'<tr style="{row_bg}">'
            f'<td style="padding:8px;border:1px solid #ddd;">{j["text"]}{new_badge}</td>'
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
    <h2 style="color:#4a4a4a">Lever Jobs — Weekly Digest</h2>
    <p><strong style="color:#2ecc71">{new_count} new</strong> role(s) &nbsp;|&nbsp;
       {seen_count} already seen &nbsp;|&nbsp;
       Last 7 days &nbsp;|&nbsp; US / Remote</p>
    <p style="font-size:12px;color:#666;">
       Data Engineer &nbsp;·&nbsp; Data Analyst &nbsp;·&nbsp; Analytics Engineer &nbsp;·&nbsp;
       Analytics Analyst &nbsp;·&nbsp; BI Analyst &nbsp;·&nbsp; ML Engineer &nbsp;·&nbsp;
       Data Scientist &nbsp;·&nbsp; AI Engineer &nbsp;·&nbsp; Software Developer &nbsp;·&nbsp;
       Software Engineer</p>
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
    plain = f"Lever Jobs — {new_count} new role(s) this run ({len(all_jobs)} total in last 7 days):\n\n"
    for j in all_jobs:
        tag = "[NEW] " if j["id"] in new_ids else "      "
        plain += (
            f"{tag}{j['text']} @ {j['_company'].replace('-',' ').title()} "
            f"| {j.get('categories',{}).get('location','')} "
            f"| {posted_label(j.get('createdAt',0))}\n  {j.get('hostedUrl','')}\n\n"
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

    print(f"[email] Sent — {new_count} job(s).")


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
    seen_keys: set = set()  # dedup by company+title to handle reposted roles

    for p in all_postings:
        title    = p.get("text", "").strip()
        location = p.get("categories", {}).get("location", "")
        job_id   = p.get("id", "")
        dedup_key = f"{p['_company']}|{title.lower()}"

        if not is_allowed_title(title):
            continue
        if not is_recent(p.get("createdAt", 0)):
            continue
        if not is_us_location(location):
            continue
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        matched.append(p)

    new_ids = {p["id"] for p in matched if p["id"] not in previously_seen}

    print(f"  Matched (title + US, 7 days):  {len(matched)}")
    print(f"  Already seen:                  {len(matched) - len(new_ids)}")
    print(f"  New (not sent before):         {len(new_ids)}")

    for p in matched:
        cats = p.get("categories", {})
        tag  = "[NEW]" if p["id"] in new_ids else "     "
        print(f"\n  {tag} {p['text']}")
        print(f"    Company:  {p['_company'].replace('-', ' ').title()}")
        print(f"    Location: {cats.get('location', '')}")
        print(f"    Posted:   {posted_label(p.get('createdAt', 0))}")
        print(f"    URL:      {p.get('hostedUrl', '')}")

    if not new_ids:
        print("\n  No new roles since last run — skipping email.")
    else:
        print(f"\n  Sending email ({len(new_ids)} new, {len(matched)} total)...")
        send_email(matched, new_ids)
        save_seen(previously_seen | new_ids)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
