"""
Startups Jobs Scraper
---------------------
Three sources in one script:
  1. HN "Who is Hiring"    — Algolia HN API (monthly thread)
  2. YC Work at a Startup  — Playwright (headless Chromium)
  3. Builtin               — Playwright (headless Chromium)

Target roles: Data Analyst, Data Engineer, Analytics Engineer,
              Analytics Analyst, Business Intelligence, AI Engineer
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
    r"|business\s+intelligence|ai\s+engineer)\b",
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

HN_ROLE_RE = re.compile(
    r"\b(data\s+engineer|data\s+analyst|analytics\s+engineer|analytics\s+analyst"
    r"|business\s+intelligence|\bBI\b|ai\s+engineer)\b",
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
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    first = lines[0] if lines else text[:200]
    parts = [p.strip() for p in re.split(r"\s*\|\s*", first)]

    company  = parts[0][:80]  if len(parts) >= 1 else ""
    title    = parts[1][:120] if len(parts) >= 2 else ""
    location = parts[2][:80]  if len(parts) >= 3 else ""

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
        ts = child.get("created_at_i", 0)
        jobs.append({
            "_source":    "hn",
            "id":         f"hn:{child['id']}",
            "title":      title,
            "company":    company,
            "location":   location,
            "url":        f"https://news.ycombinator.com/item?id={child['id']}",
            "posted_ts":  ts,
            "posted_str": posted_label(ts),
        })

    print(f"  [HN] {len(jobs)} matched")
    return jobs


# ── Sources 2 & 3: YC + Builtin (shared Playwright session) ──────────────────

YC_QUERIES = [
    "data analyst",
    "data engineer",
    "analytics engineer",
    "business intelligence",
    "ai engineer",
]

BUILTIN_CATEGORIES = ["data-analytics", "data-engineering"]
BUILTIN_PAGES      = 3

_PW_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


async def _scrape_yc(page) -> list[dict]:
    jobs: list[dict]     = []
    seen_ids: set[str]   = set()

    for query in YC_QUERIES:
        url = (
            "https://www.workatastartup.com/jobs"
            f"?q={query.replace(' ', '+')}&jobType=fulltime&location=US"
        )
        print(f"  [YC] Searching: {query!r}")
        try:
            await page.goto(url, wait_until="networkidle", timeout=30_000)
        except Exception:
            await page.wait_for_timeout(4000)

        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

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
                    const rawText = (link.innerText || link.textContent || '').trim();
                    const title = rawText.split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
                    if (!title || title.length < 4) continue;
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
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            title     = item["title"].strip()
            card_text = item.get("card_text", "")
            lines     = [l.strip() for l in card_text.splitlines() if l.strip()]

            company = ""
            for line in lines:
                if line.lower() != title.lower() and len(line) > 1:
                    company = line[:80]
                    break

            location = ""
            for line in lines:
                if US_LOCATION_RE.search(line) or "remote" in line.lower():
                    location = line[:80]
                    break

            jobs.append({
                "_source":    "yc",
                "id":         job_id,
                "title":      title,
                "company":    company,
                "location":   location,
                "url":        item["url"],
                "posted_ts":  0,
                "posted_str": "—",
            })

    print(f"  [YC] {len(jobs)} candidates extracted")
    return jobs


async def _scrape_builtin(page) -> list[dict]:
    jobs: list[dict]     = []
    seen_ids: set[str]   = set()

    for category in BUILTIN_CATEGORIES:
        for page_num in range(1, BUILTIN_PAGES + 1):
            url = f"https://builtin.com/jobs/{category}?page={page_num}"
            print(f"  [Builtin] {url}")
            try:
                await page.goto(url, wait_until="networkidle", timeout=30_000)
            except Exception:
                await page.wait_for_timeout(3000)

            await page.wait_for_timeout(1500)

            raw: list[dict] = await page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();

                    // Anchor on span.font-barlow "Posted X Ago" — guaranteed to be
                    // inside each job card. Walk up from there to find the container
                    // that holds both the job link and the company link.
                    for (const span of document.querySelectorAll('span.font-barlow')) {
                        const postedText = (span.textContent || '').trim();
                        if (!/^Posted /i.test(postedText)) continue;

                        // Walk up until we find a node containing both /job/ and /company/ links
                        let card = span.parentElement;
                        while (card && card.tagName !== 'BODY') {
                            if (card.querySelector('a[href*="/job/"]') &&
                                card.querySelector('a[href*="/company/"]')) break;
                            card = card.parentElement;
                        }
                        if (!card || card.tagName === 'BODY') continue;

                        const jobLink = card.querySelector('a[href*="/job/"]');
                        if (!jobLink) continue;
                        const href = jobLink.getAttribute('href') || '';
                        const m = href.match(/\\/job\\/[^/]+\\/(\\d+)/);
                        if (!m) continue;
                        const jobId = m[1];
                        if (seen.has(jobId)) continue;
                        seen.add(jobId);

                        // Title — first non-empty line of the job link text
                        const rawText = (jobLink.innerText || jobLink.textContent || '').trim();
                        const title = rawText.split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
                        if (!title || title.length < 4) continue;

                        // Company
                        const compLink = card.querySelector('a[href*="/company/"]');
                        const company  = compLink ? (compLink.innerText || compLink.textContent || '').trim() : '';

                        // Location
                        const cardText = card.innerText || '';
                        const locM = cardText.match(/\\b(remote|hybrid|in[- ]office)\\b/i);
                        const location = locM ? locM[0] : '';

                        results.push({
                            id: jobId, title, company, location,
                            posted: postedText,
                            url: 'https://builtin.com' + href,
                        });
                    }
                    return results;
                }
            """)

            for item in raw:
                job_id = f"builtin:{item['id']}"
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                jobs.append({
                    "_source":    "builtin",
                    "id":         job_id,
                    "title":      item["title"].strip(),
                    "company":    item.get("company", ""),
                    "location":   item.get("location", ""),
                    "url":        item["url"],
                    "posted_ts":  0,
                    "posted_str": item.get("posted", "—") or "—",
                })

    print(f"  [Builtin] {len(jobs)} candidates extracted")
    return jobs


async def fetch_playwright_jobs() -> tuple[list[dict], list[dict]]:
    print("  [Playwright] Launching browser...")
    yc_jobs:      list[dict] = []
    builtin_jobs: list[dict] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=_PW_UA)
            page    = await context.new_page()

            yc_jobs      = await _scrape_yc(page)
            builtin_jobs = await _scrape_builtin(page)

            await browser.close()
    except Exception as e:
        print(f"  [Playwright] Fatal error: {e}")
    return yc_jobs, builtin_jobs


# ── Email (3 tables) ──────────────────────────────────────────────────────────

def _make_table(jobs: list[dict], new_ids: set, show_posted_str: bool = False) -> str:
    if not jobs:
        return '<p style="color:#888;font-size:13px;">No matching roles found.</p>'

    rows = []
    for j in sorted(jobs, key=lambda x: (x["id"] in new_ids, x.get("posted_ts", 0)), reverse=True):
        is_new    = j["id"] in new_ids
        new_badge = (
            '<span style="background:#2ecc71;color:#fff;padding:2px 6px;'
            'border-radius:4px;font-size:11px;font-weight:bold;margin-left:5px;">NEW</span>'
            if is_new else ""
        )
        row_bg  = "background:#f0fff4;" if is_new else ""
        posted  = j.get("posted_str", "—") if show_posted_str else posted_label(j.get("posted_ts", 0))
        rows.append(
            f'<tr style="{row_bg}">'
            f'<td style="padding:8px;border:1px solid #ddd;">{j["title"]}{new_badge}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j.get("company","")}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{j.get("location","")}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{posted}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;"><a href="{j["url"]}">View</a></td>'
            f'</tr>'
        )

    header = (
        '<tr style="background:#4a4a4a;color:#fff">'
        '<th style="padding:10px;border:1px solid #555;text-align:left;">Role</th>'
        '<th style="padding:10px;border:1px solid #555;text-align:left;">Company</th>'
        '<th style="padding:10px;border:1px solid #555;text-align:left;">Location</th>'
        '<th style="padding:10px;border:1px solid #555;text-align:left;">Posted</th>'
        '<th style="padding:10px;border:1px solid #555;text-align:left;">Link</th>'
        '</tr>'
    )
    return (
        f'<table style="border-collapse:collapse;width:100%;max-width:1300px;margin-bottom:30px">'
        f'{header}{"".join(rows)}</table>'
    )


def _section(title: str, color: str, jobs: list[dict], new_ids: set, show_posted_str: bool = False) -> str:
    new_count = sum(1 for j in jobs if j["id"] in new_ids)
    return (
        f'<h3 style="color:{color};margin-top:30px;margin-bottom:6px;">'
        f'{title} &nbsp;<span style="font-size:13px;color:#555;font-weight:normal;">'
        f'{new_count} new · {len(jobs)} total</span></h3>'
        + _make_table(jobs, new_ids, show_posted_str)
    )


def send_email(hn_jobs: list[dict], yc_jobs: list[dict], builtin_jobs: list[dict], new_ids: set) -> None:
    total_new = len(new_ids)
    subject   = f"[Startups Scanner] {total_new} New Role(s) Found"

    roles_line = (
        "Data Engineer &nbsp;·&nbsp; Data Analyst &nbsp;·&nbsp; Analytics Engineer &nbsp;·&nbsp;"
        "Analytics Analyst &nbsp;·&nbsp; Business Intelligence &nbsp;·&nbsp; AI Engineer"
    )

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;max-width:1350px">
    <h2 style="color:#4a4a4a">Startups Jobs — Digest</h2>
    <p><strong style="color:#2ecc71">{total_new} new</strong> role(s) &nbsp;|&nbsp; US / Remote</p>
    <p style="font-size:12px;color:#666;">{roles_line}</p>

    {_section("HN — Who's Hiring", "#ff6600", hn_jobs, new_ids, show_posted_str=False)}
    {_section("YC Work at a Startup", "#fb651e", yc_jobs, new_ids, show_posted_str=False)}
    {_section("Builtin", "#0066cc", builtin_jobs, new_ids, show_posted_str=True)}

    <p style="font-size:12px;color:#888;margin-top:20px">
      Sources: HN Who's Hiring · YC Work at a Startup · Builtin
    </p>
    </body></html>
    """

    all_jobs = hn_jobs + yc_jobs + builtin_jobs
    plain    = f"Startups Jobs — {total_new} new role(s):\n\n"
    for j in sorted(all_jobs, key=lambda x: x["id"] in new_ids, reverse=True):
        tag = "[NEW] " if j["id"] in new_ids else "      "
        src = {"hn": "HN", "yc": "YC", "builtin": "Builtin"}.get(j.get("_source", ""), "?")
        plain += f"{tag}[{src}] {j['title']} @ {j.get('company','?')} | {j.get('location','')}\n  {j['url']}\n\n"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, RECIPIENTS, msg.as_string())

    print(f"[email] Sent — {total_new} new role(s).")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 55)
    print("  Startups Jobs Scraper")
    print("  Sources: HN · YC Work at a Startup · Builtin")
    print("=" * 55)

    previously_seen = load_seen()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        hn_jobs = await fetch_hn_jobs(client)

    yc_jobs, builtin_jobs = await fetch_playwright_jobs()

    print(f"\n  Raw — HN: {len(hn_jobs)}  YC: {len(yc_jobs)}  Builtin: {len(builtin_jobs)}")

    def _filter(jobs: list[dict]) -> list[dict]:
        out: list[dict] = []
        seen_dedup: set[str] = set()
        for j in jobs:
            if j["id"] in seen_dedup:
                continue
            if j.get("_source") != "hn":
                if not is_allowed_title(j.get("title", "")):
                    continue
                if not is_us_location(j.get("location", "")):
                    continue
            seen_dedup.add(j["id"])
            out.append(j)
        return out

    hn_jobs      = _filter(hn_jobs)
    yc_jobs      = _filter(yc_jobs)
    builtin_jobs = _filter(builtin_jobs)

    all_matched = hn_jobs + yc_jobs + builtin_jobs
    new_ids     = {j["id"] for j in all_matched if j["id"] not in previously_seen}

    print(f"  Matched — HN: {len(hn_jobs)}  YC: {len(yc_jobs)}  Builtin: {len(builtin_jobs)}")
    print(f"  New: {len(new_ids)}")

    for j in sorted(all_matched, key=lambda x: x["id"] in new_ids, reverse=True):
        src = {"hn": "HN", "yc": "YC", "builtin": "Builtin"}.get(j.get("_source", ""), "?")
        tag = "[NEW]" if j["id"] in new_ids else "     "
        print(f"  {tag} [{src}] {j['title']} @ {j.get('company','')} | {j.get('location','')} | {j.get('posted_str','')}")

    # Master CSV
    now_str     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    master_rows = []
    for j in all_matched:
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
            "posted":   j.get("posted_str", ""),
            "url":      j["url"],
            "found_at": now_str,
        })
    _append_master_csv(master_rows)

    if not new_ids:
        print("\n  No new roles since last run — skipping email.")
    else:
        print(f"\n  Sending email ({len(new_ids)} new)...")
        send_email(hn_jobs, yc_jobs, builtin_jobs, new_ids)
        save_seen(previously_seen | new_ids)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
