"""
Indeed Easy-Apply Bot
----------------------
Searches Indeed.com for target roles posted in the last 24 hours (United States),
filters for Easy Apply (Indeed Apply) jobs only, and auto-submits each application.

SETUP:
  1. Set INDEED_EMAIL and INDEED_PASSWORD in your .env file (or GitHub Secrets).
  2. Set APPLICANT_INFO_JSON, EMAIL_SENDER, GMAIL_APP_PASSWORD, EMAIL_TO.
  3. Run: python indeed_autoapply.py
  4. First run: log in manually if prompted, session is then saved automatically.

State files:
  indeed_session.json      — saved browser session (restored each run)
  indeed_applied_ids.json  — applied job IDs (prevents re-applying)
  indeed_applied.csv       — full application log
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
from playwright.async_api import (
    async_playwright, Page, BrowserContext,
    TimeoutError as PlaywrightTimeout,
)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# APPLICANT INFO
# ──────────────────────────────────────────────────────────────────────────────
_applicant_json = os.environ.get("APPLICANT_INFO_JSON", "")
if not _applicant_json:
    raise EnvironmentError(
        "APPLICANT_INFO_JSON is not set.\n"
        "  Locally:  add it to your .env file\n"
        "  GitHub Actions: add it as a repository secret"
    )
YOUR_INFO = json.loads(_applicant_json)
if os.environ.get("RESUME_PATH"):
    YOUR_INFO["resume_path"] = os.environ["RESUME_PATH"]
DE_RESUME_PATH = os.environ.get("DE_RESUME_PATH", "")
DS_RESUME_PATH = os.environ.get("DS_RESUME_PATH", "")

# ──────────────────────────────────────────────────────────────────────────────
# EMAIL / CREDENTIALS
# ──────────────────────────────────────────────────────────────────────────────
EMAIL_SENDER    = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO        = os.environ.get("EMAIL_TO", "")
INDEED_EMAIL    = os.environ.get("INDEED_EMAIL", "osborne.masters@gmail.com")
INDEED_PASSWORD = os.environ.get("INDEED_PASSWORD", "")

# ──────────────────────────────────────────────────────────────────────────────
# SEARCH CONFIG  (last 1 day, US, sorted by date)
# ──────────────────────────────────────────────────────────────────────────────
_B = "https://www.indeed.com/jobs?q={q}&l=United+States&fromage=1&sort=date&limit=50"
SEARCH_QUERIES = [
    {"url": _B.format(q="data+analyst"),           "type": "data_analyst"},
    {"url": _B.format(q="data+engineer"),          "type": "data_engineer"},
    {"url": _B.format(q="business+intelligence"),  "type": "business_intelligence"},
    {"url": _B.format(q="data+scientist"),         "type": "data_scientist"},
    {"url": _B.format(q="business+analyst"),       "type": "business_analyst"},
]

SESSION_FILE  = Path(__file__).parent / "indeed_session.json"
OUTPUT_CSV    = Path(__file__).parent / "indeed_applied.csv"
APPLIED_LOG   = Path(__file__).parent / "indeed_applied_ids.json"
LAST_RUN_FILE = Path(__file__).parent / "indeed_last_run_jobs.json"

DELAY_BETWEEN  = 5
PAGE_TIMEOUT   = 30_000

SENIOR_TITLE_RE = re.compile(
    r'\b(senior|lead|manager|director|principal|staff|head\s+of|vp\b)\b', re.I
)

# ──────────────────────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ──────────────────────────────────────────────────────────────────────────────

def load_applied_ids() -> dict:
    if APPLIED_LOG.exists():
        data = json.loads(APPLIED_LOG.read_text())
        if isinstance(data, list):
            return {jid: {"title": "", "company": "", "applied_at": None} for jid in data}
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


def append_csv(row: dict) -> None:
    fieldnames = ["job_id", "title", "company", "location", "apply_url", "status", "notes"]
    write_header = not OUTPUT_CSV.exists() or OUTPUT_CSV.stat().st_size == 0
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

# ──────────────────────────────────────────────────────────────────────────────
# EMAIL SUMMARY
# ──────────────────────────────────────────────────────────────────────────────

def send_summary_email(all_jobs: list[dict]) -> None:
    if not EMAIL_PASSWORD:
        print("[!] EMAIL_PASSWORD not set — skipping email.")
        return

    n_applied = sum(1 for r in all_jobs if r["status"] == "applied")
    n_failed  = sum(1 for r in all_jobs if r["status"] in ("failed", "error"))
    n_skipped = sum(1 for r in all_jobs if r["status"].startswith("skipped"))
    n_new     = sum(1 for r in all_jobs if r.get("is_new"))

    if n_new == 0:
        print("No new jobs this run — skipping email.")
        return

    status_color = {
        "applied":                    "#d4edda",
        "failed":                     "#f8d7da",
        "error":                      "#f8d7da",
        "skipped":                    "#f5f5f5",
        "skipped (already applied)":  "#f5f5f5",
        "skipped (Senior Role)":      "#fff3cd",
        "skipped (External Apply)":   "#e2e3e5",
    }

    def _row(r):
        bg      = status_color.get(r["status"], "#fff")
        title   = r.get("title", "")
        company = r.get("company", "")
        status  = r["status"].upper()
        url     = r.get("apply_url", "")
        applied_at = r.get("applied_at", "")
        if applied_at:
            try:
                applied_at = datetime.fromisoformat(applied_at).strftime("%b %d %I:%M %p")
            except Exception:
                pass
        new_badge = (
            " <span style='background:#0c5460;color:white;font-size:10px;"
            "padding:1px 5px;border-radius:3px;vertical-align:middle'>NEW</span>"
            if r.get("is_new") else ""
        )
        applied_cell = f"<td>{applied_at}</td>" if r["status"] == "applied" else "<td>—</td>"
        return (
            f"<tr style='background:{bg}'>"
            f"<td>{title}{new_badge}</td><td>{company}</td>"
            f"<td>{status}</td>"
            f"<td><a href='{url}'>Link</a></td>"
            f"{applied_cell}"
            f"</tr>"
        )

    rows     = "".join(_row(r) for r in all_jobs)
    subject  = (
        f"Indeed Auto-Apply: {n_applied} applied | {n_failed} failed | "
        f"{n_skipped} skipped — {len(all_jobs)} total"
    )
    body_html = f"""
    <h2>Indeed Auto-Apply Summary</h2>
    <p>
      <b style="color:#155724">Applied: {n_applied}</b> &nbsp;|&nbsp;
      <b style="color:#721c24">Failed/Error: {n_failed}</b> &nbsp;|&nbsp;
      <b>Skipped: {n_skipped}</b> &nbsp;|&nbsp;
      <b>Total scanned: {len(all_jobs)}</b> &nbsp;|&nbsp;
      <b style="color:#0c5460">New this run: {n_new}</b>
    </p>
    {f'<p style="color:#0c5460;font-size:13px">&#9733; {n_new} job(s) new this run are marked <b>NEW</b>.</p>' if n_new else ''}
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Status</th><th>Link</th><th>Applied At</th>
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
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, EMAIL_TO, msg.as_string())
        print(f"[+] Summary email sent to {EMAIL_TO}")
    except Exception as e:
        print(f"[!] Email failed: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# LOGIN
# ──────────────────────────────────────────────────────────────────────────────

async def ensure_logged_in(page: Page) -> None:
    await page.goto("https://www.indeed.com", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(2000)

    already_in = await page.evaluate("""() => {
        return (
            document.querySelector('[data-gnav-element-name="SignedIn"]') !== null ||
            document.querySelector('[aria-label*="Account"]') !== null ||
            document.body.innerText.includes('My Jobs') ||
            document.querySelector('a[href*="/myjobs"]') !== null
        );
    }""")
    if already_in:
        print("[+] Already logged in to Indeed.")
        return

    print("[i] Not logged in — attempting login...")
    if os.environ.get("CI") and not INDEED_PASSWORD:
        raise RuntimeError(
            "Indeed session expired and INDEED_PASSWORD not set.\n"
            "Run locally to refresh the session, then update INDEED_SESSION_B64 secret."
        )

    await page.goto(
        "https://www.indeed.com/account/login?hl=en_US&co=US",
        wait_until="domcontentloaded", timeout=PAGE_TIMEOUT
    )
    await page.wait_for_timeout(2000)

    # Step 1: enter email
    email_input = page.locator(
        'input[name="__email"], input[type="email"], input[autocomplete="email"]'
    ).first
    if await email_input.count() > 0:
        await email_input.fill(INDEED_EMAIL, timeout=5000)
        await page.wait_for_timeout(400)
        cont = page.locator('button[type="submit"], button:has-text("Continue")').first
        await cont.click(timeout=5000)
        await page.wait_for_timeout(2000)

    # Step 2: enter password
    pwd_input = page.locator('input[type="password"]').first
    if await pwd_input.count() > 0:
        await pwd_input.fill(INDEED_PASSWORD, timeout=5000)
        await page.wait_for_timeout(400)
        signin = page.locator('button[type="submit"], button:has-text("Sign in")').first
        await signin.click(timeout=5000)
        await page.wait_for_timeout(3000)

    # Handle 2FA / verification code
    needs_verify = await page.evaluate("""() => {
        const t = document.body.innerText.toLowerCase();
        return (
            t.includes('verification') || t.includes('security code') ||
            t.includes('confirm your email') || t.includes('one-time') || t.includes('otp')
        );
    }""")
    if needs_verify:
        if os.environ.get("CI"):
            raise RuntimeError("Indeed requires 2FA but running in CI — refresh session locally.")
        print("\n[!] Indeed sent a verification code — check your email/phone.")
        code = await asyncio.to_thread(input, "[>] Enter the verification code: ")
        code_input = page.locator(
            'input[name="code"], input[type="number"], input[type="text"]:visible'
        ).first
        if await code_input.count() > 0:
            await code_input.fill(code.strip(), timeout=5000)
        verify_btn = page.locator('button[type="submit"], button:has-text("Verify")').first
        await verify_btn.click(timeout=5000)
        await page.wait_for_timeout(3000)

    # Confirm logged in
    logged_in = await page.evaluate("""() => {
        return (
            document.querySelector('[data-gnav-element-name="SignedIn"]') !== null ||
            document.body.innerText.includes('My Jobs') ||
            document.querySelector('a[href*="/myjobs"]') !== null
        );
    }""")
    if not logged_in:
        if os.environ.get("CI"):
            raise RuntimeError("Indeed login failed in CI.")
        print("[!] Login may not have completed. If browser is open, log in manually.")
        await asyncio.to_thread(input, "[>] Press ENTER once logged in: ")

    print("[+] Logged in — saving session.")
    await page.context.storage_state(path=str(SESSION_FILE))

# ──────────────────────────────────────────────────────────────────────────────
# JOB COLLECTION
# ──────────────────────────────────────────────────────────────────────────────

async def collect_jobs(page: Page) -> list[dict]:
    all_jobs: list[dict] = []
    seen_ids: set[str]   = set()

    for q in SEARCH_QUERIES:
        print(f"[+] Searching: {q['url']}")
        await page.goto(q["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(3000)

        # Scroll to load lazy content
        for _ in range(4):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(700)

        jobs = await page.evaluate("""() => {
            const results = [];
            const cards = document.querySelectorAll('[data-jk], .job_seen_beacon, .tapItem');
            cards.forEach(card => {
                const jk = card.getAttribute('data-jk')
                    || card.querySelector('[data-jk]')?.getAttribute('data-jk')
                    || '';
                if (!jk) return;

                const titleEl = card.querySelector(
                    'h2 a span[title], h2 a span, [class*="jobTitle"] a span, '
                    '[class*="jobTitle"] span, h2 span[title]'
                );
                const companyEl = card.querySelector(
                    '[data-testid="company-name"], .companyName, [class*="companyName"]'
                );
                const locationEl = card.querySelector(
                    '[data-testid="text-location"], .companyLocation, [class*="location"]'
                );
                const cardText = card.innerText || '';
                const isEasyApply = (
                    cardText.toLowerCase().includes('easily apply') ||
                    card.querySelector('[class*="indeedApply"], [data-indeed-apply]') !== null
                );

                const rawTitle = titleEl
                    ? (titleEl.getAttribute('title') || titleEl.innerText || '').trim()
                    : (card.querySelector('h2')?.innerText || '').trim();

                results.push({
                    job_id:       jk,
                    title:        rawTitle,
                    company:      companyEl ? companyEl.innerText.trim() : '',
                    location:     locationEl ? locationEl.innerText.trim() : '',
                    apply_url:    'https://www.indeed.com/viewjob?jk=' + jk,
                    is_easy_apply: isEasyApply,
                });
            });
            const seen = new Set();
            return results.filter(j => {
                if (!j.job_id || seen.has(j.job_id)) return false;
                seen.add(j.job_id); return true;
            });
        }""")

        easy = sum(1 for j in jobs if j.get("is_easy_apply"))
        print(f"    {len(jobs)} jobs found ({easy} Easy Apply)")

        for job in jobs:
            if job["job_id"] not in seen_ids:
                seen_ids.add(job["job_id"])
                job["query_type"] = q["type"]
                all_jobs.append(job)

    print(f"[+] Total unique jobs: {len(all_jobs)}")
    return all_jobs

# ──────────────────────────────────────────────────────────────────────────────
# FORM HELPERS
# ──────────────────────────────────────────────────────────────────────────────

async def safe_fill(page: Page, selector: str, value: str) -> bool:
    if not value:
        return False
    try:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return False
        current = await loc.input_value()
        if current:
            return True
        await loc.scroll_into_view_if_needed(timeout=2000)
        await loc.fill(value, timeout=4000)
        return True
    except Exception:
        return False


async def click_radio_near_label(page: Page, label_pattern: str, value: str) -> bool:
    js = f"""() => {{
        const pat = new RegExp({repr(label_pattern)}, 'i');
        const valL = {repr(value.lower())};
        const containers = Array.from(document.querySelectorAll(
            'fieldset, div, li, section, [class*="question"]'
        ));
        for (const el of containers) {{
            const lbl = el.querySelector('label,legend,p,h3,h4,span');
            if (!lbl || !pat.test(lbl.innerText) || lbl.innerText.length > 600) continue;
            const radios = Array.from(el.querySelectorAll('input[type="radio"]'));
            if (!radios.length) continue;
            const match = radios.find(r => {{
                const wrap = r.closest('label') || r.parentElement || {{}};
                const txt  = (wrap.innerText || r.value || '').trim().toLowerCase();
                return txt === valL || txt.startsWith(valL);
            }});
            if (match && !match.checked) {{ match.click(); return true; }}
        }}
        return false;
    }}"""
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def fill_text_near_label(page: Page, label_pattern: str, value: str) -> bool:
    if not value:
        return False
    js = f"""() => {{
        const pat = new RegExp({repr(label_pattern)}, 'i');
        const val  = {repr(value)};
        const els  = Array.from(document.querySelectorAll(
            'div, label, fieldset, [class*="question"], [class*="field"]'
        ));
        for (const el of els) {{
            if (!pat.test(el.innerText) || el.innerText.length > 800) continue;
            const inp = el.querySelector('input[type="text"], input[type="number"], textarea');
            if (inp && !inp.value) {{
                inp.value = val;
                inp.dispatchEvent(new Event('input',  {{bubbles: true}}));
                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }}
        }}
        return false;
    }}"""
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def select_option_near_label(page: Page, label_pattern: str, option_text: str) -> bool:
    if not option_text:
        return False
    js = f"""() => {{
        const pat  = new RegExp({repr(label_pattern)}, 'i');
        const optL = {repr(option_text.lower())};
        const els  = Array.from(document.querySelectorAll(
            'div, label, fieldset, [class*="question"], [class*="field"]'
        ));
        for (const el of els) {{
            if (!pat.test(el.innerText) || el.innerText.length > 800) continue;
            const sel = el.querySelector('select');
            if (!sel) continue;
            const opt = Array.from(sel.options).find(o =>
                o.text.trim().toLowerCase().includes(optL)
            );
            if (opt) {{
                sel.value = opt.value;
                sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }}
        }}
        return false;
    }}"""
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# APPLY STEP HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

async def handle_contact_step(page: Page) -> None:
    info = YOUR_INFO
    await safe_fill(
        page,
        'input[name*="firstName"], input[id*="firstName"], input[placeholder*="First name" i]',
        info.get("first_name", "")
    )
    await safe_fill(
        page,
        'input[name*="lastName"], input[id*="lastName"], input[placeholder*="Last name" i]',
        info.get("last_name", "")
    )
    await safe_fill(
        page,
        'input[type="tel"], input[name*="phone"], input[id*="phone"]',
        info.get("phone", "")
    )
    await safe_fill(
        page,
        'input[type="email"], input[name*="email"]',
        info.get("email", "")
    )
    await safe_fill(
        page,
        'input[name*="city"], input[id*="city"], input[placeholder*="City" i]',
        info.get("city", "")
    )


async def handle_resume_step(page: Page, resume_path: str) -> None:
    # Try selecting an existing saved resume (radio button list)
    resume_radios = page.locator(
        'input[type="radio"][name*="resume"], '
        'input[type="radio"][id*="resume"], '
        '[class*="resumeCard"] input[type="radio"]'
    )
    if await resume_radios.count() > 0:
        await resume_radios.first.click(timeout=3000)
        print("    [resume] Selected saved resume")
        return

    # Fall back to file upload
    file_input = page.locator('input[type="file"]').first
    if await file_input.count() > 0 and resume_path and Path(resume_path).exists():
        await file_input.set_input_files(resume_path)
        await page.wait_for_timeout(2000)
        print(f"    [resume] Uploaded {Path(resume_path).name}")


async def handle_questions_step(page: Page) -> None:
    info = YOUR_INFO

    # Work authorization
    for pat in ["authorized to work", "legally authorized", "work authorization", "eligible to work"]:
        await click_radio_near_label(page, pat, "yes")
        await select_option_near_label(page, pat, "yes")

    # Visa sponsorship — No
    for pat in ["require.*sponsor", "sponsorship", "visa sponsor", "work visa"]:
        await click_radio_near_label(page, pat, "no")
        await select_option_near_label(page, pat, "no")

    # US Citizen
    await click_radio_near_label(page, r"us citizen|citizen of the united states", "yes")

    # Commute / in-person / hybrid
    for pat in [r"commute to", r"work on.?site", r"work in.?person", r"report to.*office"]:
        await click_radio_near_label(page, pat, "yes")

    # Conviction / background — No
    for pat in [r"convicted", r"felony", r"criminal record"]:
        await click_radio_near_label(page, pat, "no")

    # How did you hear
    for pat in [r"how did you hear", r"hear about this"]:
        await fill_text_near_label(page, pat, "Indeed")
        await select_option_near_label(page, pat, "Indeed")

    # Years of experience
    for pat in [r"years of experience", r"years.*experience"]:
        await fill_text_near_label(page, pat, "2")
        await select_option_near_label(page, pat, "1-3")
        await select_option_near_label(page, pat, "0-2")

    # Expected salary
    salary = info.get("desired_salary", "")
    if salary:
        for pat in [r"expected salary", r"salary expectation", r"desired salary", r"compensation"]:
            await fill_text_near_label(page, pat, salary)

    # Gender / race / disability / veteran — decline
    for pat in [r"gender", r"race", r"ethnicit", r"disability", r"veteran"]:
        await select_option_near_label(page, pat, "decline")
        await select_option_near_label(page, pat, "prefer not")
        await click_radio_near_label(page, pat, "decline")

    # LinkedIn URL
    linkedin = info.get("linkedin_url", "")
    if linkedin:
        await fill_text_near_label(page, r"linkedin", linkedin)


# ──────────────────────────────────────────────────────────────────────────────
# MULTI-STEP NAVIGATOR
# ──────────────────────────────────────────────────────────────────────────────

async def navigate_apply_steps(
    page: Page, job_type: str, resume_path: str
) -> tuple[bool, str]:
    max_steps = 15

    for step in range(max_steps):
        await page.wait_for_timeout(1200)

        page_text = await page.evaluate("() => document.body.innerText")
        lower     = page_text.lower()

        # Success detection
        if any(kw in lower for kw in [
            "application submitted", "application received", "you applied",
            "your application was sent", "successfully applied", "application complete",
        ]):
            return True, "submitted"

        # CAPTCHA guard
        has_captcha = await page.evaluate("""() =>
            document.querySelector('iframe[src*="recaptcha"], iframe[src*="captcha"]') !== null
            || document.body.innerText.toLowerCase().includes('are you a robot')
        """)
        if has_captcha:
            if os.environ.get("CI"):
                return False, "CAPTCHA detected in CI"
            await asyncio.to_thread(input, "[>] CAPTCHA detected — solve it then press ENTER: ")

        # Step: contact information
        if any(kw in lower[:600] for kw in ["contact information", "your contact", "personal information"]):
            await handle_contact_step(page)

        # Step: resume
        if any(kw in lower[:600] for kw in ["add your resume", "your resume", "upload resume", "select a resume"]):
            await handle_resume_step(page, resume_path)

        # Step: questions / screening
        await handle_questions_step(page)

        # Collect validation errors
        errors = await page.evaluate("""() =>
            Array.from(document.querySelectorAll(
                '[class*="error"]:not(script), [aria-invalid="true"], '
                '.icl-Field-error, [data-testid*="error"]'
            ))
            .map(e => e.innerText.trim())
            .filter(t => t && t.length < 200)
        """)

        # Submit button
        submit_btn = page.locator(
            'button[data-testid="ia-SubmitApplication-buttonWrapper"], '
            'button:has-text("Submit your application"), '
            'button:has-text("Submit application")'
        ).first
        if await submit_btn.count() > 0:
            await submit_btn.scroll_into_view_if_needed(timeout=2000)
            await submit_btn.click(timeout=5000)
            await page.wait_for_timeout(2500)
            continue

        # Continue / Next button
        cont_btn = page.locator(
            'button[data-testid="ia-continueButton"], '
            'button:has-text("Continue"), '
            'button:has-text("Next"), '
            'button[type="submit"]:not(:has-text("Submit your application"))'
        ).first
        if await cont_btn.count() > 0:
            disabled = await cont_btn.get_attribute("disabled")
            if disabled is not None:
                if errors:
                    return False, f"Validation errors: {' | '.join(errors[:3])}"
                return False, "Continue button disabled — unanswered required fields"
            await cont_btn.scroll_into_view_if_needed(timeout=2000)
            await cont_btn.click(timeout=5000)
            await page.wait_for_timeout(1500)
        else:
            if any(kw in lower for kw in ["submitted", "received", "applied"]):
                return True, "submitted"
            return False, "No Continue/Submit button found"

    return False, "Max steps reached without submission"

# ──────────────────────────────────────────────────────────────────────────────
# SINGLE JOB APPLICATION
# ──────────────────────────────────────────────────────────────────────────────

async def apply_to_job(
    job: dict,
    context: BrowserContext,
    applied_ids: dict,
    prev_run_ids: set,
) -> dict:
    jk        = job["job_id"]
    title     = job.get("title", "Unknown")
    company   = job.get("company", "")
    location  = job.get("location", "")
    apply_url = job.get("apply_url", "")
    job_type  = job.get("query_type", "data_analyst")
    is_new    = jk not in prev_run_ids

    result = {
        "job_id": jk, "title": title, "company": company,
        "location": location, "apply_url": apply_url,
        "status": "unknown", "notes": "", "is_new": is_new, "applied_at": None,
    }
    label = f"[{title[:45]}] @ {company[:30]}"

    # Already applied
    if jk in applied_ids:
        meta = applied_ids[jk]
        print(f"  {label}  →  already applied, skipping")
        result["status"]     = "skipped (already applied)"
        result["applied_at"] = meta.get("applied_at")
        result["title"]      = meta.get("title") or title
        result["company"]    = meta.get("company") or company
        return result

    # Senior role
    if SENIOR_TITLE_RE.search(title):
        print(f"  {label}  →  senior role, skipping")
        result["status"] = "skipped (Senior Role)"
        return result

    # External apply
    if not job.get("is_easy_apply"):
        print(f"  {label}  →  external apply, skipping")
        result["status"] = "skipped (External Apply)"
        return result

    # Choose resume
    resume_path = YOUR_INFO.get("resume_path", "")
    if job_type == "data_engineer" and DE_RESUME_PATH and Path(DE_RESUME_PATH).exists():
        resume_path = DE_RESUME_PATH
        print(f"\n  {label}  →  applying... (DE resume)")
    elif job_type == "data_scientist" and DS_RESUME_PATH and Path(DS_RESUME_PATH).exists():
        resume_path = DS_RESUME_PATH
        print(f"\n  {label}  →  applying... (DS resume)")
    else:
        print(f"\n  {label}  →  applying... (main resume)")

    page = await context.new_page()
    apply_page = page
    popup_opened = False

    try:
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(2500)

        # Confirm Easy Apply button is present on the detail page
        apply_btn = page.locator(
            'button.indeedApplyButton, '
            'button[id*="indeedApplyButton"], '
            '[data-indeed-apply-jobmeta] button, '
            'button:has-text("Apply now"):not(:has-text("company site")), '
            'span:has-text("Easily apply")'
        ).first

        if await apply_btn.count() == 0:
            result["status"] = "skipped (External Apply)"
            result["notes"]  = "No Indeed Apply button on detail page"
            print(f"    -> SKIPPED: No Indeed Apply button on detail page")
            return result

        # Click apply — might open popup or navigate
        try:
            async with context.expect_page(timeout=3000) as popup_info:
                await apply_btn.click(timeout=5000)
            apply_page = await popup_info.value
            popup_opened = True
            await apply_page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT)
        except PlaywrightTimeout:
            await apply_btn.click(force=True, timeout=5000)
            await page.wait_for_timeout(2000)
            apply_page = page

        await apply_page.wait_for_timeout(2000)

        success, notes = await navigate_apply_steps(apply_page, job_type, resume_path)

        if success:
            now = datetime.now().isoformat()
            result["status"]     = "applied"
            result["applied_at"] = now
            applied_ids[jk] = {"title": title, "company": company, "applied_at": now}
            print(f"    -> Applied successfully!")
            try:
                ss = Path(__file__).parent / f"indeed_applied_{jk}.png"
                await apply_page.screenshot(path=str(ss))
            except Exception:
                pass
        else:
            result["status"] = "failed"
            result["notes"]  = notes
            print(f"    -> FAILED: {notes}")
            try:
                dbg = Path(__file__).parent / f"indeed_debug_{jk}.html"
                if not dbg.exists():
                    dbg.write_text(await apply_page.content(), encoding="utf-8")
            except Exception:
                pass

        return result

    except PlaywrightTimeout:
        result["status"] = "error"
        result["notes"]  = "Page timeout"
        print(f"    -> ERROR: timeout")
        return result
    except Exception as exc:
        result["status"] = "error"
        result["notes"]  = str(exc)[:120]
        print(f"    -> ERROR: {exc}")
        return result
    finally:
        try:
            if popup_opened and apply_page != page:
                await apply_page.close()
        except Exception:
            pass
        try:
            await page.close()
        except Exception:
            pass

# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("Indeed Easy-Apply Bot")
    print("=" * 60)

    if not YOUR_INFO.get("resume_path"):
        print("[!] WARNING: resume_path is empty — applications may be submitted without a resume.")

    applied_ids  = load_applied_ids()
    prev_run_ids = load_last_run_jobs()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        ctx_kwargs: dict = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        if SESSION_FILE.exists():
            print("[+] Restoring saved session.")
            ctx_kwargs["storage_state"] = str(SESSION_FILE)

        context = await browser.new_context(**ctx_kwargs)
        page    = await context.new_page()

        await ensure_logged_in(page)
        await context.storage_state(path=str(SESSION_FILE))

        # Collect jobs
        print("\n[+] Collecting jobs from all queries...")
        jobs = await collect_jobs(page)
        await page.close()

        if not jobs:
            print("[!] No jobs found.")
            await browser.close()
            return

        # Apply
        print(f"\n[+] Processing {len(jobs)} jobs...\n")
        session_jobs = []
        new_applied  = 0
        skipped      = 0
        this_run_ids: set[str] = set()

        for idx, job in enumerate(jobs, 1):
            jk = job["job_id"]
            this_run_ids.add(jk)
            print(f"[{idx}/{len(jobs)}]", end=" ")

            result = await apply_to_job(job, context, applied_ids, prev_run_ids)
            session_jobs.append(result)

            if result["status"] == "applied":
                new_applied += 1
                save_applied_ids(applied_ids)
                append_csv({k: result.get(k, "") for k in
                    ["job_id", "title", "company", "location", "apply_url", "status", "notes"]})
            elif result["status"].startswith("skipped"):
                skipped += 1
            else:
                append_csv({k: result.get(k, "") for k in
                    ["job_id", "title", "company", "location", "apply_url", "status", "notes"]})

            if idx < len(jobs):
                await asyncio.sleep(DELAY_BETWEEN)

        # Persist state
        save_last_run_jobs(this_run_ids)
        await context.storage_state(path=str(SESSION_FILE))

        print(f"\n{'='*60}")
        print(f"[+] Done!  Applied: {new_applied}  |  Skipped: {skipped}  |  Total: {len(jobs)}")
        print(f"[+] Log saved to: {OUTPUT_CSV}")

        send_summary_email(session_jobs)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
