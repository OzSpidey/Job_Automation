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
_GH = "https://my.greenhouse.io/jobs?{query}&location=United%20States&lat=39.71614&lon=-96.999246&location_type=country&country_short_name=US&date_posted=past_day"
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
LAST_RUN_FILE = Path(__file__).parent / "json" / "greenhouse_last_run_jobs.json"
PAGE_TIMEOUT  = 60_000

SKIP_COMPANY_SLUGS = ["yipitdatajobs", "launch2"]

SENIOR_TITLE_RE = re.compile(r'\b(senior|lead|manager)\b', re.I)


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def load_last_run_jobs() -> set:
    if LAST_RUN_FILE.exists():
        return set(json.loads(LAST_RUN_FILE.read_text()))
    return set()


def save_last_run_jobs(job_ids: set) -> None:
    LAST_RUN_FILE.write_text(json.dumps(list(job_ids)))


def send_summary_email(jobs: list[dict], prev_run_ids: set) -> None:
    """Email a table of found roles, highlighting new ones."""
    if not EMAIL_PASSWORD:
        print("[!] EMAIL_PASSWORD not set — skipping email notification.")
        return

    n_new = sum(1 for j in jobs if (j.get("job_id") or j["apply_url"]) not in prev_run_ids)

    if not jobs:
        print("No jobs to email.")
        return

    NEW_BADGE = (
        " <span style='background:#0c5460;color:white;font-size:10px;"
        "padding:1px 5px;border-radius:3px;vertical-align:middle'>NEW</span>"
    )

    def _row(j):
        jid    = j.get("job_id") or j["apply_url"]
        is_new = jid not in prev_run_ids
        bg     = "#e8f5e9" if is_new else "#fff"
        badge  = NEW_BADGE if is_new else ""
        title  = j.get("title", "")
        co     = j.get("company", "")
        loc    = j.get("location", "")
        url    = j["apply_url"]
        return (
            f"<tr style='background:{bg}'>"
            f"<td style='padding:6px;border:1px solid #ddd'>{title}{badge}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{co}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{loc}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'><a href='{url}'>Apply</a></td>"
            f"</tr>"
        )

    rows = "".join(_row(j) for j in jobs)
    subject = f"Greenhouse Jobs — {len(jobs)} role(s) found ({n_new} NEW)"
    body_html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333">
    <h2 style="color:#24a87a">Greenhouse — Matching Roles</h2>
    <p>Found <strong>{len(jobs)}</strong> role(s){f" — <strong style='color:#0c5460'>{n_new} NEW</strong> since last run" if n_new else ""}.</p>
    <table style="border-collapse:collapse;width:100%;max-width:1100px">
      <tr style="background:#24a87a;color:#fff">
        <th style="padding:8px;border:1px solid #1d8a65;text-align:left;width:35%">Title</th>
        <th style="padding:8px;border:1px solid #1d8a65;text-align:left;width:20%">Company</th>
        <th style="padding:8px;border:1px solid #1d8a65;text-align:left;width:20%">Location</th>
        <th style="padding:8px;border:1px solid #1d8a65;text-align:left;width:10%">Link</th>
      </tr>
      {rows}
    </table>
    <p style="font-size:12px;color:#888;margin-top:20px">
      Source: my.greenhouse.io · United States · Posted Today
    </p>
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
        print(f"[+] Summary email sent to {EMAIL_TO}")
    except Exception as e:
        print(f"[!] Email failed: {e}")


def append_csv(row: dict) -> None:
    fieldnames = ["job_id", "title", "company", "location", "apply_url"]
    write_header = not OUTPUT_CSV.exists() or OUTPUT_CSV.stat().st_size == 0
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


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

async def fetch_real_titles(context: BrowserContext, jobs: list[dict]) -> None:
    """
    For any job whose title is blank or the placeholder 'View job', open the
    job page and scrape the real h1 title. Runs concurrently (up to 5 at a time).
    """
    needs_title = [j for j in jobs if not j.get("title") or re.match(r'^view job$', j.get("title",""), re.I)]
    if not needs_title:
        return

    print(f"[titles] Fetching real titles for {len(needs_title)} job(s)...")

    sem = asyncio.Semaphore(5)

    async def _fetch_one(job: dict) -> None:
        async with sem:
            pg = await context.new_page()
            try:
                await pg.goto(job["apply_url"], wait_until="domcontentloaded", timeout=30_000)
                await pg.wait_for_timeout(1500)
                title = await pg.evaluate("""() => {
                    const h = document.querySelector('h1, [class*="app-title"], [class*="job-title"]');
                    if (h && h.innerText.trim()) return h.innerText.trim();
                    return document.title.split('|')[0].split('-')[0].trim();
                }""")
                if title and not re.match(r'^view job$', title, re.I):
                    job["title"] = title
            except Exception:
                pass
            finally:
                await pg.close()

    await asyncio.gather(*[_fetch_one(j) for j in needs_title])
    print(f"[titles] Done.")


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

        await fetch_real_titles(context, jobs)

        # Filter out skip list and senior/lead/manager roles
        jobs = [
            j for j in jobs
            if not any(slug in j["apply_url"] for slug in SKIP_COMPANY_SLUGS)
            and not (j.get("title") and SENIOR_TITLE_RE.search(j["title"]))
        ]

        prev_run_ids = load_last_run_jobs()

        # Update last-run and save session before emailing
        _state = await context.storage_state()
        _state["origins"] = []
        SESSION_FILE.write_text(json.dumps(_state))
        save_last_run_jobs({(j.get("job_id") or j["apply_url"]) for j in jobs})

        # Write CSV
        for j in jobs:
            append_csv(j)

        # Print results
        print(f"\n{'='*60}")
        print(f"[+] Done! Found: {len(jobs)} role(s)")
        new_jobs = [j for j in jobs if (j.get("job_id") or j["apply_url"]) not in prev_run_ids]
        for j in jobs:
            jid    = j.get("job_id") or j["apply_url"]
            is_new = jid not in prev_run_ids
            print(f"  {'★' if is_new else '•'} {j.get('title', '')}{'  [NEW]' if is_new else ''}")
            print(f"    {j['apply_url']}")
        print(f"{'='*60}")
        print(f"New roles (not seen last run): {len(new_jobs)}")

        if not new_jobs:
            print("No new roles — skipping email.")
        elif not EMAIL_TO:
            print("[warn] EMAIL_TO not configured — skipping email.")
        else:
            send_summary_email(jobs, prev_run_ids)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
