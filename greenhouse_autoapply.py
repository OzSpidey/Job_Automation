"""
Greenhouse Auto-Apply Bot
--------------------------
Scrapes new Data Analyst jobs (past 5 days, United States) from Greenhouse
and auto-fills + submits each application.

SETUP:
  1. Fill in YOUR_INFO below (name, email, phone, resume path, LinkedIn URL).
  2. Run: python greenhouse_autoapply.py
  3. First run opens a browser — log in if prompted, press ENTER when ready.
  4. Applied jobs are logged to greenhouse_applied.csv (duplicates skipped).
"""

import asyncio
import csv
from datetime import datetime
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

# Load .env file for local development (no-op if not installed or file missing)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# YOUR INFO — loaded from APPLICANT_INFO_JSON env var
# Locally: set in .env file  |  GitHub Actions: set as repository secret
# ──────────────────────────────────────────────────────────────────────────────
_applicant_json = os.environ.get("APPLICANT_INFO_JSON", "")
if not _applicant_json:
    raise EnvironmentError(
        "APPLICANT_INFO_JSON is not set.\n"
        "  Locally:  add it to your .env file\n"
        "  GitHub Actions: add it as a repository secret"
    )
YOUR_INFO = json.loads(_applicant_json)
# RESUME_PATH env var overrides resume_path (GitHub Actions sets this to the decoded PDF path)
if os.environ.get("RESUME_PATH"):
    YOUR_INFO["resume_path"] = os.environ["RESUME_PATH"]

# Resume overrides for specific job types
DE_RESUME_PATH = os.environ.get("DE_RESUME_PATH", "")
DS_RESUME_PATH = os.environ.get("DS_RESUME_PATH", "")

# ──────────────────────────────────────────────────────────────────────────────
# EMAIL CONFIG — loaded from env vars / secrets
# ──────────────────────────────────────────────────────────────────────────────
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

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
]
SESSION_FILE  = Path(__file__).parent / "greenhouse_session.json"
OUTPUT_CSV    = Path(__file__).parent / "greenhouse_applied.csv"
APPLIED_LOG   = Path(__file__).parent / "greenhouse_applied_ids.json"
LAST_RUN_FILE = Path(__file__).parent / "greenhouse_last_run_jobs.json"
DELAY_BETWEEN = 5   # seconds between applications
PAGE_TIMEOUT  = 30_000

# Company URL slugs to skip (non-US offices, custom widgets that can't be automated, etc.)
SKIP_COMPANY_SLUGS = ["yipitdatajobs"]

# Job title keywords that indicate a role above target level — skip these
SENIOR_TITLE_RE = re.compile(r'\b(senior|lead|manager)\b', re.I)


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def load_applied_ids() -> dict:
    if APPLIED_LOG.exists():
        data = json.loads(APPLIED_LOG.read_text())
        if isinstance(data, list):
            # Migrate old list format to dict with metadata
            return {job_id: {"title": "", "company": "", "applied_at": None} for job_id in data}
        return data
    return {}


def save_applied_ids(ids: dict) -> None:
    APPLIED_LOG.write_text(json.dumps(ids, indent=2))


def load_last_run_jobs() -> set:
    if LAST_RUN_FILE.exists():
        return set(json.loads(LAST_RUN_FILE.read_text()))
    return set()


def save_last_run_jobs(job_ids: set) -> None:
    LAST_RUN_FILE.write_text(json.dumps(list(job_ids)))


def send_summary_email(all_jobs: list[dict]) -> None:
    """Send an HTML summary email listing every scanned job with its status."""
    if not EMAIL_PASSWORD:
        print("[!] EMAIL_PASSWORD not set — skipping email notification.")
        return

    n_applied  = sum(1 for r in all_jobs if r["status"] == "applied")
    n_failed   = sum(1 for r in all_jobs if r["status"] in ("failed", "error"))
    n_skipped  = sum(1 for r in all_jobs if r["status"].startswith("skipped"))
    n_new      = sum(1 for r in all_jobs if r.get("is_new"))

    if n_new == 0:
        print("No new jobs this run — skipping email.")
        return

    status_color = {
        "applied":               "#d4edda",
        "failed":                "#f8d7da",
        "error":                 "#f8d7da",
        "skipped":               "#f5f5f5",
        "skipped (already applied)": "#f5f5f5",
        "skipped (Senior Role)": "#fff3cd",
    }

    def _row(r):
        bg = status_color.get(r["status"], "#fff")
        title   = r.get("title") or ""
        company = r.get("company", "")
        status  = r["status"].upper()
        url     = r["apply_url"]
        applied_at = r.get("applied_at")
        if applied_at:
            try:
                applied_at = datetime.fromisoformat(applied_at).strftime("%b %d, %Y %I:%M %p")
            except Exception:
                pass
        applied_on_cell = f"<td>{applied_at or '—'}</td>" if r["status"] == "skipped (already applied)" else "<td></td>"
        new_badge = (
            " <span style='background:#0c5460;color:white;font-size:10px;"
            "padding:1px 5px;border-radius:3px;vertical-align:middle'>NEW</span>"
            if r.get("is_new") else ""
        )
        return (
            f"<tr style='background:{bg}'>"
            f"<td>{title}{new_badge}</td><td>{company}</td>"
            f"<td>{status}</td>"
            f"<td><a href='{url}'>Link</a></td>"
            f"{applied_on_cell}"
            f"</tr>"
        )

    rows = "".join(_row(r) for r in all_jobs)

    subject = f"Greenhouse Auto-Apply: {n_applied} applied | {n_failed} failed | {n_skipped} skipped — {len(all_jobs)} total"
    new_note = f" ({n_new} new this run)" if n_new else ""
    body_html = f"""
    <h2>Greenhouse Auto-Apply Summary</h2>
    <p>
      <b style="color:#155724">Applied: {n_applied}</b> &nbsp;|&nbsp;
      <b style="color:#721c24">Failed/Error: {n_failed}</b> &nbsp;|&nbsp;
      <b>Skipped: {n_skipped}</b> &nbsp;|&nbsp;
      <b>Total scanned: {len(all_jobs)}</b> &nbsp;|&nbsp;
      <b style="color:#0c5460">New this run: {n_new}</b>
    </p>
    {f'<p style="color:#0c5460;font-size:13px">&#9733; {n_new} job(s) not seen in the previous run are marked <b>NEW</b>.</p>' if n_new else ''}
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Status</th><th>Link</th><th>Applied On</th>
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


def append_csv(row: dict) -> None:
    fieldnames = ["job_id", "title", "company", "location", "apply_url", "status", "notes"]
    write_header = not OUTPUT_CSV.exists() or OUTPUT_CSV.stat().st_size == 0
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


async def safe_fill(page: Page, selector: str, value: str) -> bool:
    """Fill a field if it exists, return True on success."""
    if not value:
        return False
    try:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return False
        await loc.scroll_into_view_if_needed(timeout=3000)
        await loc.fill(value, timeout=5000)
        return True
    except Exception:
        return False


async def safe_select(page: Page, selector: str, label: str) -> bool:
    """Select a <select> option by visible text (partial match)."""
    if not label:
        return False
    try:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return False
        options = await loc.locator("option").all_text_contents()
        match = next((o for o in options if label.lower() in o.lower()), None)
        if match:
            await loc.select_option(label=match, timeout=5000)
            return True
    except Exception:
        pass
    return False


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
    await page.goto(SEARCH_QUERIES[0]["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(2000)

    # Already logged in if jobs page is showing
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

    # Clear the email field (it may have a partial value) and type the full address
    email_input = page.locator('input[type="email"], input[type="text"][name*="email"], input[placeholder*="email" i]').first
    await email_input.fill("", timeout=5000)
    await email_input.fill(email, timeout=5000)
    await page.wait_for_timeout(400)

    send_btn = page.locator('button:has-text("Send security code"), input[value*="Send"]').first
    await send_btn.click(timeout=5000)
    await page.wait_for_timeout(1500)

    print("\n" + "="*60)
    print("[!] CHECK YOUR EMAIL — a security code was sent to:")
    print(f"    {email}")
    print("="*60)

    # In headless mode the user types the OTP in the terminal
    otp_code = await asyncio.to_thread(input, "[>] Enter the security code from your email: ")
    otp_code = otp_code.strip()

    otp_input = page.locator(
        'input[name*="code"], input[id*="code"], input[placeholder*="code" i], '
        'input[type="number"], input[type="text"]:not([name*="email"])'
    ).first
    if await otp_input.count() > 0:
        await otp_input.fill(otp_code, timeout=5000)
    else:
        # Fallback: type into whatever input is focused
        await page.keyboard.type(otp_code, delay=60)

    verify_btn = page.locator(
        'button:has-text("Sign in"), button:has-text("Submit"), '
        'button:has-text("Verify"), button:has-text("Continue"), '
        'button[type="submit"], input[type="submit"]'
    ).first
    if await verify_btn.count() > 0:
        await verify_btn.click(timeout=5000)

    await page.wait_for_timeout(3000)

    # Confirm login succeeded
    logged_in = await page.evaluate("""() => {
        const b = document.body.innerText.toLowerCase();
        const onLogin = document.querySelector('input[type="email"]') !== null
            && b.includes('send security code');
        return !onLogin;
    }""")
    if not logged_in:
        raise TimeoutError("Login failed — OTP may have been incorrect or expired.")

    print("[+] Login successful — saving session.")
    await page.context.storage_state(path=str(SESSION_FILE))

    # Reload the actual search URL after login
    await page.goto(SEARCH_QUERIES[0]["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(3000)


# ──────────────────────────────────────────────────────────────────────────────
# JOB LISTING SCRAPER
# ──────────────────────────────────────────────────────────────────────────────

async def collect_jobs(page: Page) -> list[dict]:
    """Scrape all job cards from all Greenhouse search queries, de-duplicated."""
    all_jobs: list[dict] = []
    seen_urls: set[str] = set()

    for query_meta in SEARCH_QUERIES:
        search_url = query_meta["url"]
        query_type = query_meta["type"]
        print(f"[+] Loading: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(3000)

        # Scroll to trigger lazy-load
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(800)

        # Parse job cards
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

        # Fallback: link scan
        if not page_jobs:
            print("[i] Card parsing found 0 — falling back to link scan.")
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
            # De-dup within this page only (local set so merge step still works)
            local_seen: set[str] = set()
            for lnk in links:
                if lnk["apply_url"] not in local_seen:
                    local_seen.add(lnk["apply_url"])
                    page_jobs.append(lnk)

        # Merge into all_jobs, skipping cross-query duplicates
        for job in page_jobs:
            if job["apply_url"] not in seen_urls:
                seen_urls.add(job["apply_url"])
                job["query_type"] = query_type
                all_jobs.append(job)

        print(f"[+] {len(page_jobs)} listing(s) from this query (total so far: {len(all_jobs)})")

    print(f"[+] Total unique jobs across all queries: {len(all_jobs)}")
    return all_jobs


# ──────────────────────────────────────────────────────────────────────────────
# APPLICATION FILLER
# ──────────────────────────────────────────────────────────────────────────────

async def fill_by_label(page: Page, keywords: list, value: str) -> bool:
    """Fill a plain text input whose label contains ALL keywords.
    Iterates all matches so a combobox appearing first doesn't block a plain input."""
    if not value:
        return False
    try:
        pattern = re.compile(".*".join(re.escape(k) for k in keywords), re.IGNORECASE)
        els = page.get_by_label(pattern)
        count = await els.count()
        for i in range(count):
            el = els.nth(i)
            role = await el.get_attribute("role")
            if role and "combobox" in role:
                continue
            await el.scroll_into_view_if_needed(timeout=2000)
            await el.fill(value, timeout=4000)
            return True
    except Exception:
        pass
    return False


async def click_react_select(page: Page, label_pattern: str, option_start: str) -> bool:
    """
    Open ALL Greenhouse react-select dropdowns whose label matches label_pattern
    and pick the first option starting with option_start.
    Skips dropdowns that already have a value selected.
    """
    if not option_start:
        return False
    filled_any = False
    try:
        els = page.get_by_label(re.compile(label_pattern, re.IGNORECASE))
        total = await els.count()
        for i in range(total):
            el = els.nth(i)
            control = el.locator('xpath=ancestor::div[contains(@class,"select__control")][1]')
            if await control.count() == 0:
                # Fallback: click element directly
                control = el
            # Skip if already has a value
            if await control.locator('[class*="select__single-value"]').count() > 0:
                continue
            try:
                await control.scroll_into_view_if_needed(timeout=2000)
                await control.click(timeout=3000)
                await page.wait_for_timeout(700)
                opts = page.locator('[class*="select__option"]')
                for j in range(await opts.count()):
                    text = (await opts.nth(j).inner_text()).strip()
                    if text.lower().startswith(option_start.lower()):
                        await opts.nth(j).click(timeout=3000)
                        filled_any = True
                        await page.wait_for_timeout(300)
                        break
                else:
                    await page.keyboard.press("Escape")
            except Exception:
                pass
    except Exception:
        pass
    return filled_any


async def select_first_react_option(page: Page, label_pattern: str) -> bool:
    """Open the react-select matching label_pattern and pick its first available option."""
    try:
        els = page.get_by_label(re.compile(label_pattern, re.IGNORECASE))
        for i in range(await els.count()):
            el = els.nth(i)
            control = el.locator('xpath=ancestor::div[contains(@class,"select__control")][1]')
            if await control.count() == 0:
                continue
            if await control.locator('[class*="select__single-value"]').count() > 0:
                continue
            await control.scroll_into_view_if_needed(timeout=2000)
            await control.click(timeout=3000)
            await page.wait_for_timeout(700)
            opts = page.locator('[class*="select__option"]')
            if await opts.count() > 0:
                await opts.first.click(timeout=3000)
                return True
            await page.keyboard.press("Escape")
    except Exception:
        pass
    return False


async def click_react_select_years(page: Page, label_pattern: str, target: int = 2) -> bool:
    """
    Open a react-select dropdown matching label_pattern and pick the option
    whose numeric range includes `target` years (default 2).
    Handles: "0-3", "1-3 years", "2+", "Less than 3", "3-5", etc.
    """
    try:
        els = page.get_by_label(re.compile(label_pattern, re.IGNORECASE))
        for i in range(await els.count()):
            el = els.nth(i)
            control = el.locator('xpath=ancestor::div[contains(@class,"select__control")][1]')
            if await control.count() == 0:
                continue
            if await control.locator('[class*="select__single-value"]').count() > 0:
                continue
            await control.scroll_into_view_if_needed(timeout=2000)
            await control.click(timeout=3000)
            await page.wait_for_timeout(700)

            opts = page.locator('[class*="select__option"]')
            best = None
            for j in range(await opts.count()):
                text = (await opts.nth(j).inner_text()).strip()
                t = text.lower()
                # "N+" pattern: covers target if N <= target
                m = re.search(r'(\d+)\s*\+', t)
                if m and int(m.group(1)) <= target:
                    best = j
                    break
                # "X-Y" or "X to Y" range
                m = re.search(r'(\d+)\s*[-–to]+\s*(\d+)', t)
                if m:
                    lo, hi = int(m.group(1)), int(m.group(2))
                    if lo <= target <= hi:
                        best = j
                        break

            if best is not None:
                await opts.nth(best).click(timeout=3000)
                return True
            await page.keyboard.press("Escape")
    except Exception:
        pass
    return False


async def type_react_select(page: Page, label_pattern: str, text: str) -> bool:
    """
    Type into a react-select search input (for Country, Location autocompletes)
    and click the first matching option. Uses keyboard typing for React compatibility.
    """
    if not text:
        return False
    try:
        els = page.get_by_label(re.compile(label_pattern, re.IGNORECASE))
        total = await els.count()
        for i in range(total):
            el = els.nth(i)
            ctrl = el.locator('xpath=ancestor::div[contains(@class,"select__control")][1]')
            if await ctrl.count() == 0:
                continue
            if await ctrl.locator('[class*="select__single-value"]').count() > 0:
                continue  # already filled
            await ctrl.scroll_into_view_if_needed(timeout=2000)
            await ctrl.click(timeout=3000)
            await page.wait_for_timeout(400)
            await page.keyboard.type(text, delay=40)
            await page.wait_for_timeout(1000)
            opts = page.locator('[class*="select__option"]')
            count = await opts.count()
            for j in range(count):
                opt_text = (await opts.nth(j).inner_text()).strip()
                if text.lower() in opt_text.lower():
                    await opts.nth(j).click(timeout=3000)
                    return True
            if count > 0:
                await opts.first.click(timeout=3000)
                return True
            await page.keyboard.press("Escape")
    except Exception:
        pass
    return False


async def click_react_select_by_text(page: Page, text_pattern: str, option_start: str) -> bool:
    """
    Find a react-select control near a DOM element whose visible text matches
    text_pattern, then click the option containing option_start.
    Searches both from text nodes downward AND from select controls upward.
    """
    js = f"""async () => {{
        const pattern = new RegExp({repr(text_pattern)}, 'i');
        const optText = {repr(option_start.lower())};

        async function tryClick(ctrl) {{
            if (ctrl.querySelector('[class*="select__single-value"]')) return false;
            ctrl.click();
            await new Promise(r => setTimeout(r, 800));
            const opts = Array.from(document.querySelectorAll('[class*="select__option"]'));
            if (!opts.length) return false;
            const opt = opts.find(o => o.innerText.trim().toLowerCase().includes(optText));
            if (opt) {{ opt.click(); return true; }}
            document.body.click();
            return false;
        }}

        // Approach 1: walk up from matching text nodes
        const candidates = Array.from(document.querySelectorAll('div,p,span,label,legend'));
        const textEl = candidates.find(el =>
            el.children.length <= 3 && pattern.test(el.innerText) && el.innerText.length < 800
        );
        if (textEl) {{
            let node = textEl;
            for (let i = 0; i < 8; i++) {{
                node = node.parentElement;
                if (!node) break;
                const ctrl = node.querySelector('[class*="select__control"]');
                if (ctrl) return tryClick(ctrl);
            }}
        }}

        // Approach 2: walk up from unfilled react-select controls to find label text
        const controls = Array.from(document.querySelectorAll('[class*="select__control"]'))
            .filter(c => !c.querySelector('[class*="select__single-value"]'));
        for (const ctrl of controls) {{
            let node = ctrl;
            for (let i = 0; i < 8; i++) {{
                node = node.parentElement;
                if (!node) break;
                const label = node.querySelector('label,legend,p,[class*="question"]');
                if (label && pattern.test(label.innerText)) {{
                    return tryClick(ctrl);
                }}
            }}
        }}
        return false;
    }}"""
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def click_no_radio(page: Page, label_pattern: str) -> bool:
    """Click the No radio button in a group whose surrounding label matches label_pattern."""
    js = f"""() => {{
        const pat = new RegExp({repr(label_pattern)}, 'i');
        const containers = Array.from(document.querySelectorAll('div,fieldset,li,section,p'));
        for (const el of containers) {{
            const lbl = el.querySelector('label,legend,p,h3,h4,span[class*="label"],div[class*="label"],div[class*="question"]');
            if (!lbl || !pat.test(lbl.innerText) || lbl.innerText.length > 600) continue;
            const radios = Array.from(el.querySelectorAll('input[type="radio"]'));
            if (!radios.length) continue;
            const no = radios.find(r => {{
                const val  = (r.value || '').trim().toLowerCase();
                const wrap = (r.closest('label') || r.parentElement || {{}});
                const txt  = (wrap.innerText || '').trim().toLowerCase();
                return val === 'no' || txt === 'no';
            }});
            if (no && !no.checked) {{ no.click(); return true; }}
        }}
        return false;
    }}"""
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def click_checkbox_option(page: Page, label_pattern: str, option_text: str) -> bool:
    """Check a checkbox whose wrapper text contains option_text, inside a group matching label_pattern."""
    js = f"""() => {{
        const pat = new RegExp({repr(label_pattern)}, 'i');
        const optL = {repr(option_text.lower())};
        const containers = Array.from(document.querySelectorAll('div,fieldset,li,section'));
        for (const el of containers) {{
            const lbl = el.querySelector('label,legend,p,h3,h4,span[class*="label"],div[class*="label"]');
            if (!lbl || !pat.test(lbl.innerText) || lbl.innerText.length > 400) continue;
            const boxes = Array.from(el.querySelectorAll('input[type="checkbox"]'));
            if (!boxes.length) continue;
            for (const box of boxes) {{
                const wrap = box.closest('label') || box.parentElement || {{}};
                const txt  = (wrap.innerText || box.value || '').trim().toLowerCase();
                if (txt.includes(optL) && !box.checked) {{ box.click(); return true; }}
            }}
        }}
        return false;
    }}"""
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def select_option_by_label(page: Page, label_pattern: str, option_text: str) -> bool:
    """Select a native <select> option near a label matching label_pattern."""
    if not option_text:
        return False
    try:
        pattern = re.compile(label_pattern, re.IGNORECASE)
        el = page.get_by_label(pattern).first
        if await el.count() == 0:
            return False
        tag = await el.evaluate("e => e.tagName.toLowerCase()")
        if tag != "select":
            return False
        options = await el.locator("option").all_text_contents()
        match = next((o for o in options if option_text.lower() in o.lower()), None)
        if match:
            await el.select_option(label=match, timeout=5000)
            return True
    except Exception:
        pass
    return False


async def fill_any_by_text(page: Page, text_pattern: str, value: str) -> bool:
    """
    Universal filler: locates any form control (react-select, select2, native select,
    radio, text input) near DOM text matching text_pattern and fills/selects with value.
    """
    js = f"""async () => {{
        const pat = new RegExp({repr(text_pattern)}, 'i');
        const valL = {repr(value.lower())};
        const valF = {repr(value)};

        async function tryCtrl(container) {{
            // React-select
            const rs = container.querySelector('[class*="select__control"]');
            if (rs && !rs.querySelector('[class*="select__single-value"]')) {{
                rs.click();
                await new Promise(r => setTimeout(r, 1000));
                const opts = Array.from(document.querySelectorAll('[class*="select__option"]'));
                const opt = opts.find(o => o.innerText.trim().toLowerCase().includes(valL));
                if (opt) {{ opt.click(); await new Promise(r => setTimeout(r, 300)); return true; }}
                document.body.click();
            }}
            // Select2 v4 (class*="select2-selection")
            const s2v4 = container.querySelector('[class*="select2-selection"]');
            if (s2v4) {{
                const ph4 = s2v4.querySelector('[class*="placeholder"]');
                const rd4 = s2v4.querySelector('[class*="rendered"]');
                const empty4 = ph4 || !rd4 || !rd4.textContent.trim() || rd4.textContent.trim() === 'Select...';
                if (empty4) {{
                    s2v4.click();
                    await new Promise(r => setTimeout(r, 1000));
                    const opts4 = Array.from(document.querySelectorAll('.select2-results__option, .select2-results ul li'));
                    const opt4 = opts4.find(o => o.innerText.trim().toLowerCase().includes(valL));
                    if (opt4) {{ opt4.click(); await new Promise(r => setTimeout(r, 300)); return true; }}
                    document.body.click();
                }}
            }}
            // Select2 v3 (class*="select2-container", .select2-choice)
            const s2v3 = container.querySelector('.select2-container, [class*="select2-container"]');
            if (s2v3) {{
                const chosen = s2v3.querySelector('.select2-chosen');
                if (!chosen || !chosen.innerText.trim() || chosen.innerText.trim() === 'Select...') {{
                    const trigger = s2v3.querySelector('.select2-choice, .select2-default, a[class*="select2"]') || s2v3;
                    trigger.click();
                    await new Promise(r => setTimeout(r, 1000));
                    const opts3 = Array.from(document.querySelectorAll(
                        '.select2-result-label, .select2-results li[role="option"], .select2-results li'
                    ));
                    const opt3 = opts3.find(o => o.innerText.trim().toLowerCase().includes(valL));
                    if (opt3) {{ opt3.click(); await new Promise(r => setTimeout(r, 300)); return true; }}
                    document.body.click();
                    // Fallback: set hidden select value via jQuery
                    const hiddenSel = container.querySelector('select');
                    if (hiddenSel && typeof $ !== 'undefined') {{
                        const opt = Array.from(hiddenSel.options).find(o => o.text.toLowerCase().includes(valL));
                        if (opt) {{ $(hiddenSel).val(opt.value).trigger('change'); return true; }}
                    }}
                }}
            }}
            // Native select (also handles jQuery-hidden selects)
            const sel = container.querySelector('select');
            if (sel && getComputedStyle(sel).display !== 'none') {{
                const opt = Array.from(sel.options).find(o => o.text.toLowerCase().includes(valL));
                if (opt) {{ sel.value = opt.value; sel.dispatchEvent(new Event('change', {{bubbles: true}})); return true; }}
            }}
            // Hidden select with jQuery trigger (select2 hidden input)
            if (sel && typeof $ !== 'undefined') {{
                const opt = Array.from(sel.options).find(o => o.text.toLowerCase().includes(valL));
                if (opt) {{ $(sel).val(opt.value).trigger('change'); return true; }}
            }}
            // Radio buttons
            const radios = Array.from(container.querySelectorAll('input[type="radio"]'));
            if (radios.length) {{
                const r = radios.find(r => {{
                    const lbl = r.closest('label') || r.parentElement;
                    const t = (lbl ? lbl.innerText : (r.value || '')).trim().toLowerCase();
                    return t === valL || t.includes(valL);
                }});
                if (r && !r.checked) {{ r.click(); return true; }}
            }}
            return false;
        }}

        // Walk all text-bearing elements looking for one that matches our pattern
        for (const el of Array.from(document.querySelectorAll('div,p,span,label,legend'))) {{
            if (!pat.test(el.innerText) || el.innerText.length > 1200 || el.children.length > 10) continue;
            let node = el;
            for (let i = 0; i < 10; i++) {{
                if (!node) break;
                if (await tryCtrl(node)) return true;
                node = node.parentElement;
            }}
        }}
        return false;
    }}"""
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def fill_greenhouse_selects(page: Page) -> list:
    """
    Final-pass filler for Greenhouse React-Select custom questions.
    Uses JS to DISCOVER unfilled controls + their labels, then uses
    Playwright's real mouse click (not DOM .click()) to open each
    dropdown and select the matching option.
    Returns list of (question_snippet → answer) strings that were filled.
    """
    rules = [
        (r"referred.*current employee|current employee.*refer",                    "no"),
        (r"referred.*someone.*works|were you referred.*enterprise|referred.*works at", "no"),
        (r"legally authorized.*work|confirm.*authorized.*work|authorized.*us.*canada", "yes"),
        (r"hold.*visa.*type|what type.*visa|visa.*type",                           "none"),
        (r"california.*privacy notice|applicant privacy notice",                   "continue"),
        (r"please answer the following",                                            "yes"),
        (r"discharged.*resign|asked to resign|terminated",                         "no"),
        (r"reside.*kansas.*missouri|kansas.*missouri",                              "no"),
        (r"100%.*on site|onsite.*location|olathe",                                 "yes"),
        (r"require.*employment visa sponsorship|require.*visa sponsorship",         "no"),
        (r"former.*energy solutions.*intern|former.*energy solutions.*employ",      "no"),
        # Must come BEFORE the generic sponsor→no rule
        (r"does not provide visa sponsorship.*do you acknowledge|do you acknowledge.*permanent work auth.*eligible|permanent work auth.*without employer sponsorship.*eligible", "yes"),
        (r"currently hold.*temporary work auth|hold.*cpt.*opt|cpt.*opt.*stem.*expiration", "no"),
        (r"sponsor",                                                                "no"),
        (r"non.compete|restrictive covenant",                                       "no"),
        (r"relative.*employed|employ.*family",                                      "no"),
        (r"previously.*employed.*company|have you worked for",                      "no"),
        (r"previously.*employed.*subsidiaries|employed.*subsidiaries.*affiliates",  "no"),
        (r"ever.*interned.*employed.*applied|interned.*employed.*applied",          "no"),
        (r"ever.*employed.*applied.*position",                                      "no"),
        (r"willing to relocate|open to relocate",                                   "yes"),
        (r"authorized.*work.*united states|legally.*work.*us",                      "yes"),
        (r"salary requirements|what are your salary",                               "70"),
        (r"opt in.*text mess|text mess.*opt|would.*like.*text mess|sms.*opt",       "no"),
        (r"relationship.*current.*pei|pei.*associate|relationship.*current.*associate", "no"),
        (r"please review.*accept.*terms|review.*applicant.*statement|accept.*terms.*application|accept all the terms", "i certify"),
        (r"fp.?a.*team|financial planning.*analysis.*team|worked.*fp.?a",           "yes"),
        (r"experience.*building.*reports.*manually|building.*reports.*manually|reports.*manually.*excel", "yes"),
        (r"restaurant.*retail.*hospitality|supported.*restaurant.*retail|retail.*hospitality.*past", "no"),
        (r"describe.*gender.{0,20}identit|gender.{0,20}identit.*describe|how.*describe.*gender|i identify my gender", "decline"),
        (r"describe.*racial|which ethnicities|ethnic.*background|racial.*ethnic",   "decline"),
        (r"have.*disability|identify.*disability|disability.*chronic",              "decline"),
        (r"military veteran|veteran.*service member|service member.*veteran|identify.*veteran|armed forces", "decline"),
        (r"sexual orient|lgbtq|lesbian.*gay|lgb.{0,5}community",                   "decline"),
        # O2EP / general location/drug questions
        (r"reside in southern california|currently reside.*southern california|southern california", "no"),
        (r"willing.*drug test|drug test.*law|submit.*drug test",                    "yes"),
        (r"experience.*business enterprise|accounting.*systems|enterprise.*software", "yes"),
        (r"3.?5 years.*similar position|similar position.*3.?5 years|years of experience.*similar", "yes"),
        # Perry Ellis / accept terms
        (r"accept.*applicant.*statement|review.*accept.*application|agree.*terms.*application", "i certify"),

        # ── New rules for previously failing companies ─────────────────────────
        # Fairlife / internal employee of company
        (r"internal.*employee.*fairlife|ever been.*employee.*fairlife|employee.*coca.cola|internal.*employee.*coca", "no"),
        # SMS recruiting consent (Fairlife and others)
        (r"consent.*receive.*sms|consent.*sms.*message|sms.*message.*recruiting|recruiting.*sms|i consent.*sms", "no"),
        # Client/partner/competitor employment (Addepar)
        (r"employed.*client.*partner.*competitor|client.*partner.*competitor", "no"),
        # Generic data/job applicant privacy notice
        (r"job applicant.*data.*privacy|data privacy.*notice|applicant.*data.*privacy", "continue"),
        # Azure experience (Notexternal/Bpcs)
        (r"2\+.*years.*experience.*azure|years.*experience.*azure|experience.*azure.*2\+|years.*azure", "yes"),
        # Data engineering fundamentals (Notexternal/Bpcs)
        (r"2\+.*years.*data engineer|data engineering.*fundamental|years.*data engineer", "yes"),
        # Microsoft vendor/employee (Notexternal/Bpcs)
        (r"worked.*microsoft.*vendor|microsoft.*employee.*vendor|ever worked.*microsoft.*vendor", "no"),
        # Onsite Redmond WA (Notexternal/Bpcs)
        (r"onsite.*redmond|comfortable.*onsite.*redmond|comfortable.*coming.*onsite.*redmond", "no"),
        # Agency experience (Dept)
        (r"\bagency experience\b|have.*agency.*experience|do you have.*agency.*experience", "no"),
        # Client-facing experience (Dept)
        (r"client.facing experience|client facing experience|have.*client.*facing", "yes"),
        # DEPT® privacy statement
        (r"dept.*privacy.*statement|privacy.*statement.*dept", "continue"),
        # Referred by DEPT® employee
        (r"referred.*dept.*employ|dept.*employ.*refer|referred.*job.*dept.*employee", "no"),
        # One-year contract OK (Charterschoolgrowthfund)
        (r"one year contract.*extension.*ok|contract.*possibility.*extension.*ok|extension.*ok with you", "yes"),
        # Portfolio school employee — select YES to accept condition
        (r"current employee.*portfolio.*school|emeritus.*portfolio.*school|portfolio.*school.*select.*yes", "yes"),
        # Federal government client experience (Devtechnology)
        (r"experience.*federal.*government.*client|federal.*government.*client|supporting.*federal.*government", "no"),
        # US government citizenship requirement (Devtechnology / Effectual)
        (r"us government.*requires.*citizenship|government.*requires.*us.*citizen|position requires.*us.*citizen.*requirement|candidates.*be.*u\.?s\.?\s*citizen.*requirement", "no"),
        # Minimum 3 years BA experience (Devtechnology)
        (r"minimum.*3 years.*business analyst|3 years.*business analyst|minimum.*business analyst.*experience", "yes"),
        # Technical background React/Node (Devtechnology)
        (r"technical.*background.*react|technical.*background.*exposure.*react|development.*technical.*background.*react", "yes"),
        # Jira and Confluence experience (Devtechnology)
        (r"experience.*jira.*confluence|jira.*and.*confluence|jira.*confluence", "yes"),
        # Reston VA headquarters orientation/interview (Devtechnology)
        (r"reston.*headquarters|reston.*va.*headquarters|onsite.*reston.*orientation|reston.*orientation", "yes"),
        # Fully remote states list — MA is included (Devtechnology)
        (r"fully remote.*primary address.*following.*states|remote.*primary address.*states|candidates.*primary address.*following states", "yes"),
        # Mobile messages consent (Devtechnology)
        (r"agree.*receive.*mobile messages|mobile.*messages.*job application|mobile.*messages.*relation.*job", "yes"),
        # Farm credit association (Compeerfinancial)
        (r"farm credit.*association|worked.*farm credit|previously.*farm credit", "no"),
        # Compeer Financial team member
        (r"current team member.*compeer|previous team member.*compeer|team member.*compeer", "no"),
        # Open to relocating / within driving distance (Compeerfinancial)
        (r"open.*relocating.*driving distance|within driving distance|currently.*driving distance", "yes"),
        # SQL expertise level (Dept)
        (r"expertise.*level.*sql|sql.*expertise.*level|level.*expertise.*sql|what.*expertise.*sql", "advanced"),
        # Python/R expertise level (Dept)
        (r"expertise.*python.*r|expertise.*level.*python|level.*expertise.*python|python.*r.*expertise", "advanced"),
        # CJA: current or former employee
        (r"current or former.*employee.*cja|current.*former.*employee.*cja|former.*employee.*cja", "no"),
        # CJA: data pipeline / ETL experience
        (r"have.*worked.*data.*pipeline|data.*pipeline.*etl.*process|worked.*with.*data.*pipeline", "yes"),
        # CJA: KPI / business metrics validation comfort
        (r"comfortable.*validating.*kpi|validating.*kpi|comfort.*kpi.*business|kpi.*validating", "comfortable"),
        # CJA: best describe experience level
        (r"best describe.*your.*experience|how would you best describe.*experience", "mid"),
        # CJA: SQL experience level
        (r"level.*sql.*experience|sql.*experience.*level|what.*level.*sql", "advanced"),
        # Seattle area — No (Truveta)
        (r"located.*greater.*seattle|greater.*seattle.*area", "no"),
    ]

    # Step 1: JS discovery — return {inputId, label} for each unfilled react-select control
    # Using the input's id to find its <label for="..."> avoids picking the wrong sibling label
    controls_info = await page.evaluate("""() => {
        const controls = Array.from(document.querySelectorAll('[class*="select__control"]'))
            .filter(c => c.querySelector('[class*="select__placeholder"]') !== null);
        return controls.map(ctrl => {
            const input = ctrl.querySelector('input');
            const inputId = input ? input.id : '';
            // Prefer label[for=id] association; fall back to DOM parent walk
            let qText = '';
            if (inputId) {
                const lbl = document.querySelector('label[for="' + inputId + '"]');
                if (lbl) qText = lbl.innerText.trim();
            }
            if (!qText) {
                let node = ctrl;
                for (let i = 0; i < 14; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    const lbl = node.querySelector('label');
                    if (lbl) { qText = lbl.innerText.trim(); break; }
                }
            }
            return { inputId, label: qText };
        });
    }""")

    # When the short answer keyword doesn't appear literally in option text,
    # try these expanded phrases in order.
    answer_fallbacks = {
        "yes":      ["yes", "i verify", "i confirm", "i agree", "i certify", "authorized to work",
                     "eligible", "i am authorized"],
        "no":       ["no", "i am not", "not authorized", "i do not", "never", "none of the above"],
        "none":     ["none", "do not hold", "i do not hold", "no visa", "n/a", "not applicable",
                     "i do not currently hold"],
        "continue": ["continue", "i have read", "acknowledge", "i certify", "i accept", "i agree"],
        "decline":  ["decline", "prefer not", "choose not", "i don't wish", "i do not wish",
                     "i prefer not"],
        "70":        ["70", "60", "80", "$60", "$70", "$80", "$65", "$75", "70,000", "60,000", "80,000"],
        "i certify": ["i certify", "i verify", "i confirm", "i agree", "i accept", "i have read",
                      "all of the information", "foregoing applicant"],
        "advanced":   ["advanced", "expert", "proficient", "high", "experienced", "senior level"],
        "comfortable": ["comfortable", "very comfortable", "confident", "proficient"],
        "mid":        ["mid", "mid-level", "intermediate", "some experience", "moderate"],
    }

    rules_compiled = [(re.compile(pat, re.IGNORECASE), val) for pat, val in rules]
    filled = []

    label_preview = [i.get('label','')[:40] for i in (controls_info or []) if i.get('label')]
    print(f"    [gh-scan] found {len(controls_info or [])} unfilled selects: {label_preview}")

    # Step 2: For each matching control, use Playwright's real click via input ID
    for item in (controls_info or []):
        label_text = item.get('label', '')
        input_id   = item.get('inputId', '')
        if not label_text:
            continue

        answer = None
        for pattern, val in rules_compiled:
            if pattern.search(label_text):
                answer = val
                break
        if not answer:
            print(f"    [gh-scan] no rule for: {label_text[:70]!r}")
            continue

        print(f"    [gh-try] {label_text[:50]!r} → {answer!r}")

        # Direct target: the select__control div containing this input
        if input_id:
            control = page.locator(f'div[class*="select__control"]:has(input#{input_id})').first
        else:
            control = None

        clicked = False
        if control and await control.count() > 0:
            try:
                if await control.locator('[class*="select__single-value"]').count() > 0:
                    continue  # already filled
                await control.scroll_into_view_if_needed(timeout=2000)
                await control.click(timeout=3000)
                await page.wait_for_timeout(900)

                opts = page.locator('[class*="select__option"]')
                n_opts = await opts.count()
                all_opt_texts = []
                for j in range(n_opts):
                    all_opt_texts.append((await opts.nth(j).inner_text()).strip())
                if all_opt_texts:
                    print(f"    [gh-opts] {all_opt_texts[:8]}")

                # Try answer directly, then each fallback phrase
                candidates = [answer.lower()] + [
                    p for p in answer_fallbacks.get(answer.lower(), [])
                    if p != answer.lower()
                ]
                for candidate in candidates:
                    for j, text in enumerate(all_opt_texts):
                        if candidate in text.lower():
                            await opts.nth(j).click(timeout=3000)
                            filled.append(f"{label_text[:60]} → {answer}")
                            await page.wait_for_timeout(300)
                            clicked = True
                            break
                    if clicked:
                        break

                if not clicked and n_opts > 0:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(600)
                    await page.evaluate("document.body.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}))")
                    await page.wait_for_timeout(400)
            except Exception as e:
                print(f"    [gh-err] {label_text[:40]!r}: {e}")

        if not clicked:
            # Fallback: JS mousedown to open + click option
            ans_lower = answer.lower()
            fallback_phrases = answer_fallbacks.get(ans_lower, [ans_lower])
            if ans_lower not in fallback_phrases:
                fallback_phrases = [ans_lower] + fallback_phrases
            import json as _json
            phrases_js = _json.dumps(fallback_phrases)
            input_id_js = input_id or ''
            js_fallback = f"""async () => {{
                const inputId = {repr(input_id_js)};
                const phrases = {phrases_js};
                let ctrl = null;
                if (inputId) {{
                    const inp = document.getElementById(inputId);
                    if (inp) {{
                        let n = inp;
                        for (let i = 0; i < 10; i++) {{
                            n = n.parentElement;
                            if (!n) break;
                            if ((n.className||'').includes('select__control')) {{ ctrl = n; break; }}
                        }}
                    }}
                }}
                if (!ctrl) return false;
                if (ctrl.querySelector('[class*="select__single-value"]')) return false;
                ctrl.dispatchEvent(new MouseEvent('mousedown',{{bubbles:true,cancelable:true,view:window}}));
                ctrl.dispatchEvent(new MouseEvent('mouseup',{{bubbles:true,cancelable:true,view:window}}));
                ctrl.click();
                await new Promise(r=>setTimeout(r,1200));
                const opts = Array.from(document.querySelectorAll('[class*="select__option"]'));
                for (const phrase of phrases) {{
                    const opt = opts.find(o=>o.innerText.trim().toLowerCase().includes(phrase));
                    if (opt) {{
                        opt.dispatchEvent(new MouseEvent('mousedown',{{bubbles:true,cancelable:true,view:window}}));
                        opt.click();
                        await new Promise(r=>setTimeout(r,400));
                        return true;
                    }}
                }}
                document.body.dispatchEvent(new MouseEvent('mousedown',{{bubbles:true,cancelable:true,view:window}}));
                return false;
            }}"""
            try:
                if await page.evaluate(js_fallback):
                    filled.append(f"{label_text[:60]} → {answer} [js]")
                    await page.wait_for_timeout(300)
                    print(f"    [gh-js] filled via mousedown fallback")
            except Exception:
                pass

    # Step 3: Legacy jQuery/select2 hidden-select fallback
    rules_json = json.dumps([[p, v] for p, v in rules])
    legacy_js = f"""() => {{
        const rules = {rules_json};
        function findAnswer(text) {{
            for (const [pat, ans] of rules) {{
                if (new RegExp(pat, 'i').test(text)) return ans;
            }}
            return null;
        }}
        const hiddenSels = Array.from(document.querySelectorAll(
            'select[name^="answers["], select[id^="question_"]'
        )).filter(s => !s.value || s.value === '' || s.value === '0');
        const filled = [];
        for (const sel of hiddenSels) {{
            let container = sel.parentElement;
            for (let i = 0; i < 8 && container; i++) {{
                if ((container.id || '').startsWith('question_') || container.classList.contains('question')) break;
                container = container.parentElement;
            }}
            const lbl = container ? container.querySelector('label') : null;
            const qText = lbl ? lbl.innerText.trim() : '';
            const answer = qText ? findAnswer(qText) : null;
            if (!answer) continue;
            const opt = Array.from(sel.options).find(o => o.text.toLowerCase().includes(answer));
            if (!opt) continue;
            if (typeof $ !== 'undefined') {{
                $(sel).val(opt.value).trigger('change');
            }} else {{
                sel.value = opt.value;
                sel.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
            filled.push(qText.substring(0, 60) + ' → ' + answer + ' [jq]');
        }}

        return filled;
    }}"""
    try:
        legacy_result = await page.evaluate(legacy_js)
        filled.extend(legacy_result or [])
    except Exception:
        pass

    return filled


async def click_yes_radio(page: Page, label_pattern: str) -> bool:
    """Click the Yes radio button in a group whose surrounding label matches label_pattern."""
    js = f"""() => {{
        const pat = new RegExp({repr(label_pattern)}, 'i');
        const containers = Array.from(document.querySelectorAll('div,fieldset,li,section,p'));
        for (const el of containers) {{
            const lbl = el.querySelector('label,legend,p,h3,h4,span[class*="label"],div[class*="label"],div[class*="question"]');
            if (!lbl || !pat.test(lbl.innerText) || lbl.innerText.length > 600) continue;
            const radios = Array.from(el.querySelectorAll('input[type="radio"]'));
            if (!radios.length) continue;
            const yes = radios.find(r => {{
                const val  = (r.value || '').trim().toLowerCase();
                const wrap = (r.closest('label') || r.parentElement || {{}});
                const txt  = (wrap.innerText || '').trim().toLowerCase();
                return val === 'yes' || txt === 'yes';
            }});
            const target = yes || radios[0];
            if (target && !target.checked) {{ target.click(); return true; }}
        }}
        return false;
    }}"""
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def check_all_except_none(page: Page, label_pattern: str) -> int:
    """Check every checkbox in a group matching label_pattern except options containing 'none'."""
    js = f"""() => {{
        const pat = new RegExp({repr(label_pattern)}, 'i');
        const containers = Array.from(document.querySelectorAll('div,fieldset,section'));
        for (const el of containers) {{
            const lbl = el.querySelector('label,legend,p,h3,h4,span[class*="label"],div[class*="label"],div[class*="question"]');
            if (!lbl || !pat.test(lbl.innerText) || lbl.innerText.length > 600) continue;
            const boxes = Array.from(el.querySelectorAll('input[type="checkbox"]'));
            if (!boxes.length) continue;
            let count = 0;
            for (const box of boxes) {{
                const wrap = box.closest('label') || box.parentElement || {{}};
                const txt  = (wrap.innerText || box.value || '').trim().toLowerCase();
                if (txt.includes('none')) continue;
                if (!box.checked) {{ box.click(); count++; }}
            }}
            if (count) return count;
        }}
        return 0;
    }}"""
    try:
        return int(await page.evaluate(js))
    except Exception:
        return 0


async def navigate_to_application_form(page: Page, job: dict) -> None:
    """
    Navigate to the Greenhouse application form.
    Handles: direct form URLs, linked Apply buttons, and in-page Apply buttons.
    """
    await page.goto(job["apply_url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(2000)

    # Already on a form page if submit button is present
    if await page.locator('button[type="submit"], input[type="submit"]').count() > 0:
        return

    # Look for "Apply for this Job" link/button
    apply_btn = page.locator(
        'a:has-text("Apply for this Job"), a:has-text("Apply Now"), '
        'a:has-text("Apply Here"), button:has-text("Apply for this Job"), '
        'a[class*="apply"], a[data-provides="apply"]'
    ).first

    if await apply_btn.count() > 0:
        href = await apply_btn.get_attribute("href")
        if href and href.startswith("http"):
            await page.goto(href, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        else:
            await apply_btn.click(timeout=5000)
        await page.wait_for_timeout(2000)
        return

    # Scroll down — the form may render below the job description
    for _ in range(8):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(300)
        if await page.locator('button[type="submit"], input[type="submit"]').count() > 0:
            return


async def fill_application(page: Page, info: dict) -> tuple[bool, str]:
    """Fill and submit a Greenhouse application form. Returns (success, note)."""

    # Scroll through the whole form once to trigger lazy rendering
    for _ in range(8):
        await page.evaluate("window.scrollBy(0, 400)")
        await page.wait_for_timeout(200)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)

    # ── Personal details ─────────────────────────────────────────────────────
    await safe_fill(page, 'input[id*="first_name"], input[name*="first_name"], input[autocomplete="given-name"]',  info["first_name"])
    await safe_fill(page, 'input[id*="last_name"],  input[name*="last_name"],  input[autocomplete="family-name"]', info["last_name"])
    await safe_fill(page, 'input[id*="email"],      input[name*="email"],      input[type="email"]',               info["email"])
    await safe_fill(page, 'input[id*="phone"],      input[name*="phone"],      input[type="tel"]',                 info["phone"])

    # ── Preferred / alternate first name ─────────────────────────────────────
    await fill_by_label(page, ["preferred", "first"], info["first_name"])
    await fill_by_label(page, ["preferred name"],     info["first_name"])

    # ── Full legal name / location (xAI-style fields) ─────────────────────────
    await fill_by_label(page, ["full legal name"],  "Osborne V. Lopes")
    await fill_by_label(page, ["your location"],    "Boston, Massachusetts")

    # ── Country / Location (react-select autocomplete) ───────────────────────
    await type_react_select(page, r"country", info.get("country", "United States"))
    await click_react_select_by_text(page, r"^country", "United States")
    await type_react_select(page, r"location.*city|city.*location|^location$",
                            info.get("city", "") + ", " + info.get("state", ""))

    # ── Address (plain text fields) ───────────────────────────────────────────
    for sel in ['input[id*="address"], input[name*="address"]',
                'input[placeholder*="address" i]', 'input[placeholder*="street" i]']:
        await safe_fill(page, sel, info.get("address", ""))
    await fill_by_label(page, ["street address"],    info.get("address", ""))
    await fill_by_label(page, ["mailing address"],  info.get("address", ""))
    await fill_by_label(page, ["home address"],     info.get("address", ""))
    await fill_by_label(page, ["permanent address"], info.get("address", ""))
    await fill_by_label(page, ["current address"],  info.get("address", ""))
    await safe_fill(page, 'input[id*="state"], input[name*="state"]', info.get("state", ""))
    await safe_fill(page, 'input[id*="zip"], input[id*="postal"]',    info.get("zip", ""))
    # Combined city/state/zip field (e.g. "City, State, Zip Code")
    city_state_zip = f"{info.get('city','')}, {info.get('state','')} {info.get('zip','')}".strip()
    await fill_by_label(page, ["city, state, zip"], city_state_zip)
    await fill_by_label(page, ["city, state"],      f"{info.get('city','')}, {info.get('state','')}")
    # Plain "City" text input fallback
    await fill_by_label(page, ["city"],             info.get("city", ""))
    # Notice period (UK/EU forms) — 0 weeks if asked
    await fill_by_label(page, ["notice period"],    "0")
    # "Address 1" / "Address Line 1" custom question fields (Compeer Financial etc.)
    await fill_by_label(page, ["address 1"],         info.get("address", ""))
    await fill_by_label(page, ["address line 1"],    info.get("address", ""))
    # County field
    await fill_by_label(page, ["county"],            "Suffolk")

    # ── Resume upload ────────────────────────────────────────────────────────
    if info["resume_path"] and Path(info["resume_path"]).exists():
        try:
            resume_input = page.locator(
                'input[type="file"][id*="resume"], input[type="file"][name*="resume"], '
                'input[type="file"][accept*="pdf"], input[type="file"]'
            ).first
            if await resume_input.count() > 0:
                await resume_input.set_input_files(info["resume_path"], timeout=8000)
                print("    [resume] uploaded")
        except Exception as e:
            print(f"    [resume] upload failed: {e}")

    # ── LinkedIn / website ───────────────────────────────────────────────────
    await safe_fill(page, 'input[id*="linkedin"], input[name*="linkedin"], input[placeholder*="LinkedIn" i]', info["linkedin_url"])
    await fill_by_label(page, ["linkedin"], info["linkedin_url"])
    await safe_fill(page, 'input[id*="website"],  input[name*="website"],  input[id*="blog"]',               info.get("website", ""))

    # ── Cover letter (textarea + file upload — attaches resume when no text CL) ─
    if info.get("cover_letter"):
        await safe_fill(page, 'textarea[id*="cover"], textarea[name*="cover"]', info["cover_letter"])
    if info["resume_path"] and Path(info["resume_path"]).exists():
        try:
            cover_file = page.locator(
                'input[type="file"][id*="cover"], input[type="file"][name*="cover"]'
            ).first
            if await cover_file.count() > 0:
                await cover_file.set_input_files(info["resume_path"], timeout=8000)
                print("    [cover] attached resume as cover letter file")
        except Exception:
            pass

    # ── "Years of experience" dropdowns → pick option covering 2 years ────────
    for pat in [r"how many years", r"years of experience", r"years.*experience",
                r"experience.*years"]:
        await click_react_select_years(page, pat, target=2)

    # ── Open-text custom questions ────────────────────────────────────────────
    if info.get("core_values_answer"):
        await fill_by_label(page, ["core values", "resonates"], info["core_values_answer"])
    if info.get("tech_excitement_answer"):
        await fill_by_label(page, ["exciting technology"],       info["tech_excitement_answer"])
        await fill_by_label(page, ["most exciting"],             info["tech_excitement_answer"])
    if info.get("interest_answer"):
        await fill_by_label(page, ["sparked your interest"],     info["interest_answer"])
        await fill_by_label(page, ["interest in this role"],     info["interest_answer"])
        await fill_by_label(page, ["why are you interested"],    info["interest_answer"])
    if info.get("team_answer"):
        await fill_by_label(page, ["high performing team"],      info["team_answer"])
        await fill_by_label(page, ["characteristics of a"],      info["team_answer"])
        await fill_by_label(page, ["important characteristics"], info["team_answer"])
    if info.get("ideal_candidate_answer"):
        await fill_by_label(page, ["ideal candidate"],           info["ideal_candidate_answer"])
        await fill_by_label(page, ["makes you", "ideal"],        info["ideal_candidate_answer"])
        await fill_by_label(page, ["why are you a good fit"],    info["ideal_candidate_answer"])
        await fill_by_label(page, ["why are you the right"],     info["ideal_candidate_answer"])
    if info.get("exceptional_work_answer"):
        await fill_by_label(page, ["exceptional work"],          info["exceptional_work_answer"])
        await fill_by_label(page, ["most proud of"],             info["exceptional_work_answer"])
        await fill_by_label(page, ["proudest achievement"],      info["exceptional_work_answer"])
    if info.get("agile_answer"):
        await fill_by_label(page, ["scrum", "agile"],                          info["agile_answer"])
        await fill_by_label(page, ["prioritize", "competing stakeholders"],    info["agile_answer"])
        await fill_by_label(page, ["prioritize", "initiatives"],               info["agile_answer"])

    # ── 84.51° specific open-text answers ────────────────────────────────────
    if info.get("eighty_451_why_interested"):
        await fill_by_label(page, ["interested in 84.51"],               info["eighty_451_why_interested"])
    if info.get("eighty_451_skills"):
        await fill_by_label(page, ["knowledge and skills", "84.51"],     info["eighty_451_skills"])
        await fill_by_label(page, ["knowledge and skills", "bring to"],  info["eighty_451_skills"])

    # ── Current / most recent employer ───────────────────────────────────────
    if info.get("current_employer"):
        await fill_by_label(page, ["who is your current employer"],  info["current_employer"])
        await fill_by_label(page, ["current/most recent employer"],  info["current_employer"])
        await fill_by_label(page, ["most recent employer"],          info["current_employer"])
        await fill_by_label(page, ["current employer"],              info["current_employer"])

    # ── Previous employment → N/A when not applicable ────────────────────────
    await fill_by_label(page, ["if yes, provide dates of employment"],    "N/A")
    await fill_by_label(page, ["dates of employment", "if no"],           "N/A")
    await fill_by_label(page, ["provide previous employment details"],    "N/A")
    await fill_by_label(page, ["previous employment details", "type n/a"], "N/A")

    # ── Application sign-off / full-name disclosure fields ───────────────────
    if info.get("sign_full_name"):
        await fill_by_label(page, ["sign", "full name"],              info["sign_full_name"])
        await fill_by_label(page, ["type your full name"],            info["sign_full_name"])

    # ── AI disclaimer ("I understand") ───────────────────────────────────────
    await fill_by_label(page, ["artificial intelligence", "I understand"],  "I understand")
    await fill_by_label(page, ["use of artificial intelligence"],           "I understand")

    # ── Full residential address (Energy Solutions) ───────────────────────────
    if info.get("residential_address"):
        await fill_by_label(page, ["full residential address"],  info["residential_address"])

    # ── Energy Solutions: Python experience Yes ───────────────────────────────
    await click_react_select(page, r"1 year.*python|python.*experience.*libraries|year of python", "yes")

    # ── DHP Ace / employment detail fields ───────────────────────────────────
    if info.get("current_job_title"):
        await fill_by_label(page, ["your job title"],                    info["current_job_title"])
        await fill_by_label(page, ["current title"],                     info["current_job_title"])
        await fill_by_label(page, ["current job title"],                 info["current_job_title"])
        await fill_by_label(page, ["job title", "current"],              info["current_job_title"])
    if info.get("current_job_responsibilities"):
        await fill_by_label(page, ["job responsibilities"],              info["current_job_responsibilities"])
        await fill_by_label(page, ["your responsibilities"],             info["current_job_responsibilities"])
        await fill_by_label(page, ["what are", "responsibilities"],      info["current_job_responsibilities"])

    # ── Former-employee conditional textarea (GOSH, etc.) ───────────────────
    await fill_by_label(page, ["former employee", "brand"],    "N/A")
    await fill_by_label(page, ["former employee", "position"], "N/A")
    await fill_by_label(page, ["if you are a former employee"], "N/A")

    # ── Realtor.com / News Corp specifics ────────────────────────────────────
    await fill_by_label(page, ["known relative", "n/a"],               "N/A")
    await fill_by_label(page, ["known relative"],                       "N/A")
    # "Current Company" / "Current Title" text fields
    if info.get("current_employer"):
        await fill_by_label(page, ["current company"],                  info["current_employer"])
    # ── DHP Ace discharge + reason for leaving ────────────────────────────────
    if info.get("reason_for_leaving"):
        await fill_by_label(page, ["reason for seeking new opportunity"],    info["reason_for_leaving"])
        await fill_by_label(page, ["reason for leaving"],                    info["reason_for_leaving"])
        await fill_by_label(page, ["seeking new opportunity"],               info["reason_for_leaving"])
    for _agree_opt in ["agree", "I agree", "I have read", "Acknowledge"]:
        if await click_react_select(page, r"realtor.*privacy notice|privacy notice.*realtor|selecting agree.*privacy", _agree_opt):
            break
    await click_react_select_by_text(page, r"realtor.*privacy|privacy notice.*realtor", "agree")

    # ── Kard / multi-country work-auth (native-select fallback) ──────────────
    await select_option_by_label(page, r"legally authorized.*work.*us.*canada|authorized.*us.*canada.*argentina", "Yes")
    # "What influenced your decision to apply?" referral source
    await click_react_select(page, r"influenced.*decision.*apply|most influenced.*apply|how.*learn.*kard|hear about.*kard", info.get("referral_source", "LinkedIn"))

    # ── 84.51° visa type + California privacy (JS proximity fallback) ────────
    await click_react_select_by_text(page, r"currently hold.*visa|hold.*visa.*type|type.*visa", "none")
    await click_react_select_by_text(page, r"currently hold.*visa|hold.*visa.*type|type.*visa", "n/a")
    await click_react_select_by_text(page, r"california.*applicant.*privacy|applicant privacy notice", "continue")
    await click_react_select_by_text(page, r"california.*applicant.*privacy|applicant privacy notice", "yes")
    await click_react_select_by_text(page, r"california.*applicant.*privacy|applicant privacy notice", "acknowledge")

    # ── Cordial / experience-level dropdowns ──────────────────────────────────
    await click_react_select(page, r"optimizing.*pipeline|pipeline.*production|experience.*optim", "Extensive")
    await click_react_select(page, r"cloud environment|large dataset.*cloud|experience.*aws|aws.*experience", "Advanced")

    # ── Orchestration tools — check everything except "None" ─────────────────
    await check_all_except_none(page, r"orchestration tool|which.*tool.*production|tools.*used.*production")

    # ── References (Capital TG style) ────────────────────────────────────────
    await fill_by_label(page, ["references", "contact information"], "Available upon request")
    await fill_by_label(page, ["two references"],                    "Available upon request")

    # ── Employee referral ("were you referred by a [company] employee?") ─────
    await fill_by_label(page, ["referred by"],  "No")
    await fill_by_label(page, ["ctg employee"], "No")
    # Hillpointe plain-text referred question (INPUT type=text, not a select)
    await fill_by_label(page, ["referred to this position", "current employee"], "No")
    await fill_by_label(page, ["referred to this position"], "No")
    # Hillpointe / generic referred-by-employee questions (all widget types)
    for _ref_pat in [r"referred.*current employee", r"current employee.*refer.*position",
                     r"referred.*employee of"]:
        await click_react_select(page, _ref_pat, "no")
        await click_no_radio(page, _ref_pat)
        await select_option_by_label(page, _ref_pat, "No")
        await fill_any_by_text(page, _ref_pat, "No")

    # ── "If not referred, enter N/A" referral name text fields ───────────────
    await fill_by_label(page, ["if you were referred, by who"],    "N/A")
    await fill_by_label(page, ["if not referred, enter"],          "N/A")
    await fill_by_label(page, ["referred, by who"],                "N/A")
    await click_react_select(page, r"how.*know.*referral|identify.*referral source|not referred.*select|identify how you know", "not applicable")
    await click_react_select(page, r"identify.*referral source|identify how you know.*referral", "Not Applicable")

    # ── "How did you hear about us?" (react-select + plain text) ────────────────
    await click_react_select(page, r"hear about|referral|source", info["referral_source"])
    await fill_by_label(page, ["how did you hear"],             info.get("referral_source", "LinkedIn"))
    await fill_by_label(page, ["hear about this opportunity"],  info.get("referral_source", "LinkedIn"))
    await fill_by_label(page, ["first learn about"],            info.get("referral_source", "LinkedIn"))
    await fill_by_label(page, ["how did you first learn"],      info.get("referral_source", "LinkedIn"))

    # ── Vega-style custom widget: type "Yes" directly into location question ──
    await fill_by_label(page, ["suitable for you"],  "Yes")
    await fill_by_label(page, ["location suitable"], "Yes")

    # ── Work authorization / citizenship / age → Yes ─────────────────────────
    for pat in [r"authorized.*work", r"legally authorized", r"eligible.*employ",
                r"drivers?.?license", r"authorized.*country", r"authorized.*united states",
                r"18 years|18 or older", r"legal.*age|age.*legal",
                r"commutable distance|commut.*distance",
                r"able.*work.*united states|able.*work.*us\b",
                r"u\.?s\.?\s*citizen|us citizen|united states citizen",
                r"currently.*reside.*u\.?s|reside.*united states",
                r"public trust|dhs background|aware.*clearance|willing.*background check",
                r"background check.*required|willing.*background",
                r"experience.*redshift|experience.*dbt|experience.*databricks|experience.*following tools",
                r"essential functions|perform.*essential|essential.*position",
                r"i acknowledge|information.*true.*complete|true.*complete.*accurate",
                r"background screening|background check.*subject|subject.*background",
                r"comfortable commuting|commuting to|commute.*office|office.*commute",
                r"reliably commute|commute.*reliably",
                r"willing to relocate|open to relocate|relocate.*position",
                r"suitable for you|location suitable|position.*suitable|this location",
                r"comfortable.*onsite|onsite.*setting|comfortable.*on.?site|on.?site.*comfortable",
                r"willing to consider.*salary|salary.*within.*range|consider.*base salary|base salary.*range",
                r"bachelor.*degree|have.*bachelor|currently have.*degree",
                r"owned.*production.*platform|production.*data platform|data platform.*monitoring",
                r"databricks.*lakehouse|lakehouse.*architecture|designed.*implemented.*databricks",
                r"optimizing performance.*data|cost.*large.scale.*data|cluster tuning|job optimization",
                r"data governance.*databricks|unity catalog|access controls.*databricks|data lineage",
                r"eligible.*work.*legally|legally.*work|eligible.*work.*united states",
                r"hybrid role.*san francisco|hybrid.*chicago|required.*come into office",
                r"added to.*crm|crm.*future roles|future.*opportunities.*crm",
                r"california consumer privacy|ccpa|privacy act acknowledgement",
                r"authorized.*work.*us.*canada|authorized.*canada.*argentina|authorized.*brazil",
                r"willing.*come.*office|come in.*\d+.*days.*week|\d+.*days.*per.*week.*office",
                r"b2b saas|direct experience.*saas|saas.*company.*experience",
                r"ai.powered tools|used ai.*tools|ai tools.*productivity|ai.*enhance.*analysis",
                r"high school diploma|diploma.*equivalent.*ged|ged.*equivalent",
                r"minimum.*2 years.*relevant|minimum.*years.*work experience|at least.*2 years.*experience",
                r"2 years.*relevant work|relevant work experience.*2",
                r"python experience|1 year.*python|year of python|experience.*pandas|experience.*numpy",
                r"willing.*work onsite|onsite.*full.?time|willing.*full.?time.*onsite|onsite.*location",
                r"legally authorized.*work.*us|legally authorized.*canada.*argentina",
                r"experience.*jira.*confluence|jira.*and.*confluence|jira.*confluence",
                r"minimum.*3 years.*business analyst|3 years.*business analyst",
                r"technical.*background.*react|technical.*background.*exposure.*react",
                r"reston.*headquarters|onsite.*reston.*orientation",
                r"fully remote.*primary address.*following.*states|remote.*primary.*address.*states",
                r"one year contract.*ok|contract.*possibility.*extension.*ok",
                r"portfolio.*school.*select.*yes|current employee.*portfolio.*accept",
                r"open.*relocating.*driving distance|within driving distance",
                r"agree.*receive.*mobile messages|mobile.*messages.*job application"]:
        await click_react_select(page, pat, "yes")

    # Native-select + radio fallback for Yes questions that aren't react-select
    for _yes_pat in [r"willing.*work onsite|onsite.*full.?time",
                     r"legally authorized.*work.*us|legally authorized.*canada.*argentina",
                     r"experience.*jira.*confluence|jira.*and.*confluence",
                     r"minimum.*3 years.*business analyst|3 years.*business analyst",
                     r"technical.*background.*react|technical.*background.*exposure.*react",
                     r"reston.*headquarters|onsite.*reston",
                     r"fully remote.*primary address.*following.*states",
                     r"one year contract.*ok|contract.*extension.*ok",
                     r"open.*relocating.*driving distance|within driving distance",
                     r"agree.*receive.*mobile messages"]:
        await select_option_by_label(page, _yes_pat, "Yes")
        await click_yes_radio(page, _yes_pat)
        await fill_any_by_text(page, _yes_pat, "Yes")

    # ── Universal fallback for stubborn select/radio fields ──────────────────
    # These use fill_any_by_text which handles every widget type via JS proximity search
    _universal_yes = [
        (r"legally authorized.*work.*us.*canada|authorized.*us.*canada.*argentina", "Yes"),
        (r"willing.*work onsite.*full.?time|onsite.*full.?time.*location", "Yes"),
    ]
    _universal_no = [
        (r"referred.*current employee of hillpointe",                         "No"),
        (r"require.*employment visa sponsorship|require.*visa sponsorship",    "No"),
        (r"former.*energy solutions.*intern|former.*energy solutions.*employ", "No"),
        (r"discharged.*resign|asked to resign|terminated.*discharged",         "No"),
        (r"federal.*government.*client|experience.*federal.*government",       "No"),
        (r"employed.*client.*partner.*competitor|client.*partner.*competitor", "No"),
        (r"current team member.*compeer|previous team member.*compeer",        "No"),
        (r"farm credit.*association|worked.*farm credit",                      "No"),
        (r"current or former.*employee.*cja|former.*employee.*cja",           "No"),
        (r"microsoft.*employee.*vendor|worked.*microsoft.*vendor",             "No"),
        (r"internal.*employee.*fairlife|ever.*employee.*fairlife",             "No"),
    ]
    _universal_other = [
        (r"currently hold.*visa.*type|what type.*visa.*hold|hold a visa",      "None"),
        (r"california applicant privacy notice",                               "Continue"),
        (r"how did you hear about this opportunity",                           "LinkedIn"),
        (r"if you were not referred.*not applicable|identify.*referral.*not applicable", "Not Applicable"),
        (r"how.*know.*referral source|identify how you know.*referral",        "Not Applicable"),
        (r"please answer the following",                                       "Yes"),
        (r"reside in the state of kansas|reside.*kansas.*missouri",            "No"),
        (r"100%.*on site.*olathe|onsite.*olathe|olathe.*onsite",               "Yes"),
    ]
    for _pat, _val in _universal_yes + _universal_no + _universal_other:
        await fill_any_by_text(page, _pat, _val)

    # ── 84.51° hub relocation dropdown ───────────────────────────────────────
    await click_react_select(page, r"hub location|in-office expectation.*hub|willing.*relocate.*hub|live.*hub.*relocate", "I am willing to relocate")

    # ── Radio-button fallback for Yes questions (VEGA-style) ─────────────────
    for radio_pat in [r"suitable for you|location suitable|this location",
                      r"comfortable.*onsite|onsite.*setting|comfortable.*on.?site",
                      r"willing.*come.*office|come in.*days.*week|\d+.*days.*per.*week.*office"]:
        await click_yes_radio(page, radio_pat)
    # JS text-type fallback for location/onsite dropdowns that resist react-select
    await click_react_select_by_text(page, r"suitable for you|location suitable|this location", "yes")

    # CCPA / California Applicant Privacy Notice — try multiple option labels
    for _ccpa_opt in ["Continue", "Yes", "Acknowledge", "I have read", "I acknowledge"]:
        await click_react_select(page, r"california.*privacy|ccpa|privacy act|applicant privacy notice", _ccpa_opt)
    await click_react_select_by_text(page, r"california.*privacy|ccpa|applicant privacy notice", "Continue")
    await click_react_select_by_text(page, r"california.*privacy|ccpa|applicant privacy notice", "Yes")

    # ── Sponsorship / visa / company-specific → No ────────────────────────────
    for pat in [r"require.*sponsor", r"government sponsor", r"immigration.*benefit",
                r"export control", r"protected individual", r"department of defense",
                r"require.*visa", r"visa.*country",
                r"related.*employ|related.*staff|related.*person",
                r"previously worked|worked.*before|worked at \w+",
                r"have you worked for|worked for \w+",
                r"sponsorship",
                r"securities license",
                r"previously.*man\b|worked.*man\b",
                r"non.compete|non.solicit|restrictive covenant",
                r"subject.*non.compete|non.compete.*agreement",
                r"employed by iem|employed by ips|worked at iem|worked at ips",
                r"family member.*employ|employ.*family|family.*currently employ",
                r"relatives.*work|family.*work.*for|relatives.*family.*currently work",
                r"vega employee|referred by.*employee|employee.*referral",
                r"former.*intern|former.*employee.*company|former.*energy solutions",
                r"ever been employed.*with|employed with.*before|employed.*\w+.*before",
                r"employed.*by 84\.51|84\.51.*employ|worked.*84\.51",
                r"employed.*dunnhumby|dunnhumby.*employ",
                r"employed.*kroger\b|kroger.*co.*employ",
                r"bound.*commitments|commitments.*contracts.*affect",
                r"referred.*current employee of|current employee.*refer.*position",
                r"employed.*realtor\.com|previously.*realtor|news corp.*subsidiary",
                r"relative.*employed.*realtor|realtor.*relative|news corp.*relative",
                r"discharged.*resign|asked to resign|terminated.*discharged"]:
        await click_react_select(page, pat, "no")

    # ── Work permit (only required if under 18 — we are 18+, answer Yes/NA) ──
    await click_react_select(page, r"work permit|state.*permit", "yes")

    # ── NYC / location hybrid acceptance → Yes ────────────────────────────────
    await click_react_select(page, r"new york|nyc|on.site.*new york|hybrid.*york", "yes")

    # ── Consent / acknowledgement questions → Yes ─────────────────────────────
    await click_react_select(page, r"text messages?|sms consent|receive.*text", "yes")
    await click_react_select(page, r"privacy policy|personal information.*collect|proceeding.*yes|acknowledge.*agree", "yes")

    # ── Securities / licenses → No ────────────────────────────────────────────
    await fill_by_label(page, ["securities license"], "No")
    await click_react_select(page, r"securities license|series 7|series 63", "no")

    # ── State dropdown (react-select) ────────────────────────────────────────
    for state_pat in [r"^state\b", r"which state|state.*reside|reside.*state",
                      r"current.*state|state.*current", r"state.*live|live.*state"]:
        await click_react_select(page, state_pat, info.get("state_full", "Massachusetts"))

    # ── "Where do you currently reside?" (text or react-select) ──────────────
    reside_val = f"{info.get('city','')}, {info.get('state','')}"
    await fill_by_label(page, ["where do you currently reside"], reside_val)
    await fill_by_label(page, ["where do you reside"],           reside_val)
    await click_react_select(page, r"where.*reside|reside.*where|currently reside", reside_val)

    # ── Education level dropdown ──────────────────────────────────────────────
    for edu_pat in [r"highest.*education|level.*education|education.*level",
                    r"education.*completed|highest.*degree"]:
        await click_react_select(page, edu_pat, info.get("education", "Bachelor"))

    # ── Working preference / location preference ──────────────────────────────
    for work_pref_opt in ["Fully remote", "Remote", "Location Flexible"]:
        if await click_react_select(page, r"working preferences?|work.*preference|remote.*location.*flexible|location.*specific", work_pref_opt):
            break
    # JS proximity fallback for very-long Komodo-style labels
    await click_react_select_by_text(page, r"working preferences", "fully remote")

    # ── Clearance as plain text field ────────────────────────────────────────
    await fill_by_label(page, ["clearance", "currently hold"], "None")
    await fill_by_label(page, ["what clearance"],              "None")

    # ── Visa type → None ─────────────────────────────────────────────────────
    for _vis_opt in ["None", "N/A", "Not Applicable", "No Visa", "No Current Visa", "Do Not Hold", "No"]:
        if await click_react_select(page, r"currently hold.*visa|hold.*visa.*type|visa.*type|type.*visa|what type.*visa", _vis_opt):
            break
    await fill_by_label(page, ["currently hold", "visa"],     "None")
    await fill_by_label(page, ["what type", "visa"],          "None")

    # ── Sponsorship text-field fallback (Kard / multi-country forms) ─────────
    await fill_by_label(page, ["require sponsorship", "relevant details"],   "No, I do not require visa sponsorship.")
    await fill_by_label(page, ["sponsorship for employment visa", "include"], "No")

    # ── DHP Ace / employment date fields ─────────────────────────────────────
    if info.get("current_employer_start_date"):
        await fill_by_label(page, ["start date", "current", "employer"],     info["current_employer_start_date"])
        await fill_by_label(page, ["start date", "most recent employer"],    info["current_employer_start_date"])
    await fill_by_label(page, ["end date", "if currently employed", "n/a"], "N/A")
    await fill_by_label(page, ["end date", "most recent employer"],         "N/A")

    # ── "Please answer the following" consent/privacy fallback → Yes ─────────
    await click_react_select(page, r"please answer the following", "Yes")
    await click_react_select(page, r"please answer the following", "Continue")

    # ── DoD / security clearance → None/lowest option ───────────────────────
    _clear_pat = r"clearance status|security clearance|dod.*clearance|current.*clearance|clearance.*hold|what clearance"
    _cleared = False
    for clear_opt in ["None", "No Clearance", "Uncleared", "No clearance", "N/A", "Public Trust", "No Active"]:
        if await click_react_select(page, _clear_pat, clear_opt):
            _cleared = True
            break
    if not _cleared:
        # Last resort: open dropdown and pick last option (typically "None" or "No clearance")
        await click_react_select_by_text(page, r"clearance", "none")

    # ── Desired compensation (text) ───────────────────────────────────────────
    if info.get("salary"):
        await fill_by_label(page, ["desired total compensation"], info["salary"])
        await fill_by_label(page, ["desired compensation"],       info["salary"])
        await fill_by_label(page, ["expected compensation"],      info["salary"])
        await fill_by_label(page, ["compensation expectations"],  info["salary"])
        await fill_by_label(page, ["what are your compensation"], info["salary"])
        await fill_by_label(page, ["desired pay"],                info["salary"])
        await fill_by_label(page, ["annual base salary"],         "$70,000.00")
        await fill_by_label(page, ["specifically list"],          "$70,000.00")
        await fill_by_label(page, ["base salary", "accept"],      "$70,000.00")

    # ── City via type-react-select fallback (for autocomplete city fields) ────
    await type_react_select(page, r"^city\b", info.get("city", "Boston"))

    # ── Country — keyboard-type "United States" to filter dropdown ───────────
    await type_react_select(page, r"^country", info.get("country", "United States"))

    # ── EEO / demographic questions → Decline to self-identify ───────────────
    for eeo_pat in [r"identify.*gender|gender.*identify|i identify my gender|describe.*gender|gender.*identit",
                    r"identify as.*hispanic|hispanic.*latino|i identify as\b|describe.*racial|which ethnicities|ethnic.*background",
                    r"sexual orientation|identify.*sexual|describe.*sexual",
                    r"lgbtqia\+?|lgbtq\b|lesbian.*gay.*bisexual|identify.*part of.*lgbt|part of.*lgbtq|lgb.*community",
                    r"i have a disability|disability status|disability.*status|have.*disability.*chronic",
                    r"veteran status|race.*ethnicity|military veteran|identify.*veteran|armed forces"]:
        for eeo_opt in ["Decline", "I don't wish", "Choose not", "Prefer not", "I choose not",
                        "No", "I do not have", "I do not identify"]:
            if await click_react_select(page, eeo_pat, eeo_opt):
                break

    # ── IEM-style long consent/acknowledgement paragraphs → Yes ──────────────
    for consent_text in [r"essential functions of the position", r"I authorize.*verify", r"information.*true.*complete"]:
        await click_react_select_by_text(page, consent_text, "yes")

    # ── Years of experience fallback (plain text) ─────────────────────────────
    await fill_by_label(page, ["years of relevant experience"], "2")
    await fill_by_label(page, ["years of experience"],          "2")

    # ── City / Zip (extra selectors) ─────────────────────────────────────────
    await safe_fill(page, 'input[id*="city"],  input[name*="city"]',  info.get("city", ""))
    await safe_fill(page, 'input[placeholder*="city" i]',             info.get("city", ""))
    await fill_by_label(page, ["zip code"],     info.get("zip", ""))
    await fill_by_label(page, ["postal code"],  info.get("zip", ""))
    await safe_fill(page, 'input[placeholder*="zip" i]',              info.get("zip", ""))

    # Re-fill full residential address AFTER zip fills (prevents zip from overwriting it)
    if info.get("residential_address"):
        await fill_by_label(page, ["full residential address"],  info["residential_address"])

    # ── Salary ───────────────────────────────────────────────────────────────
    if info.get("salary"):
        await safe_fill(page, 'input[id*="salary"], input[name*="salary"]', info["salary"])
        await fill_by_label(page, ["salary"], info["salary"])

    # ── Years of experience — numeric text inputs for specific tools ─────────
    await fill_by_label(page, ["years", "dbt", "production"],          "2")
    await fill_by_label(page, ["years", "python", "data-related"],     "3")
    await fill_by_label(page, ["years", "python", "data"],             "3")
    await fill_by_label(page, ["years", "snowflake"],                  "1")
    await fill_by_label(page, ["years", "sql", "queries"],             "4")
    await fill_by_label(page, ["years", "looker"],                     "1")
    await fill_by_label(page, ["years", "jira", "confluence"],         "3")
    await fill_by_label(page, ["years", "data analysis", "validation"], "3")
    await fill_by_label(page, ["years", "qa", "qc"],                   "2")
    await fill_by_label(page, ["years", "aws"],                        "1")

    # ── Highest degree of education (text input fallback) ─────────────────────
    await fill_by_label(page, ["highest degree", "education"], info.get("education", "Bachelor's"))

    # ── How did you hear (text input fallback) ────────────────────────────────
    await fill_by_label(page, ["hear about this job"], info.get("referral_source", "LinkedIn"))

    # ── Consulting experience (Effectual) ─────────────────────────────────────
    await fill_by_label(page, ["consulting experience"],
        "Yes — I have provided data analysis consulting for academic and research projects, "
        "building dashboards and ETL pipelines for end users.")

    # ── AWS experience description (Effectual / Devtechnology) ────────────────
    await fill_by_label(page, ["describe", "aws"],
        "I have used AWS S3 for data lake storage, AWS Glue for ETL pipelines, "
        "and Redshift for data warehousing in academic and personal projects.")

    # ── Professional certifications (Devtechnology) ───────────────────────────
    await fill_by_label(page, ["professional certifications"],
        "No active certifications currently; preparing for AWS Cloud Practitioner.")

    # ── Attracted to position (Devtechnology) ────────────────────────────────
    await fill_by_label(page, ["attracted", "dev technology"],
        info.get("interest_answer", "I am drawn to the opportunity to apply my analytical and "
        "technical skills in impactful, mission-driven work."))
    await fill_by_label(page, ["attracted", "business analyst", "position"],
        info.get("interest_answer", "I am drawn to the opportunity to apply my analytical and "
        "technical skills in impactful, mission-driven work."))

    # ── Charter School Growth Fund open-text questions ────────────────────────
    await fill_by_label(page, ["dbt", "model", "design differently"],
        "I built a staging model for customer transactions that aggregated raw events into daily "
        "snapshots. If redesigning it, I would split it into modular intermediate models for "
        "better reusability and unit-testability.")
    await fill_by_label(page, ["messiest source"],
        "A manually-maintained Excel workbook with merged cells, inconsistent date formats, and "
        "row-level comments used as data flags. I standardized it by parsing each sheet separately "
        "in Python and consolidating into a unified schema.")
    await fill_by_label(page, ["validating", "dataset", "pipeline output"],
        "I validated a sales reporting pipeline by cross-referencing aggregated totals against raw "
        "source tables, catching a 15% discrepancy caused by duplicate records from a faulty ETL join.")
    await fill_by_label(page, ["ai", "generated", "code", "problem"],
        "Yes — a SQL JOIN was suggested on a non-unique key that would have caused row duplication. "
        "I caught it during code review by verifying join key cardinality before merging.")

    # ── CJA checkbox tools — check all applicable tools ──────────────────────
    await check_all_except_none(page, r"which tools.*used regularly|tools.*used regularly|regularly.*tools")

    # ── Work location preference checkboxes (Itero Group) ────────────────────
    await click_checkbox_option(page, r"work location preference|location preference", "Remote")
    await click_checkbox_option(page, r"work location preference|location preference", "Hybrid")

    # ── Truveta visa sponsorship checkbox — select F1/OPT option ─────────────
    await click_checkbox_option(page,
        r"visa sponsorship.*working.*us|require.*visa.*sponsorship.*working|visa sponsorship.*continue",
        "f1")
    await click_checkbox_option(page,
        r"visa sponsorship.*working.*us|require.*visa.*sponsorship.*working|visa sponsorship.*continue",
        "opt")

    # ── Seattle area → No radio/text (Truveta) ────────────────────────────────
    await click_no_radio(page, r"located.*greater.*seattle|greater.*seattle")
    await fill_any_by_text(page, r"located.*greater.*seattle|greater.*seattle", "No")
    await fill_by_label(page, ["not located", "greater seattle", "willing"], "Yes")
    await fill_by_label(page, ["relocate", "greater seattle"],               "Yes")
    await fill_by_label(page, ["willing to relocate", "seattle"],            "Yes")

    # ── US-citizenship-required radio → No (Effectual / Devtechnology) ────────
    for _cit_pat in [r"position requires.*u\.?s\.?\s*citizen|candidates.*be.*u\.?s\.?\s*citizen",
                     r"us government.*requires.*citizenship|government.*citizenship.*requirement"]:
        await click_no_radio(page, _cit_pat)
        await fill_any_by_text(page, _cit_pat, "No")

    # ── Legally-authorized-to-work radio (Techholding) — broader pattern ──────
    for _auth_pat in [r"legally authorized.*work.*united states.*employer",
                      r"authorized.*work.*any employer"]:
        await click_yes_radio(page, _auth_pat)
        await fill_any_by_text(page, _auth_pat, "Yes")

    # ── Visa sponsorship radio → No (Techholding) ────────────────────────────
    for _spon_pat in [r"require.*employment visa sponsorship",
                      r"will you.*require.*visa sponsorship.*future"]:
        await click_no_radio(page, _spon_pat)

    # ── Terms / consent checkboxes ────────────────────────────────────────────
    for chk_sel in [
        'input[type="checkbox"][id*="terms"]',
        'input[type="checkbox"][id*="consent"]',
        'input[type="checkbox"][id*="agree"]',
        'input[type="checkbox"][name*="terms"]',
        'input[type="checkbox"][name*="consent"]',
        'input[type="checkbox"][id*="gdpr"]',
        'input[type="checkbox"][id*="demographic"]',
        'input[type="checkbox"][id*="acknowledge"]',
    ]:
        try:
            chk = page.locator(chk_sel)
            for i in range(await chk.count()):
                if not await chk.nth(i).is_checked():
                    await chk.nth(i).check(timeout=3000)
        except Exception:
            pass

    await page.wait_for_timeout(600)

    # ── Greenhouse hidden-select pass (catches any still-unset answers[] selects) ──
    gh_filled = await fill_greenhouse_selects(page)
    if gh_filled:
        print(f"    [gh-selects] filled: {gh_filled}")
        await page.wait_for_timeout(500)

    # ── Submit ────────────────────────────────────────────────────────────────
    submit_btn = page.locator(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Submit Application"), button:has-text("Submit")'
    ).first
    if await submit_btn.count() == 0:
        return False, "No submit button found"

    try:
        try:
            await submit_btn.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
        await submit_btn.click(timeout=10000)
        await page.wait_for_timeout(4000)
    except Exception as e:
        return False, f"Submit error: {e}"

    confirmed = await page.evaluate("""() => {
        const b = document.body.innerText.toLowerCase();
        return b.includes('application submitted')
            || b.includes('thank you for applying')
            || b.includes('successfully submitted')
            || b.includes('your application has been received')
            || b.includes('application received')
            || b.includes("we'll be in touch")
            || b.includes('application complete')
            || b.includes('your information has been submitted')
            || b.includes('we have received your application')
            || b.includes('application was submitted');
    }""")
    # Also treat URL change as confirmation
    if not confirmed:
        confirmed = any(kw in page.url for kw in ["confirmation", "submitted", "success", "thank"])
    if confirmed:
        return True, "Application submitted successfully"

    errors = await page.evaluate("""() => {
        return Array.from(document.querySelectorAll(
            '[class*="error"]:not([style*="display: none"]), .field_with_errors'
        )).map(e => e.innerText.trim()).filter(Boolean).slice(0, 6);
    }""")
    if errors:
        return False, "Validation errors: " + " | ".join(errors)

    # No validation errors after submit → assume it went through
    # (Greenhouse confirmation pages vary; erring on the side of "submitted")
    return True, "Submitted (no errors detected — confirmation text not matched)"


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    # Validate resume path early
    if YOUR_INFO["resume_path"] and not Path(YOUR_INFO["resume_path"]).exists():
        print(f"[!] Resume not found: {YOUR_INFO['resume_path']}")
        print("    Update resume_path in YOUR_INFO and re-run.")
        raise SystemExit(1)

    if not YOUR_INFO["resume_path"]:
        print("[!] WARNING: resume_path is empty — applications will be submitted without a resume.")
        print("    Continuing in 5 seconds... (Ctrl+C to abort)")
        await asyncio.sleep(5)

    applied_ids = load_applied_ids()

    async with async_playwright() as p:
        # ── Browser / session ────────────────────────────────────────────────
        browser = await p.chromium.launch(headless=True, slow_mo=0)
        if SESSION_FILE.exists():
            print("[+] Using saved session.")
            context = await browser.new_context(storage_state=str(SESSION_FILE))
        else:
            context = await browser.new_context()

        page = await context.new_page()

        # ── Login (skipped if session is valid) ──────────────────────────────
        await ensure_logged_in(page, YOUR_INFO["email"])

        # ── Collect job listings ─────────────────────────────────────────────
        jobs = await collect_jobs(page)
        if not jobs:
            print("[!] No jobs found. The page structure may have changed.")
            print("    A screenshot has been saved for debugging.")
            await page.screenshot(path=str(Path(__file__).parent / "debug_greenhouse.png"))
            await browser.close()
            return

        # ── Apply to each job ────────────────────────────────────────────────
        new_applied   = 0
        skipped       = 0
        session_jobs  = []  # every job scanned this run (for email)
        prev_run_ids  = load_last_run_jobs()

        for idx, job in enumerate(jobs, 1):
            job_id = job.get("job_id") or job["apply_url"]
            title  = job.get("title", "Unknown")
            co     = job.get("company", "")
            loc    = job.get("location", "")

            # Derive company from URL slug when scraper couldn't find it
            if not co:
                m = re.search(r'greenhouse\.io/([^/]+)/jobs/', job["apply_url"])
                if m:
                    co = m.group(1).replace("-", " ").title()

            # Clear placeholder titles — real title will be scraped from the page
            if re.match(r'^view job$', title, re.I):
                title = ""

            label = f"[{idx}/{len(jobs)}] {title}" + (f" @ {co}" if co else "")

            if job_id in applied_ids:
                meta = applied_ids[job_id]
                display_title = meta.get("title") or title
                display_co    = meta.get("company") or co
                print(f"{label}  →  already applied, skipping")
                skipped += 1
                session_jobs.append({
                    "title": display_title, "company": display_co,
                    "apply_url": job["apply_url"], "status": "skipped (already applied)",
                    "applied_at": meta.get("applied_at"),
                    "is_new": job_id not in prev_run_ids,
                })
                continue

            print(f"\n{label}")
            print(f"    URL: {job['apply_url']}")

            try:
                # Skip companies on the explicit skip list
                if any(slug in job["apply_url"] for slug in SKIP_COMPANY_SLUGS):
                    print(f"    -> SKIPPED: Company on skip list")
                    append_csv({
                        "job_id": job_id, "title": title, "company": co,
                        "location": loc, "apply_url": job["apply_url"],
                        "status": "skipped", "notes": "Company on SKIP_COMPANY_SLUGS list",
                    })
                    session_jobs.append({
                        "title": title, "company": co,
                        "apply_url": job["apply_url"], "status": "skipped",
                        "is_new": job_id not in prev_run_ids,
                    })
                    continue

                # Skip senior/lead/manager roles if title is already known before navigation
                if title and SENIOR_TITLE_RE.search(title):
                    print(f"    -> SKIPPED: Senior/Lead/Manager role")
                    append_csv({
                        "job_id": job_id, "title": title, "company": co,
                        "location": loc, "apply_url": job["apply_url"],
                        "status": "skipped", "notes": "Senior/Lead/Manager role",
                    })
                    session_jobs.append({
                        "title": title, "company": co,
                        "apply_url": job["apply_url"], "status": "skipped (Senior Role)",
                        "is_new": job_id not in prev_run_ids,
                    })
                    continue

                await navigate_to_application_form(page, job)
                if page.url != job["apply_url"]:
                    print(f"    Form URL: {page.url}")

                # Scrape real title from page if we don't have one
                if not title:
                    scraped_title = await page.evaluate("""() => {
                        const h = document.querySelector('h1, [class*="app-title"], [class*="job-title"]');
                        if (h && h.innerText.trim()) return h.innerText.trim();
                        return document.title.split('|')[0].split('-')[0].trim();
                    }""")
                    if scraped_title and not re.match(r'^view job$', scraped_title, re.I):
                        title = scraped_title

                # Skip senior/lead/manager roles identified from page title
                if title and SENIOR_TITLE_RE.search(title):
                    print(f"    -> SKIPPED: Senior/Lead/Manager role ({title})")
                    append_csv({
                        "job_id": job_id, "title": title, "company": co,
                        "location": loc, "apply_url": page.url,
                        "status": "skipped", "notes": "Senior/Lead/Manager role",
                    })
                    session_jobs.append({
                        "title": title, "company": co,
                        "apply_url": page.url, "status": "skipped (Senior Role)",
                        "is_new": job_id not in prev_run_ids,
                    })
                    continue

                # Skip jobs whose page prominently shows a non-US location
                is_non_us = await page.evaluate("""() => {
                    const top = document.body.innerText.substring(0, 8000).toLowerCase();
                    return ['\\bindia\\b', 'bangalore', 'bengaluru', 'hyderabad',
                            'mumbai', 'chennai', 'new delhi', '\\bpune\\b', 'kolkata'
                    ].some(kw => top.includes(kw));
                }""")
                if is_non_us:
                    print(f"    -> SKIPPED: Non-US location detected")
                    append_csv({
                        "job_id": job_id, "title": title, "company": co,
                        "location": loc, "apply_url": page.url,
                        "status": "skipped", "notes": "Non-US location",
                    })
                    session_jobs.append({
                        "title": title, "company": co,
                        "apply_url": page.url, "status": "skipped",
                        "is_new": job_id not in prev_run_ids,
                    })
                    continue

                apply_info = dict(YOUR_INFO)
                if job.get("query_type") == "data_engineer" and DE_RESUME_PATH and Path(DE_RESUME_PATH).exists():
                    apply_info["resume_path"] = DE_RESUME_PATH
                    print(f"    [resume] using DE resume")
                elif job.get("query_type") == "data_scientist" and DS_RESUME_PATH and Path(DS_RESUME_PATH).exists():
                    apply_info["resume_path"] = DS_RESUME_PATH
                    print(f"    [resume] using DS resume")
                elif job.get("query_type") in ("data_analyst", "business_intelligence", "business_analyst"):
                    # BI resume is already the default in YOUR_INFO
                    print(f"    [resume] using BI resume")

                success, note = await fill_application(page, apply_info)

                status = "applied" if success else "failed"
                print(f"    -> {status.upper()}: {note}")

                # Debug: save HTML for any failing form (once per job_id)
                _debug_html = Path(__file__).parent / f"debug_{job_id}.html"
                if not success and not _debug_html.exists():
                    _debug_html.write_text(await page.content(), encoding="utf-8")
                    print(f"    [debug] HTML saved: {_debug_html.name}")

                append_csv({
                    "job_id":    job_id,
                    "title":     title,
                    "company":   co,
                    "location":  loc,
                    "apply_url": page.url,
                    "status":    status,
                    "notes":     note,
                })

                session_jobs.append({
                    "title": title, "company": co,
                    "apply_url": page.url, "status": status,
                    "is_new": job_id not in prev_run_ids,
                })

                if success:
                    applied_ids[job_id] = {
                        "title": title, "company": co,
                        "applied_at": datetime.now().isoformat(),
                    }
                    save_applied_ids(applied_ids)
                    new_applied += 1

                    # Screenshot as proof
                    ss_path = Path(__file__).parent / f"applied_{job_id}.png"
                    await page.screenshot(path=str(ss_path))
                    print(f"    [screenshot] {ss_path.name}")

            except Exception as exc:
                print(f"    -> ERROR: {exc}")
                append_csv({
                    "job_id": job_id, "title": title, "company": co,
                    "location": loc, "apply_url": job["apply_url"],
                    "status": "error", "notes": str(exc),
                })
                session_jobs.append({
                    "title": title, "company": co,
                    "apply_url": job["apply_url"], "status": "error",
                    "is_new": job_id not in prev_run_ids,
                })

            if idx < len(jobs):
                await asyncio.sleep(DELAY_BETWEEN)

        # Save session for next run
        await context.storage_state(path=str(SESSION_FILE))

        save_last_run_jobs({(j.get("job_id") or j["apply_url"]) for j in jobs})

        print(f"\n{'='*60}")
        print(f"[+] Done!  Applied: {new_applied}  |  Skipped: {skipped}  |  Total: {len(jobs)}")
        print(f"[+] Log saved to: {OUTPUT_CSV}")

        send_summary_email(session_jobs)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
