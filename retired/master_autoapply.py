"""
RETIRED — 2026-05-16
====================
Retired by choice — owner prefers to review matched jobs manually before
applying rather than auto-submitting. May be re-activated in the future
if automated applying is reconsidered. The workflow (master_autoapply.yml)
has been removed.

What this script did:
  - Read csv/new_jobs.csv, which is the consolidated job feed written by
    the Greenhouse, Lever, and Ashby scrapers.
  - Filtered to Data Analyst, Data Engineer, and Business Intelligence
    roles posted within the last 24 hours.
  - Used Playwright (headless Chromium) to navigate to each job's
    application form and auto-fill it: personal info, resume upload,
    LinkedIn, work authorization radios, EEO/demographic dropdowns
    (always answered "decline to self-identify"), and common screener
    questions (visa sponsorship, relocation, salary, etc.).
  - Supported three ATS platforms via platform-specific form fillers:
      * Greenhouse  — public job-boards.greenhouse.io forms
      * Lever       — jobs.lever.co forms
      * Ashby       — jobs.ashbyhq.com forms
  - Tracked applied IDs in json/master_applied_ids.json and failed
    attempts in json/master_failed_ids.json (permanently skipped after
    2 failed attempts).
  - Emailed a colour-coded HTML summary (applied / failed / error) in EST.

Original script preserved here for reference only. Do not re-activate
without reviewing applicant info, resume path, and role filter regex.

────────────────────────────────────────────────────────────────────────────────

master_autoapply.py
--------------------
Reads csv/new_jobs.csv (written by Greenhouse / Lever / Ashby scrapers),
filters to Data Analyst / Data Engineer / Business Intelligence roles,
and auto-applies using Playwright. Sends one summary email in EST.
"""

import asyncio
import csv
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, Page

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Applicant info ─────────────────────────────────────────────────────────────
_applicant_json = os.environ.get("APPLICANT_INFO_JSON", "")
if not _applicant_json:
    raise EnvironmentError("APPLICANT_INFO_JSON is not set.")
YOUR_INFO = json.loads(_applicant_json)
if os.environ.get("RESUME_PATH"):
    YOUR_INFO["resume_path"] = os.environ["RESUME_PATH"]

# ── Email / paths ──────────────────────────────────────────────────────────────
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

BASE_DIR     = Path(__file__).parent
MASTER_CSV   = BASE_DIR / "csv"  / "new_jobs.csv"
APPLIED_LOG  = BASE_DIR / "json" / "master_applied_ids.json"
FAILED_LOG   = BASE_DIR / "json" / "master_failed_ids.json"
MAX_ATTEMPTS = 2          # permanently skip a job after this many failed attempts
OUTPUT_CSV   = BASE_DIR / "csv"  / "master_applied.csv"

EST          = ZoneInfo("America/New_York")
PAGE_TIMEOUT = 60_000
DELAY_BETWEEN = 5         # seconds between each application attempt

# ── Role filter — only apply to these title patterns ───────────────────────────
AUTOAPPLY_RE = re.compile(r'data\s+analyst|data\s+engineer|business\s+intelligence', re.I)


# ── State ──────────────────────────────────────────────────────────────────────

def load_applied() -> dict:
    if APPLIED_LOG.exists():
        data = json.loads(APPLIED_LOG.read_text())
        return data if isinstance(data, dict) else {k: {} for k in data}
    return {}

def save_applied(ids: dict) -> None:
    APPLIED_LOG.parent.mkdir(parents=True, exist_ok=True)
    APPLIED_LOG.write_text(json.dumps(ids, indent=2))

def load_failed() -> dict:
    if FAILED_LOG.exists():
        data = json.loads(FAILED_LOG.read_text())
        return data if isinstance(data, dict) else {}
    return {}

def save_failed(ids: dict) -> None:
    FAILED_LOG.parent.mkdir(parents=True, exist_ok=True)
    FAILED_LOG.write_text(json.dumps(ids, indent=2))

def append_output_csv(rows: list[dict]) -> None:
    if not rows:
        return
    cols = ["source", "job_id", "title", "company", "location", "role",
            "posted", "url", "status", "note", "applied_at_est"]
    write_header = not OUTPUT_CSV.exists() or OUTPUT_CSV.stat().st_size == 0
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ── Job loading ────────────────────────────────────────────────────────────────

def load_pending(applied: dict, failed: dict) -> list[dict]:
    """Read new_jobs.csv, skip already-applied/permanently-failed, filter by role and age."""
    if not MASTER_CSV.exists():
        print(f"[!] Master CSV not found: {MASTER_CSV}")
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    seen_ids: set[str] = set()
    pending: list[dict] = []
    with open(MASTER_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = f"{row.get('source','')}:{row.get('job_id','')}"
            if key in seen_ids or key in applied:
                continue
            if failed.get(key, 0) >= MAX_ATTEMPTS:
                continue
            seen_ids.add(key)
            if not AUTOAPPLY_RE.search(row.get("title", "")):
                continue
            try:
                dt = datetime.fromisoformat(row["posted"].replace("Z", "+00:00")).astimezone(timezone.utc)
                if dt < cutoff:
                    continue
            except Exception:
                continue
            pending.append(row)
    return pending


# ── Shared Playwright helpers ──────────────────────────────────────────────────

async def safe_fill(page: Page, selector: str, value: str) -> bool:
    """Fill the first matching visible input; silently skip if not found."""
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

async def fill_by_label(page: Page, keywords: list, value: str) -> bool:
    """Fill an input whose associated label contains all keywords (in order)."""
    if not value:
        return False
    try:
        pat = re.compile(".*".join(re.escape(k) for k in keywords), re.IGNORECASE)
        els = page.get_by_label(pat)
        for i in range(await els.count()):
            el = els.nth(i)
            if "combobox" in (await el.get_attribute("role") or ""):
                continue
            await el.scroll_into_view_if_needed(timeout=2000)
            await el.fill(value, timeout=4000)
            return True
    except Exception:
        pass
    return False

async def type_react_select(page: Page, label_pattern: str, text: str) -> bool:
    """Type into a React-Select control found by its label, then pick the best match."""
    if not text:
        return False
    try:
        els = page.get_by_label(re.compile(label_pattern, re.IGNORECASE))
        for i in range(await els.count()):
            ctrl = els.nth(i).locator('xpath=ancestor::div[contains(@class,"select__control")][1]')
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
            for j in range(await opts.count()):
                if text.lower() in (await opts.nth(j).inner_text()).strip().lower():
                    await opts.nth(j).click(timeout=3000)
                    return True
            if await opts.count() > 0:
                await opts.first.click(timeout=3000)
                return True
            await page.keyboard.press("Escape")
    except Exception:
        pass
    return False

async def click_radio(page: Page, label_pattern: str, value: str) -> bool:
    """Click a radio button whose containing label matches label_pattern and whose value/text matches value."""
    js = f"""() => {{
        const pat = new RegExp({repr(label_pattern)}, 'i');
        const val = {repr(value.lower())};
        for (const el of document.querySelectorAll('div,fieldset,li,section,p')) {{
            const lbl = el.querySelector('label,legend,p,h3,h4,span[class*="label"],div[class*="label"],div[class*="question"]');
            if (!lbl || !pat.test(lbl.innerText) || lbl.innerText.length > 600) continue;
            const radios = Array.from(el.querySelectorAll('input[type="radio"]'));
            if (!radios.length) continue;
            const r = radios.find(r => {{
                const wrap = r.closest('label') || r.parentElement || {{}};
                const t = ((r.value||'') + ' ' + (wrap.innerText||'')).trim().toLowerCase();
                return t.includes(val);
            }});
            const target = r || (val === 'yes' ? radios[0] : null);
            if (target && !target.checked) {{ target.click(); return true; }}
        }}
        return false;
    }}"""
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False

async def fill_greenhouse_selects(page: Page) -> list:
    """
    Handle Greenhouse React-Select screener questions with pre-defined answers.
    Rules are matched by regex against the question label text; the first match wins.
    answer_fallbacks maps each canonical answer to a list of option-text substrings
    tried in order so we handle different phrasing across companies.
    """
    rules = [
        (r"referred.*current employee|current employee.*refer",                       "no"),
        (r"legally authorized.*work|authorized.*us.*canada",                          "yes"),
        (r"hold.*visa.*type|what type.*visa",                                         "none"),
        (r"california.*privacy notice|applicant privacy notice|job applicant.*privacy","continue"),
        (r"discharged.*resign|terminated",                                             "no"),
        (r"require.*visa sponsorship",                                                 "no"),
        (r"does not provide visa sponsorship.*acknowledge|permanent work auth.*eligible","yes"),
        (r"currently hold.*temporary work auth|cpt.*opt",                             "no"),
        (r"\bsponsor\b",                                                               "no"),
        (r"non.compete|restrictive covenant",                                          "no"),
        (r"relative.*employed|employ.*family",                                         "no"),
        (r"previously.*employed.*company|have you worked for",                         "no"),
        (r"willing to relocate",                                                       "yes"),
        (r"authorized.*work.*united states|legally.*work.*us",                        "yes"),
        (r"salary requirements",                                                       "70"),
        (r"opt in.*text mess|sms.*opt",                                               "no"),
        (r"accept.*terms.*application|please review.*accept",                         "i certify"),
        (r"describe.*gender|i identify my gender|\bgender\b",                          "decline"),
        (r"describe.*racial|ethnic.*background|hispanic|latino|\brace\b",             "decline"),
        (r"have.*disability|identify.*disability|disability status|\bdisability\b",   "decline"),
        (r"military veteran|identify.*veteran|veteran status|\bveteran\b",            "decline"),
        (r"sexual orient|lgbtq",                                                       "decline"),
        (r"consent.*sms|sms.*recruiting",                                              "no"),
        (r"level.*sql|sql.*expertise",                                                 "advanced"),
        (r"years.*azure|azure.*experience",                                            "yes"),
        (r"years.*data engineer",                                                      "yes"),
        (r"have.*worked.*data.*pipeline",                                              "yes"),
        (r"convicted.*felony|felony.*convict|criminal.*record",                       "no"),
        (r"how.*find.*out|how.*hear.*about|source.*opening|find.*position",           "indeed"),
        (r"related.*employee|related.*any.*person",                                    "no"),
        (r"require.*sponsorship|future.*require.*sponsor",                             "no"),
    ]
    answer_fallbacks = {
        "yes":      ["yes", "i verify", "i confirm", "i agree", "i certify", "authorized to work", "eligible"],
        "no":       ["no", "i am not", "not authorized", "i do not", "never"],
        "none":     ["none", "do not hold", "n/a", "not applicable"],
        "continue": ["continue", "i have read", "acknowledge", "i certify", "i accept"],
        "decline":  ["decline", "prefer not", "choose not", "i prefer not"],
        "70":       ["70", "$70", "70,000", "60", "80"],
        "i certify":["i certify", "i verify", "i confirm", "i agree", "foregoing applicant"],
        "advanced": ["advanced", "expert", "proficient"],
        "indeed":   ["indeed", "job board", "online", "internet", "linkedin", "glassdoor",
                     "ziprecruiter", "google", "website", "career"],
    }
    controls_info = await page.evaluate("""() => {
        const controls = Array.from(document.querySelectorAll('[class*="select__control"]'))
            .filter(c => c.querySelector('[class*="select__placeholder"]'));
        return controls.map(ctrl => {
            const input = ctrl.querySelector('input');
            const inputId = input ? input.id : '';
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
    rules_compiled = [(re.compile(p, re.IGNORECASE), v) for p, v in rules]
    filled = []
    for item in (controls_info or []):
        label_text = item.get("label", "")
        input_id   = item.get("inputId", "")
        if not label_text:
            continue
        answer = next((v for p, v in rules_compiled if p.search(label_text)), None)
        if not answer:
            continue
        if input_id:
            control = page.locator(
                f'div[class*="select__control"]:has(input[id="{input_id}"])'
            ).first
        else:
            escaped = label_text[:40].replace('"', '\\"')
            control = page.locator(
                f'xpath=//label[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                f'"abcdefghijklmnopqrstuvwxyz"), "{escaped.lower()[:30]}")]'
                f'/following::div[contains(@class,"select__control")][1]'
            ).first
        clicked = False
        if control and await control.count() > 0:
            try:
                if await control.locator('[class*="select__single-value"]').count() > 0:
                    continue
                await control.scroll_into_view_if_needed(timeout=2000)
                await control.click(timeout=3000)
                await page.wait_for_timeout(900)
                opts = page.locator('[class*="select__option"]')
                texts = [(await opts.nth(j).inner_text()).strip() for j in range(await opts.count())]
                candidates = [answer.lower()] + [p for p in answer_fallbacks.get(answer.lower(), []) if p != answer.lower()]
                for cand in candidates:
                    for j, t in enumerate(texts):
                        if cand in t.lower():
                            await opts.nth(j).click(timeout=3000)
                            filled.append(f"{label_text[:50]} → {answer}")
                            clicked = True
                            break
                    if clicked:
                        break
                if not clicked and texts:
                    if answer == "indeed":
                        await opts.first.click(timeout=3000)
                    else:
                        await page.keyboard.press("Escape")
            except Exception:
                pass
    return filled


# ── Platform-specific form fillers ─────────────────────────────────────────────

async def _fill_personal(page: Page, info: dict) -> None:
    """Fill name/email/phone fields common to all three platforms."""
    await safe_fill(page, 'input[id*="first_name"], input[name*="first_name"], input[autocomplete="given-name"]',  info["first_name"])
    await safe_fill(page, 'input[id*="last_name"],  input[name*="last_name"],  input[autocomplete="family-name"]', info["last_name"])
    await safe_fill(page, 'input[id*="email"],      input[name*="email"],      input[type="email"]',               info["email"])
    await safe_fill(page, 'input[id*="phone"],      input[name*="phone"],      input[type="tel"]',                 info["phone"])
    await fill_by_label(page, ["first name"],  info["first_name"])
    await fill_by_label(page, ["last name"],   info["last_name"])
    await fill_by_label(page, ["email"],       info["email"])
    await fill_by_label(page, ["phone"],       info["phone"])

async def _upload_resume(page: Page, info: dict) -> None:
    """Upload resume PDF to the first file input found on the page."""
    resume = info.get("resume_path", "")
    if not resume or not Path(resume).exists():
        return
    try:
        inp = page.locator(
            'input[type="file"][id*="resume"], input[type="file"][name*="resume"], '
            'input[type="file"][accept*="pdf"], input[type="file"]'
        ).first
        if await inp.count() > 0:
            await inp.set_input_files(resume, timeout=8000)
            print("    [resume] uploaded")
    except Exception as e:
        print(f"    [resume] failed: {e}")

_SUBMIT_SKIP = re.compile(r'^(dismiss|cancel|close|back|no|skip)$', re.I)

async def _find_submit_btn(page: Page):
    """
    Find the real submit button. Priority:
      1. Visible button whose text contains 'submit' or 'apply' and is wide enough to be real (>80px).
      2. Any rendered type="submit" that isn't a tiny dismiss-style button.
    Lever uses type="button" for its main CTA, so we can't rely on type alone.
    """
    cand = page.locator('button, input[type="submit"], input[type="button"]')
    for i in range(await cand.count()):
        b = cand.nth(i)
        box = await b.bounding_box()
        if not box or box["width"] < 80 or box["height"] < 24 or box["y"] < 0:
            continue
        try:
            txt = (await b.inner_text()).strip().lower()
        except Exception:
            txt = (await b.get_attribute("value") or "").lower()
        if re.search(r'\b(submit|apply)\b', txt) and not _SUBMIT_SKIP.match(txt):
            return b

    typed = page.locator('button[type="submit"], input[type="submit"]')
    for i in range(await typed.count()):
        b = typed.nth(i)
        box = await b.bounding_box()
        if not box or box["width"] < 50 or box["height"] < 20 or box["y"] < 0:
            continue
        try:
            txt = (await b.inner_text()).strip().lower()
        except Exception:
            txt = ""
        if not _SUBMIT_SKIP.match(txt):
            return b
    return None

async def _submit_and_confirm(page: Page) -> tuple[bool, str]:
    """Click submit and check the result. Returns (success, note)."""
    btn = await _find_submit_btn(page)
    if btn is None:
        return False, "no submit button"
    original_url = page.url
    try:
        await btn.scroll_into_view_if_needed(timeout=8000)
        await btn.click(timeout=15_000)
        await page.wait_for_timeout(4000)
    except Exception as e:
        return False, f"submit failed: {e}"
    if page.url != original_url:
        return True, "submitted (URL changed)"
    text = (await page.evaluate("document.body.innerText")).lower()
    if any(p in text for p in ["application submitted", "thank you", "we've received",
                                "successfully submitted", "application received", "thanks for applying"]):
        return True, ""
    if any(p in text for p in ["please fix", "required field", "field is required",
                                "fields are required", "fix the following", "must be filled"]):
        visible_inputs = await page.locator(
            'input[type="text"]:visible, input[type="email"]:visible, textarea:visible'
        ).count()
        if visible_inputs > 0:
            return False, "validation error after submit"
        return True, "submitted (form gone - phrase in job description)"
    return True, "no confirmation detected"


async def fill_greenhouse(page: Page, job: dict, info: dict) -> tuple[bool, str]:
    """Navigate to and fill a Greenhouse public application form (job-boards.greenhouse.io)."""
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(2000)

    # Some URLs land on the job description — click through to the actual form
    if await page.locator('button[type="submit"], input[type="submit"]').count() == 0:
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

    # Scroll to load lazy fields, then reset to top before filling
    for _ in range(8):
        await page.evaluate("window.scrollBy(0, 400)")
        await page.wait_for_timeout(150)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)

    await _fill_personal(page, info)
    await fill_by_label(page, ["full legal name"], f"{info['first_name']} {info['last_name']}")
    await type_react_select(page, r"country", info.get("country", "United States"))
    await type_react_select(page, r"location.*city|^location$",
                            f"{info.get('city','')}, {info.get('state','')}")
    for sel in ['input[id*="address"]', 'input[placeholder*="address" i]']:
        await safe_fill(page, sel, info.get("address", ""))
    await fill_by_label(page, ["street address"], info.get("address", ""))
    await fill_by_label(page, ["city"],           info.get("city", ""))
    await safe_fill(page, 'input[id*="state"]',   info.get("state", ""))
    await safe_fill(page, 'input[id*="zip"], input[id*="postal"]', info.get("zip", ""))
    await _upload_resume(page, info)
    await safe_fill(page, 'input[id*="linkedin"], input[placeholder*="LinkedIn" i]', info.get("linkedin_url", ""))
    await fill_by_label(page, ["linkedin"], info.get("linkedin_url", ""))
    if info.get("cover_letter"):
        await safe_fill(page, 'textarea[id*="cover"], textarea[name*="cover"]', info["cover_letter"])
    await click_radio(page, r"legally authorized.*work|authorized.*work.*us", "yes")
    await click_radio(page, r"require.*visa sponsorship",                     "no")
    await fill_greenhouse_selects(page)

    # EEO selects that have no formal label[for=id] — locate via JS proximity search
    _decline_terms = ["decline", "prefer not", "choose not", "i don't wish"]
    for _label_kw in ["gender", "hispanic", "veteran", "disability"]:
        try:
            clicked = await page.evaluate("""(kw) => {
                const pat = new RegExp(kw, 'i');
                for (const lbl of document.querySelectorAll('label')) {
                    if (!pat.test(lbl.innerText)) continue;
                    let node = lbl;
                    for (let i = 0; i < 8; i++) {
                        if (!node) break;
                        const ctrl = node.querySelector('[class*="select__control"]');
                        if (ctrl && ctrl.querySelector('[class*="select__placeholder"]')) {
                            ctrl.click();
                            return true;
                        }
                        node = node.parentElement;
                    }
                }
                return false;
            }""", _label_kw)
            if clicked:
                await page.wait_for_timeout(700)
                opts = page.locator('[class*="select__option"]')
                for j in range(await opts.count()):
                    t = (await opts.nth(j).inner_text()).strip().lower()
                    if any(d in t for d in _decline_terms):
                        await opts.nth(j).click(timeout=3000)
                        break
                else:
                    await page.keyboard.press("Escape")
        except Exception:
            pass

    return await _submit_and_confirm(page)


async def fill_lever(page: Page, job: dict, info: dict) -> tuple[bool, str]:
    """Navigate to and fill a Lever application form (jobs.lever.co)."""
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(2000)

    # Lever may show the job description first — click Apply if the form isn't visible yet
    apply_btn = page.locator(
        'a:has-text("Apply"), a:has-text("Apply for this job"), '
        'a[class*="apply"], .posting-btn-apply'
    ).first
    if await apply_btn.count() > 0 and await page.locator('input[name="name"]').count() == 0:
        href = await apply_btn.get_attribute("href")
        if href and href.startswith("http"):
            await page.goto(href, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        else:
            await apply_btn.click(timeout=5000)
        await page.wait_for_timeout(2000)

    for _ in range(6):
        await page.evaluate("window.scrollBy(0, 400)")
        await page.wait_for_timeout(150)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)

    # Lever uses a single "Full name" field rather than split first/last
    await safe_fill(page, 'input[name="name"]', f"{info['first_name']} {info['last_name']}")
    await _fill_personal(page, info)  # fallback split fields

    await safe_fill(page, 'input[name="email"]', info["email"])
    await safe_fill(page, 'input[name="phone"]', info["phone"])
    await safe_fill(page, 'input[name="org"]',   info.get("current_company", ""))

    await safe_fill(page, 'input[name="urls[LinkedIn]"]',  info.get("linkedin_url", ""))
    await safe_fill(page, 'input[name="urls[GitHub]"]',    info.get("github_url", ""))
    await safe_fill(page, 'input[name="urls[Portfolio]"]', info.get("website", ""))
    await fill_by_label(page, ["linkedin"], info.get("linkedin_url", ""))

    await safe_fill(page, 'input[name="location"]',
                    f"{info.get('city','')}, {info.get('state','')}")
    await fill_by_label(page, ["location"], f"{info.get('city','')}, {info.get('state','')}")

    await _upload_resume(page, info)

    if info.get("cover_letter"):
        await safe_fill(page, 'textarea[name="comments"], textarea[id*="additional"]',
                        info["cover_letter"])

    # Work authorization — Lever gates the submit button on these being answered
    await click_radio(page, r"legally authorized.*work|authorized.*work.*us|authorized to work", "yes")
    await click_radio(page, r"require.*visa sponsorship|require.*sponsorship|visa.*sponsor",     "no")
    await click_radio(page, r"currently hold.*temporary|cpt.*opt|opt.*cpt",                      "no")
    await click_radio(page, r"relative.*employed|employ.*family|related.*employee",              "no")

    # Lever EEO — native <select> elements
    for sel_name in ['select[name*="eeo"]', 'select[name*="gender"]',
                     'select[name*="race"]', 'select[name*="veteran"]', 'select[name*="disability"]']:
        try:
            loc = page.locator(sel_name).first
            if await loc.count() > 0:
                options = await loc.locator("option").all_text_contents()
                decline = next((o for o in options if "decline" in o.lower() or "prefer not" in o.lower()), None)
                if decline:
                    await loc.select_option(label=decline, timeout=3000)
        except Exception:
            pass

    # Lever EEO — radio-based demographic questions
    await click_radio(page, r"gender|identify.*gender", "decline")
    await click_radio(page, r"race|ethnicity|ethnic",   "decline")
    await click_radio(page, r"veteran|military",        "decline")
    await click_radio(page, r"disability|disabled",     "decline")

    # JS fallback for disability radio groups that have no readable labels
    await page.evaluate("""() => {
        for (const radio of document.querySelectorAll('input[type="radio"]')) {
            const name = radio.name;
            const group = Array.from(document.querySelectorAll('input[type="radio"][name="' + name + '"]'));
            if (group.some(r => r.checked)) continue;
            const target = group.find(r => {
                const wrap = r.closest('label') || r.parentElement || {};
                const t = ((r.value || '') + ' ' + (wrap.innerText || '')).toLowerCase();
                return t.includes("not") || t.includes("don't") || t.includes("do not") || t.includes("no, i");
            });
            if (target) target.click();
        }
    }""")

    return await _submit_and_confirm(page)


async def fill_ashby(page: Page, job: dict, info: dict) -> tuple[bool, str]:
    """Navigate to and fill an Ashby application form (jobs.ashbyhq.com)."""
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(2500)

    for _ in range(6):
        await page.evaluate("window.scrollBy(0, 400)")
        await page.wait_for_timeout(150)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)

    await _fill_personal(page, info)

    location_str = f"{info.get('city','')}, {info.get('state','')}"
    await fill_by_label(page, ["location"],    location_str)
    await fill_by_label(page, ["city"],        info.get("city", ""))
    await type_react_select(page, r"location", location_str)

    await _upload_resume(page, info)

    await fill_by_label(page, ["linkedin"],    info.get("linkedin_url", ""))
    await fill_by_label(page, ["linkedin url"], info.get("linkedin_url", ""))
    await safe_fill(page, 'input[placeholder*="LinkedIn" i]', info.get("linkedin_url", ""))

    await fill_by_label(page, ["website"],     info.get("website", ""))
    await fill_by_label(page, ["portfolio"],   info.get("website", ""))

    # Ashby EEO — native <select> elements, pick "Decline to self-identify"
    decline_labels = ["decline", "prefer not", "choose not to", "i don't wish"]
    for label_kw in [["gender"], ["race"], ["ethnicity"], ["veteran"], ["disability"]]:
        try:
            el = page.get_by_label(re.compile("|".join(label_kw), re.I)).first
            if await el.count() == 0:
                continue
            tag = await el.evaluate("e => e.tagName.toLowerCase()")
            if tag == "select":
                options = await el.locator("option").all_text_contents()
                match = next((o for o in options if any(d in o.lower() for d in decline_labels)), None)
                if match:
                    await el.select_option(label=match, timeout=3000)
        except Exception:
            pass

    # Ashby EEO — react-select demographic dropdowns
    for label_pat in [r"gender", r"race|ethnicity", r"veteran", r"disability"]:
        try:
            els = page.get_by_label(re.compile(label_pat, re.I))
            for i in range(await els.count()):
                ctrl = els.nth(i).locator('xpath=ancestor::div[contains(@class,"select__control")][1]')
                if await ctrl.count() == 0:
                    continue
                if await ctrl.locator('[class*="select__single-value"]').count() > 0:
                    continue
                await ctrl.click(timeout=3000)
                await page.wait_for_timeout(700)
                opts = page.locator('[class*="select__option"]')
                for j in range(await opts.count()):
                    t = (await opts.nth(j).inner_text()).strip().lower()
                    if any(d in t for d in decline_labels):
                        await opts.nth(j).click(timeout=3000)
                        break
                else:
                    await page.keyboard.press("Escape")
        except Exception:
            pass

    await click_radio(page, r"authorized.*work|legally authorized", "yes")
    await click_radio(page, r"require.*sponsor",                    "no")

    return await _submit_and_confirm(page)


# ── Router ─────────────────────────────────────────────────────────────────────

async def apply_to_job(page: Page, job: dict, info: dict) -> tuple[bool, str]:
    """Dispatch to the correct platform filler based on the job's source field."""
    source = job.get("source", "").lower()
    if source == "greenhouse":
        return await fill_greenhouse(page, job, info)
    if source == "lever":
        return await fill_lever(page, job, info)
    if source == "ashby":
        return await fill_ashby(page, job, info)
    return False, f"unknown source: {source}"


# ── Email ──────────────────────────────────────────────────────────────────────

def _est(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(EST).strftime("%b %d, %Y %I:%M %p EST")
    except Exception:
        return iso

def send_summary_email(results: list[dict]) -> None:
    if not EMAIL_PASSWORD or not results:
        return

    n_applied = sum(1 for r in results if r["status"] == "applied")
    n_failed  = sum(1 for r in results if r["status"] != "applied")

    status_color = {"applied": "#d4edda", "failed": "#f8d7da", "error": "#f8d7da"}
    rows = ""
    for r in sorted(results, key=lambda x: x.get("applied_at_est", "")):
        bg = status_color.get(r["status"], "#fff")
        rows += (
            f"<tr style='background:{bg}'>"
            f"<td>{r.get('title','')}</td>"
            f"<td>{r.get('company','')}</td>"
            f"<td>{r.get('source','').capitalize()}</td>"
            f"<td>{r.get('role','')}</td>"
            f"<td>{r['status'].upper()}</td>"
            f"<td style='white-space:nowrap'>{r.get('applied_at_est','')}</td>"
            f"<td>{r.get('note','') or ''}</td>"
            f"<td><a href='{r.get('url','')}'>Link</a></td>"
            f"</tr>"
        )

    body = f"""
    <h2>Master Auto-Apply Summary</h2>
    <p>
      <b style="color:#155724">Applied: {n_applied}</b> &nbsp;|&nbsp;
      <b style="color:#721c24">Failed/Error: {n_failed}</b> &nbsp;|&nbsp;
      <b>Total: {len(results)}</b>
    </p>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Source</th><th>Role</th>
        <th>Status</th><th>Applied At (EST)</th><th>Note</th><th>Link</th>
      </tr>
      {rows}
    </table>
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Auto-Apply: {n_applied} applied | {n_failed} failed | {len(results)} total"
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, EMAIL_TO, msg.as_string())
        print(f"[+] Summary email sent to {EMAIL_TO}")
    except Exception as e:
        print(f"[!] Email failed: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

async def run() -> None:
    applied = load_applied()
    failed  = load_failed()
    pending = load_pending(applied, failed)
    print(f"[i] {len(pending)} eligible job(s) across all sources")

    if not pending:
        print("[i] Nothing to apply to.")
        return

    results: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        for job in pending:
            title   = job.get("title", "")
            company = job.get("company", "")
            source  = job.get("source", "")
            print(f"\n[>] [{source.upper()}] {title} @ {company}")

            result = {**job, "status": "error", "note": "", "applied_at_est": ""}
            page = await context.new_page()
            try:
                success, note = await apply_to_job(page, job, YOUR_INFO)
                now_est = datetime.now(EST).isoformat()
                if success:
                    result["status"]         = "applied"
                    result["applied_at_est"] = _est(now_est)
                    applied[f"{source}:{job['job_id']}"] = {
                        "title": title, "company": company,
                        "applied_at": now_est,
                    }
                    print(f"    [+] applied at {result['applied_at_est']}")
                else:
                    result["status"] = "failed"
                    result["note"]   = note
                    key = f"{source}:{job['job_id']}"
                    failed[key] = failed.get(key, 0) + 1
                    print(f"    [-] failed: {note} ({failed[key]}/{MAX_ATTEMPTS} attempts)")
            except Exception as e:
                result["note"] = str(e)[:120]
                key = f"{source}:{job['job_id']}"
                failed[key] = failed.get(key, 0) + 1
                print(f"    [!] error: {e} ({failed[key]}/{MAX_ATTEMPTS} attempts)")
            finally:
                await page.close()

            results.append(result)
            await asyncio.sleep(DELAY_BETWEEN)

        await browser.close()

    save_applied(applied)
    save_failed(failed)
    append_output_csv(results)

    n_ok = sum(1 for r in results if r["status"] == "applied")
    if n_ok > 0:
        send_summary_email(results)
    else:
        print("[i] No successful applications this run — skipping email.")

    print(f"\n[i] Done — {n_ok}/{len(results)} applied")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
