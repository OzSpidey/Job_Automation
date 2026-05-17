"""
icims_scraper.py

Scrapes public iCIMS career portals for data/analytics/engineering roles.
Uses sitemap.xml for job discovery (no keyword filter — gets all jobs),
then regex-classifies titles derived from URL slugs (detail pages are JS-rendered).

To add a company:
  1. Confirm sitemap exists: https://{tenant}.icims.com/sitemap.xml  (must return 200)
  2. Add to icims_companies.json: {"name": "...", "tenant": "..."}
"""

import csv
import json
import os
import re
import smtplib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────────────────────────────
MAX_AGE_DAYS   = 1
PRUNE_DAYS     = 7
WORKERS        = 20
OUTPUT_CSV     = Path(__file__).parent / "csv"  / "icims_jobs.csv"
SEEN_LOG       = Path(__file__).parent / "json" / "icims_seen_jobs.json"
COMPANIES_FILE = Path(__file__).parent / "json" / "icims_companies.json"

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# ── Filters ────────────────────────────────────────────────────────────────────
ALLOWED_TITLE_RE = re.compile(
    r"("
    r"\bdata\b.{0,40}\banalyst\b"           # data [quality/reporting/…] analyst
    r"|\banalyst\b.{0,40}\bdata\b"          # analyst […] data
    r"|\bdata\b.{0,40}\bengineer\b"         # data [pipeline/…] engineer
    r"|\bengineer\b.{0,40}\bdata\b"         # engineer […] data
    r"|\bai\b.{0,20}\bengineer\b"           # AI [generative/…] engineer
    r"|\bengineer\b.{0,20}\bai\b"           # engineer […] AI
    r"|\banalytics\b"                        # anything with analytics
    r"|\bBI\b"                               # BI Developer / Power BI / …
    r"|\bbusiness\b.{0,30}\bintelligence\b" # business [process/…] intelligence
    r"|\bintelligence\b.{0,30}\bbusiness\b" # intelligence […] business
    r")",
    re.I,
)
SKIP_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|lead|manager|principal|staff|director|head|vp|"
    r"architect|consultant|iii|iv)\b",
    re.I,
)
NOISE_TITLE_RE = re.compile(
    r"\b(data\s+center|payroll|medical\s+coding|bilingual)\b",
    re.I,
)
ENTRY_LEVEL_RE = re.compile(
    r"\b(junior|jr\.?|associate|entry[\s\-]level|new\s+grad|graduate)\b",
    re.I,
)

_CLASSIFY_PATTERNS = [
    (re.compile(r"\bdata\b.{0,40}\bengineer\b|\bengineer\b.{0,40}\bdata\b", re.I), "Data Engineer"),
    (re.compile(r"\bai\b.{0,20}\bengineer\b|\bengineer\b.{0,20}\bai\b",     re.I), "AI Engineer"),
    (re.compile(r"\bbusiness\b.{0,30}\bintelligence\b|\bBI\b",               re.I), "Business Intelligence"),
    (re.compile(r"\bdata\b.{0,40}\banalyst\b|\banalyst\b.{0,40}\bdata\b",   re.I), "Data Analyst"),
    (re.compile(r"\banalytics\b",                                             re.I), "Analytics"),
    (re.compile(r"\bdata\b",                                                  re.I), "Data (Other)"),
]

def _classify(title: str) -> str:
    for pat, label in _CLASSIFY_PATTERNS:
        if pat.search(title):
            return label
    return "Other"

def is_allowed_title(title: str) -> bool:
    if SKIP_TITLE_RE.search(title):
        return False
    if NOISE_TITLE_RE.search(title):
        return False
    return bool(ALLOWED_TITLE_RE.search(title))

def is_entry_level(title: str) -> bool:
    return bool(ENTRY_LEVEL_RE.search(title))

# ── Persistence ────────────────────────────────────────────────────────────────
def load_seen_ids() -> dict:
    """Returns {job_id: date_str}, pruning entries older than PRUNE_DAYS."""
    if not SEEN_LOG.exists():
        return {}
    raw = json.loads(SEEN_LOG.read_text(encoding="utf-8"))
    if isinstance(raw, list):                          # migrate from old flat-list format
        today = datetime.utcnow().strftime("%Y-%m-%d")
        raw = {jid: today for jid in raw}
    cutoff = (datetime.utcnow() - timedelta(days=PRUNE_DAYS)).strftime("%Y-%m-%d")
    return {jid: dt for jid, dt in raw.items() if dt >= cutoff}

def save_seen_ids(seen: dict) -> None:
    SEEN_LOG.parent.mkdir(parents=True, exist_ok=True)
    SEEN_LOG.write_text(json.dumps(seen, indent=2, sort_keys=True), encoding="utf-8")

def load_companies() -> list:
    return json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))

CSV_COLS = ["title", "company", "location", "role", "posted", "link", "found_on", "is_new", "entry_level"]

def append_csv(row: dict) -> None:
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not OUTPUT_CSV.exists()
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        if write_header:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_COLS})

# ── Date helpers ───────────────────────────────────────────────────────────────
_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

def parse_lastmod(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if "T" in s:
            from datetime import timezone as _tz
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(_tz.utc).replace(tzinfo=None)
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except Exception:
        return None

def format_age(dt: datetime | None) -> str:
    """Return relative posting age with hour/minute precision for recent posts."""
    if not dt:
        return ""
    delta = int((datetime.utcnow() - dt).total_seconds())
    if delta < 0:
        return "Posted just now"
    if delta < 3600:
        m = max(1, delta // 60)
        return f"Posted {m} minute{'s' if m != 1 else ''} ago"
    if delta < 86400:
        h = delta // 3600
        return f"Posted {h} hour{'s' if h != 1 else ''} ago"
    d = delta // 86400
    if d == 1:
        return "Posted 1 day ago"
    return f"Posted {d} days ago"

def slug_to_title(slug: str) -> str:
    """'data-engineer---ai' → 'Data Engineer - Ai'"""
    import urllib.parse
    slug = urllib.parse.unquote(slug)
    slug = re.sub(r"-{2,}", "\x00", slug)  # multi-dash → placeholder
    slug = slug.replace("-", " ")          # single dash → space
    slug = slug.replace("\x00", " - ")     # placeholder → " - "
    return " ".join(slug.split()).title()   # collapse whitespace + title-case

# ── Sitemap ────────────────────────────────────────────────────────────────────
def fetch_sitemap_jobs(tenant: str) -> list:
    url = f"https://{tenant}.icims.com/sitemap.xml"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        tree    = ET.fromstring(resp.content)
        cutoff  = datetime.utcnow() - timedelta(days=MAX_AGE_DAYS + 1)
        jobs    = []
        for node in tree.findall("sm:url", _SITEMAP_NS):
            loc_el = node.find("sm:loc",     _SITEMAP_NS)
            mod_el = node.find("sm:lastmod", _SITEMAP_NS)
            if loc_el is None:
                continue
            loc = loc_el.text or ""
            # Only job detail pages: /jobs/{id}/{slug}/job
            if "/jobs/" not in loc or not loc.endswith("/job"):
                continue
            lastmod = parse_lastmod(mod_el.text if mod_el is not None else "")
            if lastmod and lastmod < cutoff:
                continue
            m = re.search(r"/jobs/(\d+)/([^/]+)/job", loc)
            if not m:
                continue
            raw_mod = mod_el.text if mod_el is not None else ""
            has_time = lastmod is not None and "T" in raw_mod
            jobs.append({
                "url":          loc,
                "lastmod":      lastmod.strftime("%Y-%m-%d") if lastmod else "",
                "lastmod_time": lastmod.strftime("%H:%M UTC") if has_time else "",
                "lastmod_dt":   lastmod,
                "job_id":       m.group(1),
                "title_slug":   m.group(2),
            })
        return jobs
    except Exception:
        return []


# ── Per-company worker ─────────────────────────────────────────────────────────
def process_company(company, seen_ids, all_current_jobs, lock, csv_lock, counter):
    tenant = company["tenant"]
    name   = company["name"]
    print(f"[→] {name}")

    sitemap_jobs = fetch_sitemap_jobs(tenant)
    if not sitemap_jobs:
        print(f"    [–] {name} — no recent sitemap jobs")
        return

    matched_new = 0
    for sj in sitemap_jobs:
        # iCIMS detail pages are JS-rendered (SPA) — title comes from slug
        title = slug_to_title(sj["title_slug"])
        if not is_allowed_title(title):
            continue

        job_id = f"{tenant}_{sj['job_id']}"

        with lock:
            is_new = job_id not in seen_ids
            if is_new:
                seen_ids[job_id] = datetime.utcnow().strftime("%Y-%m-%d")

        row = {
            "title":       title,
            "company":     name,
            "location":    "—",
            "role":        _classify(title),
            "posted":      sj["lastmod"],
            "posted_time": sj.get("lastmod_time", ""),
            "age":         format_age(sj.get("lastmod_dt")),
            "link":        sj["url"],
            "found_on":    datetime.now().strftime("%Y-%m-%d %H:%M"),
            "is_new":      is_new,
            "entry_level": is_entry_level(title),
        }

        with lock:
            all_current_jobs.append(row)

        if is_new:
            with csv_lock:
                append_csv(row)
            with lock:
                counter[0] += 1
            matched_new += 1
            print(f"    [+] NEW: {title} | {sj['lastmod']}")

    if matched_new == 0:
        print(f"    [–] {name} — no new matches")

# ── Email ──────────────────────────────────────────────────────────────────────
def send_summary_email(all_jobs: list, new_count: int) -> None:
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set — skipping email.")
        return
    if not all_jobs:
        print("[i] No jobs to send — skipping email.")
        return

    today = datetime.now().strftime("%Y-%m-%d")

    def _row(j):
        badges = ""
        if j.get("entry_level"):
            badges += "&nbsp;<span style='background:#1565c0;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px'>ENTRY</span>"
        bg = "#f1f8e9" if j.get("posted") == today else ""
        time_html = (
            f"<br><span style='font-size:11px;color:#888'>{j['posted_time']}</span>"
            if j.get("posted_time") else ""
        )
        age_html = (
            f"<br><span style='font-size:11px;color:#666'>({j.get('age','')})</span>"
            if j.get("age") else ""
        )
        return (
            f"<tr style='background:{bg}'>"
            f"<td>{j['title']}{badges}</td>"
            f"<td>{j['company']}</td>"
            f"<td>{j['location']}</td>"
            f"<td style='white-space:nowrap'>{j.get('posted','')}{time_html}{age_html}</td>"
            f"<td><a href='{j['link']}'>Apply</a></td>"
            f"</tr>"
        )

    by_role: dict = {}
    for j in all_jobs:
        by_role.setdefault(j.get("role", "Other"), []).append(j)

    sections = ""
    for role_label, jobs in sorted(by_role.items()):
        jobs = sorted(jobs, key=lambda j: j.get("posted", ""), reverse=True)
        role_rows = "".join(_row(j) for j in jobs)
        sections += f"""
    <h3 style='margin-top:24px'>{role_label} ({len(jobs)})</h3>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Link</th>
      </tr>
      {role_rows}
    </table>"""

    subject   = f"[iCIMS] {new_count} new role(s) — {datetime.now().strftime('%b %d, %Y %H:%M')}"
    body_html = f"""
    <h2>iCIMS — New Roles</h2>
    <p><b>{new_count} new role(s)</b> found across 102 companies. Green rows = posted today.
    &nbsp;<span style='background:#1565c0;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px'>ENTRY</span> = entry-level.</p>
    {sections}
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body_html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_TO, msg.as_string())
        print(f"[+] Email sent to {EMAIL_TO}")
    except Exception as e:
        print(f"[!] Email failed: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    seen_ids      = load_seen_ids()
    companies     = load_companies()
    all_current_jobs: list = []
    counter       = [0]
    lock          = threading.Lock()
    csv_lock      = threading.Lock()

    print(f"[+] iCIMS scraper started — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"    max age: {MAX_AGE_DAYS}d | companies: {len(companies)} | workers: {WORKERS}\n")

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(process_company, company, seen_ids, all_current_jobs, lock, csv_lock, counter): company
            for company in companies
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  [!] Unhandled error: {e}")

    new_count = counter[0]
    save_seen_ids(seen_ids)

    print(f"\n{'='*65}")
    print(f"[+] Done — {new_count} new job(s) found across {len(companies)} companies")

    if new_count:
        with lock:
            jobs_to_send = [j for j in all_current_jobs if j.get("is_new")]
        jobs_to_send.sort(key=lambda j: j.get("posted", ""), reverse=True)
        send_summary_email(jobs_to_send, new_count)
    else:
        print("[i] No new jobs — skipping email.")

if __name__ == "__main__":
    main()
