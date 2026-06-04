"""
oracle_autoapply.py

Oracle Auto-Apply Bot — reads scraped Oracle Cloud Recruiting (ORC) job CSVs and
applies via headless Playwright. Sibling of workday_autoapply.py; reuses the same
applicant identity, resume, answer rules and email reporting, but navigates Oracle's
"Candidate Experience" apply flow (Oracle JET / Redwood UI).

Key differences from Workday:
  • Identity verification uses a 6-DIGIT CODE emailed to the candidate (not a click
    link). fetch_verification_code() reads it over IMAP.
  • The apply flow is one configurable sequence of sections (Personal Info → Education
    → Experience → resume → questionnaire/disclosures → Review → Submit) rather than
    Workday's discrete wizard pages. apply_to_job() walks it with a resilient step loop.

Env vars (set as GitHub Secrets / local .env):
  ROLES                 comma-separated Oracle role slugs (aa,aie,bia,ra,sd,se,de,ds,
                        da,bi,analyst) or "all" for the combined all-roles CSVs
  ORACLE_APPLICANT_INFO JSON applicant profile (falls back to WD_APPLICANT_INFO)
  ORACLE_PASSWORD       password used when creating / signing into Oracle candidate accounts
  ORACLE_RESUME_PATH    absolute path to resume PDF on the runner
  ORACLE_SESSION_B64    base64-encoded Playwright storage_state (updated each run)
  HEADLESS              "true" for CI, "false" for local visible browser (default: true)
  MAX_APPLY             max applications per run (default: 20)
  ORACLE_CONCURRENCY    applications to run in parallel (default: 1)
  ORACLE_MAX_AGE_DAYS   skip queued jobs found more than this many days ago (default: 2)
  EMAIL_SENDER          Gmail address for summary email
  GMAIL_APP_PASSWORD    Gmail app password
  EMAIL_TO              recipient address for run summary
  WD_VERIFY_IMAP_USER   inbox that receives Oracle verification codes (reused from Workday)
  WD_VERIFY_IMAP_PASSWORD  Gmail app password for that inbox

APPLICANT_INFO JSON keys (same as Workday):
  first_name, last_name, email, phone
  address_line1, city, state, zip, country
  linkedin, github
  work_authorized ("Yes"/"No"), needs_sponsorship ("Yes"/"No")
  how_did_you_hear, willing_to_relocate, willing_to_travel
  salary_expectation, available_start, years_experience
  education  [] — list of {school, degree, field, end, current, gpa}
  experience [] — list of {company, title, start, end, current, description}
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
    # Prefer Oracle-specific info, fall back to the shared Workday profile.
    for env_key in ("ORACLE_APPLICANT_INFO", "WD_APPLICANT_INFO", "APPLICANT_INFO_JSON"):
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
ORA_PASSWORD = os.environ.get("ORACLE_PASSWORD", "") or os.environ.get("WORKDAY_PASSWORD", "")
RESUME_PATH  = os.environ.get("ORACLE_RESUME_PATH", "") or os.environ.get("WD_RESUME_PATH", "")
SESSION_B64  = os.environ.get("ORACLE_SESSION_B64", "")
HEADLESS     = os.environ.get("HEADLESS", "true").lower() == "true"
MAX_APPLY    = int(os.environ.get("MAX_APPLY", "20"))
CONCURRENCY  = max(1, int(os.environ.get("ORACLE_CONCURRENCY", "1")))  # apps in parallel
MAX_QUEUE_AGE_DAYS = int(os.environ.get("ORACLE_MAX_AGE_DAYS", "2"))   # skip jobs older than this
# Companies to never apply to (clearance-required / not wanted)
IGNORED_COMPANIES = {"booz allen", "guidehouse", "leidos"}

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

# Inbox that receives Oracle candidate-verification emails (the account email used
# when creating Oracle accounts). Reused from Workday so the same gmail/app-password
# secrets work. Used to auto-read the 6-digit verification code via IMAP.
VERIFY_IMAP_USER = os.environ.get("WD_VERIFY_IMAP_USER", "")   # e.g. osbornelopes.neu@gmail.com
VERIFY_IMAP_PASS = os.environ.get("WD_VERIFY_IMAP_PASSWORD", "")  # 16-char Gmail app password

SESSION_FILE = ROOT / "json" / "oracle_auth.json"
APPLIED_LOG  = ROOT / "json" / "oracle_applied.json"
ANSWERS_FILE = ROOT / "json" / "oracle_answers.json"
OUTPUT_CSV   = ROOT / "csv"  / "oracle_applied.csv"

# ── Debug screenshots ────────────────────────────────────────────────────────
DEBUG_SHOTS = os.environ.get("ORACLE_DEBUG_SHOTS", "").lower() in ("1", "true", "yes")
SHOTS_DIR = ROOT / "_oracle_debug_shots"
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

def build_queue(roles: str, applied_ids: set) -> list[dict]:
    """Build the apply queue from csv/oracle_jobs_*.csv, newest-first, de-duped by
    link and filtered against already-applied + stale (older than MAX_QUEUE_AGE_DAYS).

    Oracle CSV header: title,company,location,posted,link,found_on  (no role/is_new).
    Role slugs are used directly (aa, aie, bia, ra, sd, se, de, ds, da, bi, analyst);
    batch files carry _b1/_b2 suffixes which the trailing glob '*' absorbs.
    """
    csv_dir = ROOT / "csv"
    if roles == "all":
        files = sorted(glob.glob(str(csv_dir / "oracle_jobs_all*.csv")))
    else:
        role_list = [r.strip() for r in roles.split(",") if r.strip()]
        files = []
        seen = set()
        for r in role_list:
            # exact-slug match plus batch suffixes; the underscore guards against a
            # short code (e.g. "da") also matching "data_*".
            for pattern in (f"oracle_jobs_{r}.csv", f"oracle_jobs_{r}_*.csv"):
                for f in sorted(glob.glob(str(csv_dir / pattern))):
                    if f not in seen:
                        seen.add(f)
                        files.append(f)

    from datetime import datetime as _dt, timedelta as _td
    cutoff = _dt.now() - _td(days=MAX_QUEUE_AGE_DAYS)

    def _is_fresh(row: dict) -> bool:
        fo = (row.get("found_on") or "").strip()
        if not fo:
            return True  # no date → keep (can't tell)
        try:
            return _dt.strptime(fo.split()[0], "%Y-%m-%d") >= cutoff
        except Exception:
            return True

    seen_links: set[str] = set()
    jobs: list[dict] = []
    stale = 0
    for f in files:
        with open(f, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                link = (row.get("link") or "").strip()
                if not link or link in seen_links or link in applied_ids:
                    continue
                company = (row.get("company") or "").lower()
                if any(ig in company for ig in IGNORED_COMPANIES):
                    continue
                if not _is_fresh(row):
                    stale += 1
                    continue
                seen_links.add(link)
                jobs.append(row)

    if stale:
        print(f"[i] Skipped {stale} stale job(s) older than {MAX_QUEUE_AGE_DAYS}d")
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

# ── Generic Playwright helpers (shared with Workday bot) ─────────────────────────

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

async def _fill_text_verified(page: Page, value: str, selectors: list[str],
                              fid: str = "") -> bool:
    """Fill a text/textarea field robustly and verify the value stuck.

    Oracle JET inputs (like Workday's React inputs) sometimes ignore Playwright's
    .fill(), so we try .fill(), then keyboard typing, then a JS value-set with
    native input/change events dispatched — verifying input_value() each time.
    """
    if not value:
        return False
    ordered = ([f'#{fid}'] if fid else []) + [s for s in selectors if s != f'#{fid}']
    for sel in ordered:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0 or not await loc.is_visible(timeout=1500):
                continue
            try:
                if (await loc.input_value()).strip():
                    return True  # already populated
            except Exception:
                pass
            try:
                await loc.fill(value)
                if (await loc.input_value()).strip() == value:
                    return True
            except Exception:
                pass
            try:
                await loc.click(timeout=4000)
                await loc.press("Control+a")
                await loc.press("Delete")
                await page.keyboard.type(value, delay=15)
                await loc.press("Tab")
                if (await loc.input_value()).strip() == value:
                    return True
            except Exception:
                pass
            try:
                await loc.evaluate(
                    """(el, v) => {
                        const proto = el.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                        setter.call(el, v);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    }""",
                    value,
                )
                if (await loc.input_value()).strip() == value:
                    return True
            except Exception:
                pass
        except Exception:
            continue
    return False

async def dismiss_cookie_banner(page: Page) -> bool:
    """Dismiss a cookie-consent / privacy overlay if one is covering the page."""
    for sel in [
        '#onetrust-accept-btn-handler',
        '#truste-consent-button',
        'button#accept-recommended-btn-handler',
        '[aria-label="Accept cookies"]',
        'button:has-text("Accept All")',
        'button:has-text("Accept Cookies")',
        'button:has-text("Accept all cookies")',
        'button:has-text("Accept")',
        'button:has-text("I Agree")',
        'button:has-text("Agree")',
        'button:has-text("Got it")',
        'button:has-text("Allow all")',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=800):
                await loc.click(timeout=3000)
                await page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    return False

# ── Answer engine (shared with Workday bot) ──────────────────────────────────────

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
    if re.search(r'(relative|family member|immediate family).{0,60}(work|employ)', ll) or \
       re.search(r'(do you have|any).{0,30}(relative|family member).{0,40}(employ|work)', ll):
        if re.search(r"type\b.{0,6}none|enter\b.{0,6}none|provide .{0,30}name", ll):
            return "none"
        return "No"
    if re.search(r'type of employment|employment (type|desired)|desired employment|employment status (desired|preference)', ll):
        return "Full-time"
    if re.search(r"enter\b.{0,6}n/?a|please enter n/?a|if not a current|if not applicable", ll):
        return "N/A"
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

    # ── Contact info — ONLY for short field labels ────────────────────────────
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
        return info.get("how_did_you_hear", "LinkedIn")
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
        return ""

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

def _resolve_special(token: str, options: list[str]) -> str | None:
    """Resolve an _answer_from_profile special token against a dropdown's option
    texts. Returns the option text to pick, or None if nothing fits."""
    opts = [o.strip() for o in options if o and o.strip()]
    low = [o.lower() for o in opts]

    def _first(pred):
        for o, lo in zip(opts, low):
            if pred(lo):
                return o
        return None

    if token == "__MASTERS__":
        return (_first(lambda o: "master" in o)
                or _first(lambda o: "graduate degree" in o or "post graduate" in o or "postgraduate" in o)
                or _first(lambda o: "bachelor" in o))
    if token == "__YEARS_2PLUS__":
        # Prefer a range that starts at 2-4; else the lowest non-zero range.
        for want in ("2-4", "2 - 4", "2-5", "2 to 4", "3-5", "2+", "2 or more"):
            m = _first(lambda o: want in o)
            if m:
                return m
        return _first(lambda o: re.search(r'\b[2-9]\b', o) is not None) or (opts[0] if opts else None)
    if token == "__DECLINE__":
        return (_first(lambda o: "decline" in o or "do not wish" in o or "not to answer" in o
                       or "not to identify" in o)
                or _first(lambda o: o.startswith("i am not")))
    return None

# ── Verification code over IMAP ──────────────────────────────────────────────────

def fetch_verification_code() -> str | None:
    """Read the verification inbox over IMAP, find the newest Oracle candidate
    verification email, and return its 6-digit code (or None)."""
    if not VERIFY_IMAP_USER or not VERIFY_IMAP_PASS:
        return None
    import imaplib, email as _email
    from email.header import decode_header as _decode_header
    code = None
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(VERIFY_IMAP_USER, VERIFY_IMAP_PASS)
        M.select("INBOX")
        typ, data = M.search(None, "ALL")
        ids = data[0].split()
        for num in reversed(ids[-10:]):
            typ, msgdata = M.fetch(num, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            msg = _email.message_from_bytes(msgdata[0][1])
            subj = str(_decode_header(msg.get("Subject", ""))[0][0] or "")
            frm = (msg.get("From", "") or "").lower()
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
            haystack = f"{subj}\n{body}"
            # Only treat as a verification email if it looks like one (avoid pulling
            # a random 6-digit number out of an unrelated message).
            if not re.search(r'verif|verification|one[- ]time|access code|pin|confirm', haystack, re.I) \
               and "oracle" not in frm:
                continue
            # 6-digit code, optionally shown as the verification "code is 123456".
            m = re.search(r'(?:code|pin)[^0-9]{0,20}(\d{6})', haystack, re.I) \
                or re.search(r'\b(\d{6})\b', haystack)
            if m:
                code = m.group(1)
                break
        M.logout()
    except Exception as e:
        print(f"  [!] IMAP verification read failed: {e}")
    return code

# ── Oracle DOM helpers ───────────────────────────────────────────────────────────

async def _label_for(page: Page, loc) -> str:
    """Best-effort human label for an Oracle form control: aria-label,
    aria-labelledby, associated oj-label / <label>, or the nearest field
    container's text."""
    try:
        return await loc.evaluate(
            """(el) => {
                const clean = s => (s||'').replace(/\\s+/g,' ').trim();
                // aria-label
                let al = el.getAttribute && el.getAttribute('aria-label');
                if (al && al.trim()) return clean(al);
                // aria-labelledby
                let lb = el.getAttribute && el.getAttribute('aria-labelledby');
                if (lb) {
                    let t = lb.split(/\\s+/).map(id => {
                        let n = document.getElementById(id); return n ? n.innerText : '';
                    }).join(' ');
                    if (clean(t)) return clean(t);
                }
                // <label for=id>
                if (el.id) {
                    let lab = document.querySelector('label[for="'+CSS.escape(el.id)+'"]');
                    if (lab && clean(lab.innerText)) return clean(lab.innerText);
                }
                // closest oj component with a label-hint or an oj-label child
                let host = el.closest('oj-input-text, oj-text-area, oj-select-single, oj-c-select-single, oj-radioset, oj-checkboxset, oj-input-date, .oj-form-control, .oj-flex-item, [role="group"]');
                if (host) {
                    let lh = host.getAttribute && host.getAttribute('label-hint');
                    if (lh && lh.trim()) return clean(lh);
                    let lab = host.querySelector('oj-label, label, .oj-label, legend');
                    if (lab && clean(lab.innerText)) return clean(lab.innerText);
                    let t = clean(host.innerText);
                    if (t) return t.slice(0, 160);
                }
                return '';
            }"""
        ) or ""
    except Exception:
        return ""

async def click_apply(page: Page) -> bool:
    """Click the Apply button on an Oracle job posting page."""
    await dismiss_cookie_banner(page)
    return await try_click(page,
        '[data-qa="applyButton"]',
        'button[title="Apply" i]',
        'a[title="Apply" i]',
        'button:has-text("Apply Now")',
        'a:has-text("Apply Now")',
        'oj-button:has-text("Apply")',
        'button:has-text("Apply")',
        'a:has-text("Apply")',
        '[role="button"]:has-text("Apply")',
        timeout=8000,
    )

async def handle_identity(page: Page, company: str, title: str, link: str) -> str:
    """Oracle identity step: sign in with an existing candidate account, or register
    with email + 6-digit code. Returns "ok", "needs_review:<why>", or "skip".
    """
    await page.wait_for_timeout(1500)
    await dismiss_cookie_banner(page)
    body = ""
    try:
        body = (await page.evaluate("document.body.innerText")).lower()
    except Exception:
        pass

    # Existing-candidate sign-in: a visible password field is present.
    pwd = page.locator('input[type="password"]:visible').first
    has_pwd = await pwd.count() > 0 and await pwd.is_visible(timeout=1500)
    email_field = page.locator(
        'input[type="email"]:visible, input[autocomplete="username"]:visible, '
        'input[name*="mail" i]:visible, input[id*="mail" i]:visible'
    ).first
    has_email = await email_field.count() > 0 and await email_field.is_visible(timeout=1500)

    if has_pwd:
        if has_email:
            try:
                if not (await email_field.input_value()):
                    await email_field.fill(INFO.get("email", ""))
            except Exception:
                pass
        await pwd.fill(ORA_PASSWORD)
        await try_click(page,
            'button:has-text("Sign In")', 'button:has-text("Sign in")',
            'oj-button:has-text("Sign In")', 'button[type="submit"]',
            'button:has-text("Log In")', timeout=6000)
        await page.wait_for_timeout(2500)
        return "ok"

    # Registration / verify-identity path: enter email → request code.
    if has_email:
        await email_field.fill(INFO.get("email", ""))
        await page.wait_for_timeout(400)
        await try_click(page,
            'button:has-text("Verify")', 'button:has-text("Send")',
            'button:has-text("Continue")', 'button:has-text("Next")',
            'oj-button:has-text("Continue")', 'button[type="submit"]',
            timeout=6000)
        await page.wait_for_timeout(2500)

        # A code field should now be present. Poll IMAP for the 6-digit code.
        code = None
        for attempt in range(6):  # ~60s total
            code = fetch_verification_code()
            if code:
                break
            await page.wait_for_timeout(10000)
        if not code:
            send_verification_email(company, title, link)
            return "needs_review:no_verification_code"

        await _enter_code(page, code)
        await page.wait_for_timeout(2000)
        # Some tenants then ask to set a password for the new account.
        await _maybe_set_password(page)
        return "ok"

    # No identity fields at all → assume already authenticated (session cookie).
    return "ok"

async def _enter_code(page: Page, code: str) -> None:
    """Type a 6-digit verification code into either a single input or six boxes."""
    # Single combined field.
    single = page.locator(
        'input[autocomplete="one-time-code"]:visible, '
        'input[name*="code" i]:visible, input[id*="code" i]:visible, '
        'input[aria-label*="code" i]:visible'
    ).first
    if await single.count() > 0 and await single.is_visible(timeout=1500):
        try:
            await single.fill(code)
            await try_click(page,
                'button:has-text("Verify")', 'button:has-text("Submit")',
                'button:has-text("Continue")', 'button[type="submit"]', timeout=5000)
            return
        except Exception:
            pass
    # Six separate single-character boxes.
    boxes = page.locator('input[maxlength="1"]:visible')
    n = await boxes.count()
    if n >= len(code):
        for i, ch in enumerate(code):
            try:
                await boxes.nth(i).fill(ch)
            except Exception:
                pass
        await try_click(page,
            'button:has-text("Verify")', 'button:has-text("Submit")',
            'button:has-text("Continue")', 'button[type="submit"]', timeout=5000)

async def _maybe_set_password(page: Page) -> None:
    """If a 'create password' form appears for the new account, fill it."""
    try:
        pwds = page.locator('input[type="password"]:visible')
        n = await pwds.count()
        if n == 0:
            return
        for i in range(min(n, 2)):
            try:
                await pwds.nth(i).fill(ORA_PASSWORD)
            except Exception:
                pass
        await try_click(page,
            'button:has-text("Save")', 'button:has-text("Create")',
            'button:has-text("Continue")', 'button:has-text("Submit")',
            'button[type="submit"]', timeout=5000)
        await page.wait_for_timeout(2000)
    except Exception:
        pass

async def upload_resume(page: Page) -> bool:
    """Attach the resume PDF if a file input is present and nothing is attached yet."""
    if not RESUME_PATH or not Path(RESUME_PATH).exists():
        return False
    try:
        # Already attached? (Oracle shows the filename / a "Remove" control.)
        body = (await page.evaluate("document.body.innerText")).lower()
        rname = Path(RESUME_PATH).name.lower()
        if rname in body or "resume.pdf" in body:
            return True
        fi = page.locator('input[type="file"]').first
        if await fi.count() == 0:
            return False
        await fi.set_input_files(RESUME_PATH)
        await page.wait_for_timeout(3000)  # ORC parses the resume to auto-populate
        return True
    except Exception as e:
        print(f"  [!] resume upload failed: {str(e)[:60]}")
        return False

async def fill_personal_info(page: Page) -> None:
    """Fill the basic contact fields on the Personal Information section."""
    info = INFO
    await _fill_text_verified(page, info.get("first_name", ""),
        ['input[name*="first" i][name*="name" i]', 'input[aria-label*="First Name" i]',
         'input[id*="FirstName" i]'])
    await _fill_text_verified(page, info.get("last_name", ""),
        ['input[name*="last" i][name*="name" i]', 'input[aria-label*="Last Name" i]',
         'input[id*="LastName" i]'])
    await _fill_text_verified(page, info.get("email", ""),
        ['input[type="email"]', 'input[aria-label*="Email" i]', 'input[id*="Email" i]'])
    await _fill_text_verified(page, info.get("phone", ""),
        ['input[type="tel"]', 'input[aria-label*="Phone" i]', 'input[id*="Phone" i]'])
    await _fill_text_verified(page, info.get("address_line1", ""),
        ['input[aria-label*="Address" i]', 'input[id*="Address" i]', 'input[name*="address" i]'])
    await _fill_text_verified(page, info.get("city", ""),
        ['input[aria-label*="City" i]', 'input[id*="City" i]', 'input[name*="city" i]'])
    await _fill_text_verified(page, info.get("zip", ""),
        ['input[aria-label*="Postal" i]', 'input[aria-label*="Zip" i]', 'input[id*="Postal" i]'])

async def _select_oracle_option(page: Page, want: str) -> bool:
    """After an oj-select dropdown is opened, click the option matching `want`."""
    await page.wait_for_timeout(400)
    # Oracle often renders a filter box inside the dropdown.
    filt = page.locator('.oj-listbox-search input:visible, input[role="combobox"]:visible').first
    if await filt.count() > 0 and await filt.is_visible(timeout=800):
        try:
            await filt.fill(want[:20])
            await page.wait_for_timeout(600)
        except Exception:
            pass
    for sel in (
        f'[role="option"]:text-is("{want}")',
        f'.oj-listbox-result-label:text-is("{want}")',
        f'[role="option"]:has-text("{want}")',
        f'li:has-text("{want}")',
    ):
        opt = page.locator(sel).first
        if await opt.count() > 0 and await opt.is_visible(timeout=1200):
            await opt.click()
            await page.wait_for_timeout(300)
            return True
    return False

async def _dropdown_options(page: Page) -> list[str]:
    try:
        return await page.evaluate(
            """() => Array.from(document.querySelectorAll('[role="option"], .oj-listbox-result-label'))
                    .map(e => (e.innerText||'').trim()).filter(Boolean)"""
        )
    except Exception:
        return []

async def fill_oracle_dropdowns(page: Page, answers: dict, unknowns: list[str]) -> None:
    """Answer every visible single-select dropdown using the profile/answer rules."""
    triggers = page.locator(
        'oj-select-single, oj-c-select-single, [role="combobox"], '
        'div.oj-select-choice, select'
    )
    n = await triggers.count()
    for i in range(n):
        trig = triggers.nth(i)
        try:
            if not await trig.is_visible(timeout=500):
                continue
            # Skip if it already shows a chosen value.
            cur = (await trig.inner_text()).strip().lower()
            if cur and cur not in ("select a value", "select...", "select one", "choose...", ""):
                continue
            label = await _label_for(page, trig)
            if not label:
                continue
            ans = _answer_from_profile(label, answers)
            if ans is None:
                unknowns.append(label)
                continue
            if ans == "":
                continue

            tag = await trig.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                opts = await trig.evaluate(
                    "el => Array.from(el.options).map(o => o.label || o.text)")
                want = ans
                if ans.startswith("__"):
                    want = _resolve_special(ans, opts) or ""
                if want:
                    try:
                        await trig.select_option(label=want)
                    except Exception:
                        await trig.select_option(value=want)
                continue

            # Custom oj-select: open it, then resolve a special token against the
            # rendered options if needed.
            await trig.click()
            await page.wait_for_timeout(500)
            want = ans
            if ans.startswith("__"):
                want = _resolve_special(ans, await _dropdown_options(page)) or ""
            if want and not await _select_oracle_option(page, want):
                await page.keyboard.press("Escape")
        except Exception:
            continue

async def fill_oracle_radios(page: Page, answers: dict) -> None:
    """Answer oj-radioset / role=radiogroup questions (Yes/No style)."""
    groups = page.locator('oj-radioset, [role="radiogroup"]')
    n = await groups.count()
    for i in range(n):
        grp = groups.nth(i)
        try:
            if not await grp.is_visible(timeout=500):
                continue
            # Skip if a radio is already selected in this group.
            checked = await grp.locator('[aria-checked="true"], input:checked').count()
            if checked:
                continue
            label = await _label_for(page, grp)
            ans = _answer_from_profile(label, answers) if label else None
            if not ans or ans.startswith("__"):
                # Default Yes/No groups conservatively to "No" only when we truly
                # can't tell? No — skip unknowns so they surface as needs_review.
                continue
            radio = grp.locator(
                f'label:has-text("{ans}"), [role="radio"]:has-text("{ans}"), '
                f'oj-option:has-text("{ans}")'
            ).first
            if await radio.count() > 0 and await radio.is_visible(timeout=1000):
                await radio.click()
                await page.wait_for_timeout(200)
        except Exception:
            continue

async def fill_oracle_text_questions(page: Page, answers: dict, unknowns: list[str]) -> None:
    """Fill required free-text inputs/textareas that are still empty."""
    fields = page.locator(
        'oj-input-text input:visible, oj-text-area textarea:visible, '
        'input[type="text"]:visible, textarea:visible'
    )
    n = await fields.count()
    for i in range(n):
        fld = fields.nth(i)
        try:
            if not await fld.is_visible(timeout=400):
                continue
            if (await fld.input_value()).strip():
                continue
            label = await _label_for(page, fld)
            if not label:
                continue
            ans = _answer_from_profile(label, answers)
            if ans is None:
                # Only flag as unknown if the field is required.
                try:
                    required = await fld.evaluate(
                        "el => !!(el.required || el.getAttribute('aria-required') === 'true' "
                        "|| el.closest('[required]') || el.closest('[aria-required=\"true\"]'))")
                except Exception:
                    required = False
                if required:
                    unknowns.append(label)
                continue
            if ans and not ans.startswith("__"):
                try:
                    await fld.fill(ans)
                except Exception:
                    pass
        except Exception:
            continue

async def tick_required_checkboxes(page: Page) -> None:
    """Tick required consent / acknowledgement checkboxes (privacy, T&C, eSignature)."""
    boxes = page.locator(
        'oj-checkboxset input[type="checkbox"]:visible, '
        'input[type="checkbox"][aria-required="true"]:visible, '
        'input[type="checkbox"][required]:visible'
    )
    n = await boxes.count()
    for i in range(n):
        cb = boxes.nth(i)
        try:
            if not await cb.is_visible(timeout=400):
                continue
            if await cb.is_checked():
                continue
            label = (await _label_for(page, cb)).lower()
            # Only tick affirmative consent boxes, never an opt-out / "do not".
            if re.search(r"do not|opt out|unsubscribe", label):
                continue
            await cb.click(timeout=3000)
            await page.wait_for_timeout(150)
        except Exception:
            continue

async def click_continue(page: Page) -> bool:
    """Advance to the next section of the Oracle apply flow."""
    return await try_click(page,
        '[data-qa="continueButton"]',
        'oj-button:has-text("Continue")',
        'button:has-text("Continue")',
        'button:has-text("Save and Continue")',
        'button:has-text("Next")',
        'button:has-text("Save")',
        '[role="button"]:has-text("Continue")',
        timeout=6000,
    )

async def find_submit(page: Page) -> bool:
    return await try_click(page,
        '[data-qa="submitButton"]',
        'oj-button:has-text("Submit")',
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
        '[role="button"]:has-text("Submit")',
        timeout=5000,
    )

SUCCESS_PHRASES = (
    "application submitted", "successfully submitted", "thank you for applying",
    "thank you for your application", "we have received your application",
    "application received", "your application has been submitted", "congratulations",
)
ASSESSMENT_PHRASES = ("take assessment", "complete the assessment", "assessment required")

# ── Apply ────────────────────────────────────────────────────────────────────────

async def apply_to_job(page: Page, job: dict, answers: dict) -> tuple[str, str]:
    """Drive one Oracle Candidate-Experience application end-to-end.
    Returns (status, notes) where status is applied / needs_review / error / skipped.
    """
    title   = job.get("title", "")
    company = job.get("company", "")
    link    = job.get("link", "")
    unknowns: list[str] = []

    await page.goto(link, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2000)
    await _shot(page, "job_page")

    if not await click_apply(page):
        return "needs_review", "no_apply_button"
    await page.wait_for_timeout(2500)
    await _shot(page, "after_apply")

    # Identity / sign-in / register + 6-digit code.
    ident = await handle_identity(page, company, title, link)
    if ident.startswith("needs_review"):
        return "needs_review", ident.split(":", 1)[1] if ":" in ident else ident
    await page.wait_for_timeout(2000)
    await _shot(page, "after_identity")

    # Some tenants gate the flow behind a data-privacy consent first.
    await tick_required_checkboxes(page)
    await dismiss_cookie_banner(page)

    # Walk the configurable section sequence. Each loop: fill what we recognise on
    # the current section, then Continue. Bail if we can't advance twice in a row.
    last_sig = ""
    stuck = 0
    MAX_SECTIONS = 14
    for section in range(MAX_SECTIONS):
        await page.wait_for_timeout(1200)
        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        await _shot(page, f"section_{section}")

        body = ""
        try:
            body = (await page.evaluate("document.body.innerText")).lower()
        except Exception:
            pass

        if any(p in body for p in SUCCESS_PHRASES):
            return "applied", "; ".join(dict.fromkeys(unknowns))

        if any(p in body for p in ASSESSMENT_PHRASES):
            return "needs_review", "assessment required — complete manually"

        # Fill recognised controls on this section.
        await upload_resume(page)
        await fill_personal_info(page)
        await fill_oracle_dropdowns(page, answers, unknowns)
        await fill_oracle_radios(page, answers)
        await fill_oracle_text_questions(page, answers, unknowns)
        await tick_required_checkboxes(page)

        # Try to submit (last section) before/after Continue.
        if await find_submit(page):
            await page.wait_for_timeout(3500)
            try:
                body = (await page.evaluate("document.body.innerText")).lower()
            except Exception:
                pass
            if any(p in body for p in SUCCESS_PHRASES):
                return "applied", "; ".join(dict.fromkeys(unknowns))

        advanced = await click_continue(page)
        await page.wait_for_timeout(2000)

        # Detect "stuck" — same content signature and no advance.
        try:
            sig = (await page.evaluate("document.body.innerText"))[:400]
        except Exception:
            sig = ""
        if not advanced and sig == last_sig:
            stuck += 1
            if stuck >= 2:
                note = "stuck"
                if unknowns:
                    note = "unknown_questions: " + "; ".join(dict.fromkeys(unknowns))[:160]
                return "needs_review", note
        else:
            stuck = 0
        last_sig = sig

    return "needs_review", "too_many_sections"

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
        f"[Oracle] Auto-Apply: {n_applied} applied | {n_review} needs review "
        f"— {datetime.now().strftime('%b %d, %Y %H:%M')}"
    )
    body_html = f"""
    <html><body style="font-family:sans-serif;color:#333;font-size:13px">
    <h2>Oracle Auto-Apply Run</h2>
    <p>
      <b style="color:#155724">Applied: {n_applied}</b> &nbsp;|&nbsp;
      <b style="color:#856404">Needs Review: {n_review}</b> &nbsp;|&nbsp;
      Total: {len(results)}
    </p>
    <p style="color:#666;font-size:12px">
      For <b>needs_review</b> jobs: check the Notes column — if it mentions an unknown question,
      add the answer to <code>json/oracle_answers.json</code> and rerun.
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

def send_verification_email(company: str, title: str, job_url: str = "") -> None:
    """Notify the user that an Oracle account needs a verification code we couldn't
    auto-read (IMAP unset or code not found)."""
    to = EMAIL_TO or "lopes.o@northeastern.edu"
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        print(f"  [!] Verification email not sent (EMAIL creds unset). Verify manually.")
        return
    try:
        recipients = [a.strip() for a in to.split(",") if a.strip()]
        body = f"""<html><body style="font-family:sans-serif">
        <h3>Oracle account verification needed</h3>
        <p>The auto-apply bot is applying to <b>{title}</b> at <b>{company}</b> but the
        Oracle candidate account needs a 6-digit verification code that could not be read
        automatically.</p>
        <p>Check the inbox for <b>{VERIFY_IMAP_USER or INFO.get('email','')}</b> for the code.</p>
        <p><a href="{job_url}">{job_url}</a></p>
        </body></html>"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Oracle] Verify account to apply: {title} @ {company}"
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
        print("[!] No applicant info found. Set ORACLE_APPLICANT_INFO (or WD_APPLICANT_INFO).")
        return
    if not ORA_PASSWORD:
        print("[!] ORACLE_PASSWORD not set.")
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
    if session_state and CONCURRENCY == 1:
        ctx_kwargs["storage_state"] = session_state

    lock = asyncio.Lock()
    sem = asyncio.Semaphore(CONCURRENCY)
    total = len(queue)

    async with async_playwright() as pw:

        async def run_one(i: int, job: dict) -> None:
            title   = job.get("title", "Unknown")
            company = job.get("company", "Unknown")
            link    = job.get("link", "")
            async with sem:
                print(f"\n[{i+1}/{total}] {title} @ {company}")
                browser = None
                try:
                    browser = await pw.chromium.launch(
                        headless=HEADLESS,
                        args=["--no-sandbox", "--disable-dev-shm-usage"] if HEADLESS else [],
                        slow_mo=0 if HEADLESS else 60,
                    )
                    context = await browser.new_context(**ctx_kwargs)
                    page = await context.new_page()
                    page.set_default_timeout(8000)
                    status, notes = await apply_to_job(page, job, answers)
                    # Persist refreshed session cookies (serial mode only).
                    if CONCURRENCY == 1 and status == "applied":
                        try:
                            state = await context.storage_state()
                            SESSION_FILE.parent.mkdir(exist_ok=True)
                            SESSION_FILE.write_text(json.dumps(state))
                        except Exception:
                            pass
                except PlaywrightTimeout as e:
                    status, notes = "error", f"timeout:{str(e)[:60]}"
                except Exception as e:
                    status, notes = "error", f"{type(e).__name__}:{str(e)[:60]}"
                finally:
                    if browser is not None:
                        try:
                            await browser.close()
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
                    if status in ("applied", "already_applied") or "assessment" in notes:
                        applied_ids.add(link)
                        save_applied(applied_ids)
                    results.append(row)
                    append_csv(row)
                    save_answers(answers)

        await asyncio.gather(
            *(run_one(i, job) for i, job in enumerate(queue)),
            return_exceptions=True,
        )

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
