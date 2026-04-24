"""
Indeed Jobs Scraper via Adzuna API
------------------------------------
Uses the Adzuna API to find jobs posted in the last 24 hours matching
target roles. Tracks new vs. seen jobs and emails a summary.

No browser, no Cloudflare, no login required.

Run:  python indeed_adzuna_scraper.py
"""

import json
import os
import re
import smtplib
import sys
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

# ── CONFIG ────────────────────────────────────────────────────────────────────

ADZUNA_APP_ID  = os.environ.get("ADZUNA_APP_ID",  "1e421cb5")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "9b3d4f5a031c8dad9be8c73c721546b4")

SEARCH_QUERIES = [
    "data analyst",
    "data engineer",
    "business intelligence analyst",
    "data scientist",
    "business analyst",
]

LOCATION     = "United States"
MAX_DAYS_OLD = 1      # last 24 hours
RESULTS_PAGE = 50     # max per query

SEEN_FILE = Path(__file__).parent / "adzuna_seen_jobs.json"

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER",       "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD",  "")
EMAIL_TO       = os.environ.get("EMAIL_TO",            "")

SENIOR_RE = re.compile(
    r'\b(senior|sr\.?|lead|manager|director|principal|staff|head of|vp|vice president)\b',
    re.I
)

# ── STATE ─────────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))

# ── FETCH ─────────────────────────────────────────────────────────────────────

def fetch_jobs(query: str) -> list[dict]:
    url = f"https://api.adzuna.com/v1/api/jobs/us/search/1"
    params = {
        "app_id":           ADZUNA_APP_ID,
        "app_key":          ADZUNA_APP_KEY,
        "results_per_page": RESULTS_PAGE,
        "what":             query,
        "where":            LOCATION,
        "max_days_old":     MAX_DAYS_OLD,
        "sort_by":          "date",
        "content-type":     "application/json",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [!] API error for '{query}': {e}")
        return []

    jobs = []
    for r in data.get("results", []):
        job_id  = str(r.get("id", ""))
        title   = r.get("title", "").strip()
        company = r.get("company", {}).get("display_name", "").strip()
        loc     = r.get("location", {}).get("display_name", "").strip()
        url_    = r.get("redirect_url", "")
        created = r.get("created", "")
        desc    = re.sub(r'<[^>]+>', '', r.get("description", "")).strip()
        sal_min = r.get("salary_min")
        sal_max = r.get("salary_max")

        salary = ""
        if sal_min and sal_max:
            salary = f"${int(sal_min):,} – ${int(sal_max):,}"
        elif sal_min:
            salary = f"${int(sal_min):,}+"

        # Parse date for display
        posted = ""
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                posted = dt.strftime("%b %d %I:%M %p")
            except Exception:
                posted = created[:10]

        jobs.append({
            "job_id":  job_id,
            "title":   title,
            "company": company,
            "location": loc,
            "url":     url_,
            "posted":  posted,
            "salary":  salary,
            "snippet": desc[:250],
        })

    return jobs

# ── EMAIL ─────────────────────────────────────────────────────────────────────

def send_email(new_jobs: list[dict]) -> None:
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set — skipping email.")
        return

    def job_row(j):
        is_senior = bool(SENIOR_RE.search(j["title"]))
        bg        = "#fff3cd" if is_senior else "#d4edda"
        badge     = (
            " <span style='background:#856404;color:#fff;font-size:10px;"
            "padding:1px 5px;border-radius:3px'>SENIOR</span>"
            if is_senior else
            " <span style='background:#155724;color:#fff;font-size:10px;"
            "padding:1px 5px;border-radius:3px'>NEW</span>"
        )
        salary_cell = f"<td>{j['salary']}</td>" if j["salary"] else "<td>—</td>"
        return (
            f"<tr style='background:{bg}'>"
            f"<td><a href='{j['url']}' style='font-weight:bold;color:#2557a7'>"
            f"{j['title']}</a>{badge}</td>"
            f"<td>{j['company']}</td>"
            f"<td>{j['location']}</td>"
            f"{salary_cell}"
            f"<td>{j['posted']}</td>"
            f"<td style='font-size:11px;color:#555'>{j['snippet'][:120]}...</td>"
            f"</tr>"
        )

    target  = [j for j in new_jobs if not SENIOR_RE.search(j["title"])]
    senior  = [j for j in new_jobs if SENIOR_RE.search(j["title"])]

    # Show target roles first, then senior
    ordered = target + senior
    rows    = "".join(job_row(j) for j in ordered)

    subject = (
        f"Indeed (Adzuna): {len(target)} new role(s) — "
        f"{datetime.now().strftime('%b %d %I:%M %p')}"
    )

    body = f"""
    <h2 style="color:#2557a7">Indeed Job Alert</h2>
    <p>
      <b style="color:#155724">{len(target)} target role(s)</b> &nbsp;|&nbsp;
      <b style="color:#856404">{len(senior)} senior role(s)</b> &nbsp;|&nbsp;
      <b>{len(new_jobs)} total new</b> — posted in last 24 hrs
    </p>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px;width:100%">
      <tr style="background:#2557a7;color:white">
        <th>Title</th><th>Company</th><th>Location</th>
        <th>Salary</th><th>Posted</th><th>Snippet</th>
      </tr>
      {rows}
    </table>
    <p style="font-size:12px;color:#888;margin-top:16px">
      Click any title to open the job and apply directly.
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
        print(f"[+] Email sent → {len(new_jobs)} new job(s)")
    except Exception as e:
        print(f"[!] Email failed: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("Indeed Jobs Scraper (via Adzuna API)")
    print("=" * 55)

    seen     = load_seen()
    all_jobs = []
    seen_ids = set()

    for query in SEARCH_QUERIES:
        print(f"[+] Searching: {query}")
        jobs = fetch_jobs(query)
        print(f"    {len(jobs)} jobs returned")

        for job in jobs:
            jid = job["job_id"]
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            job["is_new"] = jid not in seen
            all_jobs.append(job)

    new_jobs   = [j for j in all_jobs if j["is_new"]]
    target_new = [j for j in new_jobs if not SENIOR_RE.search(j["title"])]
    senior_new = [j for j in new_jobs if SENIOR_RE.search(j["title"])]

    print(f"\n{'='*55}")
    print(f"Total unique jobs  : {len(all_jobs)}")
    print(f"New this run       : {len(new_jobs)}")
    print(f"  Target roles     : {len(target_new)}")
    print(f"  Senior (flagged) : {len(senior_new)}")

    if new_jobs:
        print("\n── New Jobs ──────────────────────────────────────────")
        for j in new_jobs:
            tag = "[SENIOR]" if SENIOR_RE.search(j["title"]) else "[NEW]  "
            sal = f" | {j['salary']}" if j["salary"] else ""
            print(f"  {tag}  {j['title']} @ {j['company']}{sal}")
            print(f"          {j['location']} | {j['posted']}")
            print(f"          {j['url']}")
            print()

    # Save seen state
    seen.update(seen_ids)
    save_seen(seen)

    if not new_jobs:
        print("\nNo new jobs this run — skipping email.")
    else:
        send_email(new_jobs)

    print("[+] Done.")


if __name__ == "__main__":
    main()
