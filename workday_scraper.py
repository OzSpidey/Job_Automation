"""
workday_scraper.py

Scrapes public Workday career APIs for Data Engineer roles.
No login or account required — uses the same JSON endpoints Workday career pages call.

To add a company:
  1. Go to the company's careers page
  2. DevTools → Network tab → filter by "jobs" → find POST to *.myworkdayjobs.com
  3. The URL pattern: {tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{career}/jobs
  4. Add an entry to workday_companies.json
"""

import argparse
import csv
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
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
        "seen_log":     "workday_seen_de.json",
        "output_csv":   "workday_jobs_de.csv",
    },
    "da": {
        "label":        "Data Analyst",
        "search_terms": ["Data Analyst"],
        "allow_re":     re.compile(r"\bdata\s+analyst\b", re.I),
        "seen_log":     "workday_seen_da.json",
        "output_csv":   "workday_jobs_da.csv",
    },
    "bi": {
        "label":        "Business Intelligence",
        "search_terms": ["Business Intelligence"],
        "allow_re":     re.compile(r"\bbusiness\s+intelligence\b", re.I),
        "seen_log":     "workday_seen_bi.json",
        "output_csv":   "workday_jobs_bi.csv",
    },
    "bia": {
        "label":        "BI Analyst",
        "search_terms": ["BI Analyst"],
        "allow_re":     re.compile(r"\bbi\s+(analyst|developer|engineer|specialist)\b", re.I),
        "seen_log":     "workday_seen_bia.json",
        "output_csv":   "workday_jobs_bia.csv",
    },
    "ra": {
        "label":        "Reporting Analyst",
        "search_terms": ["Reporting Analyst"],
        "allow_re":     re.compile(r"\breporting\s+analyst\b", re.I),
        "seen_log":     "workday_seen_ra.json",
        "output_csv":   "workday_jobs_ra.csv",
    },
    "aa": {
        "label":        "Analytics Analyst",
        "search_terms": ["Analytics Analyst"],
        "allow_re":     re.compile(r"\banalytics\s+analyst\b", re.I),
        "seen_log":     "workday_seen_aa.json",
        "output_csv":   "workday_jobs_aa.csv",
    },
    "ds": {
        "label":        "Data Scientist",
        "search_terms": ["Data Scientist"],
        "allow_re":     re.compile(r"\bdata\s+scientist\b", re.I),
        "seen_log":     "workday_seen_ds.json",
        "output_csv":   "workday_jobs_ds.csv",
    },
    "sd": {
        "label":        "Software Developer",
        "search_terms": ["Software Developer"],
        "allow_re":     re.compile(r"\bsoftware\s+developer\b", re.I),
        "seen_log":     "workday_seen_sd.json",
        "output_csv":   "workday_jobs_sd.csv",
    },
    "se": {
        "label":        "Software Engineer",
        "search_terms": ["Software Engineer"],
        "allow_re":     re.compile(r"\bsoftware\s+engineer\b", re.I),
        "seen_log":     "workday_seen_se.json",
        "output_csv":   "workday_jobs_se.csv",
    },
    "aie": {
        "label":        "AI Engineer",
        "search_terms": ["AI Engineer"],
        "allow_re":     re.compile(r"\bai\s+engineer\b", re.I),
        "seen_log":     "workday_seen_aie.json",
        "output_csv":   "workday_jobs_aie.csv",
    },
}

# ── Parse role argument ────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--role", choices=["de", "da", "bi", "bia", "ra", "aa", "ds", "sd", "se", "aie"], default=None)
_parser.add_argument("--batch", choices=["1", "2"], default=None)
_args, _ = _parser.parse_known_args()

if _args.role:
    _profile     = _ROLES[_args.role]
    SEARCH_TERMS = _profile["search_terms"]
    ALLOWED_TITLE_RE = _profile["allow_re"]
    _seen_file   = _profile["seen_log"]
    _csv_file    = _profile["output_csv"]
    _role_label  = _profile["label"]
else:
    # No --role: search all three (original behaviour)
    SEARCH_TERMS = ["Data Engineer", "Data Analyst", "Business Intelligence Analyst"]
    ALLOWED_TITLE_RE = re.compile(
        r"\b(data\s+engineer|data\s+analyst|business\s+intelligence)\b",
        re.I,
    )
    _seen_file  = "workday_seen_ids.json"
    _csv_file   = "workday_jobs.csv"
    _role_label = "DE / DA / BI"

if _args.batch:
    _seen_file  = _seen_file.replace(".json", f"_{_args.batch}.json")
    _csv_file   = _csv_file.replace(".csv",  f"_{_args.batch}.csv")
    _role_label = f"{_role_label} (batch {_args.batch})"

# ── Config ─────────────────────────────────────────────────────────────────────
MAX_AGE_DAYS  = 3    # skip jobs older than this (Workday shows "Posted X Days Ago")
REQUEST_DELAY = 2.0  # seconds between company requests
RESULTS_LIMIT = 20   # jobs to fetch per company (first page only)

OUTPUT_CSV     = Path(__file__).parent / "csv" / _csv_file
SEEN_LOG       = Path(__file__).parent / "json" / _seen_file
COMPANIES_FILE = Path(__file__).parent / "json" / "workday_companies.json"

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

# Titles with these signals are preferred entry-level roles
ENTRY_LEVEL_RE = re.compile(
    r"\b(junior|jr\.?|associate|entry[\s\-]level|new\s+grad|graduate)\b",
    re.I,
)

# ── Location filter ────────────────────────────────────────────────────────────
_US_STATES = (
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    r"MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
US_LOCATION_RE = re.compile(
    rf"\b(united\s+states|usa|u\.s\.a?\.?|remote|{_US_STATES})\b", re.I
)

# ── Age parser — "Posted 3 Days Ago" / "Posted Today" / "Posted 30+ Days Ago" ──
POSTED_DAYS_RE = re.compile(r"(\d+)\+?\s+day", re.I)


# ── Default company list (written to workday_companies.json on first run) ──────
DEFAULT_COMPANIES = [
    {"name": "Salesforce",      "tenant": "salesforce",      "instance": "wd12", "career": "External_Career_Site"},
    {"name": "Target",          "tenant": "target",          "instance": "wd5",  "career": "WD"},
    {"name": "Nike",            "tenant": "nike",            "instance": "wd1",  "career": "CorporateCareers"},
    {"name": "Accenture",       "tenant": "accenture",       "instance": "wd3",  "career": "AccentureCareers"},
    {"name": "Deloitte",        "tenant": "deloitte",        "instance": "wd1",  "career": "careers"},
    {"name": "EY",              "tenant": "ey",              "instance": "wd5",  "career": "ey"},
    {"name": "Spotify",         "tenant": "spotify",         "instance": "wd14", "career": "spotify"},
    {"name": "Lyft",            "tenant": "lyft",            "instance": "wd5",  "career": "lyft"},
    {"name": "DocuSign",        "tenant": "docusign",        "instance": "wd5",  "career": "DocuSign"},
    {"name": "Workday",         "tenant": "workday",         "instance": "wd5",  "career": "Workday"},
    {"name": "Okta",            "tenant": "okta",            "instance": "wd5",  "career": "okta"},
    {"name": "ServiceNow",      "tenant": "servicenow",      "instance": "wd5",  "career": "External"},
    {"name": "Twilio",          "tenant": "twilio",          "instance": "wd5",  "career": "twilio"},
    {"name": "Stripe",          "tenant": "stripe",          "instance": "wd5",  "career": "stripe"},
    {"name": "Airbnb",          "tenant": "airbnb",          "instance": "wd5",  "career": "Airbnb"},
    {"name": "Robinhood",       "tenant": "robinhood",       "instance": "wd5",  "career": "Robinhood"},
    {"name": "Coinbase",        "tenant": "coinbase",        "instance": "wd5",  "career": "coinbase"},
    {"name": "Wayfair",         "tenant": "wayfair",         "instance": "wd5",  "career": "Wayfair"},
    {"name": "DraftKings",      "tenant": "draftkings",      "instance": "wd1",  "career": "DraftKings"},
    {"name": "Toast",           "tenant": "toast",           "instance": "wd5",  "career": "ToastCareers"},
    {"name": "HubSpot",         "tenant": "hubspot",         "instance": "wd5",  "career": "HubSpot"},
    {"name": "Rapid7",          "tenant": "rapid7",          "instance": "wd5",  "career": "Rapid7"},
    {"name": "Klaviyo",         "tenant": "klaviyo",         "instance": "wd5",  "career": "klaviyo"},
    {"name": "Fidelity",        "tenant": "fidelity",        "instance": "wd5",  "career": "Fidelity"},
    {"name": "State Street",    "tenant": "statestreet",     "instance": "wd5",  "career": "StateStreet"},
    {"name": "Liberty Mutual",  "tenant": "libertymutual",   "instance": "wd5",  "career": "LibertyMutual"},
]


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
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
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
        return True  # blank = don't filter out
    return bool(US_LOCATION_RE.search(location))


def posted_days_ago(posted_text: str) -> int:
    """Parse 'Posted 3 Days Ago' → 3. 'Posted Today' → 0. '30+' → 31."""
    text = posted_text.lower()
    if "today" in text or "just now" in text or "hour" in text:
        return 0
    m = POSTED_DAYS_RE.search(text)
    if m:
        n = int(m.group(1))
        return n + 1 if "+" in text else n
    return 999  # unknown = treat as old


# ── Workday API ────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Tried in order when the configured career path returns 422 or 404
CAREER_PATH_FALLBACKS = [
    "External_Career_Site",
    "careers",
    "Careers",
    "External",
    "external",
    "JobBoard",
    "CareersExternal",
]

WD_INSTANCE_FALLBACKS = ["wd1", "wd3", "wd5", "wd12", "wd14", "wd501"]


def build_api_url(tenant: str, instance: str, career: str) -> str:
    return f"https://{tenant}.{instance}.myworkdayjobs.com/wday/cxs/{tenant}/{career}/jobs"


def build_job_url(company: dict, external_path: str) -> str:
    t = company["tenant"]
    i = company["instance"]
    c = company["career"]
    return f"https://{t}.{i}.myworkdayjobs.com/en-US/{c}{external_path}"


def _post(url: str, search: str, offset: int) -> tuple[int, list, int]:
    """Single POST attempt. Returns (status_code, job_list, total)."""
    payload = {
        "limit":         RESULTS_LIMIT,
        "offset":        offset,
        "searchText":    search,
        "locations":     [],
        "appliedFacets": {},
    }
    try:
        resp = requests.post(url, json=payload, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return 200, data.get("jobPostings", []), data.get("total", 0)
        return resp.status_code, [], 0
    except requests.exceptions.Timeout:
        return -1, [], 0
    except requests.exceptions.ConnectionError:
        return -2, [], 0
    except Exception:
        return -3, [], 0


def fetch_jobs(company: dict, search: str) -> list[dict]:
    """
    Fetch all pages of jobs for a company/search term. If the configured career
    path fails with 422/404, automatically tries common fallback paths and
    instance numbers. Saves a discovered working config back to the company dict.
    """
    t = company["tenant"]
    i = company["instance"]
    c = company["career"]

    url = build_api_url(t, i, c)
    status, jobs, total = _post(url, search, 0)

    if status != 200:
        if status in (-1,):
            print(f"  [skip] {company['name']} — timeout")
            return []
        if status in (-2, -3):
            print(f"  [skip] {company['name']} — connection error")
            return []
        if status == 401:
            print(f"  [skip] {company['name']} — requires auth (private Workday)")
            return []

        # 422 = career path wrong, 404 = tenant/instance wrong — try discovery
        if status in (422, 404):
            found = False
            for fallback_career in CAREER_PATH_FALLBACKS:
                if fallback_career == c:
                    continue
                test_url = build_api_url(t, i, fallback_career)
                s, jobs, total = _post(test_url, search, 0)
                if s == 200:
                    print(f"  [discovered] {company['name']} career path: {fallback_career}")
                    company["career"] = fallback_career
                    url = test_url
                    found = True
                    break

            if not found:
                for fallback_instance in WD_INSTANCE_FALLBACKS:
                    if fallback_instance == i:
                        continue
                    for fallback_career in [c] + CAREER_PATH_FALLBACKS:
                        test_url = build_api_url(t, fallback_instance, fallback_career)
                        s, jobs, total = _post(test_url, search, 0)
                        if s == 200:
                            print(f"  [discovered] {company['name']} → {fallback_instance}/{fallback_career}")
                            company["instance"] = fallback_instance
                            company["career"]   = fallback_career
                            url = test_url
                            found = True
                            break
                    if found:
                        break

            if not found:
                print(f"  [skip] {company['name']} — could not discover working endpoint")
                return []
        else:
            print(f"  [skip] {company['name']} — HTTP {status}")
            return []

    return list(jobs)


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
        f"[Workday] {new_count} new {_role_label} role(s) — "
        f"{datetime.now().strftime('%b %d, %Y %H:%M')}"
    )
    body_html = f"""
    <h2>Workday — {_role_label} Jobs (Last {MAX_AGE_DAYS} Day)</h2>
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
    if _args.batch == "1":
        companies = companies[:300]
    elif _args.batch == "2":
        companies = companies[300:]
    all_current_jobs: list[dict] = []  # all matching jobs within age window
    new_count = 0                      # how many are genuinely new this run

    print(f"[+] Workday scraper started — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"    Searching: {SEARCH_TERMS} | max age: {MAX_AGE_DAYS}d | companies: {len(companies)}\n")

    for company in companies:
        print(f"[→] {company['name']}")

        all_postings = []
        seen_req_ids: set = set()
        for term in SEARCH_TERMS:
            for job in fetch_jobs(company, term):
                rid = job.get("jobReqId") or job.get("externalPath", "")
                if rid not in seen_req_ids:
                    seen_req_ids.add(rid)
                    all_postings.append(job)
            time.sleep(0.5)

        if not all_postings:
            time.sleep(REQUEST_DELAY)
            continue

        matched_new = 0
        for job in all_postings:
            title         = job.get("title", "").strip()
            location      = job.get("locationsText", "").strip()
            posted_text   = job.get("postedOn", "").strip()
            external_path = job.get("externalPath", "")
            job_req_id    = job.get("jobReqId", external_path)

            job_id = f"{company['tenant']}_{job_req_id}"

            if not is_allowed_title(title):
                continue
            if not is_us_location(location):
                continue

            age = posted_days_ago(posted_text)
            if age > MAX_AGE_DAYS:
                continue

            job_url = build_job_url(company, external_path)
            is_new  = job_id not in seen_ids

            row = {
                "title":       title,
                "company":     company["name"],
                "location":    location,
                "posted":      posted_text,
                "link":        job_url,
                "found_on":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "is_new":      is_new,
                "entry_level": is_entry_level(title),
            }
            all_current_jobs.append(row)

            if is_new:
                append_csv(row)
                seen_ids.add(job_id)
                matched_new += 1
                new_count   += 1
                print(f"    [+] NEW: {title} | {location} | {posted_text}")

        if matched_new == 0:
            print(f"    [–] No new matches")

        time.sleep(REQUEST_DELAY)

    save_seen_ids(seen_ids)

    # Persist any career paths/instances discovered during this run
    COMPANIES_FILE.write_text(json.dumps(companies, indent=2), encoding="utf-8")

    print(f"\n{'='*65}")
    print(f"[+] Done — {new_count} new job(s) found across {len(companies)} companies")
    if new_count:
        print(f"    Saved → {OUTPUT_CSV.name}")

    if new_count:
        # Sort: new+entry-level first, then new, then seen
        all_current_jobs.sort(key=lambda j: (
            0 if (j["is_new"] and j["entry_level"]) else
            1 if j["is_new"] else 2
        ))
        send_summary_email(all_current_jobs, new_count)
    else:
        print("[i] No new jobs — skipping email.")


if __name__ == "__main__":
    main()
