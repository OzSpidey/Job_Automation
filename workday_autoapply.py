"""
workday_autoapply.py

Workday Auto-Apply Bot — reads scraped job CSVs and applies via headless Playwright.

Env vars (all set as GitHub Secrets / local .env):
  ROLES                 comma-separated role codes: de,da,aie,ds,bi,se,swe,analyst
                        or "all" for the combined all-roles CSVs
  WD_APPLICANT_INFO     JSON blob with full applicant profile (see WD_INFO keys below)
  WORKDAY_PASSWORD      password used when creating / signing into Workday accounts
  WD_RESUME_PATH        absolute path to resume PDF on the runner
  WD_SESSION_B64        base64-encoded Playwright storage_state (updated each run)
  HEADLESS              "true" for CI, "false" for local visible browser (default: true)
  MAX_APPLY             max applications per run (default: 20)
  EMAIL_SENDER          Gmail address for summary email
  GMAIL_APP_PASSWORD    Gmail app password
  EMAIL_TO              recipient address for run summary

WD_APPLICANT_INFO JSON keys:
  first_name, last_name, email, phone
  address_line1, city, state, zip, country
  linkedin, github
  work_authorized ("Yes"/"No")   — legally authorized to work in US
  needs_sponsorship ("Yes"/"No") — requires H-1B/sponsorship now or in future
  how_did_you_hear               — default "LinkedIn"
  willing_to_relocate            — default "Yes"
  willing_to_travel              — default "No"
  salary_expectation             — leave blank to skip
  available_start                — e.g. "2 weeks" or "Immediately"
  years_experience               — string like "2"
  education  []                  — list of {school, degree, field, end, current, gpa}
  experience []                  — list of {company, title, start, end, current, description}
"""

import asyncio
import base64
import csv
import glob
import json
import os
import re
import smtplib
import sys
from datetime import datetime
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

# ── Config ─────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent

def _load_info() -> dict:
    for env_key in ("WD_APPLICANT_INFO", "APPLICANT_INFO_JSON"):
        raw = os.environ.get(env_key, "")
        if raw:
            return json.loads(raw)
    for fname in ("applicant_info_secret.txt", "txt/applicant_info_secret.txt"):
        p = ROOT / fname
        if p.exists():
            return json.loads(p.read_text())
    return {}

INFO         = _load_info()
ROLES_ENV    = os.environ.get("ROLES", "all").strip().lower()
WD_PASSWORD  = os.environ.get("WORKDAY_PASSWORD", "")
RESUME_PATH  = os.environ.get("WD_RESUME_PATH", "")
SESSION_B64  = os.environ.get("WD_SESSION_B64", "")
HEADLESS     = os.environ.get("HEADLESS", "true").lower() == "true"
MAX_APPLY    = int(os.environ.get("MAX_APPLY", "20"))
CONCURRENCY  = max(1, int(os.environ.get("WD_CONCURRENCY", "1")))  # apps in parallel

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

# Inbox that receives Workday account-verification emails (the account email used
# when creating Workday accounts). Used to auto-click the verification link via IMAP.
VERIFY_IMAP_USER = os.environ.get("WD_VERIFY_IMAP_USER", "")   # e.g. osbornelopes.neu@gmail.com
VERIFY_IMAP_PASS = os.environ.get("WD_VERIFY_IMAP_PASSWORD", "")  # 16-char Gmail app password

SESSION_FILE = ROOT / "json" / "workday_auth.json"
APPLIED_LOG  = ROOT / "json" / "workday_applied.json"
ANSWERS_FILE = ROOT / "json" / "workday_answers.json"
OUTPUT_CSV   = ROOT / "csv"  / "workday_applied.csv"

# ── Debug screenshots ────────────────────────────────────────────────────────
DEBUG_SHOTS = os.environ.get("WD_DEBUG_SHOTS", "").lower() in ("1", "true", "yes")
SHOTS_DIR = ROOT / "_wd_debug_shots"
_shot_n = [0]

async def _shot(page, label: str) -> None:
    if not DEBUG_SHOTS:
        return
    try:
        SHOTS_DIR.mkdir(exist_ok=True)
        _shot_n[0] += 1
        path = SHOTS_DIR / f"{_shot_n[0]:02d}_{label}.png"
        await page.screenshot(path=str(path), full_page=True)
        print(f"  [shot] {path.name}")
    except Exception as e:
        print(f"  [shot-err] {label}: {str(e)[:50]}")

# ── Persistence ────────────────────────────────────────────────────────────────

def load_applied() -> set:
    if APPLIED_LOG.exists():
        data = json.loads(APPLIED_LOG.read_text())
        return set(data) if isinstance(data, list) else set(data.keys())
    return set()

def save_applied(ids: set) -> None:
    APPLIED_LOG.parent.mkdir(exist_ok=True)
    APPLIED_LOG.write_text(json.dumps(sorted(ids), indent=2))

def load_answers() -> dict:
    if ANSWERS_FILE.exists():
        return json.loads(ANSWERS_FILE.read_text())
    return {}

def save_answers(answers: dict) -> None:
    ANSWERS_FILE.parent.mkdir(exist_ok=True)
    ANSWERS_FILE.write_text(json.dumps(answers, indent=2, ensure_ascii=False))

def append_csv(row: dict) -> None:
    OUTPUT_CSV.parent.mkdir(exist_ok=True)
    fieldnames = ["title", "company", "location", "link", "status", "applied_on", "notes"]
    write_header = not OUTPUT_CSV.exists() or OUTPUT_CSV.stat().st_size == 0
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)

# ── Queue ──────────────────────────────────────────────────────────────────────

_ROLE_SLUG = {
    "da":  "data_analyst",
    "de":  "data_engineer",
    "bi":  "business_intelligence",
}

def build_queue(roles: str, applied_ids: set) -> list[dict]:
    csv_dir = ROOT / "csv"
    if roles == "all":
        files = sorted(glob.glob(str(csv_dir / "workday_jobs_all*.csv")))
    else:
        role_list = [r.strip() for r in roles.split(",")]
        files = []
        for r in role_list:
            slug = _ROLE_SLUG.get(r, r)
            # Full slug name (new) + short-code legacy files. The legacy patterns
            # require an underscore/exact match so "da" doesn't also match
            # "data_engineer" (both start with "da").
            patterns = [f"workday_jobs_{slug}*.csv"]
            if r != slug:
                patterns += [f"workday_jobs_{r}_*.csv", f"workday_jobs_{r}.csv"]
            seen = set()
            for pattern in patterns:
                for f in sorted(glob.glob(str(csv_dir / pattern))):
                    if f not in seen:
                        seen.add(f)
                        files.append(f)

    seen_links: set[str] = set()
    jobs: list[dict] = []
    for f in files:
        with open(f, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                link = (row.get("link") or "").strip()
                if not link or link in seen_links or link in applied_ids:
                    continue
                seen_links.add(link)
                jobs.append(row)

    jobs.sort(key=lambda j: j.get("found_on", ""), reverse=True)
    return jobs

# ── Session management ─────────────────────────────────────────────────────────

def session_from_b64() -> dict | None:
    if not SESSION_B64:
        return None
    try:
        return json.loads(base64.b64decode(SESSION_B64).decode())
    except Exception:
        return None

def session_to_b64(state: dict) -> str:
    clean = {"cookies": state.get("cookies", []), "origins": []}
    return base64.b64encode(json.dumps(clean).encode()).decode()

# ── Playwright helpers ─────────────────────────────────────────────────────────

async def try_click(page: Page, *selectors: str, timeout: int = 5000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.wait_for(state="visible", timeout=timeout)
                await loc.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False

async def try_fill(page: Page, value: str, *selectors: str, overwrite: bool = False) -> bool:
    if not value:
        return False
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0 or not await loc.is_visible(timeout=2000):
                continue
            if not overwrite:
                current = await loc.input_value()
                if current:
                    return True  # Already filled — don't overwrite
            await loc.fill(value)
            return True
        except Exception:
            continue
    return False

async def combobox_select(page: Page, value: str, *selectors: str) -> bool:
    """Handle both native <select> and Workday's custom combobox widgets."""
    if not value:
        return False
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0 or not await loc.is_visible(timeout=2000):
                continue
            tag = await loc.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                try:
                    await loc.select_option(label=value)
                    return True
                except Exception:
                    pass
            # Workday custom combobox: click → type → pick from listbox
            await loc.click()
            await page.wait_for_timeout(400)
            await loc.fill(value[:4])
            await page.wait_for_timeout(700)
            opt = page.locator(
                f'[role="option"]:has-text("{value}"), '
                f'li[role="option"]:has-text("{value}"), '
                f'div[role="option"]:has-text("{value}")'
            ).first
            if await opt.count() > 0:
                await opt.click()
                return True
            # Try partial match
            opts = await page.locator('[role="option"]').all()
            for o in opts:
                txt = (await o.inner_text()).strip()
                if value.lower() in txt.lower():
                    await o.click()
                    return True
        except Exception:
            continue
    return False

async def combobox_select_any(page: Page, values: list[str], *selectors: str) -> bool:
    """Try each value in order, return True on first match."""
    for value in values:
        if await combobox_select(page, value, *selectors):
            return True
    return False

async def fill_hear_about_us(page: Page) -> bool:
    """Fill 'How Did You Hear About Us' — open the prompt, then try a fallback
    chain of sources (Other → LinkedIn → Glassdoor → Greenhouse → Indeed)."""
    try:
        await page.locator('.wd-icon-prompts').first.click()
        await page.wait_for_timeout(600)
        search = page.locator('[data-automation-id="searchBox"]').first
        await search.wait_for(state="visible", timeout=4000)
        for term in ["Other", "LinkedIn", "Glassdoor", "Greenhouse", "Indeed", "Company Website"]:
            try:
                await search.fill(term)
                await search.press("Enter")
                await page.wait_for_timeout(700)
                leaf = page.locator('[data-automation-id="promptLeafNode"]').first
                if await leaf.count() and await leaf.is_visible(timeout=2000):
                    await leaf.click()
                    await page.wait_for_timeout(400)
                    return True
                radio = page.locator('[data-automation-id="radioBtn"]').first
                if await radio.count() and await radio.is_visible(timeout=1500):
                    await radio.click()
                    await page.wait_for_timeout(400)
                    return True
                # No match for this term — clear and try the next
                await search.fill("")
                await page.wait_for_timeout(200)
            except Exception:
                continue
        return False
    except Exception:
        return False

async def radio_click(page: Page, answer: str) -> bool:
    """Click a radio button whose label text matches answer (Yes/No style)."""
    for sel in [
        f'label:has-text("{answer}")',
        f'[role="radio"]:has-text("{answer}")',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=1500):
                await loc.click()
                return True
        except Exception:
            continue
    return False

# ── Auth ───────────────────────────────────────────────────────────────────────

async def handle_auth(page: Page, prefer_signin: bool = False) -> bool:
    """Log in or create Workday account if prompted. Returns True on success.

    Routes by FIELD PRESENCE (not body text), so the "Create Account" link on a
    Sign-In page can't fool it:
      • verify-password field present  → create account
      • visible password field present → sign in
      • only email field present       → two-step: enter email, continue, recurse

    prefer_signin=True forces the sign-in path (used after an account was already
    created this run, e.g. Corewell: create → verify → sign in).
    """
    # Some tenants (e.g. Corewell) show a social-login chooser first — reveal the
    # email/password form by clicking "Sign in with email", then (unless we want
    # to sign in) switch to the Create Account form via its link.
    try:
        has_email_field = await page.locator(
            '[data-automation-id="email"]:visible, input[type="email"]:visible'
        ).count() > 0
        revealed = False
        if not has_email_field:
            if await try_click(page,
                '[data-automation-id="SignInWithEmailButton"]',
                'button:has-text("Sign in with email")',
                '[role="button"]:has-text("Sign in with email")',
                'a:has-text("Sign in with email")',
                'button:has-text("Continue with email")',
                timeout=3000):
                revealed = True
                await page.wait_for_timeout(1800)
        # If we just revealed a Sign-In form (no verify-password field) and there's
        # a Create Account link, switch to the create form — UNLESS prefer_signin
        # (account already exists, just sign in).
        if revealed and not prefer_signin:
            has_verify_now = await page.locator(
                '[data-automation-id="verifyPassword"]:visible, input[name*="erify" i]:visible'
            ).count() > 0
            if not has_verify_now:
                if await try_click(page,
                    '[data-automation-id="createAccountLink"]',
                    'a:has-text("Create Account")',
                    'button:has-text("Create Account")',
                    timeout=3000):
                    await page.wait_for_timeout(1800)
    except Exception:
        pass

    try:
        email_input = page.locator(
            '[data-automation-id="email"]:visible, input[type="email"]:visible'
        ).first
        has_email = await email_input.count() > 0 and await email_input.is_visible(timeout=5000)
    except Exception:
        has_email = False

    has_verify = await page.locator(
        '[data-automation-id="verifyPassword"]:visible, '
        'input[name*="erify" i]:visible, input[id*="erify" i]:visible'
    ).count() > 0
    pwd = page.locator(
        '[data-automation-id="password"]:visible, input[type="password"]:visible'
    ).first
    has_pwd = await pwd.count() > 0 and await pwd.is_visible(timeout=2000)

    # No auth fields at all → nothing to do
    if not has_email and not has_pwd and not has_verify:
        return True

    # ── Create account ──────────────────────────────────────────────────────
    if has_verify:
        return await _create_account(page)

    # ── Sign in (existing account) ──────────────────────────────────────────
    if has_pwd:
        if has_email:
            cur = ""
            try:
                cur = await email_input.input_value()
            except Exception:
                pass
            if not cur:
                await email_input.fill(INFO.get("email", ""))
                await page.wait_for_timeout(300)
        await pwd.fill(WD_PASSWORD)
        await page.wait_for_timeout(400)
        await try_click(page,
            '[data-automation-id="signInButton"]',
            '[role="button"][aria-label="Sign In"]',
            'button:has-text("Sign In")',
            'button[type="submit"]',
            timeout=6000,
        )
        await page.wait_for_timeout(2500)
        await page.wait_for_load_state("domcontentloaded")
        return True

    # ── Two-step: only email field → enter email, continue ──────────────────
    if has_email:
        await email_input.fill(INFO.get("email", ""))
        await page.wait_for_timeout(500)
        await try_click(page,
            '[data-automation-id="signInButton"]',
            '[role="button"][aria-label="Sign In"]',
            '[role="button"][aria-label="Continue"]',
            'button:has-text("Sign In")',
            'button:has-text("Continue")',
            'button[type="submit"]',
            timeout=6000,
        )
        await page.wait_for_timeout(2000)
        await page.wait_for_load_state("domcontentloaded")
        return True

    # New account — Create Account button (fallback)
    try:
        ca = page.locator(
            '[data-automation-id="createAccountButton"], button:has-text("Create Account")'
        ).first
        if await ca.is_visible(timeout=3000):
            return await _create_account(page)
    except Exception:
        pass

    return True

async def _create_account(page: Page) -> bool:
    # Target only VISIBLE fields — Workday often keeps a hidden Sign-In form in the DOM too
    await try_fill(page, INFO.get("email", ""),
        '[data-automation-id="email"]:visible', '[data-automation-id="email"]',
        'input[type="email"]:visible', overwrite=True)

    # Password + verify — click the exact VISIBLE field and type directly
    for sel in ['[data-automation-id="password"]:visible',
                '[data-automation-id="verifyPassword"]:visible']:
        try:
            fld = page.locator(sel).first
            await fld.wait_for(state="visible", timeout=5000)
            await fld.click(timeout=4000)
            await fld.fill(WD_PASSWORD)
            print(f"  [+] Filled {sel}")
        except Exception as e:
            print(f"  [!] Could not fill {sel}: {str(e)[:50]}")
    await page.wait_for_timeout(400)

    # Tick the "I agree to terms / create account" checkbox if present
    try:
        cb = page.locator(
            '[data-automation-id="createAccountCheckbox"], '
            'input[type="checkbox"][aria-required="true"]'
        ).first
        if await cb.is_visible(timeout=2000):
            if (await cb.get_attribute("aria-checked")) != "true" and not await cb.is_checked():
                await cb.click(timeout=4000)
    except Exception:
        pass

    await try_click(page,
        '[data-automation-id="createAccountButton"]',
        '[role="button"][aria-label="Create Account"]',
        'button:has-text("Create Account")',
        'button[type="submit"]',
        timeout=6000,
    )
    await page.wait_for_timeout(2500)

    body = (await page.evaluate("document.body.innerText")).lower()
    # Detect a REAL email-verification page — precise phrases only.
    # NOTE: do NOT match bare "verify" — the create-account form itself has a
    # "Verify New Password" field, which would false-trigger and hang on input().
    needs_email_verify = any(kw in body for kw in [
        "check your email", "confirmation link", "verification link",
        "we sent you", "we've sent", "verify your email",
    ])
    if needs_email_verify:
        if HEADLESS:
            print("  [!] Email verification required — needs manual action")
            return False
        print("\n  [!] Check lopes.o@northeastern.edu and click the verification link.")
        input("  Press Enter when done…")
        await page.wait_for_timeout(2000)
    return True

# ── Step detection ─────────────────────────────────────────────────────────────

async def wait_for_content(page: Page, timeout: int = 14000) -> None:
    """Wait until the step's real content has rendered (Workday shows a spinner
    between steps; the body is just 'skip to main content' until it loads)."""
    import time as _time
    deadline = _time.time() + timeout / 1000
    while _time.time() < deadline:
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        try:
            body = (await page.evaluate("document.body.innerText")).strip().lower()
        except Exception:
            body = ""
        # Meaningful once there's content beyond the skip-link / chrome
        meaningful = len(body.replace("skip to main content", "").strip()) > 40
        # Or once an actual form control / footer is present
        try:
            has_form = await page.locator(
                '[data-automation-id*="formField"], [data-automation-id="pageFooterNextButton"], '
                '[data-automation-id="password"], input, button[aria-haspopup="listbox"]'
            ).count() > 0
        except Exception:
            has_form = False
        if meaningful or has_form:
            return
        await page.wait_for_timeout(700)

async def current_step(page: Page) -> str:
    """Return lowercased title of the current form step."""
    for sel in [
        '[data-automation-id="currentStep"] span',
        '[aria-current="step"] span',
        '[data-automation-id="formContainer"] h2',
        '[data-automation-id="step"] h2',
        'h2[role="heading"]',
    ]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                return (await el.inner_text()).strip().lower()
        except Exception:
            continue
    return ""

async def click_next(page: Page) -> bool:
    return await try_click(page,
        '[data-automation-id="pageFooterNextButton"]',
        '[data-automation-id="bottom-navigation-next-button"]',
        '[data-automation-id="saveAndContinueButton"]',
        '[data-automation-id="nextButton"]',
        'button:has-text("Save and Continue")',
        'button:has-text("Next Page")',
        'button:has-text("Next")',
        'button:has-text("Continue")',
        timeout=7000,
    )

# ── Step fillers ───────────────────────────────────────────────────────────────

async def _select_phone_device_type(page: Page) -> None:
    """Click the Workday phone-type button dropdown and pick Mobile."""
    try:
        btn = page.locator('button[name="phoneType"]').first
        if not await btn.is_visible(timeout=3000):
            return
        await btn.click()
        await page.wait_for_timeout(600)
        # Options appear as buttons or divs in a listbox — try both
        for sel in [
            '[role="listbox"] button:has-text("Mobile")',
            '[role="option"]:has-text("Mobile")',
            'button[aria-label*="Mobile" i]',
            'li:has-text("Mobile")',
            'div[role="option"]:has-text("Mobile")',
        ]:
            opt = page.locator(sel).first
            if await opt.count() > 0 and await opt.is_visible(timeout=1500):
                await opt.click()
                return
        # Fallback: any visible element containing exactly "Mobile"
        opts = await page.locator('[role="listbox"] *').all()
        for o in opts:
            try:
                txt = (await o.inner_text()).strip()
                if txt == "Mobile":
                    await o.click()
                    return
            except Exception:
                continue
    except Exception:
        pass

async def fill_my_information(page: Page) -> None:
    info = INFO
    await try_fill(page, info.get("first_name", ""),
        '[data-automation-id="legalNameSection_firstName"]',
        'input[aria-label*="First Name" i]')
    await try_fill(page, info.get("last_name", ""),
        '[data-automation-id="legalNameSection_lastName"]',
        'input[aria-label*="Last Name" i]')
    await try_fill(page, info.get("phone", ""),
        '[data-automation-id="phone-number"]',
        'input[aria-label*="Phone" i]', 'input[type="tel"]',
        'input[name*="phone" i]')
    await _select_phone_device_type(page)
    await try_fill(page, info.get("address_line1", ""),
        '[data-automation-id="addressSection_addressLine1"]',
        'input[aria-label*="Address Line 1" i]', 'input[aria-label*="Street" i]')
    await try_fill(page, info.get("city", ""),
        '[data-automation-id="addressSection_city"]',
        'input[aria-label*="City" i]')
    await try_fill(page, info.get("zip", ""),
        '[data-automation-id="addressSection_postalCode"]',
        'input[aria-label*="Postal Code" i]', 'input[aria-label*="Zip" i]')
    await try_fill(page, info.get("county", "Suffolk"),
        '[data-automation-id="addressSection_regionSubdivision1"]',
        'input[name="regionSubdivision1"]',
        'input[aria-label*="County" i]')
    await combobox_select(page, info.get("country", "United States of America"),
        '[data-automation-id="countryDropdown"]',
        '[data-automation-id="addressSection_country"]',
        'select[aria-label*="Country" i]')
    await combobox_select(page, info.get("state", "Massachusetts"),
        '[data-automation-id="addressSection_countryRegion"]',
        'select[aria-label*="State" i]', 'select[aria-label*="Province" i]')

async def upload_resume(page: Page) -> bool:
    if not RESUME_PATH or not Path(RESUME_PATH).exists():
        print(f"  [!] Resume not found: {RESUME_PATH!r}")
        return False

    # If OUR resume is already attached (e.g. we uploaded it on a previous visit
    # to this step), do nothing — avoids a delete/re-upload loop.
    resume_stem = Path(RESUME_PATH).stem.lower()
    try:
        names = await page.locator(
            '[data-automation-id="file-name"], [data-automation-id*="fileName" i], '
            '[data-automation-id="attachment-name"]'
        ).all_inner_texts()
        if any(resume_stem in (n or "").lower() for n in names):
            return True
    except Exception:
        pass

    # Remove any already-attached resume(s) first (e.g. pre-filled by "use last
    # application"), so we upload a fresh copy.
    try:
        del_sel = ('[data-automation-id="delete-file"], '
                   'button[aria-label*="Delete" i], '
                   'button:has(svg.wd-icon-trash), '
                   '[data-automation-id="deleteButton"]')
        removed = 0
        for _ in range(5):  # cap to avoid loops
            btn = page.locator(del_sel).first
            if await btn.count() and await btn.is_visible(timeout=1000):
                await btn.click(timeout=4000)
                await page.wait_for_timeout(800)
                removed += 1
            else:
                break
        if removed:
            print(f"  [+] Removed {removed} existing resume(s)")
    except Exception:
        pass

    # Only attempt if the upload drop zone is actually on this page
    if not await page.locator('[data-automation-id="file-upload-drop-zone"]').is_visible(timeout=1500):
        return False
    try:
        await page.evaluate("""() => {
            document.querySelectorAll('input[type="file"]').forEach(el => {
                el.style.display = 'block';
                el.style.visibility = 'visible';
            });
        }""")
        file_input = page.locator('input[type="file"]').first
        if await file_input.count() > 0:
            await file_input.set_input_files(RESUME_PATH)
            await page.wait_for_timeout(3500)
            print(f"  [+] Resume uploaded: {Path(RESUME_PATH).name}")
            return True
    except Exception as e:
        print(f"  [!] Direct upload error: {e}")

    # Fallback: file chooser via Select files button
    try:
        async with page.expect_file_chooser(timeout=6000) as fc:
            await try_click(page,
                '[data-automation-id="select-files"]',
                'button:has-text("Select files")',
                'button:has-text("Select a File")',
                'button:has-text("Select File")',
                'button:has-text("Upload")',
                '[aria-label*="Upload" i]')
        await fc.value.set_files(RESUME_PATH)
        await page.wait_for_timeout(3500)
        print(f"  [+] Resume uploaded via file chooser")
        return True
    except Exception as e:
        print(f"  [!] File chooser error: {e}")
    return False

_ROLE_TITLE = {
    "da": "Data Analyst", "de": "Data Engineer", "bi": "Business Intelligence Analyst",
}

def _role_title() -> str:
    first = (ROLES_ENV.split(",")[0] if ROLES_ENV else "").strip()
    return _ROLE_TITLE.get(first, "Data Analyst")

async def fill_my_experience(page: Page) -> None:
    await upload_resume(page)
    info = INFO
    # LinkedIn and GitHub/Portfolio — only if field is visible and empty
    await try_fill(page, info.get("linkedin", ""),
        'input[aria-label*="LinkedIn" i]',
        'input[placeholder*="linkedin" i]',
        '[data-automation-id*="linkedIn"]')
    await try_fill(page, info.get("github", ""),
        'input[aria-label*="Website" i]',
        'input[aria-label*="GitHub" i]',
        'input[aria-label*="Portfolio" i]',
        'input[placeholder*="website" i]')
    await fill_work_experience(page)
    await fill_education(page)

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]

async def _fill_exp_date(page: Page, which: str, ym: str) -> None:
    """Set a work-experience MM/YYYY date (which='startDate'/'endDate').
    Prefers the calendar month-tile; falls back to typing the spinbuttons."""
    try:
        parts = ym.split("-")
        year, mnum = parts[0], int(parts[1])
        month_name = _MONTHS[mnum - 1]
    except Exception:
        return
    # Calendar approach: open this field's calendar, click the month tile
    try:
        icon = page.locator(f'[id*="{which}"] [data-automation-id="dateIcon"]').first
        if await icon.count() and await icon.is_visible(timeout=1500):
            await icon.click(timeout=4000)
            await page.wait_for_timeout(600)
            tile = page.locator(
                f'[data-automation-id="monthPickerTileLabel"][aria-label="{month_name} {year}"]'
            ).first
            if await tile.count() and await tile.is_visible(timeout=2000):
                await tile.click(timeout=4000)
                await page.wait_for_timeout(400)
                return
    except Exception:
        pass
    # Fallback: type into the month/year spinbuttons
    mo = page.locator(f'[id*="{which}"] [data-automation-id="dateSectionMonth-input"]').first
    yr = page.locator(f'[id*="{which}"] [data-automation-id="dateSectionYear-input"]').first
    try:
        if await mo.count() and await mo.is_visible(timeout=1000):
            await mo.click(timeout=4000)
            await page.keyboard.type(f"{mnum:02d}")
        if await yr.count() and await yr.is_visible(timeout=1000):
            await yr.click(timeout=4000)
            await page.keyboard.type(year)
    except Exception:
        pass

async def fill_work_experience(page: Page) -> None:
    """Fill a work-experience entry (job title role-aware, company, start/end dates)
    if the section is present. Only fills empty fields."""
    jt = page.locator('input[id*="jobTitle" i], [data-automation-id="jobTitle"]').first
    if not (await jt.count() and await jt.is_visible(timeout=1000)):
        return
    info = INFO
    exp_list = info.get("experience") or []
    exp = exp_list[0] if exp_list else {}
    title   = exp.get("title") or _role_title()
    company = exp.get("company") or "Atlas SP"
    start   = exp.get("start") or "2025-01"
    end     = exp.get("end") or "2025-08"
    await try_fill(page, title,
        'input[id*="jobTitle" i]', 'input[aria-label*="Job Title" i]')
    await try_fill(page, company,
        'input[id*="companyName" i]', 'input[aria-label*="Company" i]')
    await _fill_exp_date(page, "startDate", start)
    await _fill_exp_date(page, "endDate", end)

async def fill_education(page: Page) -> None:
    """Fill School/University + Field of Study. Handles text box, typeahead, or
    listbox dropdown for the school field (varies by tenant)."""
    info = INFO
    edu_list = info.get("education") or []
    edu = edu_list[0] if edu_list else {}
    school = edu.get("school") or "Northeastern University"
    field = edu.get("field") or "Computer Science"

    # School / University — try text input first, then typeahead, then listbox
    try:
        school_in = page.locator(
            'input[id*="schoolName" i], input[aria-label*="School" i], '
            'input[aria-label*="University" i], input[id*="school" i]'
        ).first
        if await school_in.count() and await school_in.is_visible(timeout=1500):
            cur = ""
            try:
                cur = await school_in.input_value()
            except Exception:
                pass
            if not cur:
                await school_in.fill(school)
                await page.wait_for_timeout(900)
                # Typeahead: click a matching suggestion if one appears
                sugg = page.locator(
                    '[role="option"], [data-automation-id="promptOption"], li[role="option"]'
                ).filter(has_text=re.compile("northeastern", re.I)).first
                if await sugg.count() and await sugg.is_visible(timeout=1500):
                    await sugg.click(timeout=4000)
        else:
            # Listbox/combobox variant
            await combobox_select(page, school,
                'button[aria-label*="School" i]', 'button[aria-label*="University" i]')
    except Exception:
        pass

    # Field of Study
    await try_fill(page, field,
        'input[id*="fieldOfStudy" i]', 'input[aria-label*="Field of Study" i]',
        '[data-automation-id="fieldOfStudy"]')

# ── Application questions ──────────────────────────────────────────────────────

def _answer_from_profile(label: str, answers: dict) -> str | None:
    """
    Map a question label to a profile value.
    Returns None for unknown questions so the caller can ask the user.
    Returns "" to explicitly skip (e.g. EEO questions we prefer not to answer).
    """
    ll = label.lower().strip()
    info = INFO

    # ── Work authorization ────────────────────────────────────────────────────
    if re.search(r'legally authorized|authorized to work|legally eligible|eligible to work|residence or work permit|work permit|legal right to work|right to work|verify.*legal right', ll):
        return info.get("work_authorized", "Yes")
    if re.search(r'at least 18|over 18|18 years|age of 18|are you 18|legal working age', ll):
        return "Yes"
    if re.search(r'require sponsorship|need.*sponsorship|will you.*sponsor|now or in the future', ll):
        return info.get("needs_sponsorship", "No")
    if re.search(r'reside.*(location|area)|commutable distance|live.*within.*commut|located within|within commut|commuting distance', ll):
        return "Yes"
    if re.search(r'willing to work on-?site|work on-?site|\bon-?site\b|willing to work in (the )?office', ll):
        return "Yes"
    if re.search(r'federal government|defense health|\bdha\b|political appointee|served in the military', ll):
        return "No"
    if re.search(r'government agency|state[- ]owned entity|state owned', ll):
        return "No"
    if re.search(r'previously worked|ever worked for|worked for .{0,30}\b(before|previously)\b|former (employee|contractor)|previously (employed|been employed)', ll):
        return "No"
    if re.search(r"enter\b.{0,6}n/?a|please enter n/?a|if not a current|if not applicable", ll):
        return "N/A"
    # "Do you have … experience …" questions → Yes (must come before the
    # years-of-experience rule below, which would otherwise return a number).
    if re.search(r'do you have\b.{0,80}\bexperience\b', ll):
        return "Yes"
    # ── EEO / self-identification dropdowns ───────────────────────────────────
    if re.search(r'what is your sex|gender', ll):
        return info.get("gender", "Male")
    if re.search(r'ethnicity|hispanic|latino|\brace\b', ll):
        return info.get("ethnicity", "Asian")
    if re.search(r'veteran', ll):
        return "__DECLINE__"
    if re.search(r'highest level of education|level of education|education or training|highest.*(degree|education)|\bdegree\b', ll):
        return "__MASTERS__"
    if re.search(r'status(es)? applies to you|which.*status|immigration status|residency status', ll):
        return info.get("work_status", "Permanent Resident")
    if re.search(r'how many years|years of .{0,40}experience|years.*experience.*do you have', ll):
        return "__YEARS_2PLUS__"

    # ── Contact info — ONLY for short field labels, so words like "State" inside a
    #    long screening question (e.g. "…Community and State") don't false-match. ──
    if len(ll) <= 35:
        if re.search(r'\bfirst name\b', ll):      return info.get("first_name")
        if re.search(r'\blast name\b', ll):       return info.get("last_name")
        if re.search(r'\bemail\b', ll):           return info.get("email")
        if re.search(r'\bphone\b', ll):           return info.get("phone")
        if re.search(r'address.*line 1|street address', ll): return info.get("address_line1")
        if re.search(r'\bcity\b', ll):            return info.get("city")
        if re.search(r'\bstate\b|\bprovince\b', ll): return info.get("state", "Massachusetts")
        if re.search(r'\bzip\b|\bpostal\b', ll):  return info.get("zip")
        if re.search(r'\bcountry\b', ll):         return info.get("country", "United States of America")
        if re.search(r'\blinkedin\b', ll):        return info.get("linkedin")
        if re.search(r'github|portfolio|personal.*website|website.*url', ll): return info.get("github")

    # ── Common screening questions ────────────────────────────────────────────
    if re.search(r'how did you hear|how did you find|referral source|source of hire', ll):
        return "__HEAR_ABOUT_US__"
    if re.search(r'salary|compensation|expected.*pay|pay expectation|annual gross|gross (figure|salary|pay)|annual (figure|amount)', ll):
        return info.get("salary_expectation") or "75000"
    if re.search(r'available.*start|start.*date|when.*start|earliest.*start', ll):
        return info.get("available_start", "")
    if re.search(r'willing.*relocat|open.*relocat', ll):
        return info.get("willing_to_relocate", "Yes")
    if re.search(r'willing.*travel|amount.*travel|travel.*percent', ll):
        return info.get("willing_to_travel", "No")
    if re.search(r'years.*experience|experience.*years|how many years', ll):
        return str(info.get("years_experience", ""))
    if re.search(r'\bpronoun', ll):
        return ""  # Skip — not mandatory typically, but skip if asked

    # ── EEO / Voluntary disclosures — prefer not to answer ───────────────────
    if re.search(r'\bgender\b|\bsex\b', ll):       return ""
    if re.search(r'\brace\b|\bethnicity\b', ll):   return ""
    if re.search(r'\bdisability\b', ll):           return ""
    if re.search(r'\bveteran\b|\bmilitary\b', ll): return ""

    # ── Check saved answers (fuzzy word overlap) ──────────────────────────────
    for saved_q, saved_a in answers.items():
        sq = saved_q.lower().strip()
        if ll == sq or ll in sq or sq in ll:
            return saved_a
        q_words = set(re.findall(r'\w{4,}', ll))
        s_words = set(re.findall(r'\w{4,}', sq))
        if q_words and s_words:
            overlap = len(q_words & s_words) / max(len(q_words), len(s_words))
            if overlap >= 0.6:
                return saved_a

    return None  # Truly unknown

async def fill_terms_and_conditions(page: Page) -> bool:
    """Handle T&C pages — detected by body text or checkbox presence.
    Page 1: dropdown asking to agree → answer Yes.
    Page 2: acceptTermsAndAgreements checkbox → tick it.
    """
    body = (await page.evaluate("document.body.innerText")).lower()
    tc_checkbox = page.locator('input[name="acceptTermsAndAgreements"]')
    has_tc_checkbox = await tc_checkbox.count() > 0
    is_terms_page = "terms and conditions" in body or "i have read and consent" in body or has_tc_checkbox
    if not is_terms_page:
        return False

    # Answer each dropdown based on ITS OWN question text:
    #   terms-confirmation question → Yes, every other question → No
    btns = await page.locator('button[aria-haspopup="listbox"]').all()
    for btn in btns:
        try:
            if not await btn.is_visible(timeout=500):
                continue
            current_val = (await btn.inner_text()).strip().lower()
            if current_val and current_val != "select one":
                continue  # Already answered
            # Read the question text from this dropdown's closest form-field ancestor
            qtext = ""
            container = btn.locator(
                'xpath=ancestor::*[contains(@data-automation-id,"formField")][1]'
            )
            if await container.count():
                qtext = (await container.inner_text()).lower()
            if not qtext:
                qtext = (await btn.get_attribute("aria-label") or "").lower()

            is_terms_q = bool(re.search(
                r"select yes.*confirm|read.{0,40}understand.{0,40}agree|agree.{0,40}terms",
                qtext,
            ))
            want = "Yes" if is_terms_q else "No"

            await btn.click(timeout=4000)
            await page.wait_for_timeout(400)
            opt = page.get_by_role("option", name=re.compile(rf"^{want}$", re.I)).first
            if not await opt.count():
                opt = page.locator(
                    f'[role="listbox"] [role="option"]:text-is("{want}"), '
                    f'[role="listbox"] div:text-is("{want}")'
                ).first
            if await opt.is_visible(timeout=1500):
                await opt.click(timeout=4000)
            await page.wait_for_timeout(300)
        except Exception:
            continue

    await page.wait_for_timeout(500)
    # Tick the accept checkbox if present
    if has_tc_checkbox and await tc_checkbox.is_visible(timeout=2000):
        if (await tc_checkbox.get_attribute("aria-checked")) != "true":
            await tc_checkbox.click(timeout=4000)
    return True

async def fill_disability_form(page: Page) -> bool:
    """Handle self-identified disability page: name, today's date, I do not want to answer."""
    name_input = page.locator('[id*="selfIdentifiedDisabilityData--name"]').first
    if not await name_input.count():
        return False
    if await name_input.is_visible(timeout=2000):
        await name_input.fill("Osborne Lopes")
    # Date: prefer the calendar "Selected Today" button; fall back to typing MM/DD/YYYY
    filled_date = False
    try:
        cal_icon = page.locator('[data-automation-id="dateIcon"]').first
        if await cal_icon.is_visible(timeout=2000):
            await cal_icon.click(timeout=4000)
            await page.wait_for_timeout(500)
            today_btn = page.locator('[data-automation-id="datePickerSelectedToday"]').first
            if await today_btn.is_visible(timeout=2000):
                await today_btn.click(timeout=4000)
                await page.wait_for_timeout(300)
                filled_date = True
    except Exception:
        pass
    if not filled_date:
        from datetime import date as _date
        today = _date.today()
        for sel, val in [
            ('[data-automation-id="dateSectionMonth-input"]', f"{today.month:02d}"),
            ('[data-automation-id="dateSectionDay-input"]',   f"{today.day:02d}"),
            ('[data-automation-id="dateSectionYear-input"]',  str(today.year)),
        ]:
            inp = page.locator(sel).first
            if await inp.is_visible(timeout=1000):
                await inp.click(timeout=4000)
                await inp.fill(val)
                await page.wait_for_timeout(150)
    # Select "No, I do not have a disability" (NOT the first option, which is "Yes")
    no_label = page.locator('label').filter(
        has_text=re.compile(r"no.{0,5}i\s*(don'?t|do not).{0,15}have.{0,5}a?\s*disability", re.I)
    ).first
    if await no_label.count() and await no_label.is_visible(timeout=2000):
        await no_label.click(timeout=4000)
    else:
        # Fallback: find the input whose sibling/own label says No-disability
        clicked = await try_click(
            page,
            'label:has-text("do not have a disability")',
            'label:has-text("don\'t have a disability")',
        )
        if not clicked:
            # Last resort: the SECOND disabilityStatus input (Yes / No / decline order)
            inputs = page.locator('input[id*="disabilityStatus"]')
            if await inputs.count() >= 2:
                await inputs.nth(1).click(timeout=4000)
    return True

async def fill_mandatory_listbox_dropdowns(page: Page, answers: dict | None = None) -> None:
    """For each unanswered required listbox dropdown, choose the answer from the
    profile based on its question text (work-auth → Yes, sponsorship → No, …).
    Falls back to "No" only when the profile has no specific answer."""
    answers = answers or {}
    btns = await page.locator('button[aria-haspopup="listbox"]').all()
    for btn in btns:
        try:
            if not await btn.is_visible(timeout=500):
                continue
            # Answer any dropdown still on "Select One" (don't require the aria-label
            # to literally say "required" — many Workday tenants omit that).
            current_val = (await btn.inner_text()).strip()
            if current_val and current_val.lower() != "select one":
                continue  # Already answered

            # Read the question text (form-field ancestor) and consult the profile
            qtext = ""
            container = btn.locator(
                'xpath=ancestor::*[contains(@data-automation-id,"formField")][1]'
            )
            if await container.count():
                qtext = (await container.inner_text()).strip()
            if not qtext:
                qtext = await btn.get_attribute("aria-label") or ""

            profile_ans = _answer_from_profile(qtext, answers)
            # Determine HOW to pick the option:
            #   "yes"/"no"     → exact match
            #   "__DECLINE__"  → a "prefer not to answer" option
            #   "__HEAR_ABOUT_US__" → handled elsewhere, skip
            #   other value (e.g. "Asian", "Male") → contains-match
            #   None           → unknown Yes/No question → default No
            if profile_ans == "__HEAR_ABOUT_US__":
                mode = "hearabout"  # listbox variant — pick a source below
            elif profile_ans == "__MASTERS__":
                mode = "masters"
            elif profile_ans == "__YEARS_2PLUS__":
                mode = "years2plus"
            elif profile_ans == "__DECLINE__":
                mode = "decline"
            elif profile_ans and profile_ans.lower() in ("yes", "no"):
                mode, want = "exact", profile_ans
            elif profile_ans and re.fullmatch(r"\d{4,7}", profile_ans.strip()):
                # Numeric profile answer (e.g. salary 75000) on a listbox →
                # the options are almost always ranges, so pick the bracket
                # that contains the number rather than an exact match.
                mode, want = "salaryrange", profile_ans.strip()
            elif profile_ans:
                mode, want = "contains", profile_ans
            else:
                mode, want = "exact", "No"

            await btn.click(timeout=4000)
            await page.wait_for_timeout(700)
            # Scope option search to the listbox this button controls (aria-controls),
            # since Workday renders the options in a popup that may lack role=listbox.
            ctrl = await btn.get_attribute("aria-controls")
            scope = page
            if ctrl and await page.locator(f'#{ctrl}').count():
                scope = page.locator(f'#{ctrl}')
            opts = scope.locator(
                '[role="option"], [data-automation-id="promptOption"], li, div'
            )
            # Diagnostic: dump what the open listbox exposes (read _wd_dd_debug.txt)
            if DEBUG_SHOTS:
                try:
                    expanded = await btn.get_attribute("aria-expanded")
                    role_opts = await page.get_by_role("option").all_inner_texts()
                    li_opts = await page.locator('[role="listbox"] li, [role="listbox"] [role="option"]').all_inner_texts()
                    with open(SHOTS_DIR.parent / "_wd_dd_debug.txt", "a", encoding="utf-8") as _f:
                        _f.write(f"\nQ={qtext[:50]!r} want={mode}/{locals().get('want','')} "
                                 f"expanded={expanded} ctrl={ctrl}\n"
                                 f"  role_options={role_opts}\n  li_options={li_opts}\n")
                except Exception as _e:
                    pass
            opt = None
            if mode == "decline":
                opt = opts.filter(has_text=re.compile(
                    r"not wish|prefer not|decline|do not want|don'?t wish|choose not", re.I
                )).first
            elif mode == "hearabout":
                # Prefer a known source; else pick the first real (non "Select One") option
                for term in ["LinkedIn", "Indeed", "Glassdoor", "Greenhouse",
                             "Company Website", "Job Board", "Other"]:
                    cand = opts.filter(has_text=re.compile(re.escape(term), re.I)).first
                    if await cand.count() and await cand.is_visible(timeout=600):
                        opt = cand
                        break
                if opt is None:
                    for o in await opts.all():
                        try:
                            t = (await o.inner_text()).strip()
                        except Exception:
                            t = ""
                        if t and t.lower() != "select one":
                            opt = o
                            break
            elif mode == "masters":
                # Prefer Master of Science / M.S., else any master's option
                for term in ["Master of Science", r"\bM\.?S\.?\b", "Master's Degree",
                             "Masters", "Master"]:
                    cand = opts.filter(has_text=re.compile(term, re.I)).first
                    if await cand.count() and await cand.is_visible(timeout=600):
                        opt = cand
                        break
            elif mode == "years2plus":
                # Pick an option whose range starts at 2+ ("2-3", "2+", "2 to 5", "2 or more")
                all_opts = await opts.all()
                texts = []
                for o in all_opts:
                    try:
                        texts.append((o, (await o.inner_text()).strip()))
                    except Exception:
                        pass
                for pat in [r'^\s*2\s*[-+]', r'^\s*2\s*(to|or)\b', r'^\s*2\b', r'\b2\s*[-+]', r'\b2\b']:
                    for o, t in texts:
                        if t and re.search(pat, t, re.I):
                            opt = o
                            break
                    if opt is not None:
                        break
            elif mode == "salaryrange":
                # Options are salary brackets ("$50,000 - $74,999", "$75,000+",
                # "Less than $50,000"). Pick the bracket that contains `want`,
                # else the highest bracket whose lower bound is <= want, else the
                # first real option.
                target = int(want)
                all_opts = await opts.all()
                texts = []
                for o in all_opts:
                    try:
                        t = (await o.inner_text()).strip()
                    except Exception:
                        t = ""
                    if t and t.lower() != "select one":
                        nums = [int(n.replace(",", "")) for n in re.findall(r'\d[\d,]{3,}', t)]
                        texts.append((o, t, nums))
                best = None
                for o, t, nums in texts:
                    if len(nums) >= 2 and nums[0] <= target <= nums[1]:
                        best = o
                        break
                    if len(nums) == 1:
                        if re.search(r'less than|under|up to|below', t, re.I) and target <= nums[0]:
                            best = o
                        elif re.search(r'\+|more|above|over|greater', t, re.I) and target >= nums[0]:
                            best = o
                if best is None and texts:
                    best = texts[0][0]  # first real option as a safe fallback
                opt = best
            elif mode == "contains":
                # Word-boundary match so "Male" doesn't also match "Female"
                opt = opts.filter(has_text=re.compile(rf'\b{re.escape(want)}\b', re.I)).first
            else:  # exact (Yes/No)
                opt = page.get_by_role("option", name=re.compile(rf'^\s*{re.escape(want)}\s*$', re.I)).first
                if not await opt.count():
                    opt = scope.get_by_text(re.compile(rf'^\s*{re.escape(want)}\s*$', re.I)).first
                if not await opt.count():
                    opt = opts.filter(has_text=re.compile(rf'^\s*{re.escape(want)}\b', re.I)).first

            clicked_opt = False
            if opt is not None and await opt.count():
                try:
                    await opt.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    await opt.click(timeout=4000, force=True)
                    clicked_opt = True
                except Exception:
                    pass
            if not clicked_opt:
                # Close the dropdown so a stuck-open menu doesn't block the page.
                # (Do NOT press Enter — it would select whatever option is highlighted.)
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
            await page.wait_for_timeout(300)
        except Exception:
            continue

async def fill_checkbox_questions(page: Page, answers: dict | None = None) -> None:
    """Handle Yes/No checkbox-group questions (each option is a checkbox + label).
    Ticks the option matching the profile answer; defaults to No."""
    answers = answers or {}
    groups = await page.locator(
        '[data-automation-id*="formField"], '
        'fieldset[data-automation-id*="CheckboxGroup"], '
        '[aria-required="true"]:has(input[type="checkbox"])'
    ).all()
    for grp in groups:
        try:
            cbs = grp.locator('input[type="checkbox"][aria-required="true"]')
            n = await cbs.count()
            if n == 0:
                continue
            # Skip if any option is already checked
            already = False
            for i in range(n):
                try:
                    if await cbs.nth(i).is_checked():
                        already = True
                        break
                except Exception:
                    pass
            if already:
                continue
            qtext = (await grp.inner_text()).strip()
            profile_ans = _answer_from_profile(qtext, answers)
            want = profile_ans if (profile_ans and profile_ans.lower() in ("yes", "no")) else "No"
            # Match exact, or starts-with (e.g. "No, or I prefer not to identify"),
            # and for "No" also the decline-style options.
            if want.lower() == "no":
                pat = r"^\s*no\b|prefer not to (identify|answer|say)|none of the above|i do not"
            else:
                pat = rf'^\s*{re.escape(want)}\b'
            lbl = grp.locator('label').filter(has_text=re.compile(pat, re.I)).first
            if await lbl.count() and await lbl.is_visible(timeout=1500):
                await lbl.click(timeout=4000)
                await page.wait_for_timeout(200)
        except Exception:
            continue

async def fill_date_questions(page: Page) -> None:
    """Fill empty required date fields (e.g. 'available to start') with today's
    date via the calendar 'Selected Today' button."""
    wrappers = await page.locator('[data-automation-id="dateInputWrapper"]').all()
    for w in wrappers:
        try:
            disp = w.locator('[data-automation-id="dateSectionMonth-display"]').first
            if await disp.count():
                txt = (await disp.inner_text()).strip().upper()
                if txt and txt != "MM":
                    continue  # already filled
            cal = w.locator('[data-automation-id="dateIcon"]').first
            if await cal.count() and await cal.is_visible(timeout=1000):
                await cal.click(timeout=4000)
                await page.wait_for_timeout(500)
                today_btn = page.locator('[data-automation-id="datePickerSelectedToday"]').first
                if await today_btn.is_visible(timeout=2000):
                    await today_btn.click(timeout=4000)
                    await page.wait_for_timeout(300)
        except Exception:
            continue

async def fill_radio_questions(page: Page, answers: dict | None = None) -> None:
    """Handle Yes/No radio-button questions whose container is aria-required but
    not necessarily role=radiogroup (e.g. Dow's 'previously worked' question).
    Reads the question from aria-labelledby and answers from the profile (default No)."""
    answers = answers or {}
    groups = await page.locator(
        '[role="radiogroup"][aria-required="true"], '
        '[aria-required="true"]:has(input[type="radio"])'
    ).all()
    for grp in groups:
        try:
            radios = grp.locator('input[type="radio"]')
            n = await radios.count()
            if n == 0:
                continue
            # Skip if an option is already selected
            answered = False
            for i in range(n):
                try:
                    if await radios.nth(i).is_checked():
                        answered = True
                        break
                except Exception:
                    pass
            if answered:
                continue
            # Question text: prefer aria-labelledby, fall back to form-field ancestor
            qtext = ""
            labelledby = await grp.get_attribute("aria-labelledby")
            if labelledby:
                for lid in labelledby.split():
                    el = page.locator(f'#{lid}')
                    if await el.count():
                        try:
                            qtext += " " + (await el.inner_text())
                        except Exception:
                            pass
            if not qtext.strip():
                container = grp.locator('xpath=ancestor::*[contains(@data-automation-id,"formField")][1]')
                if await container.count():
                    qtext = await container.inner_text()
            ans = _answer_from_profile(qtext, answers) or "No"
            if ans.lower() not in ("yes", "no"):
                ans = "No"
            lbl = grp.locator('label').filter(
                has_text=re.compile(rf'^\s*{ans}\s*$', re.I)
            ).first
            if await lbl.count() and await lbl.is_visible(timeout=1500):
                await lbl.click(timeout=4000)
                await page.wait_for_timeout(200)
        except Exception:
            continue

async def fill_application_questions(page: Page, answers: dict, unknowns: list[str]) -> None:
    await page.wait_for_timeout(800)

    # Find all currently-empty required fields via JS
    fields = await page.evaluate("""() => {
        const out = [];
        const SKIP_TYPES = new Set(['file', 'hidden', 'submit', 'button', 'checkbox', 'reset']);
        const inputs = document.querySelectorAll(
            'input[aria-required="true"], select[aria-required="true"], textarea[aria-required="true"], [role="combobox"][aria-required="true"], [role="spinbutton"][aria-required="true"]'
        );
        inputs.forEach(el => {
            if (SKIP_TYPES.has(el.type)) return;
            if (el.offsetParent === null) return;          // hidden
            if ((el.value || '').trim()) return;           // already filled

            let label = el.getAttribute('aria-label') || '';
            if (!label && el.id) {
                const lbl = document.querySelector('label[for="' + el.id + '"]');
                if (lbl) label = lbl.innerText.trim();
            }
            if (!label && el.getAttribute('aria-labelledby')) {
                label = el.getAttribute('aria-labelledby').split(' ')
                    .map(id => { const e = document.getElementById(id); return e ? e.innerText.trim() : ''; })
                    .join(' ').trim();
            }
            if (!label) {
                let node = el.parentElement;
                for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
                    const lbl = node.querySelector('label,[class*="label"],[data-automation-id$="label"]');
                    if (lbl && lbl !== el && lbl.innerText.trim()) {
                        label = lbl.innerText.trim();
                        break;
                    }
                }
            }
            // Last resort: use the question container's text (Workday supplementary
            // questions put the prompt in a div, not a <label>).
            if (!label) {
                let node = el.parentElement;
                for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
                    const aid = node.getAttribute && node.getAttribute('data-automation-id') || '';
                    if (/formField|questionItem|question/i.test(aid)) {
                        const t = (node.innerText || '').trim();
                        if (t) { label = t; break; }
                    }
                }
            }
            label = label.replace(/\\*/g, '').replace(/\\s+/g, ' ').trim();
            if (!label) return;

            out.push({
                label,
                type:          el.type || el.getAttribute('role') || el.tagName.toLowerCase(),
                id:            el.id || '',
                automation_id: el.getAttribute('data-automation-id') || '',
                name:          el.name || '',
                tag:           el.tagName.toLowerCase(),
            });
        });
        return out;
    }""")

    # Also scan for required radio groups (Yes/No questions)
    radio_groups = await page.evaluate("""() => {
        const groups = [];
        document.querySelectorAll('[role="radiogroup"][aria-required="true"]').forEach(g => {
            let label = g.getAttribute('aria-label') || g.getAttribute('aria-labelledby') || '';
            if (!label) {
                let node = g.previousElementSibling || g.parentElement;
                if (node) label = node.innerText.trim().replace(/\\*/g,'').trim();
            }
            if (label) groups.push({ label, id: g.id || '', automation_id: g.getAttribute('data-automation-id') || '' });
        });
        return groups;
    }""")

    for field in fields + radio_groups:
        label = field.get("label", "").strip()
        ftype = field.get("type", "text")
        fid   = field.get("id", "")
        aid   = field.get("automation_id", "")

        if not label:
            continue

        answer = _answer_from_profile(label, answers)

        if answer is None:
            # Unknown — ask locally or log for CI
            if not HEADLESS:
                print(f"\n  [?] Unknown mandatory field: '{label}'")
                user_ans = input("  Your answer (blank to skip): ").strip()
                if user_ans:
                    answers[label] = user_ans
                    save_answers(answers)
                    answer = user_ans
            else:
                print(f"  [?] Unknown field: '{label}' — flagging needs_review")
                unknowns.append(label)
            continue

        if not answer:
            continue  # Explicitly skipped (e.g. EEO)

        # Build locator selectors (most-specific first)
        sels = []
        if aid:   sels.append(f'[data-automation-id="{aid}"]')
        if fid:   sels.append(f'#{fid}')
        short_label = re.escape(label[:25])
        sels.append(f'[aria-label*="{short_label}" i]')

        if answer == "__HEAR_ABOUT_US__":
            await fill_hear_about_us(page)
            continue
        if answer == "__YEARS_2PLUS__":
            answer = INFO.get("years_experience", "2")  # text field → numeric years
        if answer == "__MASTERS__":
            answer = "Master of Science"
        if answer in ("__DECLINE__",):
            continue  # decline handled only for dropdowns
        if ftype in ("text", "email", "tel", "number", "textarea", "search", "spinbutton", ""):
            await try_fill(page, str(answer), *sels, overwrite=False)
        elif ftype in ("select", "combobox"):
            await combobox_select(page, str(answer), *sels)
        elif ftype in ("radio", "radiogroup"):
            await radio_click(page, str(answer))

# ── Submit ─────────────────────────────────────────────────────────────────────

async def submit_application(page: Page) -> str:
    clicked = await try_click(page,
        '[data-automation-id="bottom-navigation-next-button"]',
        'button:has-text("Submit My Application")',
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
        timeout=8000,
    )
    if not clicked:
        return "needs_review:submit_button_not_found"

    await page.wait_for_timeout(4000)
    body = (await page.evaluate("document.body.innerText")).lower()
    if any(kw in body for kw in [
        "thank you", "successfully submitted", "application received",
        "your application has been", "we have received", "submitted",
    ]):
        return "applied"
    # Check for validation errors
    if any(kw in body for kw in ["error", "required", "please complete", "invalid"]):
        return "needs_review:validation_errors"
    return "applied"

# ── Main application flow ─────────────────────────────────────────────────────

async def apply_to_job(page: Page, job: dict, answers: dict) -> tuple[str, str]:
    """Apply to one job. Returns (status, notes)."""
    url = (job.get("link") or "").strip()
    if not url:
        return "skipped", "no_url"

    unknowns: list[str] = []

    # Navigate
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
    except PlaywrightTimeout:
        return "error", "navigation_timeout"
    except Exception as e:
        return "error", f"navigation:{str(e)[:60]}"

    body = (await page.evaluate("document.body.innerText")).lower()

    # Already applied?
    if any(kw in body for kw in ["you have already applied", "application already submitted"]):
        return "already_applied", ""

    # Find and click Apply button
    apply_loc = page.locator(
        '[data-automation-id="applyButton"], '
        '[data-automation-id="adventureButton"], '
        'button[aria-label*="Apply for Job" i], '
        'button:has-text("Apply for Job"), '
        'button:has-text("Apply Now"), '
        'a:has-text("Apply for Job"), '
        'a:has-text("Apply")'
    ).first

    try:
        await apply_loc.wait_for(state="visible", timeout=12000)
    except Exception:
        return "skipped", "no_apply_button"

    try:
        await apply_loc.click(timeout=8000)
    except Exception as e:
        return "skipped", f"click_failed:{str(e)[:40]}"

    await page.wait_for_timeout(2500)
    await page.wait_for_load_state("domcontentloaded")
    await _shot(page, "after_apply_click")

    # Use last application if offered (pre-fills all fields)
    try:
        last_app = page.locator('[data-automation-id="useMyLastApplication"]').first
        if await last_app.is_visible(timeout=4000):
            await last_app.click()
            await page.wait_for_timeout(2500)
            await page.wait_for_load_state("domcontentloaded")
            print("  [+] Used last application")
            await _shot(page, "after_use_last_app")
    except Exception:
        pass

    # Handle auth
    await _shot(page, "before_auth")
    auth_ok = await handle_auth(page)
    await _shot(page, "after_auth")
    if not auth_ok:
        return "needs_review", "email_verification_required"

    await page.wait_for_timeout(2000)
    await page.wait_for_load_state("domcontentloaded")

    # Wait for the Workday application wizard to be present
    try:
        await page.wait_for_selector(
            '[data-automation-id="formContainer"], [data-automation-id="currentStep"], '
            '[data-automation-id="step"], form[data-automation-id]',
            timeout=15000,
        )
    except Exception:
        pass  # Proceed anyway — some tenants render differently

    await page.wait_for_timeout(1500)

    # Walk through form steps
    auth_attempts = 0
    verify_email_sent = False
    verify_waits = 0
    stuck_sig = None
    stuck_count = 0
    for step_num in range(20):
        await wait_for_content(page)
        step = await current_step(page)

        body = (await page.evaluate("document.body.innerText")).lower()
        # Diagnostic: first non-empty line of the page body identifies the step
        _snippet = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        print(f"  Step {step_num+1}: {step or '(unknown)'}  | {_snippet[:70]}")
        await _shot(page, f"step_{step_num+1}")

        # Submission succeeded — recognize Workday's confirmation page
        if any(kw in body for kw in [
            "successfully submitted", "application submitted",
            "thank you for applying", "we have received your application",
        ]):
            print("  [+] Application submitted")
            return "applied", "; ".join(unknowns)

        # External assessment required (e.g. Fiserv WOTC) — can't automate; stop here
        if ("take assessment" in body or "complete the assessment" in body
                or await page.locator('[data-automation-id="inlineAssessmentButton"]').count() > 0):
            print("  [!] Assessment required — stopping (apply manually)")
            return "needs_review", "assessment required — complete manually"

        # Create Account / Sign In step — the form often renders late, so the
        # pre-loop handle_auth() can miss it. Handle it here where it's rendered.
        # A visible password field only ever appears on an auth page.
        has_pw_field = await page.locator(
            '[data-automation-id="password"]:visible, input[type="password"]:visible'
        ).count() > 0
        is_auth_page = has_pw_field or (
            "create account" in body and "verify new password" in body
        ) or "sign in with email" in body or "continue with email" in body
        if is_auth_page:
            # Account needs email verification before sign-in
            if any(kw in body for kw in [
                "verify your account before you sign in", "request a verification email",
                "account may need verification",
            ]):
                company = job.get("company", "")
                title = job.get("title", "")
                tenant = (url.split("//", 1)[-1].split(".", 1)[0]) if url else ""
                # Try to auto-click the verification link from the inbox (IMAP)
                link = await asyncio.to_thread(fetch_verification_link, tenant)
                if link:
                    print(f"  [+] Got verification link from inbox — opening it")
                    try:
                        await page.goto(link, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(2500)
                    except Exception as e:
                        print(f"  [!] Failed to open verification link: {e}")
                    # Account verified — re-enter the application: job → Apply → sign in
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)
                    await try_click(page,
                        '[data-automation-id="applyButton"]',
                        '[data-automation-id="adventureButton"]',
                        'button:has-text("Apply")', 'a:has-text("Apply")',
                        timeout=12000)
                    await page.wait_for_timeout(2500)
                    try:
                        last_app = page.locator('[data-automation-id="useMyLastApplication"]').first
                        if await last_app.is_visible(timeout=4000):
                            await last_app.click(timeout=4000)
                            await page.wait_for_timeout(2000)
                    except Exception:
                        pass
                    await wait_for_content(page)
                    await handle_auth(page, prefer_signin=True)
                    await page.wait_for_timeout(2500)
                    verify_waits += 1
                    if verify_waits > 3:
                        return "needs_review", "verification_failed"
                    continue
                # No IMAP configured / link not found yet — notify user and wait for manual click
                if not verify_email_sent:
                    await asyncio.to_thread(send_verification_email, job.get("company", ""), job.get("title", ""), url)
                    verify_email_sent = True
                print("  [!] Account verification required — waiting 30s for manual verification…")
                await page.wait_for_timeout(30000)
                await handle_auth(page, prefer_signin=True)
                await page.wait_for_timeout(2500)
                verify_waits += 1
                if verify_waits > 3:
                    return "needs_review", "verification_timeout"
                continue
            # Bail on a credentials/lockout error — never hammer (it locks the account)
            if any(kw in body for kw in [
                "wrong email address or password", "account might be locked",
                "account is locked", "incorrect password",
            ]):
                print("  [!] Sign-in rejected — wrong password or locked account")
                return "needs_review", "auth_failed:wrong_password_or_locked"
            tenant = (url.split("//", 1)[-1].split(".", 1)[0]) if url else ""
            if auth_attempts < 1:
                # First encounter — create the account (or sign in if it exists)
                auth_attempts += 1
                print(f"  → Auth page (attempt {auth_attempts}: create / sign-in)")
                await handle_auth(page)
                await page.wait_for_timeout(2500)
                await page.wait_for_load_state("domcontentloaded")
                await _shot(page, f"step_{step_num+1}_after_auth")
                continue
            elif auth_attempts < 5:
                auth_attempts += 1
                # If a sign-in form is right here (email+password visible, no verify),
                # the account exists — just sign in (e.g. U-Haul: create → sign-in form).
                has_signin_form = await page.locator(
                    '[data-automation-id="password"]:visible, input[type="password"]:visible'
                ).count() > 0
                has_verify_field = await page.locator(
                    '[data-automation-id="verifyPassword"]:visible, input[name*="erify" i]:visible'
                ).count() > 0
                if has_signin_form and not has_verify_field:
                    print("  → Sign-in form present — signing in")
                    await handle_auth(page, prefer_signin=True)
                    await page.wait_for_timeout(2500)
                    await page.wait_for_load_state("domcontentloaded")
                    await _shot(page, f"step_{step_num+1}_after_auth")
                    continue
                # Otherwise the account needs email verification, then sign-in (e.g.
                # Corewell). Fetch the verification link via IMAP, open it, re-enter.
                link = await asyncio.to_thread(fetch_verification_link, tenant)
                if link:
                    print(f"  [+] Got verification link from inbox — opening it")
                    try:
                        await page.goto(link, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(2500)
                    except Exception as e:
                        print(f"  [!] Failed to open verification link: {e}")
                else:
                    if not verify_email_sent:
                        await asyncio.to_thread(send_verification_email, job.get("company", ""), job.get("title", ""), url)
                        verify_email_sent = True
                    print("  [!] Verification email not found yet — waiting 20s…")
                    await page.wait_for_timeout(20000)
                # Re-enter the application and sign in (account now exists/verified)
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
                await try_click(page,
                    '[data-automation-id="applyButton"]',
                    '[data-automation-id="adventureButton"]',
                    'button:has-text("Apply")', 'a:has-text("Apply")',
                    timeout=12000)
                await page.wait_for_timeout(2500)
                try:
                    last_app = page.locator('[data-automation-id="useMyLastApplication"]').first
                    if await last_app.is_visible(timeout=4000):
                        await last_app.click(timeout=4000)
                        await page.wait_for_timeout(2000)
                except Exception:
                    pass
                await wait_for_content(page)
                await handle_auth(page, prefer_signin=True)
                await page.wait_for_timeout(2500)
                await page.wait_for_load_state("domcontentloaded")
                await _shot(page, f"step_{step_num+1}_after_auth")
                continue
            return "needs_review", "auth_stuck"

        # Review/submit step
        if any(kw in body for kw in [
            "review and submit", "review your application",
            "submit your application", "my review",
        ]):
            result = await submit_application(page)
            return result, "; ".join(unknowns)

        if await fill_terms_and_conditions(page):
            # Voluntary-Disclosures pages bundle terms + EEO dropdowns/checkboxes,
            # so still run the other fillers (don't short-circuit).
            await fill_mandatory_listbox_dropdowns(page, answers)
            await fill_checkbox_questions(page, answers)
        elif await fill_disability_form(page):
            pass  # Disability page handled
        elif any(k in step for k in ("information", "contact", "personal")):
            await fill_my_information(page)
            await fill_radio_questions(page, answers)
        elif any(k in step for k in ("experience", "background", "resume", "my experience")):
            await fill_my_experience(page)
        elif any(k in step for k in ("voluntary", "disclos", "eeo", "equal opportunity", "diversity")):
            pass  # Prefer not to disclose — just click Next
        elif any(k in step for k in ("question", "additional", "application question", "screening")):
            await fill_mandatory_listbox_dropdowns(page, answers)
            await fill_checkbox_questions(page, answers)
            await fill_radio_questions(page, answers)
            await fill_date_questions(page)
            await fill_application_questions(page, answers, unknowns)
        else:
            # Unknown step — try all fillers; each is a no-op if fields aren't present
            await fill_my_information(page)
            await fill_my_experience(page)
            await fill_mandatory_listbox_dropdowns(page, answers)
            await fill_checkbox_questions(page, answers)
            await fill_radio_questions(page, answers)
            await fill_date_questions(page)
            await fill_application_questions(page, answers, unknowns)

        await page.wait_for_timeout(600)
        await _shot(page, f"step_{step_num+1}_filled")

        # Surface any validation errors so we know exactly which field is blocking
        try:
            errs = await page.evaluate("""() => {
                const out = [];
                document.querySelectorAll(
                    '[data-automation-id="errorMessage"], [role="alert"], '
                    '[data-automation-id*="error" i], .css-error, [class*="error" i]'
                ).forEach(e => {
                    const t = (e.innerText || '').trim();
                    if (t && t.length < 200) out.push(t);
                });
                return [...new Set(out)];
            }""")
            for e in errs[:8]:
                print(f"     [err] {e}")
        except Exception:
            errs = []

        # No-progress guard: if the same step shows the same blocking errors on
        # consecutive iterations, we're stuck on a field we can't fill — bail
        # early instead of burning all 20 iterations.
        sig = (step, tuple(sorted(errs))[:5]) if errs else None
        if sig and sig == stuck_sig:
            stuck_count += 1
            if stuck_count >= 2:
                blocking = "; ".join(errs[:3])[:160]
                print(f"  [!] No progress on '{step}' — blocked by: {blocking}")
                return "needs_review", f"stuck_field:{blocking}"
        else:
            stuck_sig = sig
            stuck_count = 0

        if not await click_next(page):
            # Try submit directly (last step without clear heading)
            if await try_click(page, 'button:has-text("Submit")', timeout=3000):
                await page.wait_for_timeout(3000)
                return "applied", "; ".join(unknowns)
            return "needs_review", f"stuck_at_step_{step_num+1}:{step}"

        await page.wait_for_timeout(2500)
        await page.wait_for_load_state("domcontentloaded")

    return "needs_review", "too_many_steps"

# ── Email summary ──────────────────────────────────────────────────────────────

def send_summary_email(results: list[dict]) -> None:
    if not EMAIL_PASSWORD or not EMAIL_TO:
        return

    n_applied = sum(1 for r in results if r["status"] == "applied")
    n_review  = sum(1 for r in results if "needs_review" in r["status"])

    def _row(r: dict) -> str:
        bg = (
            "#d4edda" if r["status"] == "applied" else
            "#fff3cd" if "needs_review" in r["status"] else
            "#f8d7da"
        )
        return (
            f"<tr style='background:{bg}'>"
            f"<td style='padding:5px;border:1px solid #ddd'>{r['title']}</td>"
            f"<td style='padding:5px;border:1px solid #ddd'>{r['company']}</td>"
            f"<td style='padding:5px;border:1px solid #ddd'>{r['status']}</td>"
            f"<td style='padding:5px;border:1px solid #ddd'>{(r.get('notes') or '')[:60]}</td>"
            f"<td style='padding:5px;border:1px solid #ddd'><a href='{r['link']}'>Link</a></td>"
            f"</tr>"
        )

    rows = "".join(_row(r) for r in results)
    subject = (
        f"[Workday] Auto-Apply: {n_applied} applied | {n_review} needs review "
        f"— {datetime.now().strftime('%b %d, %Y %H:%M')}"
    )
    body_html = f"""
    <html><body style="font-family:sans-serif;color:#333;font-size:13px">
    <h2>Workday Auto-Apply Run</h2>
    <p>
      <b style="color:#155724">Applied: {n_applied}</b> &nbsp;|&nbsp;
      <b style="color:#856404">Needs Review: {n_review}</b> &nbsp;|&nbsp;
      Total: {len(results)}
    </p>
    <p style="color:#666;font-size:12px">
      For <b>needs_review</b> jobs: check the Notes column — if it mentions an unknown question,
      add the answer to <code>json/workday_answers.json</code> and rerun.
    </p>
    <table border="1" cellpadding="5" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:12px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Status</th><th>Notes</th><th>Link</th>
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
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_SENDER, EMAIL_PASSWORD)
            s.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        print(f"[+] Summary email → {EMAIL_TO}")
    except Exception as e:
        print(f"[!] Email failed: {e}")

def fetch_verification_link(tenant_hint: str = "") -> str | None:
    """Read the verification inbox over IMAP, find the newest Workday account-
    verification email, and return its verification URL (or None)."""
    if not VERIFY_IMAP_USER or not VERIFY_IMAP_PASS:
        return None
    import imaplib, email as _email
    from email.header import decode_header as _decode_header
    link = None
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(VERIFY_IMAP_USER, VERIFY_IMAP_PASS)
        M.select("INBOX")
        typ, data = M.search(None, "ALL")
        ids = data[0].split()
        # Newest first, scan the last ~10 messages
        for num in reversed(ids[-10:]):
            typ, msgdata = M.fetch(num, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            msg = _email.message_from_bytes(msgdata[0][1])
            subj = str(_decode_header(msg.get("Subject", ""))[0][0])
            # Collect the HTML/plain body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() in ("text/html", "text/plain"):
                        try:
                            body += part.get_payload(decode=True).decode(errors="replace")
                        except Exception:
                            pass
            else:
                try:
                    body = msg.get_payload(decode=True).decode(errors="replace")
                except Exception:
                    body = str(msg.get_payload())
            # Find a Workday verification URL in this email
            urls = re.findall(r'https?://[^\s"\'<>]+', body)
            for u in urls:
                lu = u.lower()
                if "myworkdayjobs" in lu and any(
                    k in lu for k in ("verify", "activate", "confirm", "register", "token", "validate")
                ):
                    if not tenant_hint or tenant_hint.lower() in lu:
                        link = u.replace("&amp;", "&")
                        break
            if link:
                break
        M.logout()
    except Exception as e:
        print(f"  [!] IMAP verification read failed: {e}")
    return link

def send_verification_email(company: str, title: str, job_url: str = "") -> None:
    """Notify the user that a Workday account needs email verification to continue."""
    to = EMAIL_TO or "lopes.o@northeastern.edu"
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        print(f"  [!] Verification email not sent (EMAIL creds unset). Verify manually.")
        return
    try:
        recipients = [a.strip() for a in to.split(",") if a.strip()]
        body = f"""<html><body style="font-family:sans-serif">
        <h3>Workday account verification needed</h3>
        <p>The auto-apply bot is applying to <b>{title}</b> at <b>{company}</b> but the
        Workday account needs email verification before it can sign in.</p>
        <p>Please open the inbox for <b>{INFO.get('email','')}</b>, click the verification
        link, and the bot will continue automatically.</p>
        <p><a href="{job_url}">{job_url}</a></p>
        </body></html>"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Workday] Verify account to apply: {title} @ {company}"
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = ", ".join(recipients)
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_SENDER, EMAIL_PASSWORD)
            s.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        print(f"  [+] Verification email → {to}")
    except Exception as e:
        print(f"  [!] Verification email failed: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    if not INFO:
        print("[!] No applicant info found. Set WD_APPLICANT_INFO env var.")
        return
    if not WD_PASSWORD:
        print("[!] WORKDAY_PASSWORD not set.")
        return

    applied_ids = load_applied()
    answers     = load_answers()
    results: list[dict] = []

    queue = build_queue(ROLES_ENV, applied_ids)
    if not queue:
        print(f"[i] No pending jobs (roles={ROLES_ENV}).")
        return

    print(f"[+] Queue: {len(queue)} job(s)  |  roles={ROLES_ENV}  |  "
          f"headless={HEADLESS}  |  concurrency={CONCURRENCY}")

    session_state = session_from_b64()
    if not session_state and SESSION_FILE.exists():
        try:
            session_state = json.loads(SESSION_FILE.read_text())
        except Exception:
            pass
    if session_state:
        print("[+] Session state loaded.")

    ctx_kwargs: dict = {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1280, "height": 900},
    }
    # Only seed cookies in serial mode — in parallel each tenant gets a clean
    # context so sessions don't collide.
    if session_state and CONCURRENCY == 1:
        ctx_kwargs["storage_state"] = session_state

    lock = asyncio.Lock()
    sem = asyncio.Semaphore(CONCURRENCY)
    total = len(queue)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"] if HEADLESS else [],
            slow_mo=0 if HEADLESS else 60,
        )

        async def run_one(i: int, job: dict) -> None:
            title   = job.get("title", "Unknown")
            company = job.get("company", "Unknown")
            link    = job.get("link", "")
            async with sem:
                print(f"\n[{i+1}/{total}] {title} @ {company}")
                # Each application gets its own isolated context (separate cookies)
                context = await browser.new_context(**ctx_kwargs)
                page = await context.new_page()
                page.set_default_timeout(8000)
                try:
                    status, notes = await apply_to_job(page, job, answers)
                except PlaywrightTimeout as e:
                    status, notes = "error", f"timeout:{str(e)[:60]}"
                except Exception as e:
                    status, notes = "error", f"{type(e).__name__}:{str(e)[:60]}"
                finally:
                    try:
                        await context.close()
                    except Exception:
                        pass

                print(f"  → [{company}] {status}" + (f"  |  {notes}" if notes else ""))

                row = {
                    "title":      title,
                    "company":    company,
                    "location":   job.get("location", ""),
                    "link":       link,
                    "status":     status,
                    "applied_on": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "notes":      notes,
                }
                async with lock:
                    # Don't retry jobs that succeeded OR are blocked by an external
                    # assessment (can't be automated).
                    if status in ("applied", "already_applied") or "assessment" in notes:
                        applied_ids.add(link)
                        save_applied(applied_ids)
                    results.append(row)
                    append_csv(row)
                    save_answers(answers)

        await asyncio.gather(*(run_one(i, job) for i, job in enumerate(queue)))
        await browser.close()

    # Summary
    n_applied = sum(1 for r in results if r["status"] == "applied")
    n_review  = sum(1 for r in results if "needs_review" in r["status"])
    n_skip    = sum(1 for r in results if r["status"] in ("skipped", "already_applied"))
    print(f"\n{'='*60}")
    print(f"  Applied: {n_applied}  |  Needs review: {n_review}  |  Skipped: {n_skip}")
    print(f"  Log: {OUTPUT_CSV.name}")
    print(f"{'='*60}")

    save_answers(answers)
    send_summary_email(results)


if __name__ == "__main__":
    asyncio.run(main())
