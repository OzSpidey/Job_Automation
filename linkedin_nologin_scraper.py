"""
LinkedIn Jobs Scraper — No Login Required
------------------------------------------
Uses LinkedIn's public guest API endpoint to scrape job listings
without a session cookie, Selenium, or any account risk.

Endpoint: linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
Detail:   linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}

Run:  python linkedin_nologin_scraper.py
"""

import json
import os
import random
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
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

# ── CONFIG ────────────────────────────────────────────────────────────────────

ROLES = [
    "Data Engineer",
    "Data Analyst",
    "Business Intelligence Analyst",
    "Business Intelligence Engineer",
    "Analytics Engineer",
    "AI Engineer",
]

GEO_ID         = "103644278"  # United States
TIME_WINDOW    = "r7200"      # jobs posted in last 2 hours (buffer for LinkedIn indexing latency)
MAX_PAGES      = 5            # pages per role (guest API returns ~10 cards per page, regardless of filter)
DUPE_THRESHOLD = 0.7          # stop paginating when ≥70% of a page is already in seen (only checked from page 3 onwards)
FETCH_DETAILS  = True         # fetch job description to check experience requirements
REPOST_ID_GAP  = 3_000_000   # job IDs this far below the reference max are flagged as reposts

SKIP_COMPANY_KEYWORDS = {"rotaract"}

SKIP_COMPANIES = {
    "aaratech",
    "bcforward",
    "beaconfire inc.",
    "enhance it",
    "fetchjobs.co",
    "fusion it",
    "jobs via dice",
    "haystack",
    "insight global",
    "rk infotech llc",
    "robert half",
    "sundayy",
    "talent ally",
    "talentally",
    "tech consulting",
    "smart it frame llc",
    "winaxis llc",
}


SEEN_FILE = Path(__file__).parent / "json" / "linkedin_nologin_seen.json"

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER",       "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD",  "")
EMAIL_TO       = os.environ.get("EMAIL_TO",            "")

SENIOR_RE = re.compile(
    r'\b(senior|sr\.?|lead|manager|director|principal|staff|head of|avp|vp|vice president|architect)\b',
    re.I,
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"

# ── STATE ─────────────────────────────────────────────────────────────────────

def load_seen() -> dict:
    """Returns {job_id: iso_timestamp}. Drops entries older than 7 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    if not SEEN_FILE.exists():
        return {}
    raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        # migrate old flat list — assign current time so they expire in 7 days
        return {jid: datetime.now(timezone.utc).isoformat() for jid in raw}
    result = {}
    for jid, ts in raw.items():
        try:
            if datetime.fromisoformat(ts) > cutoff:
                result[jid] = ts
        except (ValueError, TypeError):
            pass
    return result

def save_seen(seen: dict) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=2), encoding="utf-8")

# ── HTTP ──────────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer":         "https://www.linkedin.com/jobs/search/",
    }

def _get(url: str, params: dict = None, retries: int = 2) -> requests.Response | None:
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=15)
            if resp.status_code == 429:
                wait = 45 + random.uniform(15, 30)
                print(f"  [!] Rate limited (429) — waiting {wait:.0f}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt == retries:
                print(f"  [!] Request failed after {retries + 1} tries: {e}")
            else:
                time.sleep(random.uniform(3, 7))
    return None

# ── PARSING ───────────────────────────────────────────────────────────────────

def parse_posted_minutes(text: str) -> int:
    """Convert '5 minutes ago', '1 hour ago', etc. to minutes. Lower = more recent."""
    if not text:
        return 99999
    t = text.lower()
    if "just now" in t or "moment" in t:
        return 0
    m = re.search(r"(\d+)\s*minute", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*hour", t)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*day", t)
    if m:
        return int(m.group(1)) * 1440
    return 99999

def parse_experience_years(text: str) -> int | None:
    if not text:
        return None
    t = text.lower()
    candidates = []
    for m in re.finditer(r'(\d+)\s*[-–]\s*\d+\s*\+?\s*years?', t):
        candidates.append(int(m.group(1)))
    for m in re.finditer(r'(?:minimum|at\s+least|min\.?)\s+(\d+)\s*\+?\s*years?', t):
        candidates.append(int(m.group(1)))
    for m in re.finditer(r'(\d+)\s*\+?\s*years?\s+of\s+(?:relevant\s+|related\s+)?experience', t):
        candidates.append(int(m.group(1)))
    for m in re.finditer(r'(\d+)\s*\+?\s*years?\s+(?:relevant\s+|related\s+)?experience', t):
        candidates.append(int(m.group(1)))
    for m in re.finditer(r'experience\s*(?:of|:)?\s*(\d+)\s*\+?\s*years?', t):
        candidates.append(int(m.group(1)))
    return min(candidates) if candidates else None

_SPONSOR_NO = re.compile(
    r'(not?\s+(?:able\s+to\s+)?(?:provide|offer|support|consider)?\s*(?:visa\s+)?sponsor'
    r'|cannot\s+(?:provide\s+)?(?:visa\s+)?sponsor'
    r'|no\s+(?:visa\s+)?sponsor'
    r'|sponsorship\s+(?:is\s+)?not\s+(?:available|provided|offered)'
    r'|does\s+not\s+(?:provide\s+)?(?:visa\s+)?sponsor'
    r'|authorized\s+to\s+work\s+in\s+the\s+u\.?s\.?\s+without\s+(?:visa\s+)?sponsor)',
    re.I,
)
_SPONSOR_YES = re.compile(
    r'(will\s+(?:provide\s+)?(?:visa\s+)?sponsor'
    r'|(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:available|provided|offered)'
    r'|h[-]?1[-]?b\s+sponsor'
    r'|open\s+to\s+(?:visa\s+)?sponsor'
    r'|we\s+(?:do\s+)?sponsor)',
    re.I,
)

def parse_sponsorship(text: str) -> str | None:
    """Returns 'yes', 'no', or None if the description doesn't mention it."""
    if not text:
        return None
    if _SPONSOR_NO.search(text):
        return "no"
    if _SPONSOR_YES.search(text):
        return "yes"
    return None

_WORK_REMOTE = re.compile(
    r'\b(fully\s+remote|100\s*%\s+remote|remote\s+(?:first|only|position|role|work|job)'
    r'|work(?:ing)?\s+(?:fully\s+)?remote(?:ly)?'
    r'|work\s+from\s+(?:home|anywhere)|wfh)\b',
    re.I,
)
_WORK_HYBRID = re.compile(r'\bhybrid\b', re.I)
_WORK_ONSITE = re.compile(
    r'\b(on[\s-]?site|in[\s-]office|in[\s-]person|fully\s+in[\s-]office)\b', re.I
)

def parse_work_type(text: str) -> str:
    if not text:
        return "—"
    if _WORK_REMOTE.search(text):
        return "Remote"
    if _WORK_HYBRID.search(text):
        return "Hybrid"
    if _WORK_ONSITE.search(text):
        return "On-site"
    return "—"

# ── FETCH SEARCH PAGE ─────────────────────────────────────────────────────────

def fetch_job_cards(role: str, offset: int = 0) -> list[dict]:
    resp = _get(SEARCH_URL, params={
        "keywords": role,
        "geoId":    GEO_ID,
        "f_TPR":    TIME_WINDOW,
        "f_JT":     "F",
        "start":    offset,
    })
    if not resp:
        return []

    soup  = BeautifulSoup(resp.text, "html.parser")
    cards = soup.find_all("div", class_=re.compile(r"base-search-card"))
    jobs  = []

    for card in cards:
        urn = card.get("data-entity-urn", "")
        m   = re.search(r"jobPosting:(\d+)", urn)
        if m:
            job_id = m.group(1)
        else:
            link = card.find("a", href=re.compile(r"/jobs/view/"))
            if not link:
                continue
            m2 = re.search(r"/jobs/view/(\d+)", link.get("href", ""))
            if not m2:
                continue
            job_id = m2.group(1)

        title_el   = card.find("h3", class_=re.compile(r"base-search-card__title"))
        company_el = card.find("h4", class_=re.compile(r"base-search-card__subtitle"))
        loc_el     = card.find("span", class_=re.compile(r"job-search-card__location"))
        time_el    = card.find("time")
        easy_apply = bool(re.search(r"easy.?apply", str(card), re.I))

        jobs.append({
            "job_id":     job_id,
            "title":      title_el.get_text(strip=True)   if title_el   else "",
            "company":    company_el.get_text(strip=True) if company_el else "",
            "location":   loc_el.get_text(strip=True)     if loc_el     else "",
            "posted":     time_el.get_text(strip=True)    if time_el    else "",
            "apply_url":  f"https://www.linkedin.com/jobs/view/{job_id}/",
            "easy_apply": easy_apply,
        })

    return jobs

# ── FETCH JOB DETAIL ──────────────────────────────────────────────────────────

def fetch_job_detail(job_id: str) -> dict:
    resp = _get(DETAIL_URL.format(job_id))
    if not resp:
        return {"description": "", "min_exp_years": None, "sponsorship": None, "work_type": "—", "easy_apply": False}

    # covers apply-link-onsite and apply-link-simple_onsite variants
    easy_apply = bool(re.search(r'apply-link[^"\'<>]*onsite', resp.text))

    soup    = BeautifulSoup(resp.text, "html.parser")
    desc_el = soup.find("div", class_=re.compile(r"show-more-less-html__markup|description__text"))
    desc    = desc_el.get_text(separator=" ", strip=True) if desc_el else ""

    return {
        "description":   desc,
        "min_exp_years": parse_experience_years(desc),
        "sponsorship":   parse_sponsorship(desc),
        "work_type":     parse_work_type(desc),
        "easy_apply":    easy_apply,
    }

# ── EMAIL ─────────────────────────────────────────────────────────────────────

def send_email(new_jobs: list[dict]) -> None:
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set — skipping email.")
        return

    def job_row(j):
        exp_cell     = f"{j['min_exp_years']}yr" if j.get("min_exp_years") else "—"
        ea_cell      = "✅ Yes" if j.get("easy_apply") else "No"
        sponsor_val  = j.get("sponsorship")
        sponsor_cell = (
            "<span style='color:green;font-weight:bold'>Yes</span>" if sponsor_val == "yes"
            else "<span style='color:#c0392b'>No</span>"            if sponsor_val == "no"
            else "—"
        )
        repost_cell  = "<span style='color:#e67e22;font-weight:bold'>⚠ Yes</span>" if j.get("reposted") else "No"
        wt           = j.get("work_type", "—")
        row_bg       = "#27ae60" if (j.get("easy_apply") and sponsor_val == "yes") else "#d4edda"
        row_color    = "color:white;font-weight:bold" if row_bg == "#27ae60" else ""
        return (
            f"<tr style='background:{row_bg};{row_color}'>"
            f"<td><a href='{j['apply_url']}' style='font-weight:bold;color:{'white' if row_bg == '#27ae60' else '#0a66c2'}'>"
            f"{j['title']}</a></td>"
            f"<td>{j['company']}</td>"
            f"<td>{j['location']}</td>"
            f"<td>{exp_cell}</td>"
            f"<td>{ea_cell}</td>"
            f"<td>{sponsor_cell}</td>"
            f"<td>{repost_cell}</td>"
            f"<td>{wt}</td>"
            f"<td>{j['posted']}</td>"
            f"</tr>"
        )

    rows    = "".join(job_row(j) for j in new_jobs)
    subject = (
        f"LinkedIn PAGINATION TEST: {len(new_jobs)} new role(s) — "
        f"{datetime.now(ET).strftime('%b %d %I:%M %p ET')}"
    )
    body = f"""
    <h2 style="color:#0a66c2">LinkedIn Job Alert — Public API</h2>
    <p><b style="color:#155724">{len(new_jobs)} new role(s)</b> — posted in last hour</p>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px;width:100%">
      <tr style="background:#0a66c2;color:white">
        <th>Title</th><th>Company</th><th>Location</th><th>Exp</th><th>Easy Apply</th><th>Sponsorship</th><th>Reposted?</th><th>Work Type</th><th>Posted</th>
      </tr>
      {rows}
    </table>
    <p style="font-size:12px;color:#888;margin-top:16px">
      Scraped via LinkedIn public guest API — no login, no account risk.<br>
      Generated {datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")}
    </p>
    """

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, EMAIL_TO, msg.as_string())
        print(f"[+] Email sent → {len(new_jobs)} new role(s)")
    except Exception as e:
        print(f"[!] Email failed: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("LinkedIn Jobs Scraper — No Login")
    print("=" * 55)

    seen     = load_seen()
    all_jobs = []
    seen_ids = set()

    for role in ROLES:
        print(f"\n[+] Role: {role}")

        offset = 0
        for page in range(MAX_PAGES):
            print(f"    Page {page + 1}/{MAX_PAGES} (start={offset})...")
            cards = fetch_job_cards(role, offset)

            if not cards:
                print("    No results — stopping pagination.")
                break

            print(f"    Got {len(cards)} cards")
            added         = 0
            already_seen  = 0

            for job in cards:
                jid = job["job_id"]

                if jid in seen_ids or jid in seen:
                    already_seen += 1

                if jid in seen_ids:
                    continue
                seen_ids.add(jid)

                company_lower = job["company"].lower().strip()
                if company_lower in SKIP_COMPANIES or any(kw in company_lower for kw in SKIP_COMPANY_KEYWORDS):
                    print(f"    SKIP company: {job['company']}")
                    continue

                if jid in seen:
                    continue

                if FETCH_DETAILS:
                    time.sleep(random.uniform(1.0, 1.6))
                    card_ea = job.get("easy_apply", False)
                    detail  = fetch_job_detail(jid)
                    job.update(detail)
                    job["easy_apply"] = card_ea or job.get("easy_apply", False)

                all_jobs.append(job)
                added += 1

            print(f"    Added {added} new jobs this page  ({already_seen}/{len(cards)} previously seen)")

            if page >= 2 and already_seen / len(cards) >= DUPE_THRESHOLD:
                print(f"    Dupe saturation (≥{int(DUPE_THRESHOLD*100)}%) — stopping pagination.")
                break

            offset += len(cards)
            time.sleep(random.uniform(3, 6))

        time.sleep(random.uniform(4, 8))

    now = datetime.now(timezone.utc).isoformat()
    for jid in seen_ids:
        if jid not in seen:
            seen[jid] = now
    save_seen(seen)

    if all_jobs:
        run_max        = max(int(j["job_id"]) for j in all_jobs)
        historical_max = max((int(k) for k in seen), default=0)
        reference_max  = max(run_max, historical_max)
        for j in all_jobs:
            j["reposted"] = (reference_max - int(j["job_id"])) > REPOST_ID_GAP

    target = sorted(
        [j for j in all_jobs if not SENIOR_RE.search(j["title"])],
        key=lambda j: parse_posted_minutes(j["posted"])
    )
    senior = [j for j in all_jobs if SENIOR_RE.search(j["title"])]

    print(f"\n{'=' * 55}")
    print(f"New this run       : {len(all_jobs)}")
    print(f"  Target roles     : {len(target)}")
    print(f"  Senior (skipped) : {len(senior)}")

    if target:
        print("\n── New Jobs ─────────────────────────────────────────")
        for j in target:
            exp      = f" | exp: {j['min_exp_years']}yr" if j.get("min_exp_years") else ""
            ea       = " | Easy Apply" if j.get("easy_apply") else ""
            sponsor  = j.get("sponsorship")
            sp       = f" | sponsor: {sponsor}" if sponsor else ""
            rp       = " | REPOST" if j.get("reposted") else ""
            wt       = f" | {j['work_type']}" if j.get("work_type") and j["work_type"] != "—" else ""
            print(f"  [NEW]  {j['title']} @ {j['company']}{exp}{ea}{sp}{rp}{wt}")
            print(f"         {j['location']} | {j['posted']}")
            print(f"         {j['apply_url']}")
            print()

    if not target:
        print("\nNo new target roles this run — skipping email.")
    else:
        send_email(target)

    print("[+] Done.")


if __name__ == "__main__":
    main()
