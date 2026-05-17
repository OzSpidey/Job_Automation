"""
twosigma_scraper.py

Scrapes Two Sigma's public Avature career portal for data/analytics/engineering roles.
URL: https://careers.twosigma.com/careers/SearchJobs
Pagination via jobOffset query param; job titles parsed from HTML.

Avature renders server-side HTML — no JS execution needed, no API key required.
"""

import csv
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_URL      = "https://twosigma.avature.net/careers/SearchJobs"
PAGE_SIZE     = 25
MAX_PAGES     = 20
REQUEST_DELAY = 1.0

SEEN_LOG      = Path(__file__).parent / "json" / "twosigma_seen_jobs.json"
OUTPUT_CSV    = Path(__file__).parent / "csv"  / "twosigma_jobs.csv"
PRUNE_DAYS    = 7

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Filters ────────────────────────────────────────────────────────────────────
ALLOWED_RE = re.compile(
    r"("
    r"\bdata\b.{0,40}\banalyst\b"
    r"|\banalyst\b.{0,40}\bdata\b"
    r"|\bdata\b.{0,40}\bengineer\b"
    r"|\bengineer\b.{0,40}\bdata\b"
    r"|\bai\b.{0,20}\bengineer\b"
    r"|\bengineer\b.{0,20}\bai\b"
    r"|\banalytics\b"
    r"|\bBI\b"
    r"|\bbusiness\b.{0,30}\bintelligence\b"
    r"|\bintelligence\b.{0,30}\bbusiness\b"
    r")",
    re.I,
)
SKIP_RE = re.compile(
    r"\b(senior|sr\.?|lead|manager|principal|staff|director|head|vp|"
    r"architect|consultant|iii|iv)\b",
    re.I,
)
NOISE_RE = re.compile(r"\b(data\s+center|payroll|medical\s+coding|bilingual)\b", re.I)
ENTRY_RE = re.compile(r"\b(junior|jr\.?|associate|entry[\s\-]level|new\s+grad|graduate|intern)\b", re.I)

_CLASSIFY = [
    (re.compile(r"\bdata\b.{0,40}\bengineer\b|\bengineer\b.{0,40}\bdata\b", re.I), "Data Engineer"),
    (re.compile(r"\bai\b.{0,20}\bengineer\b|\bengineer\b.{0,20}\bai\b",     re.I), "AI Engineer"),
    (re.compile(r"\bbusiness\b.{0,30}\bintelligence\b|\bBI\b",               re.I), "Business Intelligence"),
    (re.compile(r"\bdata\b.{0,40}\banalyst\b|\banalyst\b.{0,40}\bdata\b",   re.I), "Data Analyst"),
    (re.compile(r"\banalytics\b",                                             re.I), "Analytics"),
    (re.compile(r"\bdata\b",                                                  re.I), "Data (Other)"),
]

def classify(title: str) -> str:
    for pat, label in _CLASSIFY:
        if pat.search(title):
            return label
    return "Other"

def is_allowed(title: str) -> bool:
    if SKIP_RE.search(title):
        return False
    if NOISE_RE.search(title):
        return False
    return bool(ALLOWED_RE.search(title))

def is_entry(title: str) -> bool:
    return bool(ENTRY_RE.search(title))

# ── Persistence ─────────────────────────────────────────────────────────────────
def load_seen() -> dict:
    if not SEEN_LOG.exists():
        return {}
    raw = json.loads(SEEN_LOG.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        raw = {jid: today for jid in raw}
    cutoff = (datetime.utcnow() - timedelta(days=PRUNE_DAYS)).strftime("%Y-%m-%d")
    return {jid: dt for jid, dt in raw.items() if dt >= cutoff}

def save_seen(seen: dict) -> None:
    SEEN_LOG.parent.mkdir(parents=True, exist_ok=True)
    SEEN_LOG.write_text(json.dumps(seen, indent=2, sort_keys=True), encoding="utf-8")

CSV_COLS = ["title", "company", "location", "role", "posted", "link", "found_on", "entry_level"]

def append_csv(row: dict) -> None:
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not OUTPUT_CSV.exists()
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        if write_header:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_COLS})

# ── Scraping ────────────────────────────────────────────────────────────────────
def fetch_page(session: requests.Session, offset: int) -> list[dict]:
    """Fetch one page of Avature results; return list of {id, title, location, url}."""
    params = {
        "jobRecordsPerPage": PAGE_SIZE,
        "jobOffset": offset,
    }
    try:
        resp = session.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            print(f"  [!] HTTP {resp.status_code} at offset {offset}")
            return []
    except Exception as e:
        print(f"  [!] Request failed at offset {offset}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    jobs = []

    # Primary: links containing /careers/JobDetail/ or /careers/ViewJob/
    for a in soup.find_all("a", href=re.compile(r"/(JobDetail|ViewJob)/", re.I)):
        href = a.get("href", "")
        if not href.startswith("http"):
            href = "https://twosigma.avature.net" + href

        m = re.search(r"/(JobDetail|ViewJob)/([^/?#]+)", href, re.I)
        job_id = m.group(2) if m else href

        title = a.get_text(separator=" ", strip=True)
        if not title or len(title) < 3:
            continue

        parent = a.find_parent(["li", "div", "article"])
        location = ""
        if parent:
            loc_el = parent.find(class_=re.compile(r"location|city|region", re.I))
            if loc_el:
                location = loc_el.get_text(strip=True)

        jobs.append({"id": job_id, "title": title, "location": location or "US", "url": href})

    # Fallback: job title elements by common Avature class names
    if not jobs:
        for el in soup.find_all(class_=re.compile(r"job.?title|position.?title", re.I)):
            text = el.get_text(strip=True)
            link = el.find("a")
            if link and text:
                href = link.get("href", "")
                if not href.startswith("http"):
                    href = "https://twosigma.avature.net" + href
                m = re.search(r"/(\d+)/?$", href)
                job_id = m.group(1) if m else href
                jobs.append({"id": job_id, "title": text, "location": "US", "url": href})

    return jobs

def scrape_all() -> list[dict]:
    session = requests.Session()
    all_jobs = []
    seen_ids_this_run = set()

    print(f"[+] Two Sigma scraper — fetching up to {MAX_PAGES} pages ({PAGE_SIZE}/page)")
    for page_num in range(MAX_PAGES):
        offset = page_num * PAGE_SIZE
        jobs = fetch_page(session, offset)
        if not jobs:
            print(f"  [page {page_num+1}] empty or error — stopping")
            break

        new_on_page = 0
        for j in jobs:
            if j["id"] not in seen_ids_this_run:
                seen_ids_this_run.add(j["id"])
                all_jobs.append(j)
                new_on_page += 1

        print(f"  [page {page_num+1}] offset={offset} fetched={len(jobs)} unique={new_on_page} total={len(all_jobs)}")

        if len(jobs) < PAGE_SIZE:
            break  # last page

        if page_num < MAX_PAGES - 1:
            time.sleep(REQUEST_DELAY)

    print(f"[+] Total unique jobs fetched: {len(all_jobs)}")
    return all_jobs

# ── Email ────────────────────────────────────────────────────────────────────────
def send_email(new_jobs: list, new_count: int) -> None:
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set — skipping email.")
        return

    today = datetime.now().strftime("%Y-%m-%d")

    def _row(j):
        badges = ""
        if j.get("entry_level"):
            badges += "&nbsp;<span style='background:#1565c0;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px'>ENTRY</span>"
        bg = "#f1f8e9" if j.get("posted") == today else ""
        return (
            f"<tr style='background:{bg}'>"
            f"<td>{j['title']}{badges}</td>"
            f"<td>Two Sigma</td>"
            f"<td>{j.get('location','')}</td>"
            f"<td style='white-space:nowrap'>{j.get('posted','')}</td>"
            f"<td><a href='{j['link']}'>Apply</a></td>"
            f"</tr>"
        )

    by_role: dict = {}
    for j in new_jobs:
        by_role.setdefault(j.get("role", "Other"), []).append(j)

    sections = ""
    for role_label, jobs in sorted(by_role.items()):
        jobs = sorted(jobs, key=lambda j: j.get("title", ""))
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

    subject = f"[Two Sigma] {new_count} new role(s) — {datetime.now().strftime('%b %d, %Y %H:%M')}"
    body_html = f"""
    <h2>Two Sigma — New Roles</h2>
    <p><b>{new_count} new role(s)</b> found.
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

# ── Main ────────────────────────────────────────────────────────────────────────
def main() -> None:
    seen = load_seen()
    print(f"[+] Two Sigma scraper started — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"    seen: {len(seen)} | prune: {PRUNE_DAYS}d\n")

    raw_jobs = scrape_all()
    today    = datetime.now().strftime("%Y-%m-%d")
    new_jobs = []

    for j in raw_jobs:
        title = j["title"]
        if not is_allowed(title):
            print(f"  [–] skip: {title}")
            continue
        job_id = f"twosigma_{j['id']}"
        if job_id in seen:
            continue

        seen[job_id] = datetime.utcnow().strftime("%Y-%m-%d")
        row = {
            "title":       title,
            "company":     "Two Sigma",
            "location":    j.get("location", "US"),
            "role":        classify(title),
            "posted":      today,
            "link":        j["url"],
            "found_on":    datetime.now().strftime("%Y-%m-%d %H:%M"),
            "entry_level": is_entry(title),
        }
        new_jobs.append(row)
        append_csv(row)
        print(f"  [+] NEW: {title}")

    save_seen(seen)
    new_count = len(new_jobs)
    print(f"\n{'='*60}")
    print(f"[+] Done — {new_count} new Two Sigma role(s)")

    if new_jobs:
        send_email(new_jobs, new_count)
    else:
        print("[i] No new jobs — skipping email.")

if __name__ == "__main__":
    main()
