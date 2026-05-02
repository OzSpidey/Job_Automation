"""
oracle_scraper.py

Scrapes public Oracle Recruiting Cloud (ORC) career APIs for Data Engineer / Analyst / BI roles.
No login or account required — uses the same REST endpoints that Oracle Cloud career pages call.

To add a company:
  1. Visit the company's careers page and look for a URL like:
       https://{pod}.fa.{region}.oraclecloud.com/hcmUI/CandidateExperience/en/sites/{site}/
  2. Extract: pod (4-char code), region (e.g. "us2"), site name (e.g. "Pella-Careers")
  3. Open DevTools → Network → filter by "recruitingCEJobRequisitions" to confirm API access
  4. Add to oracle_companies.json:
       { "name": "...", "pod": "...", "region": "us2", "site": "...", "site_number": "" }
     Leave site_number blank — the scraper will auto-discover it.
"""

import argparse
import csv
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Role profiles ──────────────────────────────────────────────────────────────
_ROLES = {
    "de": {
        "label":        "Data Engineer",
        "search_terms": ["Data Engineer"],
        "allow_re":     re.compile(r"\bdata\s+engineer\b", re.I),
        "seen_log":     "oracle_seen_de.json",
        "output_csv":   "oracle_jobs_de.csv",
    },
    "da": {
        "label":        "Data Analyst",
        "search_terms": ["Data Analyst"],
        "allow_re":     re.compile(r"\bdata\s+analyst\b", re.I),
        "seen_log":     "oracle_seen_da.json",
        "output_csv":   "oracle_jobs_da.csv",
    },
    "bi": {
        "label":        "Business Intelligence",
        "search_terms": ["Business Intelligence"],
        "allow_re":     re.compile(r"\bbusiness\s+intelligence\b", re.I),
        "seen_log":     "oracle_seen_bi.json",
        "output_csv":   "oracle_jobs_bi.csv",
    },
    "bia": {
        "label":        "BI Analyst",
        "search_terms": ["BI Analyst"],
        "allow_re":     re.compile(r"\bbi\s+(analyst|developer|engineer|specialist)\b", re.I),
        "seen_log":     "oracle_seen_bia.json",
        "output_csv":   "oracle_jobs_bia.csv",
    },
    "ra": {
        "label":        "Reporting Analyst",
        "search_terms": ["Reporting Analyst"],
        "allow_re":     re.compile(r"\breporting\s+analyst\b", re.I),
        "seen_log":     "oracle_seen_ra.json",
        "output_csv":   "oracle_jobs_ra.csv",
    },
    "aa": {
        "label":        "Analytics Analyst",
        "search_terms": ["Analytics Analyst"],
        "allow_re":     re.compile(r"\banalytics\s+analyst\b", re.I),
        "seen_log":     "oracle_seen_aa.json",
        "output_csv":   "oracle_jobs_aa.csv",
    },
    "ds": {
        "label":        "Data Scientist",
        "search_terms": ["Data Scientist"],
        "allow_re":     re.compile(r"\bdata\s+scientist\b", re.I),
        "seen_log":     "oracle_seen_ds.json",
        "output_csv":   "oracle_jobs_ds.csv",
    },
    "sd": {
        "label":        "Software Developer",
        "search_terms": ["Software Developer"],
        "allow_re":     re.compile(r"\bsoftware\s+developer\b", re.I),
        "seen_log":     "oracle_seen_sd.json",
        "output_csv":   "oracle_jobs_sd.csv",
    },
    "se": {
        "label":        "Software Engineer",
        "search_terms": ["Software Engineer"],
        "allow_re":     re.compile(r"\bsoftware\s+engineer\b", re.I),
        "seen_log":     "oracle_seen_se.json",
        "output_csv":   "oracle_jobs_se.csv",
    },
    "aie": {
        "label":        "AI Engineer",
        "search_terms": ["AI Engineer"],
        "allow_re":     re.compile(r"\bai\s+engineer\b", re.I),
        "seen_log":     "oracle_seen_aie.json",
        "output_csv":   "oracle_jobs_aie.csv",
    },
}

# ── Parse role argument ────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--role", choices=list(_ROLES.keys()), default=None)
_args, _ = _parser.parse_known_args()

if _args.role:
    _profile     = _ROLES[_args.role]
    SEARCH_TERMS = _profile["search_terms"]
    ALLOWED_TITLE_RE = _profile["allow_re"]
    _seen_file   = _profile["seen_log"]
    _csv_file    = _profile["output_csv"]
    _role_label  = _profile["label"]
else:
    SEARCH_TERMS = ["Data Engineer", "Data Analyst", "Business Intelligence Analyst"]
    ALLOWED_TITLE_RE = re.compile(
        r"\b(data\s+engineer|data\s+analyst|business\s+intelligence)\b", re.I
    )
    _seen_file  = "oracle_seen_ids.json"
    _csv_file   = "oracle_jobs.csv"
    _role_label = "DE / DA / BI"

# ── Config ─────────────────────────────────────────────────────────────────────
MAX_AGE_DAYS   = 1
MAX_JOBS       = 10
REQUEST_DELAY  = 2.0
RESULTS_LIMIT  = 25

OUTPUT_CSV      = Path(__file__).parent / "csv" / _csv_file
SEEN_LOG        = Path(__file__).parent / "json" / _seen_file
COMPANIES_FILE  = Path(__file__).parent / "json" / "oracle_companies.json"

# ── Email config ───────────────────────────────────────────────────────────────
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

# ── Title filters ──────────────────────────────────────────────────────────────
SKIP_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|lead|manager|principal|staff|director|head|vp|"
    r"architect|consultant|iii|iv)\b",
    re.I,
)
ENTRY_LEVEL_RE = re.compile(
    r"\b(junior|jr\.?|associate|entry[\s\-]level|new\s+grad|graduate)\b", re.I
)

# ── Location filter ────────────────────────────────────────────────────────────
_US_STATES = (
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    r"MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
US_LOCATION_RE = re.compile(
    rf"\b(united\s+states|usa|u\.s\.a?\.?|remote|{_US_STATES})\b", re.I
)

# ── Default company list ───────────────────────────────────────────────────────
# pod  = 4-char Oracle tenant code (from the career page URL)
# region = Oracle data-center region ("us2", "us6", "eu2", etc.)
# site = site name as it appears in the career page URL path
# site_number = Oracle internal site ID; leave "" to auto-discover (usually "CX_1")
DEFAULT_COMPANIES = [
    {
        "name":        "Pella Corporation",
        "pod":         "ebgj",
        "region":      "us2",
        "site":        "Pella-Careers",
        "site_number": "",
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Persistence ────────────────────────────────────────────────────────────────

def load_seen_ids() -> set:
    if SEEN_LOG.exists():
        try:
            data = json.loads(SEEN_LOG.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set(data.keys())
        except Exception:
            pass
    return set()


def save_seen_ids(ids: set) -> None:
    SEEN_LOG.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


def append_csv(row: dict) -> None:
    fieldnames = ["title", "company", "location", "posted", "link", "found_on"]
    write_header = not OUTPUT_CSV.exists() or OUTPUT_CSV.stat().st_size == 0
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def load_companies() -> list[dict]:
    if not COMPANIES_FILE.exists():
        COMPANIES_FILE.write_text(
            json.dumps(DEFAULT_COMPANIES, indent=2), encoding="utf-8"
        )
        print(f"[+] Created {COMPANIES_FILE.name} with {len(DEFAULT_COMPANIES)} companies.")
    return json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))


# ── Filters ────────────────────────────────────────────────────────────────────

def is_allowed_title(title: str) -> bool:
    if SKIP_TITLE_RE.search(title):
        return False
    return bool(ALLOWED_TITLE_RE.search(title))


def is_entry_level(title: str) -> bool:
    return bool(ENTRY_LEVEL_RE.search(title))


def is_us_location(location: str) -> bool:
    if not location.strip():
        return True
    return bool(US_LOCATION_RE.search(location))


def posted_days_ago(posted_date_str: str) -> int:
    """Parse ISO date string '2026-04-29' to number of days ago."""
    if not posted_date_str:
        return 999
    try:
        date_part = str(posted_date_str)[:10]  # "YYYY-MM-DD"
        posted = datetime.strptime(date_part, "%Y-%m-%d").date()
        return (date.today() - posted).days
    except Exception:
        return 999


def format_posted_text(posted_date_str: str) -> str:
    days = posted_days_ago(posted_date_str)
    if days == 0:
        return "Posted Today"
    if days == 1:
        return "Posted 1 Day Ago"
    return f"Posted {days} Days Ago"


# ── Oracle Cloud API ───────────────────────────────────────────────────────────

def build_base_url(pod: str, region: str) -> str:
    return f"https://{pod}.fa.{region}.oraclecloud.com"


def build_jobs_url(pod: str, region: str, site_number: str, keyword: str, limit: int, offset: int) -> str:
    keyword_enc = urllib.parse.quote_plus(keyword)
    finder = (
        f"findReqs;siteNumber={site_number},"
        f"keyword={keyword_enc},"
        f"limit={limit},offset={offset},"
        f"sortBy=POSTING_DATES_DESC"
    )
    base = build_base_url(pod, region)
    return f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&finder={finder}&expand=requisitionList&limit={limit}&offset={offset}"


def build_sites_url(pod: str, region: str) -> str:
    base = build_base_url(pod, region)
    return f"{base}/hcmRestApi/resources/latest/recruitingCESites?onlyData=true"


def build_job_url(company: dict, job_id, external_desc_id: str = "") -> str:
    pod    = company["pod"]
    region = company["region"]
    site   = company["site"]
    suffix = f"?jr_id={external_desc_id}" if external_desc_id else ""
    return (
        f"https://{pod}.fa.{region}.oraclecloud.com"
        f"/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}{suffix}"
    )


def discover_site_number(pod: str, region: str, site: str) -> str:
    """Call the ORC sites endpoint to find the siteNumber for a given site name."""
    url = build_sites_url(pod, region)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            if not items:
                return "CX_1"
            # Prefer active sites; fall back to all if none are active
            active = [s for s in items if s.get("StatusCode") != "ORA_INACTIVE"]
            candidates = active if active else items
            site_lower = site.lower().replace("-", " ")
            for s in candidates:
                code = str(s.get("SiteCode", "") or s.get("SiteName", "")).lower().replace("-", " ")
                if site_lower in code or code in site_lower:
                    return s.get("SiteNumber", "CX_1")
            # No name match — use the first active site
            if len(candidates) == 1:
                return candidates[0].get("SiteNumber", "CX_1")
            return candidates[0].get("SiteNumber", "CX_1")
    except Exception:
        pass
    return "CX_1"


def fetch_page(pod: str, region: str, site_number: str, keyword: str, limit: int, offset: int) -> tuple[int, list, int]:
    url = build_jobs_url(pod, region, site_number, keyword, limit, offset)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            meta = (data.get("items") or [{}])[0]
            total = meta.get("TotalJobsCount", 0)
            req = meta.get("requisitionList") or []
            items = req if isinstance(req, list) else req.get("items", [])
            return 200, items, total
        return resp.status_code, [], 0
    except requests.exceptions.Timeout:
        return -1, [], 0
    except requests.exceptions.ConnectionError:
        return -2, [], 0
    except Exception:
        return -3, [], 0


def fetch_jobs(company: dict, keyword: str) -> list[dict]:
    """Fetch all matching jobs for a company/keyword, with pagination and site-number discovery."""
    pod    = company["pod"]
    region = company["region"]

    # Ensure site_number is set
    if not company.get("site_number"):
        sn = discover_site_number(pod, region, company["site"])
        company["site_number"] = sn
        print(f"  [discovered] {company['name']} siteNumber: {sn}")

    site_number = company["site_number"]
    all_jobs: list[dict] = []
    offset = 0

    while True:
        status, items, total = fetch_page(pod, region, site_number, keyword, RESULTS_LIMIT, offset)

        if status == 200:
            if not items:
                break
            all_jobs.extend(items)
            offset += len(items)
            # Stop if we've retrieved all results or hit age threshold on oldest item
            if offset >= total:
                break
            # Early-exit: if the last item on this page is already too old, don't paginate
            last_posted = items[-1].get("PostedDate", "")
            if last_posted and posted_days_ago(last_posted) > MAX_AGE_DAYS:
                break
            time.sleep(0.5)
        elif status in (-1,):
            print(f"  [skip] {company['name']} — timeout")
            break
        elif status in (-2, -3):
            print(f"  [skip] {company['name']} — connection error")
            break
        elif status == 404:
            print(f"  [skip] {company['name']} — endpoint not found (check pod/region/site)")
            break
        elif status == 401:
            print(f"  [skip] {company['name']} — requires auth (private instance)")
            break
        else:
            print(f"  [skip] {company['name']} — HTTP {status}")
            break

    return all_jobs


# ── Email summary ──────────────────────────────────────────────────────────────

def send_summary_email(all_jobs: list[dict], new_count: int) -> None:
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set — skipping email.")
        return
    if not all_jobs:
        print("[i] No jobs to send — skipping email.")
        return

    def _row(j):
        badges = ""
        if j.get("is_new"):
            badges += "&nbsp;<span style='background:#2e7d32;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px'>NEW</span>"
        if j.get("entry_level"):
            badges += "&nbsp;<span style='background:#1565c0;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px'>ENTRY</span>"
        if j.get("is_new") and j.get("entry_level"):
            bg = "#e3f2fd"
        elif j.get("is_new"):
            bg = "#f1f8e9"
        else:
            bg = ""
        return (
            f"<tr style='background:{bg}'>"
            f"<td>{j['title']}{badges}</td>"
            f"<td>{j['company']}</td>"
            f"<td>{j['location']}</td>"
            f"<td>{j['posted']}</td>"
            f"<td><a href='{j['link']}'>Apply</a></td>"
            f"</tr>"
        )

    rows    = "".join(_row(j) for j in all_jobs)
    subject = (
        f"[Oracle] {new_count} new {_role_label} role(s) — "
        f"{datetime.now().strftime('%b %d, %Y %H:%M')}"
    )
    body_html = f"""
    <h2>Oracle Recruiting Cloud — {_role_label} Jobs (Last {MAX_AGE_DAYS} Day)</h2>
    <p><b>{new_count} new role(s)</b> found. All listings from the last {MAX_AGE_DAYS} day(s) shown — new ones highlighted in green.</p>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Link</th>
      </tr>
      {rows}
    </table>
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
        print(f"[+] Summary email sent to {EMAIL_TO}")
    except Exception as e:
        print(f"[!] Email failed: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    seen_ids  = load_seen_ids()
    companies = load_companies()
    all_current_jobs: list[dict] = []
    new_count = 0

    print(f"[+] Oracle scraper started — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"    Searching: {SEARCH_TERMS} | max age: {MAX_AGE_DAYS}d | companies: {len(companies)}\n")

    for company in companies:
        print(f"[→] {company['name']}")

        all_postings: list[dict] = []
        seen_req_ids: set        = set()

        for term in SEARCH_TERMS:
            for job in fetch_jobs(company, term):
                job_id = str(job.get("Id") or job.get("RequisitionNumber", ""))
                if job_id not in seen_req_ids:
                    seen_req_ids.add(job_id)
                    all_postings.append(job)
            time.sleep(0.5)

        if not all_postings:
            time.sleep(REQUEST_DELAY)
            continue

        matched_new = 0
        for job in all_postings:
            title       = str(job.get("Title", "")).strip()
            location    = str(job.get("PrimaryLocation", "")).strip()
            posted_date = str(job.get("PostedDate", "")).strip()
            job_id      = job.get("Id") or job.get("RequisitionNumber", "")
            ext_desc_id = str(job.get("ExternalDescriptionId", ""))

            unique_id = f"{company['pod']}_{job_id}"

            if not is_allowed_title(title):
                continue
            if not is_us_location(location):
                continue

            age = posted_days_ago(posted_date)
            if age > MAX_AGE_DAYS:
                continue

            posted_text = format_posted_text(posted_date)
            job_url     = build_job_url(company, job_id, ext_desc_id)
            is_new      = unique_id not in seen_ids

            row = {
                "title":       title,
                "company":     company["name"],
                "location":    location,
                "posted":      posted_text,
                "link":        job_url,
                "found_on":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "is_new":      is_new,
                "entry_level": is_entry_level(title),
                "age_days":    age,
            }
            all_current_jobs.append(row)

            if is_new:
                append_csv(row)
                seen_ids.add(unique_id)
                matched_new += 1
                new_count   += 1
                print(f"    [+] NEW: {title} | {location} | {posted_text}")
                if new_count >= MAX_JOBS:
                    print(f"\n[!] Reached MAX_JOBS={MAX_JOBS} cap — stopping early.")
                    break

        if matched_new == 0:
            print(f"    [–] No new matches")

        if new_count >= MAX_JOBS:
            break

        time.sleep(REQUEST_DELAY)

    save_seen_ids(seen_ids)
    # Persist any site_numbers discovered during this run
    COMPANIES_FILE.write_text(json.dumps(companies, indent=2), encoding="utf-8")

    print(f"\n{'='*65}")
    print(f"[+] Done — {new_count} new job(s) found across {len(companies)} companies")
    if new_count:
        print(f"    Saved → {OUTPUT_CSV.name}")

    if new_count:
        all_current_jobs.sort(key=lambda j: j["age_days"])
        send_summary_email(all_current_jobs, new_count)
    else:
        print("[i] No new jobs — skipping email.")


if __name__ == "__main__":
    main()
