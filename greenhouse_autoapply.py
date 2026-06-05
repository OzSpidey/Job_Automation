"""
Greenhouse Jobs Scraper
-----------------------
Searches Greenhouse (my.greenhouse.io) for recent Data Analyst / Data Engineer /
Business Intelligence / Software Engineer roles posted in the United States and
emails a summary of every match found.

No applications are submitted — roles are listed for manual review.

SETUP:
  1. Set EMAIL_SENDER, GMAIL_APP_PASSWORD, EMAIL_TO in your .env file.
  2. Run: python greenhouse_autoapply.py
  3. First run opens a browser — log in if prompted, press ENTER when ready.
  4. Jobs are logged to csv/greenhouse_jobs.csv (de-duplicated per run).
"""

import asyncio
import csv
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# EMAIL CONFIG
# ──────────────────────────────────────────────────────────────────────────────
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")
GH_EMAIL       = os.environ.get("GH_EMAIL", EMAIL_TO)  # Greenhouse login email

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
_GH = "https://my.greenhouse.io/jobs?{query}&location=United%20States&lat=39.71614&lon=-96.999246&location_type=country&country_short_name=US&date_posted=past_week"
SEARCH_QUERIES = [
    {"url": _GH.format(query="query=data%20analyst"),           "type": "data_analyst"},
    {"url": _GH.format(query="query=data%20engineer"),          "type": "data_engineer"},
    {"url": _GH.format(query="query=business%20intelligence"),  "type": "business_intelligence"},
    {"url": _GH.format(query="query=data%20scientist"),         "type": "data_scientist"},
    {"url": _GH.format(query="query=business%20analyst"),       "type": "business_analyst"},
    {"url": _GH.format(query="query=bi%20analyst"),             "type": "business_intelligence"},
    {"url": _GH.format(query="query=software%20engineer"),      "type": "software_engineer"},
    {"url": _GH.format(query="query=software%20developer"),     "type": "software_developer"},
    {"url": _GH.format(query="query=devops"),                   "type": "devops"},
]
SESSION_FILE  = Path(__file__).parent / "json" / "greenhouse_session.json"
OUTPUT_CSV    = Path(__file__).parent / "csv" / "greenhouse_jobs.csv"
SEEN_LOG      = Path(__file__).parent / "json" / "greenhouse_last_run_jobs.json"
PAGE_TIMEOUT  = 60_000
PRUNE_DAYS    = 7

SKIP_COMPANY_SLUGS = ["yipitdatajobs", "launch2"]

SENIOR_TITLE_RE = re.compile(r'\b(senior|lead|manager|architect)\b', re.I)


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def format_posted(raw: str) -> str:
    """Parse an ISO datetime string into a readable date/time."""
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ").strip()
    except ValueError:
        pass
    clean = re.sub(r'[+-]\d{2}:?\d{2}$|Z$', '', raw[:26])
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(clean, fmt)
            return dt.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ").strip()
        except ValueError:
            continue
    return raw


def load_seen() -> dict:
    if not SEEN_LOG.exists():
        return {}
    raw = json.loads(SEEN_LOG.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw = {jid: today for jid in raw}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PRUNE_DAYS)).strftime("%Y-%m-%d")
    return {jid: dt for jid, dt in raw.items() if dt >= cutoff}


def save_seen(seen: dict) -> None:
    SEEN_LOG.parent.mkdir(parents=True, exist_ok=True)
    SEEN_LOG.write_text(json.dumps(seen, indent=2, sort_keys=True), encoding="utf-8")


def send_summary_email(jobs: list[dict], new_count: int) -> None:
    """Email all 24h roles with NEW badges; only called when new_count > 0."""
    if not EMAIL_PASSWORD:
        print("[!] EMAIL_PASSWORD not set — skipping email notification.")
        return

    def _row(j):
        new_badge = (
            "&nbsp;<span style='background:#22c55e;color:#fff;font-size:10px;"
            "font-weight:bold;padding:2px 5px;border-radius:3px'>NEW</span>"
            if j.get("is_new") else ""
        )
        return (
            f"<tr>"
            f"<td style='padding:6px;border:1px solid #ddd'>{j.get('title','')}{new_badge}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{j.get('company','')}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{j.get('location','')}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{j.get('posted','')}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'><a href='{j['apply_url']}'>Apply</a></td>"
            f"</tr>"
        )

    # Sort: new jobs first, then by posted_ts descending (stable two-pass)
    sorted_jobs = sorted(jobs, key=lambda j: j.get("posted_ts") or "", reverse=True)
    sorted_jobs = sorted(sorted_jobs, key=lambda j: 0 if j.get("is_new") else 1)

    rows = "".join(_row(j) for j in sorted_jobs)
    subject = f"Greenhouse: {new_count} new job(s) — {len(jobs)} total (last 24h)"
    body_html = f"""
    <html><body style="font-family:sans-serif;color:#333;font-size:13px">
    <h2>Greenhouse &mdash; {len(jobs)} job(s) posted in the last 24 hours</h2>
    <p><b>{new_count} new role(s)</b> found this run. All listings from the last 24h shown &mdash;
    <span style='background:#22c55e;color:#fff;font-size:10px;font-weight:bold;padding:2px 5px;border-radius:3px'>NEW</span>
    = new this run.</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Link</th>
      </tr>
      {rows}
    </table>
    </body></html>
    """
    try:
        recipients = [a.strip() for a in EMAIL_TO.split(",") if a.strip()]
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = ", ".join(recipients)
        msg.attach(MIMEText(body_html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        print(f"[+] Email sent to {EMAIL_TO}")
    except Exception as e:
        print(f"[!] Email failed: {e}")


def append_csv(jobs: list[dict]) -> None:
    fieldnames = ["job_id", "title", "company", "location", "posted", "apply_url", "found_on"]
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not OUTPUT_CSV.exists()
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(jobs)


# ──────────────────────────────────────────────────────────────────────────────
# LOGIN HANDLER
# ──────────────────────────────────────────────────────────────────────────────

async def ensure_logged_in(page: Page, email: str) -> None:
    """
    my.greenhouse.io uses an email OTP flow:
      1. Enter email → click Send security code
      2. User types the code they received → script detects success
    If already logged in (session cookie), skips the whole flow.
    """
    for attempt in range(3):
        try:
            await page.goto(SEARCH_QUERIES[0]["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            break
        except Exception as e:
            if attempt == 2:
                raise
            print(f"[!] Navigation timeout (attempt {attempt + 1}/3), retrying...")
            await page.wait_for_timeout(5000)
    await page.wait_for_timeout(2000)

    on_login = await page.evaluate("""() => {
        const b = document.body.innerText.toLowerCase();
        return b.includes('send security code') || b.includes('enter your email');
    }""")
    if not on_login:
        print("[+] Already logged in.")
        return

    print("[i] Login required — filling email and requesting security code...")

    if os.environ.get("CI"):
        raise RuntimeError(
            "Greenhouse session expired — OTP login required but running in CI (no terminal).\n"
            "Run locally to refresh the session, then update the GREENHOUSE_SESSION_B64 secret."
        )

    email_input = page.locator('input[type="email"], input[type="text"][name*="email"], input[placeholder*="email" i]').first
    await email_input.fill("", timeout=5000)
    await email_input.fill(email, timeout=5000)
    await page.wait_for_timeout(400)

    send_btn = page.locator('button:has-text("Send security code"), input[value*="Send"]').first
    await send_btn.click(timeout=5000)
    await page.wait_for_timeout(1500)

    print("\n" + "="*60)
    print("[!] CHECK YOUR EMAIL — a security code was sent to your registered address.")
    print("="*60)

    otp_code = await asyncio.to_thread(input, "[>] Enter the security code from your email: ")
    otp_code = otp_code.strip()

    otp_input = page.locator(
        'input[name*="code"], input[id*="code"], input[placeholder*="code" i], '
        'input[type="number"], input[type="text"]:not([name*="email"])'
    ).first
    if await otp_input.count() > 0:
        await otp_input.fill(otp_code, timeout=5000)
    else:
        await page.keyboard.type(otp_code, delay=60)

    verify_btn = page.locator(
        'button:has-text("Sign in"), button:has-text("Submit"), '
        'button:has-text("Verify"), button:has-text("Continue"), '
        'button[type="submit"], input[type="submit"]'
    ).first
    if await verify_btn.count() > 0:
        await verify_btn.click(timeout=5000)

    await page.wait_for_timeout(3000)

    logged_in = await page.evaluate("""() => {
        const b = document.body.innerText.toLowerCase();
        const onLogin = document.querySelector('input[type="email"]') !== null
            && b.includes('send security code');
        return !onLogin;
    }""")
    if not logged_in:
        raise TimeoutError("Login failed — OTP may have been incorrect or expired.")

    print("[+] Login successful — saving session.")
    _state = await page.context.storage_state()
    _state["origins"] = []
    SESSION_FILE.write_text(json.dumps(_state))

    await page.goto(SEARCH_QUERIES[0]["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(3000)


# ──────────────────────────────────────────────────────────────────────────────
# JOB LISTING SCRAPER
# ──────────────────────────────────────────────────────────────────────────────

async def _scrape_query(context: BrowserContext, query_meta: dict) -> list[dict]:
    """Scrape a single search query on its own page. Returns jobs with query_type set."""
    search_url = query_meta["url"]
    query_type = query_meta["type"]
    page = await context.new_page()
    try:
        print(f"[+] Loading: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(3000)

        for _ in range(5):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(800)

        page_jobs = await page.evaluate("""() => {
            const results = [];
            const cards = document.querySelectorAll(
                '[data-job-id], .job-post, li[class*="job"], div[class*="job-row"], article[class*="job"]'
            );
            cards.forEach(card => {
                const link = card.querySelector('a[href*="/jobs/"], a[href*="greenhouse.io"]');
                if (!link) return;
                const href = link.href || '';
                const jobIdMatch = href.match(/\\/jobs\\/(\\d+)/) || [];
                const jobId = jobIdMatch[1] || card.dataset.jobId || '';
                const titleEl = card.querySelector('h2, h3, h4, [class*="title"], [class*="job-name"], a');
                const companyEl = card.querySelector('[class*="company"], [class*="employer"], [class*="org"]');
                const locationEl = card.querySelector('[class*="location"], [class*="place"]');
                results.push({
                    job_id:    jobId,
                    title:     titleEl ? titleEl.innerText.trim() : link.innerText.trim(),
                    company:   companyEl ? companyEl.innerText.trim() : '',
                    location:  locationEl ? locationEl.innerText.trim() : '',
                    apply_url: href,
                });
            });
            const seen = new Set();
            return results.filter(j => {
                if (!j.apply_url || seen.has(j.apply_url)) return false;
                seen.add(j.apply_url); return true;
            });
        }""")

        if not page_jobs:
            print(f"[i] {query_type}: card parsing found 0 — falling back to link scan.")
            links = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('a[href]'))
                    .filter(a => /greenhouse\\.io.*jobs\\/\\d+|\\/jobs\\/\\d+/.test(a.href))
                    .map(a => {
                        const container = a.closest('li,article,tr,[class*="job"],[class*="card"],[class*="row"],[class*="item"]') || a.parentElement;
                        const titleEl = container ? container.querySelector('h1,h2,h3,h4,[class*="title"],[class*="name"],strong') : null;
                        const linkText = a.innerText.trim();
                        const rawTitle = titleEl ? titleEl.innerText.trim() : linkText;
                        const title = (rawTitle && !/^view job$/i.test(rawTitle)) ? rawTitle : linkText;
                        return {
                            job_id: (a.href.match(/\\/jobs\\/(\\d+)/) || [])[1] || '',
                            title, company: '', location: '', apply_url: a.href,
                        };
                    });
            }""")
            seen_local: set[str] = set()
            for lnk in links:
                if lnk["apply_url"] not in seen_local:
                    seen_local.add(lnk["apply_url"])
                    page_jobs.append(lnk)

        for job in page_jobs:
            job["query_type"] = query_type

        print(f"[+] {query_type}: {len(page_jobs)} listing(s)")
        return page_jobs
    finally:
        await page.close()


async def collect_jobs(context: BrowserContext) -> list[dict]:
    """Scrape all search queries in parallel, then de-duplicate by URL."""
    results = await asyncio.gather(
        *[_scrape_query(context, q) for q in SEARCH_QUERIES],
        return_exceptions=True,
    )

    all_jobs: list[dict] = []
    seen_urls: set[str] = set()
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"[!] Query '{SEARCH_QUERIES[i]['type']}' failed: {result}")
            continue
        for job in result:
            if job["apply_url"] not in seen_urls:
                seen_urls.add(job["apply_url"])
                all_jobs.append(job)

    print(f"[+] Total unique jobs across all queries: {len(all_jobs)}")
    return all_jobs


# ──────────────────────────────────────────────────────────────────────────────
# TITLE ENRICHMENT
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_job_details(jobs: list[dict]) -> None:
    """
    Use the Greenhouse boards API to fill in title, company, location, and posted date.
    No browser tabs needed — fast parallel HTTP requests.
    """
    print(f"[details] Fetching details for {len(jobs)} job(s) via API...")
    sem = asyncio.Semaphore(10)

    def _api_call(slug: str, job_id: str) -> dict | None:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    async def _fetch_one(job: dict) -> None:
        async with sem:
            m = re.search(r'greenhouse\.io/([^/]+)/jobs/(\d+)', job.get("apply_url", ""))
            if not m:
                return
            slug, job_id = m.group(1), m.group(2)
            if not job.get("company"):
                job["company"] = slug.replace("-", " ").title()
            try:
                data = await asyncio.to_thread(_api_call, slug, job_id)
                if not data:
                    return
                if data.get("title"):
                    job["title"] = data["title"]
                if data.get("company_name"):
                    job["company"] = data["company_name"]
                loc = (data.get("location") or {}).get("name") or ""
                if loc:
                    job["location"] = loc
                posted_raw = data.get("first_published") or data.get("updated_at") or ""
                if posted_raw:
                    job["posted_ts"] = posted_raw  # kept for sorting
                    job["posted"] = format_posted(posted_raw)
            except Exception as e:
                print(f"  [details-err] {job.get('apply_url','')[:70]}: {e}")

    await asyncio.gather(*[_fetch_one(j) for j in jobs])
    print(f"[details] Done.")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=0)
        if SESSION_FILE.exists():
            print("[+] Using saved session.")
            context = await browser.new_context(storage_state=str(SESSION_FILE))
        else:
            context = await browser.new_context()

        page = await context.new_page()
        await ensure_logged_in(page, GH_EMAIL)

        jobs = await collect_jobs(context)
        if not jobs:
            print("[!] No jobs found. The page structure may have changed.")
            await page.screenshot(path=str(Path(__file__).parent / "debug_greenhouse.png"))
            await browser.close()
            return

        await fetch_job_details(jobs)

        # Keep only jobs posted within the last 24 hours (or no date available)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        jobs = [
            j for j in jobs
            if not j.get("posted_ts")
            or datetime.fromisoformat(j["posted_ts"]) >= cutoff
        ]
        print(f"[+] After 24h filter: {len(jobs)} job(s)")

        # Filter out skip list, senior/lead/manager/architect, and unenriched "View job" rows
        jobs = [
            j for j in jobs
            if not any(slug in j["apply_url"] for slug in SKIP_COMPANY_SLUGS)
            and not (j.get("title") and SENIOR_TITLE_RE.search(j["title"]))
            and j.get("title", "").strip().lower() not in ("view job", "")
        ]

        # Sort newest-first (jobs without a posted date go to the end)
        jobs.sort(key=lambda j: j.get("posted_ts") or "", reverse=True)

        seen = load_seen()

        # Mark is_new on all jobs and collect new ones
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        new_jobs = []
        for j in jobs:
            jid = j.get("job_id") or j["apply_url"]
            j["is_new"] = jid not in seen
            if j["is_new"]:
                seen[jid] = today
                j["found_on"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                new_jobs.append(j)

        # Save session
        _state = await context.storage_state()
        _state["origins"] = []
        SESSION_FILE.write_text(json.dumps(_state))
        save_seen(seen)

        # Append new jobs to CSV
        if new_jobs:
            append_csv(new_jobs)

        # Print results
        print(f"\n{'='*60}")
        print(f"[+] Done! Found: {len(jobs)} total | {len(new_jobs)} new")
        for j in new_jobs:
            print(f"  [+] NEW: {j.get('title', '')} — {j.get('company', '')}")
            print(f"    {j['apply_url']}")
        print(f"{'='*60}")

        if not new_jobs:
            print("[i] No new jobs — skipping email.")
        elif not EMAIL_TO:
            print("[warn] EMAIL_TO not configured — skipping email.")
        else:
            send_summary_email(jobs, len(new_jobs))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
