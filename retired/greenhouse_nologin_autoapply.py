"""
RETIRED — 2026-05-16
====================
Retired by choice — owner prefers to review and apply to Greenhouse
no-login jobs manually rather than auto-submitting. May be re-activated
in the future if automated applying is reconsidered. The workflow
(greenhouse_nologin_autoapply.yml) has been removed.

Original script preserved here for reference only. Do not re-activate
without reviewing the applicant info, resume path, and role patterns.

────────────────────────────────────────────────────────────────────────────────

greenhouse_nologin_autoapply.py
--------------------------------
Reads jobs from csv/greenhouse_nologin_jobs.csv, filters to roles matching
greenhouse_nologin_roles.json posted within the last 24 hours, and
auto-applies using Playwright. No Greenhouse login required — application
forms on job-boards.greenhouse.io are public.
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
    raise EnvironmentError(
        "APPLICANT_INFO_JSON is not set.\n"
        "  Locally: add it to your .env file\n"
        "  GitHub Actions: add it as a repository secret"
    )
YOUR_INFO = json.loads(_applicant_json)
if os.environ.get("RESUME_PATH"):
    YOUR_INFO["resume_path"] = os.environ["RESUME_PATH"]

# ── Email config ───────────────────────────────────────────────────────────────
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
CSV_FILE     = BASE_DIR / "csv"  / "greenhouse_nologin_jobs.csv"
APPLIED_LOG  = BASE_DIR / "json" / "greenhouse_nologin_applied_ids.json"
OUTPUT_CSV   = BASE_DIR / "csv"  / "greenhouse_nologin_applied.csv"
ROLES_FILE   = BASE_DIR / "greenhouse_nologin_roles.json"

PAGE_TIMEOUT    = 60_000
DELAY_BETWEEN   = 5


# ── Roles config ───────────────────────────────────────────────────────────────

def load_role_patterns() -> list[re.Pattern]:
    cfg = json.loads(ROLES_FILE.read_text(encoding="utf-8"))
    return [re.compile(p, re.I) for p in cfg["autoapply_patterns"]]


# ── Applied-IDs state ──────────────────────────────────────────────────────────

def load_applied_ids() -> dict:
    if APPLIED_LOG.exists():
        data = json.loads(APPLIED_LOG.read_text())
        if isinstance(data, list):
            return {jid: {} for jid in data}
        return data
    return {}


def save_applied_ids(ids: dict) -> None:
    APPLIED_LOG.parent.mkdir(parents=True, exist_ok=True)
    APPLIED_LOG.write_text(json.dumps(ids, indent=2))


# ── CSV output ─────────────────────────────────────────────────────────────────

def append_csv_results(rows: list[dict]) -> None:
    if not rows:
        return
    fieldnames = ["job_id", "title", "company", "location", "url", "role", "posted", "status", "note", "applied_at"]
    write_header = not OUTPUT_CSV.exists() or OUTPUT_CSV.stat().st_size == 0
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for r in rows:
            writer.writerow(r)


# ── Job loading ────────────────────────────────────────────────────────────────

def load_pending_jobs(patterns: list[re.Pattern], applied: dict) -> list[dict]:
    if not CSV_FILE.exists():
        print(f"[!] CSV not found: {CSV_FILE}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    seen_ids: set[str] = set()
    pending: list[dict] = []

    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            job_id = row.get("job_id", "").strip()
            if not job_id or job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            if job_id in applied:
                continue

            title = row.get("title", "")
            if not any(p.search(title) for p in patterns):
                continue

            posted = row.get("posted", "")
            try:
                dt = datetime.fromisoformat(posted.replace("Z", "+00:00")).astimezone(timezone.utc)
                if dt < cutoff:
                    continue
            except Exception:
                continue

            pending.append(row)

    return pending


# ── Playwright form helpers ────────────────────────────────────────────────────

async def safe_fill(page: Page, selector: str, value: str) -> bool:
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


async def type_react_select(page: Page, label_pattern: str, text: str) -> bool:
    if not text:
        return False
    try:
        els = page.get_by_label(re.compile(label_pattern, re.IGNORECASE))
        for i in range(await els.count()):
            el = els.nth(i)
            ctrl = el.locator('xpath=ancestor::div[contains(@class,"select__control")][1]')
            if await ctrl.count() == 0:
                continue
            if await ctrl.locator('[class*="select__single-value"]').count() > 0:
                continue
            await ctrl.scroll_into_view_if_needed(timeout=2000)
            await ctrl.click(timeout=3000)
            await page.wait_for_timeout(400)
            await page.keyboard.type(text, delay=40)
            await page.wait_for_timeout(1000)
            opts = page.locator('[class*="select__option"]')
            count = await opts.count()
            for j in range(count):
                if text.lower() in (await opts.nth(j).inner_text()).strip().lower():
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
        const controls = Array.from(document.querySelectorAll('[class*="select__control"]'))
            .filter(c => !c.querySelector('[class*="select__single-value"]'));
        for (const ctrl of controls) {{
            let node = ctrl;
            for (let i = 0; i < 8; i++) {{
                node = node.parentElement;
                if (!node) break;
                const label = node.querySelector('label,legend,p,[class*="question"]');
                if (label && pattern.test(label.innerText)) return tryClick(ctrl);
            }}
        }}
        return false;
    }}"""
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def click_no_radio(page: Page, label_pattern: str) -> bool:
    js = f"""() => {{
        const pat = new RegExp({repr(label_pattern)}, 'i');
        const containers = Array.from(document.querySelectorAll('div,fieldset,li,section,p'));
        for (const el of containers) {{
            const lbl = el.querySelector('label,legend,p,h3,h4,span[class*="label"],div[class*="label"],div[class*="question"]');
            if (!lbl || !pat.test(lbl.innerText) || lbl.innerText.length > 600) continue;
            const radios = Array.from(el.querySelectorAll('input[type="radio"]'));
            if (!radios.length) continue;
            const no = radios.find(r => {{
                const val = (r.value || '').trim().toLowerCase();
                const wrap = r.closest('label') || r.parentElement || {{}};
                const txt = (wrap.innerText || '').trim().toLowerCase();
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


async def click_yes_radio(page: Page, label_pattern: str) -> bool:
    js = f"""() => {{
        const pat = new RegExp({repr(label_pattern)}, 'i');
        const containers = Array.from(document.querySelectorAll('div,fieldset,li,section,p'));
        for (const el of containers) {{
            const lbl = el.querySelector('label,legend,p,h3,h4,span[class*="label"],div[class*="label"],div[class*="question"]');
            if (!lbl || !pat.test(lbl.innerText) || lbl.innerText.length > 600) continue;
            const radios = Array.from(el.querySelectorAll('input[type="radio"]'));
            if (!radios.length) continue;
            const yes = radios.find(r => {{
                const val = (r.value || '').trim().toLowerCase();
                const wrap = r.closest('label') || r.parentElement || {{}};
                const txt = (wrap.innerText || '').trim().toLowerCase();
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


async def fill_greenhouse_selects(page: Page) -> list:
    """Handle Greenhouse React-Select custom questions with pre-defined answers."""
    rules = [
        (r"referred.*current employee|current employee.*refer",                    "no"),
        (r"referred.*someone.*works|were you referred.*enterprise|referred.*works at", "no"),
        (r"legally authorized.*work|confirm.*authorized.*work|authorized.*us.*canada", "yes"),
        (r"hold.*visa.*type|what type.*visa|visa.*type",                           "none"),
        (r"california.*privacy notice|applicant privacy notice",                   "continue"),
        (r"discharged.*resign|asked to resign|terminated",                         "no"),
        (r"require.*employment visa sponsorship|require.*visa sponsorship",         "no"),
        (r"does not provide visa sponsorship.*do you acknowledge|permanent work auth.*eligible", "yes"),
        (r"currently hold.*temporary work auth|hold.*cpt.*opt",                    "no"),
        (r"sponsor",                                                                "no"),
        (r"non.compete|restrictive covenant",                                       "no"),
        (r"relative.*employed|employ.*family",                                      "no"),
        (r"previously.*employed.*company|have you worked for",                      "no"),
        (r"ever.*interned.*employed.*applied|interned.*employed.*applied",          "no"),
        (r"willing to relocate|open to relocate",                                   "yes"),
        (r"authorized.*work.*united states|legally.*work.*us",                      "yes"),
        (r"salary requirements|what are your salary",                               "70"),
        (r"opt in.*text mess|text mess.*opt|sms.*opt",                             "no"),
        (r"please review.*accept.*terms|accept.*terms.*application",               "i certify"),
        (r"describe.*gender.{0,20}identit|i identify my gender",                   "decline"),
        (r"describe.*racial|which ethnicities|ethnic.*background",                  "decline"),
        (r"have.*disability|identify.*disability",                                  "decline"),
        (r"military veteran|veteran.*service member|identify.*veteran",             "decline"),
        (r"sexual orient|lgbtq",                                                    "decline"),
        (r"willing.*drug test|drug test.*law",                                      "yes"),
        (r"have.*worked.*data.*pipeline|data.*pipeline.*etl",                       "yes"),
        (r"level.*sql.*experience|sql.*experience.*level",                          "advanced"),
        (r"expertise.*level.*sql|sql.*expertise",                                   "advanced"),
        (r"experience.*azure.*2\+|years.*azure",                                    "yes"),
        (r"2\+.*years.*data engineer|years.*data engineer",                         "yes"),
        (r"consent.*receive.*sms|sms.*message.*recruiting",                         "no"),
        (r"job applicant.*data.*privacy|data privacy.*notice",                      "continue"),
    ]

    answer_fallbacks = {
        "yes":      ["yes", "i verify", "i confirm", "i agree", "i certify", "authorized to work", "eligible"],
        "no":       ["no", "i am not", "not authorized", "i do not", "never", "none of the above"],
        "none":     ["none", "do not hold", "i do not hold", "no visa", "n/a", "not applicable"],
        "continue": ["continue", "i have read", "acknowledge", "i certify", "i accept", "i agree"],
        "decline":  ["decline", "prefer not", "choose not", "i don't wish", "i prefer not"],
        "70":       ["70", "60", "80", "$60", "$70", "$80", "70,000", "60,000"],
        "i certify": ["i certify", "i verify", "i confirm", "i agree", "i accept", "foregoing applicant"],
        "advanced": ["advanced", "expert", "proficient", "high", "experienced"],
    }

    controls_info = await page.evaluate("""() => {
        const controls = Array.from(document.querySelectorAll('[class*="select__control"]'))
            .filter(c => c.querySelector('[class*="select__placeholder"]') !== null);
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

    rules_compiled = [(re.compile(pat, re.IGNORECASE), val) for pat, val in rules]
    filled = []

    for item in (controls_info or []):
        label_text = item.get("label", "")
        input_id   = item.get("inputId", "")
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

        if input_id:
            control = page.locator(f'div[class*="select__control"]:has(input#{input_id})').first
        else:
            control = None

        clicked = False
        if control and await control.count() > 0:
            try:
                if await control.locator('[class*="select__single-value"]').count() > 0:
                    continue
                await control.scroll_into_view_if_needed(timeout=2000)
                await control.click(timeout=3000)
                await page.wait_for_timeout(900)

                opts = page.locator('[class*="select__option"]')
                all_opt_texts = [
                    (await opts.nth(j).inner_text()).strip()
                    for j in range(await opts.count())
                ]

                candidates = [answer.lower()] + [
                    p for p in answer_fallbacks.get(answer.lower(), []) if p != answer.lower()
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

                if not clicked and all_opt_texts:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(400)
            except Exception as e:
                print(f"    [gh-err] {label_text[:40]!r}: {e}")

        if not clicked and input_id:
            ans_lower = answer.lower()
            fallback_phrases = answer_fallbacks.get(ans_lower, [ans_lower])
            if ans_lower not in fallback_phrases:
                fallback_phrases = [ans_lower] + fallback_phrases
            phrases_js = json.dumps(fallback_phrases)
            js_fallback = f"""async () => {{
                const inp = document.getElementById({repr(input_id)});
                if (!inp) return false;
                let ctrl = inp;
                for (let i = 0; i < 10; i++) {{
                    ctrl = ctrl.parentElement;
                    if (!ctrl) return false;
                    if ((ctrl.className||'').includes('select__control')) break;
                }}
                if (ctrl.querySelector('[class*="select__single-value"]')) return false;
                ctrl.dispatchEvent(new MouseEvent('mousedown',{{bubbles:true,cancelable:true,view:window}}));
                ctrl.click();
                await new Promise(r=>setTimeout(r,1200));
                const opts = Array.from(document.querySelectorAll('[class*="select__option"]'));
                for (const phrase of {phrases_js}) {{
                    const opt = opts.find(o=>o.innerText.trim().toLowerCase().includes(phrase));
                    if (opt) {{ opt.click(); await new Promise(r=>setTimeout(r,400)); return true; }}
                }}
                document.body.dispatchEvent(new MouseEvent('mousedown',{{bubbles:true}}));
                return false;
            }}"""
            try:
                if await page.evaluate(js_fallback):
                    filled.append(f"{label_text[:60]} → {answer} [js]")
                    await page.wait_for_timeout(300)
            except Exception:
                pass

    return filled


async def navigate_to_application_form(page: Page, job: dict) -> None:
    await page.goto(job["apply_url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    await page.wait_for_timeout(2000)

    if await page.locator('button[type="submit"], input[type="submit"]').count() > 0:
        return

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

    for _ in range(8):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(300)
        if await page.locator('button[type="submit"], input[type="submit"]').count() > 0:
            return


async def fill_application(page: Page, info: dict) -> tuple[bool, str]:
    """Fill and submit a Greenhouse application form. Returns (success, note)."""
    for _ in range(8):
        await page.evaluate("window.scrollBy(0, 400)")
        await page.wait_for_timeout(200)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)

    await safe_fill(page, 'input[id*="first_name"], input[name*="first_name"], input[autocomplete="given-name"]',  info["first_name"])
    await safe_fill(page, 'input[id*="last_name"],  input[name*="last_name"],  input[autocomplete="family-name"]', info["last_name"])
    await safe_fill(page, 'input[id*="email"],      input[name*="email"],      input[type="email"]',               info["email"])
    await safe_fill(page, 'input[id*="phone"],      input[name*="phone"],      input[type="tel"]',                 info["phone"])

    await fill_by_label(page, ["preferred", "first"], info["first_name"])
    await fill_by_label(page, ["preferred name"],     info["first_name"])
    await fill_by_label(page, ["full legal name"],    f"{info['first_name']} {info['last_name']}")
    await fill_by_label(page, ["your location"],      f"{info.get('city', '')}, {info.get('state', '')}")

    await type_react_select(page, r"country", info.get("country", "United States"))
    await click_react_select_by_text(page, r"^country", "United States")
    await type_react_select(page, r"location.*city|city.*location|^location$",
                            f"{info.get('city', '')}, {info.get('state', '')}")

    for sel in ['input[id*="address"], input[name*="address"]',
                'input[placeholder*="address" i]', 'input[placeholder*="street" i]']:
        await safe_fill(page, sel, info.get("address", ""))
    await fill_by_label(page, ["street address"],  info.get("address", ""))
    await fill_by_label(page, ["home address"],    info.get("address", ""))
    await fill_by_label(page, ["address 1"],       info.get("address", ""))
    await fill_by_label(page, ["address line 1"],  info.get("address", ""))
    await safe_fill(page, 'input[id*="state"], input[name*="state"]', info.get("state", ""))
    await safe_fill(page, 'input[id*="zip"], input[id*="postal"]',    info.get("zip", ""))
    await fill_by_label(page, ["city"],            info.get("city", ""))
    await fill_by_label(page, ["notice period"],   "0")

    if info.get("resume_path") and Path(info["resume_path"]).exists():
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

    await safe_fill(page, 'input[id*="linkedin"], input[name*="linkedin"], input[placeholder*="LinkedIn" i]', info.get("linkedin_url", ""))
    await fill_by_label(page, ["linkedin"], info.get("linkedin_url", ""))
    await safe_fill(page, 'input[id*="website"], input[name*="website"]', info.get("website", ""))

    if info.get("cover_letter"):
        await safe_fill(page, 'textarea[id*="cover"], textarea[name*="cover"]', info["cover_letter"])

    await click_yes_radio(page, r"legally authorized.*work|authorized.*work.*us")
    await click_no_radio(page,  r"require.*visa sponsorship|require.*sponsorship")
    await click_no_radio(page,  r"currently hold.*temporary|cpt|opt")

    await fill_greenhouse_selects(page)

    submit = page.locator('button[type="submit"], input[type="submit"]').first
    if await submit.count() == 0:
        return False, "no submit button found"

    try:
        await submit.scroll_into_view_if_needed(timeout=3000)
        await submit.click(timeout=10_000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        return False, f"submit click failed: {e}"

    page_text = (await page.evaluate("document.body.innerText")).lower()
    success_phrases = ["application submitted", "thank you", "we've received", "successfully submitted",
                       "your application", "application received", "thanks for applying"]
    if any(ph in page_text for ph in success_phrases):
        return True, ""

    error_phrases = ["please fix", "required field", "error", "invalid"]
    if any(ph in page_text for ph in error_phrases):
        return False, "form validation error after submit"

    return True, "no confirmation page detected"


# ── Email summary ──────────────────────────────────────────────────────────────

def send_summary_email(results: list[dict]) -> None:
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set — skipping email.")
        return
    if not results:
        print("[i] No results to email.")
        return

    n_applied = sum(1 for r in results if r["status"] == "applied")
    n_failed  = sum(1 for r in results if r["status"] in ("failed", "error"))
    n_skip    = sum(1 for r in results if r["status"] == "skipped")

    status_color = {
        "applied": "#d4edda",
        "failed":  "#f8d7da",
        "error":   "#f8d7da",
        "skipped": "#f5f5f5",
    }

    rows = ""
    for r in results:
        bg    = status_color.get(r["status"], "#fff")
        note  = r.get("note", "") or ""
        rows += (
            f"<tr style='background:{bg}'>"
            f"<td>{r.get('title','')}</td>"
            f"<td>{r.get('company','')}</td>"
            f"<td>{r.get('role','')}</td>"
            f"<td>{r['status'].upper()}</td>"
            f"<td>{note}</td>"
            f"<td><a href='{r.get('url','')}'>Link</a></td>"
            f"</tr>"
        )

    body = f"""
    <h2>Greenhouse No-Login Auto-Apply Summary</h2>
    <p>
      <b style="color:#155724">Applied: {n_applied}</b> &nbsp;|&nbsp;
      <b style="color:#721c24">Failed/Error: {n_failed}</b> &nbsp;|&nbsp;
      <b>Skipped: {n_skip}</b> &nbsp;|&nbsp;
      <b>Total: {len(results)}</b>
    </p>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Role</th><th>Status</th><th>Note</th><th>Link</th>
      </tr>
      {rows}
    </table>
    """

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"GH No-Login Apply: {n_applied} applied | {n_failed} failed | {len(results)} total"
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
    patterns = load_role_patterns()
    applied  = load_applied_ids()

    pending = load_pending_jobs(patterns, applied)
    print(f"[i] {len(pending)} eligible job(s) (DA/DE, posted <24h, not yet applied)")

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
            print(f"\n[>] {title} @ {company}")

            result = {
                "job_id":  job["job_id"],
                "title":   title,
                "company": company,
                "role":    job.get("role", ""),
                "location":job.get("location", ""),
                "url":     job["url"],
                "posted":  job.get("posted", ""),
                "status":  "error",
                "note":    "",
                "applied_at": "",
            }

            page = await context.new_page()
            try:
                await navigate_to_application_form(page, {"apply_url": job["url"]})
                success, note = await fill_application(page, YOUR_INFO)
                if success:
                    result["status"]     = "applied"
                    result["applied_at"] = datetime.now(timezone.utc).isoformat()
                    applied[job["job_id"]] = {
                        "title":      title,
                        "company":    company,
                        "applied_at": result["applied_at"],
                    }
                    print(f"    [+] applied")
                else:
                    result["status"] = "failed"
                    result["note"]   = note
                    print(f"    [-] failed: {note}")
            except Exception as e:
                result["note"] = str(e)[:120]
                print(f"    [!] error: {e}")
            finally:
                await page.close()

            results.append(result)
            await asyncio.sleep(DELAY_BETWEEN)

        await browser.close()

    save_applied_ids(applied)
    append_csv_results(results)
    send_summary_email(results)

    n_ok  = sum(1 for r in results if r["status"] == "applied")
    n_err = sum(1 for r in results if r["status"] != "applied")
    print(f"\n[i] Done — {n_ok} applied, {n_err} failed/error")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
