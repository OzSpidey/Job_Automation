"""
linkedin_job_applier.py

LinkedIn Jobs scraper + Easy-Apply bot.
- Logs in via li_at session cookie
- Searches configured roles filtered to last 2 hours (f_TPR=r7200)
- Scrapes up to MAX_PAGES pages per role
- Easy Apply: auto-fills and submits the application
- External Apply: captures company, link, and posting time
- Sends HTML summary email matching the Greenhouse format
- Logs all results to linkedin_jobs_applied.csv
"""

import csv
import json
import os
import re
import smtplib
import subprocess
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Applicant info ─────────────────────────────────────────────────────────────
_applicant_json = os.environ.get("APPLICANT_INFO_JSON", "")
if _applicant_json:
    YOUR_INFO = json.loads(_applicant_json)
else:
    _local = Path(__file__).parent / "applicant_info_secret.txt"
    YOUR_INFO = json.loads(_local.read_text(encoding="utf-8")) if _local.exists() else {}

# ── Email config (same env vars as greenhouse script) ─────────────────────────
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", YOUR_INFO.get("email", ""))

# ── LinkedIn session cookie ────────────────────────────────────────────────────
# Refresh: F12 → Application → Cookies → linkedin.com → copy li_at value
# On CI this is read from the LINKEDIN_COOKIE GitHub secret automatically.
LINKEDIN_COOKIE = os.environ.get("LINKEDIN_COOKIE", "")

# ── Search config ──────────────────────────────────────────────────────────────
ROLES = [
    "Data Engineer",
    "Data Analyst",
    "Business Intelligence Analyst",
]
MAX_PAGES = 5    # pages per role (each page ≈ 25 jobs)
MAX_HOURS = 2    # only process jobs posted within this many hours
GEO_URN   = "103644278"  # United States

# Run headless on CI (GitHub Actions sets CI=true), visible locally
HEADLESS = os.environ.get("CI", "").lower() == "true"

# ── Output files ───────────────────────────────────────────────────────────────
OUTPUT_CSV  = Path(__file__).parent / "linkedin_jobs_applied.csv"
APPLIED_LOG = Path(__file__).parent / "linkedin_applied_ids.json"


# ══════════════════════════════════════════════════════════════════════════════
# Applied IDs log
# ══════════════════════════════════════════════════════════════════════════════

def load_applied_ids() -> dict:
    if APPLIED_LOG.exists():
        try:
            data = json.loads(APPLIED_LOG.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {jid: {} for jid in data}
        except Exception:
            pass
    return {}


def save_applied_ids(ids: dict) -> None:
    APPLIED_LOG.write_text(json.dumps(ids, indent=2), encoding="utf-8")


def append_csv(row: dict) -> None:
    fieldnames = ["job_id", "title", "company", "posted_text", "apply_url",
                  "status", "notes", "scraped_at"]
    write_header = not OUTPUT_CSV.exists() or OUTPUT_CSV.stat().st_size == 0
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ══════════════════════════════════════════════════════════════════════════════
# Posting time parser
# ══════════════════════════════════════════════════════════════════════════════

def parse_posted_hours(text: str) -> float:
    """Convert LinkedIn posting-age text to hours. Returns 9999 if unparseable."""
    if not text:
        return 9999.0
    t = text.lower().strip()
    if "just now" in t or "moment" in t:
        return 0.0
    m = re.search(r"(\d+)\s*minute", t)
    if m:
        return int(m.group(1)) / 60
    m = re.search(r"(\d+)\s*hour", t)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+)\s*day", t)
    if m:
        return float(m.group(1)) * 24
    m = re.search(r"(\d+)\s*week", t)
    if m:
        return float(m.group(1)) * 168
    m = re.search(r"(\d+)\s*month", t)
    if m:
        return float(m.group(1)) * 720
    return 9999.0


# ══════════════════════════════════════════════════════════════════════════════
# Driver setup
# ══════════════════════════════════════════════════════════════════════════════

def launch_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )
    if HEADLESS:
        options.add_argument("--headless=new")
    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )


def inject_cookie(driver: webdriver.Chrome, value: str):
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setCookie", {
        "name":     "li_at",
        "value":    value,
        "domain":   ".linkedin.com",
        "path":     "/",
        "secure":   True,
        "httpOnly": True,
        "sameSite": "None",
    })


def build_search_url(role: str, page: int = 1) -> str:
    """LinkedIn Jobs search URL — last 2 hours, US only, paginated."""
    start = (page - 1) * 25
    url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={quote(role)}"
        f"&geoId={GEO_URN}"
        f"&f_TPR=r7200"
        f"&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON"
        f"&refresh=true"
    )
    if start > 0:
        url += f"&start={start}"
    return url


# ══════════════════════════════════════════════════════════════════════════════
# Job list helpers
# ══════════════════════════════════════════════════════════════════════════════

def wait_for_job_list(driver: webdriver.Chrome, timeout: int = 15):
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((
            By.CSS_SELECTOR,
            "li[data-occludable-job-id], .jobs-search-results__list-item",
        ))
    )


def get_job_ids_on_page(driver: webdriver.Chrome) -> list[str]:
    cards = driver.find_elements(By.CSS_SELECTOR, "li[data-occludable-job-id]")
    seen, ids = set(), []
    for c in cards:
        jid = c.get_attribute("data-occludable-job-id") or c.get_attribute("data-job-id") or ""
        jid = jid.strip()
        if jid and jid not in seen:
            seen.add(jid)
            ids.append(jid)
    return ids


def click_job_card(driver: webdriver.Chrome, job_id: str) -> bool:
    try:
        card = driver.find_element(By.CSS_SELECTOR,
            f"li[data-occludable-job-id='{job_id}']")
        link = card.find_element(By.CSS_SELECTOR,
            "a.job-card-list__title, a[href*='/jobs/view/']")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", link)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", link)
        return True
    except Exception as e:
        print(f"    [warn] Could not click job {job_id}: {e}")
        return False


def get_job_detail(driver: webdriver.Chrome) -> dict:
    """Parse the detail panel after clicking a job card."""
    result = {
        "title": "", "company": "", "location": "", "posted_text": "",
        "hours_ago": 9999.0, "is_easy_apply": False, "apply_url": "", "job_id": "",
    }
    try:
        WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                ".jobs-unified-top-card, .job-details-jobs-unified-top-card, "
                ".jobs-details__main-content"))
        )
        time.sleep(1.5)

        # Title
        for sel in [
            "h1.jobs-unified-top-card__job-title",
            "h1.job-details-jobs-unified-top-card__job-title",
            "h1[class*='job-title']",
            "h1.t-24",
            "h1",
        ]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                t = els[0].text.strip()
                if t:
                    result["title"] = t
                    break

        # Company
        for sel in [
            ".jobs-unified-top-card__company-name a",
            ".job-details-jobs-unified-top-card__company-name a",
            ".jobs-unified-top-card__primary-description a",
        ]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                result["company"] = els[0].text.strip()
                break

        # Location
        for sel in [
            ".jobs-unified-top-card__bullet",
            ".job-details-jobs-unified-top-card__bullet",
        ]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                result["location"] = els[0].text.strip()
                break

        # Posting time — search multiple selectors, then fall back to full panel text
        posted_text = ""
        for sel in [
            ".jobs-unified-top-card__posted-date",
            ".job-details-jobs-unified-top-card__primary-description-without-tagline span",
            "span.tvm__text",
            "strong",
        ]:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                t = el.text.strip()
                if re.search(r"\b(ago|now|minute|hour|day|week|month)\b", t, re.I):
                    posted_text = t
                    break
            if posted_text:
                break

        if not posted_text:
            try:
                panel_text = driver.find_element(By.CSS_SELECTOR,
                    ".jobs-unified-top-card, .job-details-jobs-unified-top-card").text
                m = re.search(
                    r"((?:Reposted\s+)?\d+\s+(?:minute|hour|day|week|month)s?\s+ago|just now)",
                    panel_text, re.I,
                )
                if m:
                    posted_text = m.group(0)
            except Exception:
                pass

        result["posted_text"] = posted_text
        result["hours_ago"]   = parse_posted_hours(posted_text)

        # Job ID and canonical URL from current URL
        url = driver.current_url
        m = re.search(r"/jobs/view/(\d+)", url)
        if not m:
            m = re.search(r"currentJobId=(\d+)", url)
        if m:
            result["job_id"]    = m.group(1)
            result["apply_url"] = f"https://www.linkedin.com/jobs/view/{m.group(1)}/"

        # Determine Easy Apply vs external
        for sel in [
            "button.jobs-apply-button",
            "button[aria-label*='Apply']",
            ".jobs-s-apply button",
        ]:
            for btn in driver.find_elements(By.CSS_SELECTOR, sel):
                if not btn.is_displayed():
                    continue
                txt = (btn.text or btn.get_attribute("aria-label") or "").lower()
                if "easy apply" in txt:
                    result["is_easy_apply"] = True
                    break
                if "apply" in txt:
                    result["is_easy_apply"] = False
                    # Try to grab external href
                    try:
                        href = btn.get_attribute("href") or ""
                        if href:
                            result["apply_url"] = href
                    except Exception:
                        pass
                    break
            else:
                continue
            break

    except TimeoutException:
        print("    [warn] Timed out waiting for detail panel")
    except Exception as e:
        print(f"    [warn] Detail parse error: {e}")

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Easy Apply form handler
# ══════════════════════════════════════════════════════════════════════════════

def _safe_click(driver: webdriver.Chrome, el):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.3)
    try:
        el.click()
    except ElementClickInterceptedException:
        driver.execute_script("arguments[0].click();", el)


def _handle_field(driver: webdriver.Chrome, el, info: dict) -> bool:
    """Fill a single form field. Returns True if handled."""
    try:
        tag        = el.tag_name.lower()
        field_id   = (el.get_attribute("id") or "").lower()
        name_attr  = (el.get_attribute("name") or "").lower()
        placeholder = (el.get_attribute("placeholder") or "").lower()
        aria_label  = (el.get_attribute("aria-label") or "").lower()
        ctx = f"{field_id} {name_attr} {placeholder} {aria_label}"

        # Fetch associated label text for context
        label_text = ""
        if field_id:
            try:
                lbl = driver.find_element(By.CSS_SELECTOR, f"label[for='{field_id}']")
                label_text = lbl.text.lower()
            except Exception:
                pass
        full_ctx = ctx + " " + label_text

        if tag == "input":
            itype = (el.get_attribute("type") or "text").lower()
            if itype in ("hidden", "file", "checkbox"):
                return False

            if itype == "radio":
                if "yes" in label_text and "sponsor" not in full_ctx:
                    _safe_click(driver, el)
                    return True
                if "no" in label_text and "sponsor" in full_ctx:
                    _safe_click(driver, el)
                    return True
                return False

            # Text-like inputs
            current = el.get_attribute("value") or ""
            if current.strip():
                return False  # already filled

            if "phone" in full_ctx or itype == "tel":
                el.clear(); el.send_keys(info.get("phone", ""))
            elif "email" in full_ctx:
                el.clear(); el.send_keys(info.get("email", ""))
            elif "first" in full_ctx and "name" in full_ctx:
                el.clear(); el.send_keys(info.get("first_name", ""))
            elif "last" in full_ctx and "name" in full_ctx:
                el.clear(); el.send_keys(info.get("last_name", ""))
            elif "city" in full_ctx:
                el.clear(); el.send_keys(info.get("city", ""))
            elif "zip" in full_ctx or "postal" in full_ctx:
                el.clear(); el.send_keys(info.get("zip", ""))
            elif "salary" in full_ctx or "compensation" in full_ctx:
                el.clear(); el.send_keys(info.get("salary", "70000"))
            elif "year" in full_ctx and ("experience" in full_ctx or "exp" in full_ctx):
                el.clear(); el.send_keys("3")
            elif "linkedin" in full_ctx:
                el.clear(); el.send_keys(info.get("linkedin_url", ""))
            elif "website" in full_ctx or "portfolio" in full_ctx:
                el.clear(); el.send_keys(info.get("website", ""))
            elif itype == "number":
                el.clear(); el.send_keys("3")
            else:
                return False
            return True

        elif tag == "select":
            sel_obj = Select(el)
            opts = [o.text.lower() for o in sel_obj.options if o.text.strip()]
            has_yes = any("yes" in o for o in opts)
            has_no  = any("no" in o for o in opts)

            # Already has a non-placeholder selection?
            if sel_obj.first_selected_option.get_attribute("value") not in ("", "0", "Select an option"):
                return False

            if has_yes and has_no:
                if "sponsor" in full_ctx:
                    sel_obj.select_by_visible_text(
                        next(o.text for o in sel_obj.options if "no" in o.text.lower()))
                else:
                    sel_obj.select_by_visible_text(
                        next(o.text for o in sel_obj.options if "yes" in o.text.lower()))
                return True
            # Numeric experience selects — pick "3" or middle option
            for opt in sel_obj.options:
                if re.search(r"\b3\b", opt.text):
                    sel_obj.select_by_visible_text(opt.text)
                    return True
            if len(sel_obj.options) > 1:
                sel_obj.select_by_index(1)
                return True

        elif tag == "textarea":
            if (el.get_attribute("value") or el.text or "").strip():
                return False
            if "cover" in full_ctx:
                val = info.get("cover_letter") or info.get("interest_answer", "")
            elif "why" in label_text or "interest" in label_text:
                val = info.get("interest_answer", "")
            elif "team" in label_text:
                val = info.get("team_answer", "")
            else:
                val = info.get("interest_answer",
                    "I am excited about this role and believe my experience is a strong match.")
            el.send_keys(val)
            return True

    except StaleElementReferenceException:
        pass
    except Exception:
        pass
    return False


def dismiss_modal(driver: webdriver.Chrome):
    """Close the Easy Apply modal without submitting."""
    try:
        for sel in [
            "button[aria-label='Dismiss']",
            "button[aria-label='Close']",
            "button.artdeco-modal__dismiss",
        ]:
            btns = driver.find_elements(By.CSS_SELECTOR, sel)
            for btn in btns:
                if btn.is_displayed():
                    _safe_click(driver, btn)
                    time.sleep(1)
                    for dsel in [
                        "button[data-control-name='discard_application_confirm_btn']",
                        "button.artdeco-modal__confirm-dialog-btn",
                    ]:
                        for db in driver.find_elements(By.CSS_SELECTOR, dsel):
                            if db.is_displayed() and "discard" in db.text.lower():
                                _safe_click(driver, db)
                    return
    except Exception:
        pass
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass
    time.sleep(1)


def close_extra_windows(driver: webdriver.Chrome, original_handle: str):
    """Close any tabs/windows opened during apply, return to original handle."""
    try:
        for handle in driver.window_handles:
            if handle != original_handle:
                driver.switch_to.window(handle)
                driver.close()
        driver.switch_to.window(original_handle)
    except Exception:
        pass


def attempt_easy_apply(driver: webdriver.Chrome, info: dict) -> tuple[str, str]:
    """
    Open and complete the LinkedIn Easy Apply modal.
    Returns (status, notes): status is 'applied' or 'failed - easy apply'
    """
    original_handle = driver.current_window_handle
    try:
        # Find Easy Apply button
        apply_btn = None
        for sel in [
            "button.jobs-apply-button",
            "button[aria-label*='Easy Apply']",
            ".jobs-s-apply button",
            ".jobs-apply-button",
        ]:
            for b in driver.find_elements(By.CSS_SELECTOR, sel):
                txt = (b.text or b.get_attribute("aria-label") or "").lower()
                if "easy apply" in txt and b.is_displayed():
                    apply_btn = b
                    break
            if apply_btn:
                break

        if not apply_btn:
            return "failed - easy apply", "Easy Apply button not found"

        _safe_click(driver, apply_btn)
        time.sleep(2.5)

        # If a new tab opened, close it and bail
        close_extra_windows(driver, original_handle)

        # Wait for modal
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR,
                    ".jobs-easy-apply-modal, .artdeco-modal, [role='dialog']"))
            )
        except TimeoutException:
            return "failed - easy apply", "Modal did not open"

        for step in range(12):
            time.sleep(1.5)

            # Close any extra tabs that opened mid-flow
            close_extra_windows(driver, original_handle)

            # Locate modal container
            modal_els = driver.find_elements(By.CSS_SELECTOR,
                ".jobs-easy-apply-modal, .artdeco-modal[role='dialog']")
            modal = modal_els[0] if modal_els else driver

            # Fill all visible fields
            for tag in ["input", "select", "textarea"]:
                for el in modal.find_elements(By.TAG_NAME, tag):
                    if el.is_displayed() and el.is_enabled():
                        _handle_field(driver, el, info)

            time.sleep(0.5)

            # Find action buttons
            footer_btns = modal.find_elements(By.CSS_SELECTOR,
                "footer button, .jobs-easy-apply-form-actions button, button[aria-label]")
            submit_btn = review_btn = next_btn = None
            for b in footer_btns:
                if not b.is_displayed():
                    continue
                txt = (b.text or b.get_attribute("aria-label") or "").strip().lower()
                if "submit application" in txt or txt == "submit":
                    submit_btn = b
                elif "review" in txt:
                    review_btn = b
                elif "next" in txt or "continue" in txt:
                    next_btn = b

            if submit_btn:
                _safe_click(driver, submit_btn)
                time.sleep(3)
                close_extra_windows(driver, original_handle)
                # Close any post-apply confirmation
                try:
                    close = WebDriverWait(driver, 6).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR,
                            "button[aria-label='Dismiss'], button[aria-label='Close']"))
                    )
                    _safe_click(driver, close)
                except Exception:
                    pass
                return "applied", ""
            elif review_btn:
                _safe_click(driver, review_btn)
            elif next_btn:
                _safe_click(driver, next_btn)
            else:
                dismiss_modal(driver)
                close_extra_windows(driver, original_handle)
                return "failed - easy apply", f"No action button found on step {step + 1}"

        dismiss_modal(driver)
        close_extra_windows(driver, original_handle)
        return "failed - easy apply", "Exceeded max steps without submitting"

    except Exception as e:
        try:
            dismiss_modal(driver)
            close_extra_windows(driver, original_handle)
        except Exception:
            pass
        return "failed - easy apply", str(e)[:150]


# ══════════════════════════════════════════════════════════════════════════════
# Summary email
# ══════════════════════════════════════════════════════════════════════════════

def send_summary_email(all_jobs: list[dict]) -> None:
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set — skipping email.")
        return
    if not all_jobs:
        print("[!] No jobs to report.")
        return

    n_easy     = sum(1 for r in all_jobs if r["status"] == "easy apply")
    n_external = sum(1 for r in all_jobs if r["status"] == "external link")
    n_skipped  = sum(1 for r in all_jobs if r["status"].startswith("skipped"))
    n_new      = sum(1 for r in all_jobs if r.get("is_new"))

    STATUS_COLOR = {
        "easy apply":     "#d4edda",
        "external link":  "#cce5ff",
        "skipped":        "#f5f5f5",
        "failed":         "#f8d7da",
    }

    def _color(status: str) -> str:
        for k, v in STATUS_COLOR.items():
            if status.startswith(k):
                return v
        return "#fff"

    def _row(r):
        status     = r["status"]
        bg         = _color(status)
        url        = r.get("apply_url", "")
        link_cell  = f"<a href='{url}'>Link</a>" if url else "—"
        new_badge  = (
            " <span style='background:#0c5460;color:white;font-size:10px;"
            "padding:1px 5px;border-radius:3px;vertical-align:middle'>NEW</span>"
            if r.get("is_new") else ""
        )
        applied_at = r.get("applied_at", "")
        if applied_at:
            try:
                applied_at = datetime.fromisoformat(applied_at).strftime("%b %d, %Y %I:%M %p")
            except Exception:
                pass
        return (
            f"<tr style='background:{bg}'>"
            f"<td>{r.get('title', '')}{new_badge}</td>"
            f"<td>{r.get('company', '')}</td>"
            f"<td>{r.get('posted_text', '')}</td>"
            f"<td><b>{status.upper()}</b></td>"
            f"<td>{link_cell}</td>"
            f"<td>{applied_at or '—'}</td>"
            f"</tr>"
        )

    rows    = "".join(_row(r) for r in all_jobs)
    subject = (
        f"LinkedIn Jobs: {n_easy} easy apply | "
        f"{n_external} external | "
        f"{n_skipped} skipped — {len(all_jobs)} total"
    )
    new_note = (
        f'<p style="color:#0c5460;font-size:13px">&#9733; {n_new} job(s) not seen '
        f'in the previous run are marked <b>NEW</b>.</p>'
        if n_new else ""
    )
    body_html = f"""
    <h2>LinkedIn Jobs Summary</h2>
    <p>
      <b style="color:#155724">Easy Apply: {n_easy}</b> &nbsp;|&nbsp;
      <b style="color:#004085">External Link: {n_external}</b> &nbsp;|&nbsp;
      <b>Skipped: {n_skipped}</b> &nbsp;|&nbsp;
      <b>Total scanned: {len(all_jobs)}</b> &nbsp;|&nbsp;
      <b style="color:#0c5460">New this run: {n_new}</b>
    </p>
    {new_note}
    <p style="font-size:12px;color:#555">
      Only jobs posted within the last {MAX_HOURS} hour(s) were processed.
      Roles searched: {", ".join(ROLES)}.
    </p>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Posted</th><th>Status</th><th>Link</th><th>Applied On</th>
      </tr>
      {rows}
    </table>
    <p style="font-size:11px;color:#888;margin-top:16px">
      Generated by linkedin_job_applier.py &mdash; {datetime.now().strftime("%Y-%m-%d %H:%M")}
    </p>
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


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if not LINKEDIN_COOKIE or LINKEDIN_COOKIE == "PASTE_li_at_HERE":
        print("[ERROR] Set LINKEDIN_COOKIE at the top of this file.")
        sys.exit(1)

    applied_ids    = load_applied_ids()
    ids_before_run = set(applied_ids.keys())
    all_jobs: list[dict] = []

    # Kill any leftover chromedriver processes (Windows only)
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "chromedriver.exe", "/T"], capture_output=True)
    time.sleep(2)

    print("[Setup] Launching Chrome...")
    driver = launch_driver()

    try:
        print("[Auth] Injecting LinkedIn session cookie...")
        driver.get("https://www.linkedin.com")
        time.sleep(3)
        inject_cookie(driver, LINKEDIN_COOKIE)
        # Navigate once to activate the cookie
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(3)

        for role in ROLES:
            print(f"\n{'=' * 60}")
            print(f"  Role: {role}  |  last {MAX_HOURS}h  |  up to {MAX_PAGES} pages")
            print("=" * 60)

            for page in range(1, MAX_PAGES + 1):
                url = build_search_url(role, page)
                print(f"\n  [Page {page}] {url}")
                driver.get(url)
                time.sleep(4)

                if "authwall" in driver.current_url or "login" in driver.current_url:
                    print("[ERROR] Cookie expired — session ended.")
                    break


                try:
                    wait_for_job_list(driver)
                except TimeoutException:
                    print("  No job cards found — end of results for this role.")
                    break

                job_ids = get_job_ids_on_page(driver)
                if not job_ids:
                    print("  No job IDs found — stopping.")
                    break
                print(f"  Found {len(job_ids)} job cards on this page")

                for idx, jid in enumerate(job_ids, 1):
                    print(f"\n  [{idx}/{len(job_ids)}] Job ID: {jid}")

                    # Session recovery — relaunch Chrome if the session died
                    try:
                        _ = driver.current_url
                    except Exception:
                        print("  [!] Session lost — relaunching Chrome...")
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        subprocess.run(["taskkill", "/F", "/IM", "chromedriver.exe", "/T"],
                                       capture_output=True)
                        time.sleep(2)
                        driver = launch_driver()
                        driver.get("https://www.linkedin.com")
                        time.sleep(2)
                        inject_cookie(driver, LINKEDIN_COOKIE)
                        driver.get(build_search_url(role, page))
                        time.sleep(5)
                        print("  [+] Session restored, continuing...")

                    # Skip already-processed jobs
                    if jid in applied_ids:
                        meta = applied_ids[jid]
                        print(f"    -> Already processed ({meta.get('status','?')}), skipping.")
                        all_jobs.append({
                            "job_id":      jid,
                            "title":       meta.get("title", ""),
                            "company":     meta.get("company", ""),
                            "posted_text": "",
                            "apply_url":   f"https://www.linkedin.com/jobs/view/{jid}/",
                            "status":      "skipped (duplicate)",
                            "notes":       f"Previously {meta.get('status', '')}",
                            "is_new":      False,
                            "applied_at":  meta.get("applied_at", ""),
                            "scraped_at":  datetime.now().isoformat(),
                        })
                        continue

                    if not click_job_card(driver, jid):
                        continue
                    time.sleep(2)

                    job = get_job_detail(driver)
                    if not job.get("job_id"):
                        job["job_id"] = jid
                    if not job.get("apply_url"):
                        job["apply_url"] = f"https://www.linkedin.com/jobs/view/{jid}/"

                    print(f"    Title  : {job['title']}")
                    print(f"    Company: {job['company']}")
                    print(f"    Posted : {job['posted_text']!r}  →  {job['hours_ago']:.1f}h ago")
                    print(f"    Type   : {'Easy Apply' if job['is_easy_apply'] else 'External'}")

                    # Time filter — skip if posted more than MAX_HOURS ago
                    if job["hours_ago"] > MAX_HOURS:
                        print(f"    -> SKIP: {job['hours_ago']:.1f}h ago exceeds {MAX_HOURS}h limit")
                        all_jobs.append({
                            **job,
                            "status":     "skipped (old)",
                            "notes":      f"{job['hours_ago']:.1f}h ago",
                            "is_new":     jid not in ids_before_run,
                            "applied_at": "",
                            "scraped_at": datetime.now().isoformat(),
                        })
                        continue

                    # Record apply type — no auto-submission
                    now_ts = datetime.now().isoformat()
                    if job["is_easy_apply"]:
                        status = "easy apply"
                        notes  = ""
                        print(f"    -> Easy Apply: {job['apply_url']}")
                    else:
                        status = "external link"
                        notes  = ""
                        print(f"    -> External: {job['apply_url']}")

                    applied_at = now_ts
                    record = {
                        "job_id":      job["job_id"],
                        "title":       job["title"],
                        "company":     job["company"],
                        "posted_text": job["posted_text"],
                        "apply_url":   job["apply_url"],
                        "status":      status,
                        "notes":       notes,
                        "is_new":      jid not in ids_before_run,
                        "applied_at":  applied_at,
                        "scraped_at":  now_ts,
                    }
                    all_jobs.append(record)
                    append_csv(record)

                    # Persist so we don't re-process on next run
                    if status in ("easy apply", "external link"):
                        applied_ids[jid] = {
                            "title":      job["title"],
                            "company":    job["company"],
                            "status":     status,
                            "applied_at": applied_at,
                        }
                        save_applied_ids(applied_ids)

                    time.sleep(2.5)

                time.sleep(3)  # between pages

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # Console summary
    print(f"\n{'=' * 60}")
    print("  FINAL SUMMARY")
    print("=" * 60)
    for j in all_jobs:
        print(f"  [{j['status'].upper():<28}]  {j.get('company',''):<30}  {j.get('title','')}")
    print(f"\n  Total: {len(all_jobs)} jobs processed.")
    print(f"  Log  : {OUTPUT_CSV}")

    send_summary_email(all_jobs)


if __name__ == "__main__":
    main()
