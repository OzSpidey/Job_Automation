"""
Amazon Jobs Scanner — University Programs
------------------------------------------
Flow (exactly as specified):
  1. Go to https://www.amazon.jobs/content/en/career-programs/university?country[]=US
  2. Click the magnifying-glass search icon
  3. Type "United States" in the Location input
  4. Click the Search submit button (button._search_gi3vf_131)
  5. Click Sort by → Most recent
  6. Scrape 40 pages for matching roles
  7. Email results to configured recipients

Target roles: Data Engineer, Business Intelligence Engineer, Business Analyst, BI Engineer

Run: python amazon_jobs_scanner.py
"""

import asyncio
import json
import os
import smtplib
import sys
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
TARGET_EMAIL    = os.environ.get("EMAIL_TO", "")
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SMTP_SERVER     = "smtp.gmail.com"
SMTP_PORT       = 465

UNIVERSITY_URL  = "https://www.amazon.jobs/content/en/career-programs/university?country%5B%5D=US"
PAGES_TO_SCRAPE = 40
SEEN_JOBS_FILE  = os.path.join(os.path.dirname(__file__), "amazon_seen_jobs.json")

TARGET_ROLES = [
    "data engineer",
    "business intelligence engineer",
    "business analyst",
    "bi engineer",
    "data analyst",
    "early grad",
    "software engineer",
    "ai engineer",
    "software developer",

]
# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def load_seen_urls() -> set[str]:
    if not os.path.exists(SEEN_JOBS_FILE):
        return set()
    with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_urls(urls: set[str]) -> None:
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(urls), f, indent=2)


def is_target_role(title: str) -> bool:
    t = title.lower()
    return any(role in t for role in TARGET_ROLES)


def send_email(jobs: list[dict], previously_seen: set[str]) -> None:
    new_count = sum(1 for j in jobs if j["url"] not in previously_seen)
    count     = len(jobs)
    subject   = f"Amazon Jobs Scraper — {count} Matching Role(s) Found ({new_count} NEW)"

    if not jobs:
        plain = "No matching jobs found (Data Engineer / BI Engineer / Business Analyst / Data Analyst, US)."
        html  = "<p>No matching jobs found.</p>"
    else:
        NEW_BADGE = '<span style="background:#e47911;color:#fff;font-size:11px;font-weight:bold;padding:2px 6px;border-radius:3px;margin-right:6px;">NEW</span>'
        rows_list = []
        for j in jobs:
            is_new = j["url"] not in previously_seen
            row_bg = 'background:#fef9f0;' if is_new else ''
            badge  = NEW_BADGE if is_new else ''
            rows_list.append(
                f'<tr style="{row_bg}">'
                f'<td style="padding:8px;border:1px solid #ddd;">{badge}{j["title"]}</td>'
                f'<td style="padding:8px;border:1px solid #ddd;"><a href="{j["url"]}">{j["url"]}</a></td>'
                f'<td style="padding:8px;border:1px solid #ddd;white-space:nowrap;">{j.get("date", "")}</td>'
                f'</tr>'
            )
        rows = "\n".join(rows_list)
        html = f"""
        <html><body style="font-family:Arial,sans-serif;color:#333">
        <h2 style="color:#232F3E">Amazon Jobs — Matching Roles</h2>
        <p>Found <strong>{count}</strong> role(s) matching:
           <em>Data Engineer &nbsp;|&nbsp; Business Intelligence Engineer &nbsp;|&nbsp;
           Business Analyst &nbsp;|&nbsp; Data Analyst &nbsp;|&nbsp; Early Grad</em>
        </p>
        <table style="border-collapse:collapse;width:100%;max-width:1100px">
          <tr style="background:#232F3E;color:#FF9900">
            <th style="padding:10px;border:1px solid #555;text-align:left;width:35%">Role</th>
            <th style="padding:10px;border:1px solid #555;text-align:left">Link</th>
            <th style="padding:10px;border:1px solid #555;text-align:left;width:15%">Date Posted</th>
          </tr>
          {rows}
        </table>
        <p style="font-size:12px;color:#888;margin-top:20px">
          Source: amazon.jobs · University Programs · United States · Most Recent
        </p>
        </body></html>
        """
        plain = f"Found {count} matching role(s) ({new_count} NEW):\n\n" + "\n".join(
            f"- {'[NEW] ' if j['url'] not in previously_seen else ''}{j['title']} ({j.get('date', 'date unknown')})\n  {j['url']}" for j in jobs
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = TARGET_EMAIL
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, TARGET_EMAIL, msg.as_string())

    print(f"[email] Sent to {TARGET_EMAIL} — {count} job(s).")


async def collect_page_jobs(page) -> list[dict]:
    """Collect job title, URL, and posting date from the current results page."""
    seen: set[str] = set()
    results = []

    # Strategy 1: find each job card container that has both a link and a posting date
    cards = await page.locator(
        '[class*="job-tile"], [class*="result-card"], '
        'li:has(a[href*="/jobs/"]), div:has(> a[href*="/jobs/"])'
    ).all()

    for card in cards:
        try:
            link_el = card.locator('h3 a, h2 a, a[href*="/jobs/"]').first
            title   = (await link_el.inner_text()).strip()
            href    = (await link_el.get_attribute("href") or "").strip()

            if not title or not href or len(title) < 5:
                continue
            if not href.startswith("http"):
                href = "https://www.amazon.jobs" + href
            if href in seen:
                continue
            seen.add(href)

            date = ""
            try:
                d = card.locator(".posting-date").first
                if await d.count() > 0:
                    date = (await d.inner_text()).strip()
            except Exception:
                pass

            results.append({"title": title, "url": href, "date": date})
        except Exception:
            continue

    # Strategy 2: fallback — grab all /jobs/ links, look for .posting-date nearby
    if not results:
        candidates = await page.locator(
            'h3 a[href*="/jobs/"], h2 a[href*="/jobs/"], a[href*="/jobs/"]'
        ).all()
        for link in candidates:
            try:
                title = (await link.inner_text()).strip()
                href  = (await link.get_attribute("href") or "").strip()
                if not title or not href or len(title) < 5:
                    continue
                if not href.startswith("http"):
                    href = "https://www.amazon.jobs" + href
                if href in seen:
                    continue
                seen.add(href)

                # Walk up the DOM to find the nearest .posting-date sibling
                date = await page.evaluate("""
                    (href) => {
                        const a = document.querySelector(`a[href="${href}"]`);
                        if (!a) return "";
                        let el = a.parentElement;
                        for (let i = 0; i < 6; i++) {
                            if (!el) break;
                            const d = el.querySelector(".posting-date");
                            if (d) return d.innerText.trim();
                            el = el.parentElement;
                        }
                        return "";
                    }
                """, href)

                results.append({"title": title, "url": href, "date": date or ""})
            except Exception:
                continue

    return results


# ──────────────────────────────────────────────────────────────────────────────
# MAIN SCRAPER
# ──────────────────────────────────────────────────────────────────────────────

async def scrape_jobs() -> list[dict]:
    matched: list[dict] = []
    seen_urls: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx     = await browser.new_context(viewport={"width": 1280, "height": 900})
        page    = await ctx.new_page()

        # ── 1. Load the university page ───────────────────────────────────────
        print(f"[1] Loading university page...")
        await page.goto(UNIVERSITY_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3_500)

        # Accept cookie banner if present
        try:
            btn = page.locator('button:has-text("Accept all")').first
            if await btn.count() > 0:
                await btn.click(timeout=4_000)
                await page.wait_for_timeout(800)
                print("  [cookies] Accepted.")
        except Exception:
            pass

        # ── 2. Click the magnifying-glass search icon ─────────────────────────
        # Selector: button containing <path clip-rule="evenodd" fill-rule="evenodd">
        # The submit button also has this path, so pick the FIRST one (the icon)
        print("[2] Clicking search icon...")
        await page.evaluate("""
            () => {
                const paths = document.querySelectorAll(
                    'path[clip-rule="evenodd"][fill-rule="evenodd"]'
                );
                for (const p of paths) {
                    const btn = p.closest('button');
                    // The icon button does NOT have a visible <span>Search</span>
                    // The submit button does — skip it
                    if (btn && !btn.querySelector('span')) {
                        btn.click();
                        return;
                    }
                }
                // Fallback: click first button with that path regardless
                const first = document.querySelector(
                    'path[clip-rule="evenodd"][fill-rule="evenodd"]'
                )?.closest('button');
                if (first) first.click();
            }
        """)
        # Give Qwik time to lazy-load the search panel JS and show the form
        await page.wait_for_timeout(4_000)

        # ── 3. Fill Location = "United States" ───────────────────────────────
        print("[3] Filling Location: United States...")
        loc = page.locator('input[placeholder="Location"]').first

        # The input starts hidden; Qwik shows it after the icon click.
        # Wait up to 10 s for it to become visible.
        try:
            await loc.wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeout:
            # If still hidden, force it visible via JS before interacting
            print("  Location still hidden — forcing visibility via JS...")
            await page.evaluate("""
                () => {
                    const inp = document.querySelector('input[placeholder="Location"]');
                    if (!inp) return;
                    inp.style.cssText += ';display:block!important;visibility:visible!important;opacity:1!important;';
                    let el = inp.parentElement;
                    while (el && el !== document.body) {
                        const cs = window.getComputedStyle(el);
                        if (cs.display === 'none' || cs.visibility === 'hidden') {
                            el.style.cssText += ';display:block!important;visibility:visible!important;';
                        }
                        el = el.parentElement;
                    }
                }
            """)
            await page.wait_for_timeout(1_000)

        await loc.click(force=True)
        await page.wait_for_timeout(300)
        await loc.fill("", force=True)
        await loc.type("United States", delay=90)
        await page.wait_for_timeout(2_500)

        # Pick the COUNTRY-level "United States" from autocomplete.
        # Iterate all options and choose the one whose text is exactly "United States"
        # (not a city like "Dallas/Fort Worth Metroplex, TX, United States").
        try:
            options = await page.locator('[role="option"], li[id*="option"]').all()
            selected = False
            for opt in options:
                text = (await opt.inner_text()).strip()
                if text.lower() == "united states":
                    await opt.click(timeout=4_000)
                    print(f"  [location] Selected exact match: '{text}'.")
                    selected = True
                    break
            if not selected and options:
                # Fallback: pick the option that STARTS with "United States" and is shortest
                best = None
                best_len = 9999
                for opt in options:
                    text = (await opt.inner_text()).strip()
                    if text.lower().startswith("united states") and len(text) < best_len:
                        best = opt
                        best_len = len(text)
                if best:
                    t = (await best.inner_text()).strip()
                    await best.click(timeout=4_000)
                    print(f"  [location] Selected shortest match: '{t}'.")
                    selected = True
            if not selected:
                print("  [location] No autocomplete match — keeping typed value.")
        except Exception as e:
            print(f"  [location] Autocomplete error: {e}")
        await page.wait_for_timeout(800)

        # ── 4. Click the Search submit button ────────────────────────────────
        # HTML: <button class="_search_gi3vf_131" q:id="3s">…<span>Search</span></button>
        # Most stable selector: button with class containing "_search_" that has a span
        print("[4] Clicking Search submit button...")
        clicked_submit = False
        try:
            submit = page.locator('button[class*="_search_"]').first
            if await submit.count() > 0:
                await submit.click(force=True, timeout=5_000)
                clicked_submit = True
                print("  [submit] Clicked via class '_search_'.")
        except Exception:
            pass

        if not clicked_submit:
            # Fallback: button containing an SVG title="Search" AND a span
            try:
                submit = page.locator(
                    'button:has(svg):has(span:has-text("Search"))'
                ).first
                await submit.click(force=True, timeout=5_000)
                clicked_submit = True
                print("  [submit] Clicked via SVG+span fallback.")
            except Exception:
                pass

        if not clicked_submit:
            print("  [submit] All selectors failed — pressing Enter.")
            await page.keyboard.press("Enter")

        # Wait for the results page to load
        await page.wait_for_timeout(5_000)
        raw_url = page.url
        print(f"  URL after search: {raw_url}")

        # Strip radius/lat/lng — Amazon sets a 24 km radius around a DC centroid
        # for "United States" which is far too restrictive for a country-wide search.
        if "radius=" in raw_url or "latitude=" in raw_url:
            parsed = urllib.parse.urlparse(raw_url)
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            for key in ("latitude", "longitude", "radius"):
                params.pop(key, None)
            clean_url = urllib.parse.urlunparse(
                parsed._replace(query=urllib.parse.urlencode(params, doseq=True))
            )
            print(f"  Navigating to radius-free URL: {clean_url}")
            await page.goto(clean_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3_000)

        # Check jobs loaded
        try:
            await page.wait_for_selector('a[href*="/jobs/"]', timeout=15_000)
        except PlaywrightTimeout:
            await page.screenshot(path="amazon_debug_after_search.png")
            print("  No job links found. Saved amazon_debug_after_search.png")
            await browser.close()
            return matched

        # ── 5. Sort by Most Recent ────────────────────────────────────────────
        # HTML: <button class="btn" data-toggle="dropdown">Sort by: Most relevant</button>
        print("[5] Clicking Sort by dropdown...")
        try:
            sort_btn = page.locator('button.btn[data-toggle="dropdown"]:has-text("Sort by")').first
            if await sort_btn.count() == 0:
                # JS fallback
                await page.evaluate(
                    "document.querySelector('button[data-toggle=\"dropdown\"]')?.click()"
                )
            else:
                await sort_btn.click(timeout=6_000)
            await page.wait_for_timeout(800)

            # <a id="listbox-sort-by--relevant-recent" ...>Most recent</a>
            recent = page.locator('#listbox-sort-by--relevant-recent').first
            if await recent.count() == 0:
                recent = page.locator('a[data-label="Most recent"]').first
            await recent.click(timeout=5_000)
            await page.wait_for_timeout(2_500)
            print("  [sort] Most Recent selected.")
        except Exception as e:
            print(f"  [sort] Skipped: {e}")

        # ── 6. Scrape 5 pages via URL pagination ─────────────────────────────
        # Capture the base results URL (after radius strip + sort) for page navigation.
        base_results_url = page.url

        for page_num in range(1, PAGES_TO_SCRAPE + 1):
            print(f"\n[page {page_num} of {PAGES_TO_SCRAPE}]")

            # Navigate using offset-based pagination (Amazon uses offset= not page=)
            if page_num > 1:
                offset = (page_num - 1) * 10
                parsed  = urllib.parse.urlparse(base_results_url)
                params  = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                params["offset"] = [str(offset)]
                params.pop("page", None)
                paged_url = urllib.parse.urlunparse(
                    parsed._replace(query=urllib.parse.urlencode(params, doseq=True))
                )
                print(f"  URL (offset={offset}): {paged_url}")
                try:
                    await page.goto(paged_url, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(2_000)
                    try:
                        await page.wait_for_selector('a[href*="/jobs/"]', timeout=8_000)
                    except PlaywrightTimeout:
                        print("  No jobs on this page — stopping pagination.")
                        break
                except Exception as e:
                    print(f"  Could not load page {page_num}: {e}")
                    break
            else:
                await page.wait_for_timeout(1_500)

            jobs_on_page = await collect_page_jobs(page)
            print(f"  Links found: {len(jobs_on_page)}")

            if not jobs_on_page:
                print("  Empty page — stopping.")
                break

            page_matches = 0
            for job in jobs_on_page:
                if job["url"] in seen_urls:
                    continue
                seen_urls.add(job["url"])
                if is_target_role(job["title"]):
                    matched.append(job)
                    page_matches += 1
                    print(f"  MATCH: {job['title']}")

            print(f"  Matches this page: {page_matches}")

        await browser.close()

    return matched


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("Amazon Jobs Scanner — University Programs · US · Most Recent")
    print("=" * 60)

    jobs = await scrape_jobs()

    print("\n" + "=" * 60)
    print(f"Total matches: {len(jobs)}")
    for j in jobs:
        print(f"  • {j['title']}")
        print(f"    {j['url']}")
    print("=" * 60)

    previously_seen = load_seen_urls()
    new_jobs = [j for j in jobs if j["url"] not in previously_seen]
    print(f"New roles (not seen before): {len(new_jobs)}")

    save_seen_urls(previously_seen | {j["url"] for j in jobs})

    if not new_jobs:
        print("No new roles — skipping email.")
    else:
        print(f"\nSending email ({len(new_jobs)} new role(s))...")
        send_email(jobs, previously_seen)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
