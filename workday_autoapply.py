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

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

SESSION_FILE = ROOT / "json" / "workday_auth.json"
APPLIED_LOG  = ROOT / "json" / "workday_applied.json"
ANSWERS_FILE = ROOT / "json" / "workday_answers.json"
OUTPUT_CSV   = ROOT / "csv"  / "workday_applied.csv"

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

def build_queue(roles: str, applied_ids: set) -> list[dict]:
    csv_dir = ROOT / "csv"
    if roles == "all":
        files = sorted(glob.glob(str(csv_dir / "workday_jobs_all*.csv")))
    else:
        role_list = [r.strip() for r in roles.split(",")]
        files = []
        for r in role_list:
            # Support both old short codes (de, bi) and new role-label slugs
            # (data_engineer, business_intelligence, ai_engineer, etc.)
            files.extend(sorted(glob.glob(str(csv_dir / f"workday_jobs_{r}*.csv"))))

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
    return jobs[:MAX_APPLY]

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
                await loc.click()
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

async def handle_auth(page: Page) -> bool:
    """Log in or create Workday account if prompted. Returns True on success."""
    try:
        email_input = page.locator(
            '[data-automation-id="email"], input[type="email"][autocomplete*="email"]'
        ).first
        if not await email_input.is_visible(timeout=5000):
            return True
    except Exception:
        return True

    email = INFO.get("email", "")
    await email_input.fill(email)
    await page.wait_for_timeout(500)

    await try_click(page,
        '[data-automation-id="signInButton"]',
        'button:has-text("Sign In")',
        'button:has-text("Continue")',
        'button[type="submit"]',
        timeout=6000,
    )
    await page.wait_for_timeout(2000)
    await page.wait_for_load_state("domcontentloaded")

    # Existing account — password field
    try:
        pwd = page.locator(
            '[data-automation-id="password"], input[type="password"]'
        ).first
        if await pwd.is_visible(timeout=3000):
            await pwd.fill(WD_PASSWORD)
            await page.wait_for_timeout(400)
            await try_click(page,
                '[data-automation-id="signInButton"]',
                'button[type="submit"]',
                'button:has-text("Sign In")',
                timeout=6000,
            )
            await page.wait_for_timeout(2500)
            await page.wait_for_load_state("domcontentloaded")
            return True
    except Exception:
        pass

    # New account — Create Account button
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
    await try_fill(page, INFO.get("first_name", ""),
        '[data-automation-id="firstName"]', 'input[name*="firstName" i]')
    await try_fill(page, INFO.get("last_name", ""),
        '[data-automation-id="lastName"]', 'input[name*="lastName" i]')
    await try_fill(page, WD_PASSWORD,
        '[data-automation-id="password"]',
        'input[type="password"]:not([name*="erif" i]):not([id*="erif" i])')
    await try_fill(page, WD_PASSWORD,
        '[data-automation-id="verifyPassword"]',
        'input[name*="erify" i]', 'input[id*="erify" i]')
    await page.wait_for_timeout(400)

    await try_click(page,
        '[data-automation-id="createAccountButton"]',
        'button:has-text("Create Account")',
        'button[type="submit"]',
        timeout=6000,
    )
    await page.wait_for_timeout(2500)

    body = (await page.evaluate("document.body.innerText")).lower()
    if any(kw in body for kw in ["verify", "verification", "check your email", "confirmation link"]):
        if HEADLESS:
            print("  [!] Email verification required — needs manual action")
            return False
        print("\n  [!] Check lopes.o@northeastern.edu and click the verification link.")
        input("  Press Enter when done…")
        await page.wait_for_timeout(2000)
    return True

# ── Step detection ─────────────────────────────────────────────────────────────

async def current_step(page: Page) -> str:
    """Return lowercased title of the current form step."""
    for sel in [
        '[data-automation-id="currentStep"] span',
        '[aria-current="step"] span',
        'h2[role="heading"]',
        'h2', 'h3',
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
        'input[aria-label*="Phone" i]', 'input[type="tel"]')
    await combobox_select(page, "Mobile",
        '[data-automation-id="phoneDeviceType"]',
        'select[aria-label*="Phone Type" i]')
    await try_fill(page, info.get("address_line1", ""),
        '[data-automation-id="addressSection_addressLine1"]',
        'input[aria-label*="Address Line 1" i]', 'input[aria-label*="Street" i]')
    await try_fill(page, info.get("city", ""),
        '[data-automation-id="addressSection_city"]',
        'input[aria-label*="City" i]')
    await try_fill(page, info.get("zip", ""),
        '[data-automation-id="addressSection_postalCode"]',
        'input[aria-label*="Postal Code" i]', 'input[aria-label*="Zip" i]')
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

    # Fallback: file chooser
    try:
        async with page.expect_file_chooser(timeout=6000) as fc:
            await try_click(page,
                'button:has-text("Upload")', 'button:has-text("Select a File")',
                'button:has-text("Select File")', '[aria-label*="Upload" i]')
        await fc.value.set_files(RESUME_PATH)
        await page.wait_for_timeout(3500)
        print(f"  [+] Resume uploaded via file chooser")
        return True
    except Exception as e:
        print(f"  [!] File chooser error: {e}")
    return False

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
    # Work experience + education are auto-parsed from resume — we don't overwrite

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
    if re.search(r'legally authorized|authorized to work in the u\.?s', ll):
        return info.get("work_authorized", "Yes")
    if re.search(r'require sponsorship|need.*sponsorship|will you.*sponsor|now or in the future', ll):
        return info.get("needs_sponsorship", "No")

    # ── Contact info ──────────────────────────────────────────────────────────
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
        return info.get("how_did_you_hear", "LinkedIn")
    if re.search(r'salary.*expect|expected.*salary|desired.*salary|compensation', ll):
        return info.get("salary_expectation", "")
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

async def fill_application_questions(page: Page, answers: dict, unknowns: list[str]) -> None:
    await page.wait_for_timeout(800)

    # Find all currently-empty required fields via JS
    fields = await page.evaluate("""() => {
        const out = [];
        const SKIP_TYPES = new Set(['file', 'hidden', 'submit', 'button', 'checkbox', 'reset']);
        const inputs = document.querySelectorAll(
            'input[aria-required="true"], select[aria-required="true"], '
            'textarea[aria-required="true"], [role="combobox"][aria-required="true"], '
            '[role="spinbutton"][aria-required="true"]'
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
            label = label.replace(/\\*/g, '').trim();
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
        'button[aria-label*="Apply for Job" i], '
        'button:has-text("Apply for Job"), '
        'button:has-text("Apply Now"), '
        'a:has-text("Apply for Job")'
    ).first

    if not await apply_loc.count():
        return "skipped", "no_apply_button"

    try:
        await apply_loc.click(timeout=8000)
    except Exception as e:
        return "skipped", f"click_failed:{str(e)[:40]}"

    await page.wait_for_timeout(2500)
    await page.wait_for_load_state("domcontentloaded")

    # Handle auth
    auth_ok = await handle_auth(page)
    if not auth_ok:
        return "needs_review", "email_verification_required"

    await page.wait_for_timeout(2000)
    await page.wait_for_load_state("domcontentloaded")

    # Walk through form steps
    for step_num in range(10):
        step = await current_step(page)
        print(f"  Step {step_num+1}: {step or '(unknown)'}")

        body = (await page.evaluate("document.body.innerText")).lower()

        # Review/submit step
        if any(kw in body for kw in [
            "review and submit", "review your application",
            "submit your application", "my review",
        ]):
            result = await submit_application(page)
            return result, "; ".join(unknowns)

        if any(k in step for k in ("information", "contact", "personal")):
            await fill_my_information(page)
        elif any(k in step for k in ("experience", "background", "resume", "my experience")):
            await fill_my_experience(page)
        elif any(k in step for k in ("voluntary", "disclos", "eeo", "equal opportunity", "diversity")):
            pass  # Prefer not to disclose — just click Next
        elif any(k in step for k in ("question", "additional", "application question", "screening")):
            await fill_application_questions(page, answers, unknowns)
        else:
            # Unknown step — still try to fill required fields
            await fill_application_questions(page, answers, unknowns)

        await page.wait_for_timeout(600)

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

    print(f"[+] Queue: {len(queue)} job(s)  |  roles={ROLES_ENV}  |  headless={HEADLESS}")

    session_state = session_from_b64()
    if not session_state and SESSION_FILE.exists():
        try:
            session_state = json.loads(SESSION_FILE.read_text())
        except Exception:
            pass
    if session_state:
        print("[+] Session state loaded.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"] if HEADLESS else [],
            slow_mo=0 if HEADLESS else 60,
        )
        ctx_kwargs: dict = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1280, "height": 900},
        }
        if session_state:
            ctx_kwargs["storage_state"] = session_state

        context: BrowserContext = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()

        for i, job in enumerate(queue):
            title   = job.get("title", "Unknown")
            company = job.get("company", "Unknown")
            link    = job.get("link", "")

            print(f"\n[{i+1}/{len(queue)}] {title} @ {company}")

            try:
                status, notes = await apply_to_job(page, job, answers)
            except PlaywrightTimeout as e:
                status, notes = "error", f"timeout:{str(e)[:60]}"
            except Exception as e:
                status, notes = "error", f"{type(e).__name__}:{str(e)[:60]}"

            print(f"  → {status}" + (f"  |  {notes}" if notes else ""))

            if status in ("applied", "already_applied"):
                applied_ids.add(link)
                save_applied(applied_ids)

            row: dict = {
                "title":      title,
                "company":    company,
                "location":   job.get("location", ""),
                "link":       link,
                "status":     status,
                "applied_on": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "notes":      notes,
            }
            results.append(row)
            append_csv(row)

            await page.wait_for_timeout(1200)

        # Persist session
        state = await context.storage_state()
        state["origins"] = []
        SESSION_FILE.parent.mkdir(exist_ok=True)
        SESSION_FILE.write_text(json.dumps(state))
        print(f"\n[+] Session saved → {SESSION_FILE.name}")
        print(f"    WD_SESSION_B64 = {session_to_b64(state)[:40]}…")

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
