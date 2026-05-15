"""
rippling_jobs_scanner.py
------------------------
Scans public Rippling ATS job boards for target roles.

How it works:
  Each board page (https://ats.rippling.com/{slug}/jobs) is a Next.js app
  that embeds all job data in a <script id="__NEXT_DATA__"> block — no
  separate API call needed.

API path inside __NEXT_DATA__:
  props.pageProps.dehydratedState.queries[0].state.data.items

Add companies to rippling_companies.json: { "slug": "Display Name", ... }
"""

import asyncio
import csv
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

BASE_DIR        = Path(__file__).parent
SEEN_FILE       = BASE_DIR / "json" / "rippling_seen_jobs.json"
CSV_FILE        = BASE_DIR / "csv"  / "rippling_jobs.csv"
COMPANIES_FILE  = BASE_DIR / "rippling_companies.json"
MASTER_CSV      = BASE_DIR / "csv"  / "new_jobs.csv"

MAX_CONCURRENT  = 20
TIMEOUT         = 15.0

# ── Master CSV ────────────────────────────────────────────────────────────────
_MASTER_COLS    = ["source", "job_id", "title", "company", "location", "role", "posted", "url", "found_at"]
_MASTER_ROLE_RE = re.compile(r'\bdata\b|business\s+intelligence', re.I)

def _append_master_csv(rows: list[dict]) -> None:
    if not rows:
        return
    existing: set[str] = set()
    if MASTER_CSV.exists():
        with open(MASTER_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing.add(f"{r.get('source','')}:{r.get('job_id','')}")
    new_rows = [r for r in rows if f"{r['source']}:{r['job_id']}" not in existing]
    if not new_rows:
        return
    MASTER_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not MASTER_CSV.exists() or MASTER_CSV.stat().st_size == 0
    with open(MASTER_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_MASTER_COLS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)
    print(f"[+] Master CSV: appended {len(new_rows)} job(s)")
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a") as f:
            f.write("MASTER_CSV_UPDATED=true\n")

# ── Role matching ─────────────────────────────────────────────────────────────
ROLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bdata\s+analyst\b',                        re.I), "Data Analyst"),
    (re.compile(r'\banalytics?\s+engineer\b',                 re.I), "Analytics Engineer"),
    (re.compile(r'\bdata\s+engineer(?:ing)?\b',               re.I), "Data Engineer"),
    (re.compile(r'\b(?:business\s+intelligence|bi)\b[^,()]{0,30}?\b(?:analyst|developer|engineer|specialist)\b',
                                                              re.I), "BI"),
    (re.compile(r'\bdata\s+scientist\b',                      re.I), "Data Scientist"),
    (re.compile(r'\bbusiness\s+analyst\b',                    re.I), "Business Analyst"),
    (re.compile(r'\breporting\s+analyst\b',                   re.I), "Reporting Analyst"),
    (re.compile(r'\bsoftware\s+engineer(?:ing)?\b',           re.I), "Software Engineer"),
    (re.compile(r'\bsoftware\s+developer\b',                  re.I), "Software Developer"),
    (re.compile(r'\b(?:machine\s+learning|ml)\s+engineer\b',  re.I), "ML Engineer"),
    (re.compile(r'\bbackend\s+engineer\b',                    re.I), "Backend Engineer"),
    (re.compile(r'\bfull[\s-]?stack\s+engineer\b',            re.I), "Full-Stack Engineer"),
    (re.compile(r'\b(?:ai|llm|genai|gen[\s-]?ai)\s+engineer\b', re.I), "AI Engineer"),
    (re.compile(r'\bengineer(?:ing)?\b',                      re.I), "Engineer"),
    (re.compile(r'\bdeveloper\b',                             re.I), "Developer"),
    (re.compile(r'\banalyst\b',                               re.I), "Analyst"),
    (re.compile(r'\bdata\b',                                  re.I), "Data"),
]

SENIOR_RE = re.compile(
    r'\b(senior|sr\.?|lead|staff|principal|manager|director|vp|'
    r'vice\s+president|head\s+of|associate\s+director)\b',
    re.I,
)

# ── Location check ─────────────────────────────────────────────────────────────
# Rippling provides structured location data — countryCode is the primary check.
_NON_US_TITLE_RE = re.compile(
    r'\b(india|canada|united\s+kingdom|\buk\b|australia|germany|france|'
    r'netherlands|singapore|japan|china|brazil|mexico|ireland|sweden|'
    r'israel|poland|london|amsterdam|berlin|toronto|sydney|paris|tokyo|'
    r'mumbai|bangalore|bengaluru|delhi|hyderabad|pune|chennai)\b',
    re.I,
)


def _is_us_job(locations: list[dict]) -> bool:
    """Return True if the job is US-based or fully remote (no location list)."""
    if not locations:
        return True  # no location data — assume open/remote
    for loc in locations:
        wt = (loc.get("workplaceType") or "").upper()
        if wt == "REMOTE":
            return True
        if loc.get("countryCode", "").upper() == "US":
            return True
    return False


def _location_str(locations: list[dict]) -> str:
    """Human-readable location for the email."""
    if not locations:
        return "Remote"
    parts = []
    for loc in locations:
        wt = (loc.get("workplaceType") or "").capitalize()
        city  = loc.get("city", "")
        state = loc.get("stateCode", "")
        if loc.get("countryCode", "").upper() != "US":
            continue  # only show US locations
        if city and state:
            parts.append(f"{city}, {state}")
        elif state:
            parts.append(state)
        elif wt:
            parts.append(wt)
    return " | ".join(parts) if parts else "Remote"


def _classify(title: str) -> str | None:
    for pat, label in ROLE_PATTERNS:
        if pat.search(title):
            return label
    return None


# ── Companies ─────────────────────────────────────────────────────────────────
try:
    COMPANIES: dict[str, str] = json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))
except FileNotFoundError:
    print(f"[!] Missing {COMPANIES_FILE.name} — create it with {{\"slug\": \"Company Name\"}}.", file=sys.stderr)
    sys.exit(1)


# ── Async fetching ─────────────────────────────────────────────────────────────

_NEXT_DATA_RE = re.compile(
    r'<script\s+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.S,
)


async def _fetch(
    client: httpx.AsyncClient,
    slug: str,
    company: str,
    sem: asyncio.Semaphore,
) -> list[dict]:
    url = f"https://ats.rippling.com/{slug}/jobs"
    async with sem:
        try:
            resp = await client.get(url, timeout=TIMEOUT)
        except Exception as exc:
            print(f"[!] {company} ({slug}): {exc}")
            return []

    if resp.status_code == 404:
        print(f"[-] {company} ({slug}): board not found")
        return []
    if resp.status_code != 200:
        print(f"[!] {company} ({slug}): HTTP {resp.status_code}")
        return []

    m = _NEXT_DATA_RE.search(resp.text)
    if not m:
        print(f"[!] {company} ({slug}): no __NEXT_DATA__ found")
        return []

    try:
        nd = json.loads(m.group(1))
        queries = nd["props"]["pageProps"]["dehydratedState"]["queries"]
        items   = queries[0]["state"]["data"]["items"]
    except (KeyError, IndexError, json.JSONDecodeError):
        print(f"[!] {company} ({slug}): unexpected __NEXT_DATA__ structure")
        return []

    hits: list[dict] = []
    for job in items:
        title     = (job.get("name") or "").strip()
        job_id    = job.get("id", "")
        job_url   = job.get("url", "") or f"https://ats.rippling.com/{slug}/jobs/{job_id}"
        locations = job.get("locations") or []

        role = _classify(title)
        if not role:
            continue
        if SENIOR_RE.search(title):
            continue
        if _NON_US_TITLE_RE.search(title):
            continue
        if not _is_us_job(locations):
            continue

        hits.append({
            "job_id":   job_id,
            "title":    title,
            "company":  company,
            "slug":     slug,
            "location": _location_str(locations),
            "role":     role,
            "url":      job_url,
        })

    if hits:
        print(f"[+] {company}: {len(hits)} match(es)")
    return hits


async def _scrape_all() -> list[dict]:
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; rippling-job-scanner/1.0)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tasks = [
            _fetch(client, slug, name, sem)
            for slug, name in COMPANIES.items()
        ]
        batches = await asyncio.gather(*tasks, return_exceptions=True)
    return [job for batch in batches if isinstance(batch, list) for job in batch]


# ── Email ─────────────────────────────────────────────────────────────────────

def _send_email(new_jobs: list[dict]) -> None:
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set — skipping email.")
        return

    rows = ""
    for j in sorted(new_jobs, key=lambda x: x["company"]):
        rows += (
            f"<tr>"
            f"<td>{j['title']}</td>"
            f"<td>{j['company']}</td>"
            f"<td>{j['location'] or '--'}</td>"
            f"<td>{j['role']}</td>"
            f"<td><a href='{j['url']}'>Apply</a></td>"
            f"</tr>"
        )

    body = f"""
    <h2>Rippling Jobs Scanner — {len(new_jobs)} new job(s)</h2>
    <p>Scanned {len(COMPANIES)} Rippling job board(s). Showing only jobs not seen before.</p>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Location</th><th>Role</th><th>Link</th>
      </tr>
      {rows}
    </table>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Rippling Jobs: {len(new_jobs)} new job(s)"
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, EMAIL_TO, msg.as_string())
        print(f"[+] Email sent to {EMAIL_TO}")
    except Exception as exc:
        print(f"[!] Email failed: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Load seen IDs (dict of job_id → first_seen ISO timestamp, pruned after 7 days)
    seen_ts: dict[str, str] = {}
    if SEEN_FILE.exists():
        raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        seen_ts = raw if isinstance(raw, dict) else {jid: datetime.now(timezone.utc).isoformat() for jid in raw}
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    seen_ts  = {k: v for k, v in seen_ts.items() if v >= week_ago}
    seen: set[str] = set(seen_ts)

    print(f"[i] {len(seen)} previously seen IDs | scraping {len(COMPANIES)} board(s)...")

    all_jobs = asyncio.run(_scrape_all())
    print(f"[i] Total matching: {len(all_jobs)}")

    new_jobs = [j for j in all_jobs if j["job_id"] not in seen]
    print(f"[i] New this run:   {len(new_jobs)}")

    if new_jobs:
        # Append to rippling-specific CSV
        CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_header = not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0
        fieldnames = ["job_id", "title", "company", "location", "role", "url", "found_at"]
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            for j in new_jobs:
                writer.writerow({**j, "found_at": now_str})
        print(f"[+] Appended to {CSV_FILE}")

        # Append data roles to master CSV
        master_rows = []
        for j in new_jobs:
            if not _MASTER_ROLE_RE.search(j["title"]):
                continue
            master_rows.append({
                "source":   "rippling",
                "job_id":   j["job_id"],
                "title":    j["title"],
                "company":  j["company"],
                "location": j["location"],
                "role":     j["role"],
                "posted":   "",
                "url":      j["url"],
                "found_at": now_str,
            })
        _append_master_csv(master_rows)

    # Mark new jobs seen
    now_iso = datetime.now(timezone.utc).isoformat()
    for j in new_jobs:
        seen_ts.setdefault(j["job_id"], now_iso)
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen_ts, sort_keys=True), encoding="utf-8")

    if new_jobs:
        _send_email(new_jobs)
    else:
        print("[i] No new jobs — skipping email.")


if __name__ == "__main__":
    main()
