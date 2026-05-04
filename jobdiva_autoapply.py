"""
JobDiva_Auto_apply.py

JobDiva portal auto-apply bot.
- Searches "Data Engineer", sorts newest-to-oldest
- Only applies to exact "Data Engineer" or "Python Data Engineer" (no senior/sr/lead)
- Skips jobs posted more than 2 days ago
- Applies via Quick Apply (No Account) with DE resume
- Logs results to jobdiva_applied.csv
- Email functionality not yet enabled — test locally first
"""

import asyncio
import csv
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Applicant info ──────────────────────────────────────────────────────────────
_applicant_json = os.environ.get("APPLICANT_INFO_JSON", "")
if _applicant_json:
    YOUR_INFO = json.loads(_applicant_json)
else:
    _local = Path(__file__).parent / "txt" / "applicant_info_secret.txt"
    YOUR_INFO = json.loads(_local.read_text(encoding="utf-8")) if _local.exists() else {}

DE_RESUME_PATH = os.environ.get("DE_RESUME_PATH", "")

# ── Email config ────────────────────────────────────────────────────────────────
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

# ── Portal config ───────────────────────────────────────────────────────────────
PORTAL_URL = (
    "https://www1.jobdiva.com/portal/"
    "?a=svjdnwzkulao5hqo7t0ifgvj8s71sf01d7dtgdstyhdixakxt6ty85zljsdyhgz2"
    "&jr_id=69f1011eecbc8c2f73203108#/"
)
SEARCH_TERMS = ["Data Engineer", "Data Analyst"]
MAX_AGE_DAYS = 2

OUTPUT_CSV  = Path(__file__).parent / "csv" / "jobdiva_applied.csv"
APPLIED_LOG = Path(__file__).parent / "json" / "jobdiva_applied_ids.json"

# Exact allowed titles (case-insensitive full match), no senior/sr variants
ALLOWED_TITLES = re.compile(r"^(python\s+data\s+engineer|data\s+engineer|data\s+analyst)$", re.I)
SKIP_TITLE_RE  = re.compile(r"\b(senior|sr\.?|lead|manager|principal|staff|ii|iii)\b", re.I)

# US location filter — matches US states, "United States", "Remote" (US-based remotes)
_US_STATES = (
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    r"MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
US_LOCATION_RE = re.compile(
    rf"\b(united\s+states|usa|u\.s\.a?\.?|remote|{_US_STATES})\b", re.I
)


# ── Persistence ─────────────────────────────────────────────────────────────────

def load_applied_ids() -> set:
    if APPLIED_LOG.exists():
        try:
            data = json.loads(APPLIED_LOG.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set(data.keys())
        except Exception:
            pass
    return set()


def save_applied_ids(ids: set) -> None:
    APPLIED_LOG.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


def append_csv(row: dict) -> None:
    fieldnames = ["title", "company", "status", "link", "applied_on"]
    write_header = not OUTPUT_CSV.exists() or OUTPUT_CSV.stat().st_size == 0
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ── Filters ──────────────────────────────────────────────────────────────────────

def is_within_age_limit(date_str: str) -> bool:
    """MM/DD/YYYY date must be within MAX_AGE_DAYS of today."""
    try:
        posted = datetime.strptime(date_str.strip(), "%m/%d/%Y")
        return (datetime.now() - posted).days <= MAX_AGE_DAYS
    except ValueError:
        return False


def is_allowed_title(title: str) -> bool:
    title = title.strip()
    if SKIP_TITLE_RE.search(title):
        return False
    return bool(ALLOWED_TITLES.match(title))


def is_us_location(location: str) -> bool:
    """Return True if location is US-based or undetectable (blank)."""
    if not location.strip():
        return True  # No location info — don't filter out
    return bool(US_LOCATION_RE.search(location))


# ── Page helpers ─────────────────────────────────────────────────────────────────

async def try_click(page: Page, *selectors: str, timeout: int = 5_000) -> bool:
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


async def try_fill(page: Page, value: str, *selectors: str) -> bool:
    if not value:
        return False
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=1_500):
                await loc.fill(value)
                return True
        except Exception:
            continue
    return False


async def try_select(page: Page, *selectors: str, label: str = "", value: str = "") -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=1_500):
                if label:
                    await loc.select_option(label=label)
                elif value:
                    await loc.select_option(value=value)
                return True
        except Exception:
            continue
    return False


# ── Email summary ────────────────────────────────────────────────────────────────

def send_summary_email(results: list[dict]) -> None:
    """Send an HTML summary email of the run results."""
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set — skipping email.")
        return
    if not results:
        print("[i] No jobs processed — skipping email.")
        return

    n_applied = sum(1 for r in results if r["status"] == "applied")
    n_failed  = sum(1 for r in results if r["status"] == "failed")

    status_color = {"applied": "#d4edda", "failed": "#f8d7da"}

    def _row(r):
        bg      = status_color.get(r["status"], "#fff")
        loc     = r.get("location", "")
        loc_cell = f" — {loc}" if loc else ""
        return (
            f"<tr style='background:{bg}'>"
            f"<td>{r['title']}{loc_cell}</td>"
            f"<td>{r['company']}</td>"
            f"<td>{r['status'].upper()}</td>"
            f"<td><a href='{r['link']}'>Link</a></td>"
            f"<td>{r['applied_on']}</td>"
            f"</tr>"
        )

    rows     = "".join(_row(r) for r in results)
    subject  = (
        f"[Cron Job] JobDiva Auto-Apply: {n_applied} applied | "
        f"{n_failed} failed — {datetime.now().strftime('%b %d, %Y')}"
    )
    body_html = f"""
    <h2>JobDiva Auto-Apply Summary</h2>
    <p>
      <b style="color:#155724">Applied: {n_applied}</b> &nbsp;|&nbsp;
      <b style="color:#721c24">Failed: {n_failed}</b> &nbsp;|&nbsp;
      <b>Total processed: {len(results)}</b>
    </p>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title / Location</th><th>Company</th><th>Status</th>
        <th>Link</th><th>Applied On</th>
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


# ── Navigation helpers ───────────────────────────────────────────────────────────

async def navigate_to_search(page: Page, search_term: str = SEARCH_TERMS[0]) -> None:
    """(Re)load the portal, search for the given term in United States, sort newest-to-oldest."""
    await page.goto(PORTAL_URL, wait_until="networkidle", timeout=45_000)
    await page.wait_for_timeout(3_000)

    await page.fill(
        "input.inputbox_search, input[placeholder*='Search job title' i]",
        search_term,
    )
    await page.wait_for_timeout(300)

    # Fill location field if the portal has one
    for loc_sel in [
        "input.inputbox_location",
        "input[placeholder*='location' i]",
        "input[placeholder*='city' i]",
        "input[placeholder*='where' i]",
        "input[placeholder*='zip' i]",
    ]:
        try:
            loc = page.locator(loc_sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=1_000):
                await loc.fill("United States")
                print(f"    [i] Location field filled: {loc_sel}")
                break
        except Exception:
            continue

    await page.wait_for_timeout(300)
    await page.click("button:has-text('Search Jobs')")
    await page.wait_for_timeout(3_000)

    try:
        await page.select_option("select.jd-form-tiny", value="1")
        await page.wait_for_timeout(2_500)
    except Exception:
        pass


# ── Form fill ────────────────────────────────────────────────────────────────────

async def fill_quick_apply_form(page: Page) -> str:
    """
    Fill the Quick Apply (No Account) form and submit.
    Returns 'applied' or 'failed'.
    """
    try:
        await page.wait_for_timeout(2_000)
        info = YOUR_INFO

        # DEBUG — dump all form fields to confirm selectors before filling
        fields = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('input, select, textarea')).map(el => ({
                tag: el.tagName, type: el.type || '', name: el.name || '',
                id: el.id || '', placeholder: el.placeholder || '',
                cls: el.className || '', ac: el.autocomplete || '',
            }))
        """)
        print(f"    [form fields detected: {len(fields)}]")
        for f in fields[:30]:
            print(f"      {f['tag']}  type={f['type']}  cls={f['cls']}  "
                  f"name={f['name']}  id={f['id']}  ac={f['ac']}")

        # ── Name / email: input.form-control.jd-form (positional) ─────────────
        # JobDiva uses class-based fields with no name/id/placeholder attributes.
        # Order: [0]=First Name, [1]=Last Name, [2]=Email
        name_fields = page.locator("input.form-control.jd-form")
        nf_count = await name_fields.count()
        print(f"    [input.form-control.jd-form count: {nf_count}]")
        if nf_count >= 1:
            await name_fields.nth(0).fill(info.get("first_name", ""))
            print("    [+] First name filled")
        if nf_count >= 2:
            await name_fields.nth(1).fill(info.get("last_name", ""))
            print("    [+] Last name filled")
        if nf_count >= 3:
            await name_fields.nth(2).fill(info.get("email", ""))
            print("    [+] Email filled")

        # ── Phone: +1 flag is pre-selected; fill digits only ──────────────────
        phone_loc = page.locator("input[type='tel'][autocomplete='tel']").first
        if await phone_loc.count() == 0:
            phone_loc = page.locator("input[type='tel']").first
        if await phone_loc.count() > 0:
            await phone_loc.fill(info.get("phone", "").lstrip("+1 "))
            print("    [+] Phone filled")
        else:
            print("    [!] Phone input not found")

        # ── Resume: hidden file input behind drag-and-drop zone ───────────────
        resume = DE_RESUME_PATH
        if resume and Path(resume).exists():
            try:
                file_input = page.locator("input[type='file']").first
                if await file_input.count() > 0:
                    await file_input.set_input_files(resume)
                    await page.wait_for_timeout(1_500)
                    print(f"    [+] Resume uploaded: {Path(resume).name}")
                else:
                    print("    [!] No file input found on form")
            except Exception as e:
                print(f"    [!] Resume upload error: {e}")
        else:
            print(f"    [!] DE Resume not found at: '{resume}'")

        # ── Submit ─────────────────────────────────────────────────────────────
        submitted = await try_click(
            page,
            "button[type='submit']",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
            timeout=5_000,
        )
        if not submitted:
            print("    [!] Submit button not found")
            return "failed"

        await page.wait_for_timeout(3_500)

        body = (await page.evaluate("document.body.innerText")).lower()
        if any(kw in body for kw in [
            "thank you", "successfully", "application received",
            "your application", "we have received",
        ]):
            return "applied"

        return "applied"

    except PlaywrightTimeout as e:
        print(f"    [!] Timeout: {e}")
        return "failed"
    except Exception as e:
        print(f"    [!] Unexpected error: {e}")
        return "failed"


# ── Job scanning ─────────────────────────────────────────────────────────────────

async def scan_jobs(page: Page) -> list[dict]:
    """Return list of {title, date, idx} from the current job-list panel."""
    # Scroll the job list panel to ensure all items are rendered
    await page.evaluate("""() => {
        const panel = document.querySelector('.jd-jobnav, [class*="jobnav"], [class*="job-list"]');
        if (panel) panel.scrollTop = panel.scrollHeight;
    }""")
    await page.wait_for_timeout(800)

    jobs = await page.evaluate("""() => {
        const out = [];
        const DATE_RE = /^\\d{2}\\/\\d{2}\\/\\d{4}$/;

        const spans = document.querySelectorAll(
            'span.text-capitalize.jd-nav-label.notranslate'
        );

        spans.forEach((span, idx) => {
            const rawTitle = span.innerText.trim();

            let date = '';
            let location = '';
            let node = span.parentElement;
            for (let i = 0; i < 8; i++) {
                if (!node) break;
                if (!date) {
                    const dateEl = Array.from(node.querySelectorAll('small.w-25'))
                        .find(s => DATE_RE.test(s.innerText.trim()));
                    if (dateEl) date = dateEl.innerText.trim();
                }
                if (!location) {
                    const locEl = node.querySelector('small.w-50');
                    if (locEl) location = locEl.innerText.trim();
                }
                if (date && location) break;
                node = node.parentElement;
            }

            out.push({ idx, title: rawTitle, date, location });
        });

        return out;
    }""")
    return jobs


# ── Main ─────────────────────────────────────────────────────────────────────────

async def main() -> None:
    applied_ids = load_applied_ids()
    results: list[dict] = []

    async with async_playwright() as p:
        _headless = os.environ.get("HEADLESS", "false").lower() == "true"
        browser = await p.chromium.launch(headless=_headless, slow_mo=0 if _headless else 80)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        print("[+] Loading JobDiva portal...")

        all_jobs = []
        seen_keys: set[str] = set()
        for term in SEARCH_TERMS:
            print(f"\n[+] Searching: '{term}'")
            await navigate_to_search(page, term)
            jobs = await scan_jobs(page)
            print(f"    Jobs found: {len(jobs)}")
            for job in jobs:
                key = f"{job.get('title', '')}|{job.get('date', '')}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    job["search_term"] = term
                    all_jobs.append(job)

        print(f"\n[+] Scanning job listings ({len(all_jobs)} total across all searches)...")

        to_apply = []
        for job in all_jobs:
            title    = job.get("title", "").strip()
            date_str = job.get("date",  "").strip()

            if not title:
                continue

            if not is_allowed_title(title):
                print(f"  [skip] '{title}' — does not match allowed titles")
                continue

            if date_str and not is_within_age_limit(date_str):
                print(f"  [skip] '{title}' — posted {date_str} (older than {MAX_AGE_DAYS}d)")
                continue

            location = job.get("location", "").strip()
            if not is_us_location(location):
                print(f"  [skip] '{title}' — non-US location: '{location}'")
                continue

            job_key = f"{title}|{date_str}"
            if job_key in applied_ids:
                print(f"  [skip] '{title}' — already applied")
                continue

            to_apply.append({**job, "key": job_key})
            loc_tag = f" | {location}" if location else ""
            print(f"  [match] '{title}' | {date_str}{loc_tag}")

        print(f"\n[+] {len(to_apply)} job(s) queued for application.")

        for i, job in enumerate(to_apply):
            print(f"\n{'─'*60}")
            print(f"  [{i+1}/{len(to_apply)}] {job['title']}  ({job['date']})")

            status  = "failed"
            company = "Unknown"
            job_url = PORTAL_URL

            try:
                # Always re-navigate so index-based clicking is stable
                await navigate_to_search(page, job.get("search_term", SEARCH_TERMS[0]))

                # Click by scan index (handles duplicate title+date pairs)
                target_idx = job["idx"]
                title_spans = page.locator("span.text-capitalize.jd-nav-label.notranslate")
                total = await title_spans.count()
                if target_idx >= total:
                    print(f"    [!] Index {target_idx} out of range ({total} spans) — skipping")
                    results.append({
                        "title": job["title"], "company": "Unknown",
                        "status": "failed", "link": PORTAL_URL,
                        "applied_on": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    })
                    append_csv(results[-1])
                    continue

                await title_spans.nth(target_idx).click()
                await page.wait_for_timeout(2_500)

                # DEBUG — dump buttons visible in detail panel
                panel_btns = await page.evaluate("""() =>
                    Array.from(document.querySelectorAll('button')).map(b => ({
                        cls: b.className, text: b.innerText.trim().substring(0, 60)
                    }))
                """)
                print(f"    [panel buttons: {[b['text'] for b in panel_btns if b['text']]}]")

                # Try to read company from right-side detail panel
                for co_sel in [
                    ".jd-company", "[class*='company-name']",
                    "span[class*='company']", "p[class*='company']",
                ]:
                    try:
                        co_el = page.locator(co_sel).first
                        if await co_el.count() > 0:
                            raw = (await co_el.text_content(timeout=1_500) or "").strip()
                            if raw:
                                company = raw
                                break
                    except Exception:
                        continue

                # Click Apply Now with verbose error reporting
                clicked_apply = False
                for sel in [
                    "button:has-text('Apply Now')",
                    ".jd-btn:has-text('Apply Now')",
                    "a:has-text('Apply Now')",
                ]:
                    try:
                        loc = page.locator(sel).first
                        cnt = await loc.count()
                        print(f"    [debug] '{sel}' count={cnt}")
                        if cnt > 0:
                            await loc.scroll_into_view_if_needed()
                            await loc.click(timeout=10_000)
                            clicked_apply = True
                            print(f"    [+] Clicked: {sel}")
                            break
                    except Exception as e:
                        print(f"    [!] '{sel}' error: {e}")

                if not clicked_apply:
                    print("    [!] Apply Now button not found")
                    raise RuntimeError("Apply Now missing")

                await page.wait_for_timeout(2_000)

                # Click Quick Apply (No Account)
                clicked_quick = await try_click(
                    page,
                    "button:has-text('Quick Apply (No Account)')",
                    "button:has-text('Quick Apply')",
                    timeout=10_000,
                )
                if not clicked_quick:
                    print("    [!] Quick Apply button not found")
                    raise RuntimeError("Quick Apply missing")

                # Fill and submit form
                status = await fill_quick_apply_form(page)

            except PlaywrightTimeout as e:
                print(f"    [!] Timeout: {e}")
                status = "failed"
            except RuntimeError:
                status = "failed"
            except Exception as e:
                print(f"    [!] Unexpected: {e}")
                status = "failed"

            print(f"    → Status: {status.upper()}")

            if status == "applied":
                applied_ids.add(job["key"])
                save_applied_ids(applied_ids)

            row = {
                "title":      job["title"],
                "company":    company,
                "location":   job.get("location", ""),
                "status":     status,
                "link":       job_url,
                "applied_on": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            results.append(row)
            append_csv(row)

            # Return to the listings page for the next job
            try:
                # Go back until we reach the portal list view
                for _ in range(4):
                    if page.url.startswith(PORTAL_URL.split("?")[0]):
                        break
                    await page.go_back()
                    await page.wait_for_timeout(1_200)

                # If not back at portal, re-navigate and re-search
                if not page.url.startswith(PORTAL_URL.split("?")[0]):
                    await navigate_to_search(page, job.get("search_term", SEARCH_TERMS[0]))
                else:
                    # Re-sort (SPA may have reset it)
                    try:
                        await page.select_option("select.jd-form-tiny", value="1")
                        await page.wait_for_timeout(1_500)
                    except Exception:
                        pass
            except Exception as e:
                print(f"    [!] Back-navigation error ({e}) — re-loading portal")
                await navigate_to_search(page)

        await browser.close()

    # ── Summary ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"[+] Run complete — {len(results)} job(s) processed")
    applied_ct = sum(1 for r in results if r["status"] == "applied")
    failed_ct  = sum(1 for r in results if r["status"] == "failed")
    print(f"    Applied: {applied_ct}   Failed: {failed_ct}")

    if results:
        print()
        header = f"  {'Title':<35} {'Company':<22} {'Status':<8} Applied On"
        print(header)
        print(f"  {'─' * (len(header) - 2)}")
        for r in results:
            print(
                f"  {r['title']:<35} {r['company']:<22} "
                f"{r['status']:<8} {r['applied_on']}"
            )

    print(f"\n  Log saved → {OUTPUT_CSV.name}")

    send_summary_email(results)


if __name__ == "__main__":
    asyncio.run(main())
