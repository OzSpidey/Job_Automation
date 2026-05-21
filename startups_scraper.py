"""
Startups Jobs Scraper
---------------------
Three sources in one script:
  1. HN "Who is Hiring"    — Algolia HN API (monthly thread)
  2. YC Work at a Startup  — Playwright (headless Chromium)
  3. Builtin               — BeautifulSoup HTML scraping

Target roles: Data Analyst, Data Engineer, Analytics Engineer,
              Business Intelligence, ML Engineer, Data Scientist,
              Software Engineer, AI Engineer
Filter:  US / Remote, entry-to-mid level (no senior/lead/manager)
"""

import asyncio
import csv
import html as html_lib
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENTS      = [e.strip() for e in os.environ.get("EMAIL_TO", "").split(",") if e.strip()]

SEEN_FILE  = Path(__file__).parent / "json" / "startups_seen_jobs.json"
MASTER_CSV = Path(__file__).parent / "csv"  / "new_jobs.csv"

# ── Filters ───────────────────────────────────────────────────────────────────
ALLOWED_TITLES = re.compile(
    r"\b(data\s+engineer|data\s+analyst|analytics\s+engineer|analytics\s+analyst"
    r"|business\s+intelligence|machine\s+learning\s+engineer|ml\s+engineer"
    r"|data\s+scientist|ai\s+engineer|software\s+developer|software\s+engineer)\b",
    re.I,
)
SKIP_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|lead|manager|principal|staff|head|director|vp|ii|iii|iv)\b", re.I
)

_US_STATES = (
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    r"MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
US_LOCATION_RE = re.compile(
    rf"\b(united\s+states|usa|u\.s\.a?\.?|remote|{_US_STATES})\b", re.I
)

# HN text is free-form — cast a wider net
HN_ROLE_RE = re.compile(
    r"\b(data\s+engineer|data\s+analyst|analytics\s+engineer|analytics\s+analyst"
    r"|business\s+intelligence|\bBI\b|machine\s+learning|ml\s+engineer"
    r"|data\s+scientist|ai\s+engineer|software\s+engineer|software\s+developer)\b",
    re.I,
)
HN_US_RE = re.compile(
    rf"remote|worldwide|{_US_STATES}"
    r"|new\s+york|san\s+francisco|seattle|boston|austin|chicago"
    r"|los\s+angeles|denver|atlanta|miami|washington|us\s+only|united\s+states",
    re.I,
)

_MASTER_COLS    = ["source", "job_id", "title", "company", "location", "role", "posted", "url", "found_at"]
_MASTER_ROLE_RE = re.compile(r"data\s+analyst|data\s+engineer|business\s+intelligence", re.I)


def _classify_master_role(title: str) -> str:
    if re.search(r"data\s+engineer",         title, re.I): return "Data Engineer"
    if re.search(r"data\s+analyst",          title, re.I): return "Data Analyst"
    if re.search(r"business\s+intelligence", title, re.I): return "Business Intelligence"
    return ""


def is_allowed_title(title: str) -> bool:
    if SKIP_TITLE_RE.search(title):
        return False
    return bool(ALLOWED_TITLES.search(title))


def is_us_location(location: str) -> bool:
    if not location.strip():
        return True
    return bool(US_LOCATION_RE.search(location))


def posted_label(ts: float) -> str:
    if not ts:
        return "—"
    try:
        days = (datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)).days
        if days == 0:  return "Today"
        if days == 1:  return "1 day ago"
        if days < 7:   return f"{days} days ago"
        if days < 14:  return "1 week ago"
        return f"{days // 7} weeks ago"
    except Exception:
        return "—"


# ── Persistence ───────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(ids: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


# ── Master CSV ────────────────────────────────────────────────────────────────

def _append_master_csv(rows: list[dict]) -> None:
    if not rows:
        return
    existing: set[str] = set()
    if MASTER_CSV.exists():
        with open(MASTER_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing.add(f"{r.get('source','')}:{r.get('job_id','')}")
    new_rows = [r for r in rows if f"{r['source']}:{r['job_id']}" not in existing]
    if not new_rows:
        return
    MASTER_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not MASTER_CSV.exists() or MASTER_CSV.stat().st_size == 0
    with open(MASTER_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_MASTER_COLS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)
    print(f"[+] Master CSV: appended {len(new_rows)} job(s)")
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a") as f:
            f.write("MASTER_CSV_UPDATED=true\n")


# ── Source 1: HN "Who is Hiring" (Algolia API) ───────────────────────────────

def _strip_html(text: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", text)).strip()


def _parse_hn_post(text: str) -> tuple[str, str, str]:
    """Extract (title, company, location) from a free-form HN job post."""
    lines = [l.strip() for l in text.replace("\n", "\n").splitlines() if l.strip()]
    first = lines[0] if lines else text[:200]
    parts = [p.strip() for p in re.split(r"\s*\|\s*", first)]

    company  = parts[0][:80]  if len(parts) >= 1 else ""
    title    = parts[1][:120] if len(parts) >= 2 else ""
    location = parts[2][:80]  if len(parts) >= 3 else ""

    # If no pipe-separated format, try to pull role from body text
    if not title:
        m = HN_ROLE_RE.search(text)
        title = m.group(0) if m else first[:80]

    return title, company, location


async def fetch_hn_jobs(client: httpx.AsyncClient) -> list[dict]:
    print("  [HN] Fetching latest 'Who is Hiring' thread...")
    try:
        resp = await client.get(
            "https://hn.algolia.com/api/v1/search_by_date"
            "?query=Ask+HN%3A+Who+is+hiring%3F"
            "&tags=ask_hn,author_whoishiring&hitsPerPage=1",
            timeout=15,
        )
        hits = resp.json().get("hits", [])
        if not hits:
            print("  [HN] No thread found.")
            return []

        thread_id    = hits[0]["objectID"]
        thread_title = hits[0].get("title", "")
        print(f"  [HN] Thread: {thread_title} (id={thread_id})")

        resp = await client.get(
            f"https://hn.algolia.com/api/v1/items/{thread_id}",
            timeout=30,
        )
        thread   = resp.json()
        children = thread.get("children", [])
        print(f"  [HN] {len(children)} top-level postings to scan")

    except Exception as e:
        print(f"  [HN] Error: {e}")
        return []

    jobs: list[dict] = []
    for child in children:
        if child.get("type") != "comment":
            continue
        text_html = child.get("text") or ""
        text      = _strip_html(text_html)
        if not text:
            continue
        if not HN_ROLE_RE.search(text):
            continue
        if not HN_US_RE.search(text):
            continue

        title, company, location = _parse_hn_post(text)
        jobs.append({
            "_source":   "hn",
            "id":        f"hn:{child['id']}",
            "title":     title,
            "company":   company,
            "location":  location,
            "url":       f"https://news.ycombinator.com/item?id={child['id']}",
            "posted_ts": child.get("created_at_i", 0),
            "snippet":   text[:220],
        })

    print(f"  [HN] {len(jobs)} matched")
    return jobs


# ── Source 2: YC Work at a Startup (Playwright) ───────────────────────────────

YC_QUERIES = [
    "data analyst",
    "data engineer",
    "analytics engineer",
    "business intelligence",
    "data scientist",
    "machine learning engineer",
]

async def fetch_yc_jobs() -> list[dict]:
    print("  [YC] Launching Playwright...")
    jobs: list[dict] = []
    seen_yc_ids: set[str] = set()

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            for query in YC_QUERIES:
                url = (
                    "https://www.workatastartup.com/jobs"
                    f"?q={query.replace(' ', '+')}"
                    "&jobType=fulltime&location=US"
                )
                print(f"  [YC] Searching: {query!r}")
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30_000)
                except Exception:
                    # networkidle timeout is common on React apps — proceed anyway
                    await page.wait_for_timeout(4000)

                # Scroll to trigger lazy-loaded content
                for _ in range(3):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(1200)

                # Extract all job links and their card context via JS
                raw: list[dict] = await page.evaluate("""
                    () => {
                        const results = [];
                        const seen = new Set();
                        for (const link of document.querySelectorAll('a[href]')) {
                            const href = link.getAttribute('href') || '';
                            const m = href.match(/\\/jobs\\/(\\d+)/);
                            if (!m) continue;
                            const jobId = m[1];
                            if (seen.has(jobId)) continue;
                            seen.add(jobId);
                            const title = link.textContent.trim();
                            if (!title || title.length < 4) continue;
                            // Walk up DOM to find the containing card
                            let card = link.parentElement;
                            for (let i = 0; i < 7 && card && card.tagName !== 'BODY'; i++) {
                                if (card.children.length >= 2) break;
                                card = card.parentElement;
                            }
                            results.push({
                                id:        jobId,
                                title:     title,
                                card_text: card ? card.innerText : '',
                                url:       'https://www.workatastartup.com/jobs/' + jobId,
                            });
                        }
                        return results;
                    }
                """)

                for item in raw:
                    job_id = f"yc:{item['id']}"
                    if job_id in seen_yc_ids:
                        continue
                    seen_yc_ids.add(job_id)

                    title     = item["title"].strip()
                    card_text = item.get("card_text", "")
                    lines     = [l.strip() for l in card_text.splitlines() if l.strip()]

                    # First non-title line that isn't the job title is usually company name
                    company = ""
                    for line in lines:
                        if line.lower() != title.lower() and len(line) > 1:
                            company = line[:80]
                            break

                    # Extract location from card text
                    location = ""
                    for line in lines:
                        if US_LOCATION_RE.search(line) or "remote" in line.lower():
                            location = line[:80]
                            break

                    jobs.append({
                        "_source":   "yc",
                        "id":        job_id,
                        "title":     title,
                        "company":   company,
                        "location":  location,
                        "url":       item["url"],
                        "posted_ts": 0,
                        "snippet":   card_text[:220],
                    })

            await browser.close()

    except Exception as e:
        print(f"  [YC] Fatal error: {e}")

    print(f"  [YC] {len(jobs)} candidates extracted")
    return jobs


# ── Source 3: Builtin (BeautifulSoup) ─────────────────────────────────────────

BUILTIN_CATEGORIES = ["data-analytics", "data-engineering"]
BUILTIN_PAGES      = 3

async def fetch_builtin_jobs(client: httpx.AsyncClient) -> list[dict]:
    print("  [Builtin] Scraping job listings...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    jobs: list[dict]     = []
    seen_builtin: set[str] = set()

    for category in BUILTIN_CATEGORIES:
        for page_num in range(1, BUILTIN_PAGES + 1):
            url = f"https://builtin.com/jobs/{category}?page={page_num}"
            try:
                resp = await client.get(url, headers=headers, timeout=15)
                if resp.status_code != 200:
                    print(f"  [Builtin] {url} → HTTP {resp.status_code}")
                    continue
            except Exception as e:
                print(f"  [Builtin] Fetch error ({url}): {e}")
                continue

            soup = BeautifulSoup(resp.text, "lxml")

            # Job links follow the pattern /job/{slug}/{numeric-id}
            job_links = soup.find_all("a", href=re.compile(r"/job/[^/]+/\d+"))
            for link in job_links:
                href = link.get("href", "")
                m = re.search(r"/job/[^/]+/(\d+)", href)
                if not m:
                    continue
                job_id_str = f"builtin:{m.group(1)}"
                if job_id_str in seen_builtin:
                    continue
                seen_builtin.add(job_id_str)

                title = link.get_text(strip=True)
                if not title:
                    continue

                # Walk up to a meaningful card container
                card = link.parent
                for _ in range(6):
                    if card is None:
                        break
                    if len(card.get_text(strip=True)) > 60:
                        break
                    card = card.parent

                card_text = card.get_text(" ", strip=True) if card else ""

                # Company name is usually the first /company/ link inside the card
                company_el = card.find("a", href=re.compile(r"/company/")) if card else None
                company    = company_el.get_text(strip=True) if company_el else ""

                # Location: look for Remote / Hybrid / city, ST pattern
                loc_m = re.search(
                    r"\b(remote|hybrid|in.office|[A-Z][a-z]+(?: [A-Z][a-z]+)?,\s*[A-Z]{2})\b",
                    card_text,
                    re.I,
                )
                location = loc_m.group(0) if loc_m else ""

                jobs.append({
                    "_source":   "builtin",
                    "id":        job_id_str,
                    "title":     title,
                    "company":   company,
                    "location":  location,
                    "url":       f"https://builtin.com{href}",
                    "posted_ts": 0,
                    "snippet":   card_text[:220],
                })

            await asyncio.sleep(0.6)

    print(f"  [Builtin] {len(jobs)} candidates extracted")
    return jobs


# ── Email ─────────────────────────────────────────────────────────────────────

_SRC_LABEL = {"hn": "HN", "yc": "YC", "builtin": "Builtin"}
_SRC_COLOR = {"hn": "#ff6600", "yc": "#fb651e", "builtin": "#0066cc"}


def send_email(all_jobs: list[dict], new_ids: set) -> None:
    new_count  = len(new_ids)
    seen_count = len(all_jobs) - new_count
    subject    = f"[Startups Scanner] {new_count} New Role(s) Found"

    all_jobs = sorted(
        all_jobs,
        key=lambda j: (j["id"] in new_ids, j.get("posted_ts", 0)),
        reverse=True,
    )

    rows_html = []
    for j in all_jobs:
        src      = j.get("_source", "")
        is_new   = j["id"] in new_ids
        src_badge = (
            f'<span style="background:{_SRC_COLOR.get(src, "#888")};color:#fff;'
            f'padding:2px 6px;border-radius:3px;font-size:10px;'
            f'font-weight:bold;margin-right:5px;">'
            f'{_SRC_LABEL.get(src, src.upper())}</span>'
        )
        new_badge = (
            '<span style="background:#2ecc71;color:#fff;padding:2px 7px;'
            'border-radius:4px;font-size:11px;font-weight:bold;margin-left:5px;">NEW</span>'
            if is_new else ""
        )
        row_bg = "background:#f0fff4;" if is_new else ""
        rows_html.append(
            f'<tr style="{row_bg}">'
            f'<td style="padding:8px;border:1px solid #ddd;">{src_badge}{j["title"]}{new_badge}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j.get("company", "")}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location", "")}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{posted_label(j.get("posted_ts", 0))}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;"><a href="{j["url"]}">View</a></td>'
            f'</tr>'
        )

    counts_by_src = {
        "HN":      sum(1 for j in all_jobs if j.get("_source") == "hn"),
        "YC":      sum(1 for j in all_jobs if j.get("_source") == "yc"),
        "Builtin": sum(1 for j in all_jobs if j.get("_source") == "builtin"),
    }

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333">
    <h2 style="color:#4a4a4a">Startups Jobs — Digest</h2>
    <p><strong style="color:#2ecc71">{new_count} new</strong> role(s) &nbsp;|&nbsp;
       {seen_count} already seen &nbsp;|&nbsp; US / Remote</p>
    <p style="font-size:12px;color:#666;">
       HN: {counts_by_src['HN']} &nbsp;·&nbsp;
       YC: {counts_by_src['YC']} &nbsp;·&nbsp;
       Builtin: {counts_by_src['Builtin']}</p>
    <p style="font-size:12px;color:#666;">
       Data Engineer &nbsp;·&nbsp; Data Analyst &nbsp;·&nbsp; Analytics Engineer &nbsp;·&nbsp;
       BI &nbsp;·&nbsp; ML Engineer &nbsp;·&nbsp; Data Scientist &nbsp;·&nbsp;
       AI Engineer &nbsp;·&nbsp; SWE</p>
    <table style="border-collapse:collapse;width:100%;max-width:1300px">
      <tr style="background:#4a4a4a;color:#fff">
        <th style="padding:10px;border:1px solid #555;text-align:left;">Role</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Company</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Location</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Posted</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Link</th>
      </tr>
      {"".join(rows_html)}
    </table>
    <p style="font-size:12px;color:#888;margin-top:20px">
      Sources: HN Who's Hiring · YC Work at a Startup · Builtin
    </p>
    </body></html>
    """

    plain = f"Startups Jobs — {new_count} new role(s):\n\n"
    for j in all_jobs:
        tag = "[NEW] " if j["id"] in new_ids else "      "
        src = _SRC_LABEL.get(j.get("_source", ""), "?")
        plain += (
            f"{tag}[{src}] {j['title']} @ {j.get('company', '?')} "
            f"| {j.get('location', '')}\n  {j['url']}\n\n"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, RECIPIENTS, msg.as_string())

    print(f"[email] Sent — {new_count} new role(s).")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 55)
    print("  Startups Jobs Scraper")
    print("  Sources: HN · YC Work at a Startup · Builtin")
    print("=" * 55)

    previously_seen = load_seen()

    # HN and Builtin share an httpx client; YC runs Playwright separately
    async with httpx.AsyncClient(follow_redirects=True) as client:
        hn_jobs, builtin_jobs = await asyncio.gather(
            fetch_hn_jobs(client),
            fetch_builtin_jobs(client),
        )

    yc_jobs = await fetch_yc_jobs()

    all_raw = hn_jobs + yc_jobs + builtin_jobs
    print(f"\n  Raw totals — HN: {len(hn_jobs)}  YC: {len(yc_jobs)}  Builtin: {len(builtin_jobs)}")

    # Filter and dedup
    matched:    list[dict] = []
    seen_dedup: set[str]   = set()

    for j in all_raw:
        title    = j.get("title", "").strip()
        location = j.get("location", "")
        src      = j.get("_source", "")
        job_id   = j["id"]

        if job_id in seen_dedup:
            continue

        # HN: role was already filtered inside fetch_hn_jobs
        if src != "hn":
            if not is_allowed_title(title):
                continue
            if not is_us_location(location):
                continue

        seen_dedup.add(job_id)
        matched.append(j)

    new_ids = {j["id"] for j in matched if j["id"] not in previously_seen}

    print(f"  Matched (after filters): {len(matched)}")
    print(f"  Already seen:            {len(matched) - len(new_ids)}")
    print(f"  New:                     {len(new_ids)}")

    for j in matched:
        src = _SRC_LABEL.get(j.get("_source", ""), "?")
        tag = "[NEW]" if j["id"] in new_ids else "     "
        print(f"\n  {tag} [{src}] {j['title']}")
        print(f"    Company:  {j.get('company', '')}")
        print(f"    Location: {j.get('location', '')}")
        print(f"    URL:      {j['url']}")

    # Master CSV — DA/DE/BI new jobs only
    now_str     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    master_rows = []
    for j in matched:
        if j["id"] not in new_ids:
            continue
        title = j.get("title", "")
        if not _MASTER_ROLE_RE.search(title):
            continue
        master_rows.append({
            "source":   j.get("_source", "startups"),
            "job_id":   j["id"],
            "title":    title,
            "company":  j.get("company", ""),
            "location": j.get("location", ""),
            "role":     _classify_master_role(title),
            "posted":   "",
            "url":      j["url"],
            "found_at": now_str,
        })
    _append_master_csv(master_rows)

    if not new_ids:
        print("\n  No new roles since last run — skipping email.")
    else:
        print(f"\n  Sending email ({len(new_ids)} new, {len(matched)} total)...")
        send_email(matched, new_ids)
        save_seen(previously_seen | new_ids)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
