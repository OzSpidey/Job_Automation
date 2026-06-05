"""
greenhouse_nologin_scraper.py
------------------------------
Scrapes active jobs from 435+ verified companies' public Greenhouse boards
using the Greenhouse public API (no auth or browser required).

API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
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

# -- Email ---------------------------------------------------------------------
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

# -- Paths ---------------------------------------------------------------------
BASE_DIR       = Path(__file__).parent
SEEN_FILE      = BASE_DIR / "json" / "greenhouse_nologin_seen.json"
CSV_FILE       = BASE_DIR / "csv"  / "greenhouse_nologin_jobs.csv"
LAST_RUN_FILE  = BASE_DIR / "greenhouse_last_run_jobs.json"
COMPANIES_FILE = BASE_DIR / "greenhouse_companies.json"
MASTER_CSV     = BASE_DIR / "csv"  / "new_jobs.csv"

# -- Master CSV ----------------------------------------------------------------
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

API_URL        = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
MAX_CONCURRENT = 200
TIMEOUT        = 10.0

# -- Role matching -------------------------------------------------------------
ROLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bdata\s+analyst\b',                        re.I), "Data Analyst"),
    (re.compile(r'\banalytics?\s+engineer\b',                 re.I), "Analytics Engineer"),
    (re.compile(r'\banalytics\b',                             re.I), "Analytics"),
    (re.compile(r'\bdata\s+engineer(?:ing)?\b',               re.I), "Data Engineer"),
    (re.compile(r'\bbusiness\s+intelligence\b|\bBI\b',            re.I), "BI"),
    (re.compile(r'\bdata\s+scientist\b',                      re.I), "Data Scientist"),
    (re.compile(r'\bbusiness\s+analyst\b',                    re.I), "Business Analyst"),
    (re.compile(r'\breporting\s+analyst\b',                   re.I), "Reporting Analyst"),
    (re.compile(r'\bsoftware\s+engineer(?:ing)?\b',           re.I), "Software Engineer"),
    (re.compile(r'\bsoftware\s+developer\b',                  re.I), "Software Developer"),
    (re.compile(r'\b(?:machine\s+learning|ml)\s+engineer\b',  re.I), "ML Engineer"),
    (re.compile(r'\bbackend\s+engineer\b',                    re.I), "Backend Engineer"),
    (re.compile(r'\bfull[\s-]?stack\s+engineer\b',            re.I), "Full-Stack Engineer"),
    # Generic catch-alls (last — specific patterns above win first)
    (re.compile(r'\b(?:ai|llm|genai|gen[\s-]?ai)\s+engineer\b', re.I), "AI Engineer"),
    (re.compile(r'\bengineer(?:ing)?\b',                      re.I), "Engineer"),
    (re.compile(r'\bdeveloper\b',                             re.I), "Developer"),
    (re.compile(r'\banalyst\b',                               re.I), "Analyst"),
    (re.compile(r'\bdata\b',                                  re.I), "Data"),
]

# Quant firms — scrape all locations, not just US
_GLOBAL_SLUGS = {"janestreet", "janestreetevents"}

SENIOR_RE = re.compile(
    r'\b(senior|sr\.?|lead|staff|principal|manager|director|vp|'
    r'vice\s+president|head\s+of|associate\s+director)\b',
    re.I,
)

# -- US location check ---------------------------------------------------------
_US_ST = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
    "MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|"
    "TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
_US_CITIES = (
    r"new\s+york(?:\s+city)?|nyc|san\s+francisco|bay\s+area|silicon\s+valley|"
    r"los\s+angeles|chicago|seattle|boston|austin|denver|atlanta|miami|"
    r"dallas|houston|phoenix|portland|minneapolis|nashville|san\s+diego|"
    r"san\s+jose|washington\s+d\.?c\.?|new\s+jersey|nationwide"
)
_US_RE = re.compile(
    rf'(?:united\s+states|usa|u\.s\.a?|u\.s\.|,\s*(?:{_US_ST})\b|(?:{_US_CITIES}))',
    re.I,
)
_NON_US_RE = re.compile(
    r'\b(india|canada|united\s+kingdom|\buk\b|australia|germany|france|'
    r'netherlands|singapore|japan|china|brazil|mexico|ireland|sweden|'
    r'israel|poland|romania|portugal|czech|argentina|colombia|chile|'
    r'new\s+zealand|south\s+africa|dubai|uae|united\s+arab\s+emirates|'
    r'hong\s+kong|taiwan|south\s+korea|spain|italy|belgium|denmark|'
    r'finland|norway|switzerland|austria|turkey|russia|philippines|'
    r'indonesia|vietnam|nigeria|egypt|kenya|ghana|ethiopia|morocco|'
    r'pakistan|ukraine|greece|hungary|saudi\s+arabia|qatar|kuwait|'
    r'bahrain|jordan|iraq|thailand|malaysia|myanmar|sri\s+lanka|'
    r'bangladesh|nepal|cambodia|laos|peru|venezuela|ecuador|bolivia|'
    r'paraguay|uruguay|panama|costa\s+rica|guatemala|honduras|'
    r'el\s+salvador|cuba|dominican\s+republic|jamaica|trinidad|'
    r'senegal|cameroon|tanzania|uganda|algeria|tunisia|zimbabwe|'
    # Major non-US cities
    r'london|amsterdam|berlin|toronto|sydney|melbourne|paris|tokyo|'
    r'mumbai|bombay|bangalore|bengaluru|delhi|hyderabad|chennai|pune|'
    r'tel\s+aviv|warsaw|dublin|stockholm|copenhagen|oslo|helsinki|'
    r'vienna|zurich|madrid|barcelona|rome|prague|bucharest|lisbon|'
    r'shanghai|beijing|shenzhen|guangzhou|s[ao]\s+paulo|bogot[a]|'
    r'lima|santiago|buenos\s+aires|cape\s+town|johannesburg|nairobi|'
    r'karachi|lahore|dhaka|colombo|kuala\s+lumpur|jakarta|bangkok|'
    r'ho\s+chi\s+minh|manila|taipei|seoul|osaka|auckland|abu\s+dhabi|'
    r'riyadh|doha|accra|lagos|cairo|addis\s+ababa|dar\s+es\s+salaam|'
    r'kampala|casablanca|tunis|algiers|harare|kigali|lusaka|abuja|'
    r'kathmandu|colombo|phnom\s+penh|vientiane|yangon|athens|budapest|'
    r'kyiv|kiev|bucharest|belgrade|zagreb|sofia|bratislava|ljubljana|'
    r'tallinn|riga|vilnius|reykjavik|valletta|nicosia|limassol)\b',
    re.I,
)


def _is_us(loc: str) -> bool:
    if not loc:
        return True
    if _NON_US_RE.search(loc):
        return False
    if _US_RE.search(loc):
        return True
    if re.search(r'\b(remote|work[\s-]from[\s-]home|wfh|hybrid)\b', loc, re.I):
        return True
    return False


def _classify(title: str) -> str | None:
    for pat, label in ROLE_PATTERNS:
        if pat.search(title):
            return label
    return None


# -- Greenhouse board slugs (loaded from gitignored greenhouse_companies.json) ---
try:
    COMPANIES: dict[str, str] = json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))
except FileNotFoundError:
    print(f"[!] Missing {COMPANIES_FILE.name} — set GREENHOUSE_COMPANIES_JSON secret or create the file locally.", file=sys.stderr)
    sys.exit(1)


# -- Async scraping ------------------------------------------------------------

async def _get_office_location(client: httpx.AsyncClient, slug: str, job_id: str) -> str:
    """Fetch individual job detail and return a combined offices location string."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
    try:
        resp = await client.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        parts = []
        for office in data.get("offices", []):
            oloc = ((office.get("location") or {}).get("name") or "")
            oname = office.get("name", "")
            if oloc:
                parts.append(oloc)
            elif oname:
                parts.append(oname)
        return " | ".join(parts)
    except Exception:
        return ""


async def _fetch(
    client: httpx.AsyncClient,
    slug: str,
    company: str,
    sem: asyncio.Semaphore,
) -> list[dict]:
    async with sem:
        url = API_URL.format(slug=slug)
        try:
            resp = await client.get(url, timeout=TIMEOUT)
            if resp.status_code == 429:
                await asyncio.sleep(5)
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

        try:
            data = resp.json()
        except Exception:
            return []

        hits: list[dict] = []
        for job in data.get("jobs", []):
            title   = (job.get("title") or "").strip()
            loc     = ((job.get("location") or {}).get("name") or "").strip()
            job_id  = str(job.get("id", ""))
            job_url = job.get("absolute_url", "")
            raw_fp  = job.get("first_published") or job.get("updated_at") or ""
            try:
                posted = datetime.fromisoformat(raw_fp.replace("Z", "+00:00")).isoformat() if raw_fp else ""
            except Exception:
                posted = raw_fp[:19] if raw_fp else ""

            role = _classify(title)
            if not role:
                continue
            if SENIOR_RE.search(title):
                continue
            if slug not in _GLOBAL_SLUGS:
                if _NON_US_RE.search(loc):
                    continue
                if _NON_US_RE.search(title):
                    continue
                if not _is_us(loc):
                    office_loc = await _get_office_location(client, slug, job_id)
                    if office_loc and _NON_US_RE.search(office_loc):
                        continue

            hits.append({
                "job_id":   job_id,
                "title":    title,
                "company":  company,
                "location": loc,
                "role":     role,
                "url":      job_url,
                "posted":   posted,
            })

        if hits:
            print(f"[+] {company}: {len(hits)} match(es)")
        return hits


async def _scrape_all() -> list[dict]:
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; greenhouse-nologin-scraper/1.0)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tasks = [
            _fetch(client, slug, name, sem)
            for slug, name in COMPANIES.items()
        ]
        batches = await asyncio.gather(*tasks, return_exceptions=True)
    return [job for batch in batches if isinstance(batch, list) for job in batch]


# -- Email helpers -------------------------------------------------------------

def _format_posted(iso: str) -> str:
    if not iso:
        return "--"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_ET)
        return dt.strftime("%b %d, %Y %H:%M ET")
    except Exception:
        return iso[:10]


def _format_ago(iso: str) -> str:
    if not iso:
        return ""
    try:
        delta = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)).total_seconds())
        if delta < 0:
            return "just now"
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            m = delta // 60
            return f"{m}m ago"
        if delta < 86400:
            h = delta // 3600
            return f"{h}h ago"
        d = delta // 86400
        return f"{d}d ago"
    except Exception:
        return ""


# -- Email ---------------------------------------------------------------------

def _send_email(jobs: list[dict], new_count: int) -> None:
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set -- skipping email.")
        return

    # Sort: new jobs first, then by posted date descending (stable two-pass)
    sorted_jobs = sorted(jobs, key=lambda j: j.get("posted") or "", reverse=True)
    sorted_jobs = sorted(sorted_jobs, key=lambda j: 0 if j.get("is_new") else 1)
    rows = ""
    for j in sorted_jobs:
        new_badge = (
            "<span style='background:#22c55e;color:#fff;font-size:10px;"
            "font-weight:bold;padding:2px 5px;border-radius:3px;margin-left:5px'>NEW</span>"
            if j.get("is_new") else ""
        )
        posted_str = _format_posted(j.get("posted", ""))
        ago_str    = _format_ago(j.get("posted", ""))
        posted_cell = (
            f"{posted_str}"
            f"<br><span style='font-size:11px;color:#666'>({ago_str})</span>"
            if ago_str else posted_str
        )
        rows += (
            f"<tr>"
            f"<td>{j['title']}{new_badge}</td>"
            f"<td>{j['company']}</td>"
            f"<td>{j['location'] or '--'}</td>"
            f"<td>{j['role']}</td>"
            f"<td style='white-space:nowrap'>{posted_cell}</td>"
            f"<td><a href='{j['url']}'>Link</a></td>"
            f"</tr>"
        )

    body = f"""
    <h2>Greenhouse &mdash; {len(jobs)} job(s) posted in the last 24 hours</h2>
    <p><b>{new_count} new role(s)</b> found this run. All listings from the last 24h shown &mdash;
    <span style='background:#22c55e;color:#fff;font-size:10px;font-weight:bold;padding:2px 5px;border-radius:3px'>NEW</span>
    = new this run.</p>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Location</th><th>Role</th><th>Posted</th><th>Link</th>
      </tr>
      {rows}
    </table>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Greenhouse: {new_count} new job(s) — {len(jobs)} total (last 24h)"
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


# -- Main ----------------------------------------------------------------------

def main() -> None:
    seen_ts: dict[str, str] = {}
    if SEEN_FILE.exists():
        raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            # migrate legacy flat list — stamp everything as now so they age out in 7 days
            now_iso = datetime.now(timezone.utc).isoformat()
            seen_ts = {job_id: now_iso for job_id in raw}
        else:
            seen_ts = raw
    # prune entries older than 7 days
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    seen_ts = {k: v for k, v in seen_ts.items() if v >= week_ago}
    seen: set[str] = set(seen_ts)

    print(f"[i] {len(seen)} previously seen IDs | scraping {len(COMPANIES)} boards...")

    all_jobs = asyncio.run(_scrape_all())
    for job in all_jobs:
        job["is_new"] = job["job_id"] not in seen
    print(f"[i] Total matching: {len(all_jobs)}")

    new_jobs = [j for j in all_jobs if j["job_id"] not in seen]
    print(f"[i] New this run:   {len(new_jobs)}")

    cutoff_24h_str = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    all_24h_jobs = [j for j in all_jobs if not j.get("posted") or j["posted"] >= cutoff_24h_str]
    new_24h_jobs  = [j for j in all_24h_jobs if j.get("is_new")]
    print(f"[i] Posted <=24h:  {len(all_24h_jobs)} total, {len(new_24h_jobs)} new")

    if new_jobs:
        CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_header = not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0
        fieldnames = ["job_id", "title", "company", "location", "role", "posted", "url", "found_at", "is_new"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        new_jobs_sorted = sorted(new_jobs, key=lambda j: j.get("posted") or "", reverse=True)
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            for j in new_jobs_sorted:
                writer.writerow({**j, "found_at": now})
        print(f"[+] Appended to {CSV_FILE}")

    # Only mark a job seen once it surfaces in the email (24h window).
    # Jobs outside the window stay out of seen so they get another chance next run.
    now_iso = datetime.now(timezone.utc).isoformat()
    for j in new_24h_jobs:
        seen_ts.setdefault(j["job_id"], now_iso)
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen_ts, sort_keys=True), encoding="utf-8")

    # Append DA/DE/BI jobs posted <24h to master CSV
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    master_rows = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for j in new_jobs:
        if not _MASTER_ROLE_RE.search(j["title"]):
            continue
        try:
            if datetime.fromisoformat(j["posted"].replace("Z", "+00:00")).astimezone(timezone.utc) < cutoff_24h:
                continue
        except Exception:
            continue
        master_rows.append({
            "source":   "greenhouse",
            "job_id":   j["job_id"],
            "title":    j["title"],
            "company":  j["company"],
            "location": j["location"],
            "role":     j["role"],
            "posted":   j["posted"],
            "url":      j["url"],
            "found_at": now_str,
        })
    _append_master_csv(master_rows)

    if new_24h_jobs:
        _send_email(all_24h_jobs, len(new_24h_jobs))
    else:
        print("[i] No new 24h jobs -- skipping email.")


if __name__ == "__main__":
    main()
