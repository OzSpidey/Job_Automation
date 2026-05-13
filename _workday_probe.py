#!/usr/bin/env python3
"""Probe a list of slugs for live Workday career portals.

For each slug, tries every (instance, career_path) combination by POSTing to
the Workday jobs API. The first combination that returns HTTP 200 is recorded
as a hit. Writes one row per slug (hit or NO_HIT) to CSV.

Status code semantics (discovered by probing real Workday):
- 200: real tenant + correct career path + correct instance -> HIT
- 404: tenant exists on this instance but career path is wrong -> try next path
- 422: wrong instance (or fake tenant) -> skip this instance entirely
- 406: Workday edge accepts ALL subdomains; root path is useless for probing.
       Must POST to /wday/cxs/{tenant}/{career}/jobs to get a real signal.

Usage
-----
    # Full sweep (designed for GitHub Actions runner)
    python _workday_probe.py --in _workday_candidates.txt --out _workday_hits.csv

    # Local smoke test - first 500 slugs, low concurrency, safer
    python _workday_probe.py --in _workday_candidates.txt --limit 500 --concurrency 20

    # Resume from offset
    python _workday_probe.py --in _workday_candidates.txt --start 10000 --limit 5000

Output CSV columns: slug, instance, career, status, careers_url
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import smtplib
import sys
import time
from datetime import datetime
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path

try:
    import aiohttp
except ImportError:
    sys.exit("Missing dependency. Run: pip install aiohttp")

WD_INSTANCES = ["wd1", "wd3", "wd5", "wd12", "wd14", "wd501"]
CAREER_PATHS = [
    "External_Career_Site",
    "careers",
    "Careers",
    "External",
    "external",
    "JobBoard",
    "CareersExternal",
]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
API_PAYLOAD = json.dumps({
    "searchText": "",
    "limit": 1,
    "offset": 0,
    "locations": [],
    "appliedFacets": {},
})
API_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": UA,
}

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")


def api_url(slug: str, instance: str, career: str) -> str:
    return (f"https://{slug}.{instance}.myworkdayjobs.com"
            f"/wday/cxs/{slug}/{career}/jobs")


def portal_url(slug: str, instance: str, career: str) -> str:
    return f"https://{slug}.{instance}.myworkdayjobs.com/en-US/{career}"


async def probe_endpoint(session, slug, instance, career, sem):
    """POST to the jobs API. Returns HTTP status code, or None on transport error."""
    url = api_url(slug, instance, career)
    async with sem:
        try:
            async with session.post(url, data=API_PAYLOAD) as r:
                # Drain the body to free the connection
                await r.read()
                return r.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return None


def dynamic_career_paths(slug: str) -> list[str]:
    """Generate plausible career-path slugs from the tenant name."""
    s = slug
    cap = slug.capitalize()
    return [s, cap, f"{s}Careers", f"{cap}Careers",
            f"{s}_Careers", f"{cap}_Careers",
            f"{s}_Career_Site", f"{cap}_Career_Site",
            "WD", "CorporateCareers"]


async def probe_company(session, slug, sem, results, progress, lock):
    """Probe every instance. Output one row per slug with one of:
        200          -> confirmed working URL (careers_url populated)
        TENANT_EXISTS -> 404 across our career-path attempts (tenant is real
                         on this instance but uses a custom career slug)
        NO_HIT        -> 422/error across all instances (not a Workday tenant)
    """
    confirmed = None         # (instance, career)  for status=200
    tenant_exists = None     # instance            for status=TENANT_EXISTS

    for instance in WD_INSTANCES:
        first_status = await probe_endpoint(session, slug, instance, CAREER_PATHS[0], sem)

        if first_status == 200:
            confirmed = (instance, CAREER_PATHS[0])
            break
        if first_status == 422 or first_status is None:
            continue
        if first_status == 404:
            # Tenant exists on this instance. Try other known + dynamic paths.
            tenant_exists = tenant_exists or instance
            extra_paths = list(dict.fromkeys(CAREER_PATHS[1:] + dynamic_career_paths(slug)))
            for career in extra_paths:
                s = await probe_endpoint(session, slug, instance, career, sem)
                if s == 200:
                    confirmed = (instance, career)
                    break
            if confirmed:
                break
        # else 5xx / weird: try next instance

    async with lock:
        if confirmed:
            inst, career = confirmed
            results.append((slug, inst, career, "200", portal_url(slug, inst, career)))
            print(f"  HIT  {slug:<40s} {inst}/{career}", flush=True)
        elif tenant_exists:
            results.append((slug, tenant_exists, "", "TENANT_EXISTS", ""))
            print(f"  ??   {slug:<40s} {tenant_exists} (custom career path)", flush=True)
        else:
            results.append((slug, "", "", "NO_HIT", ""))

        progress["done"] += 1
        if progress["done"] % 100 == 0:
            elapsed = time.time() - progress["t0"]
            rate = progress["done"] / max(elapsed, 0.1)
            eta = (progress["total"] - progress["done"]) / max(rate, 0.1)
            hits = sum(1 for r in results if r[3] == "200")
            partial = sum(1 for r in results if r[3] == "TENANT_EXISTS")
            print(
                f"[{progress['done']}/{progress['total']}] "
                f"hits={hits} tenant_exists={partial} "
                f"rate={rate:.1f}/s eta={eta/60:.1f}min",
                flush=True,
            )


def send_email(csv_path: Path) -> None:
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set — skipping email.")
        return
    if not csv_path.exists():
        print(f"[!] CSV not found: {csv_path} — skipping email.")
        return

    rows = list(csv.reader(csv_path.open(encoding="utf-8")))
    header, data = rows[0], rows[1:]
    hits = [r for r in data if r[3] == "200"]
    partial = [r for r in data if r[3] == "TENANT_EXISTS"]
    misses = len(data) - len(hits) - len(partial)

    hit_rows = "".join(
        f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td>"
        f"<td><a href='{r[4]}'>{r[4]}</a></td></tr>"
        for r in hits[:100]
    )
    partial_rows = "".join(
        f"<tr><td>{r[0]}</td><td>{r[1]}</td>"
        f"<td><i>custom career path - check company careers page</i></td></tr>"
        for r in partial[:50]
    )
    subject = (
        f"Workday Tenant Discovery — {len(hits)} confirmed / "
        f"{len(partial)} tenant-only / {len(data)} probed "
        f"({datetime.now().strftime('%b %d %H:%M')})"
    )
    body = f"""
    <h2 style="font-family:sans-serif">Workday Tenant Discovery</h2>
    <p>
      <b>{len(hits)}</b> confirmed working URLs<br>
      <b>{len(partial)}</b> tenants exist but use a custom career path
        (returned HTTP 404 — needs manual lookup)<br>
      <b>{misses}</b> not on Workday<br>
      <b>{len(data)}</b> total probed
    </p>
    <p>Full results attached as <code>{csv_path.name}</code>.</p>

    <h3>Confirmed hits (first 100)</h3>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#0a66c2;color:white">
        <th>Slug</th><th>Instance</th><th>Career Path</th><th>Career Portal URL</th>
      </tr>
      {hit_rows or '<tr><td colspan="4">No confirmed hits.</td></tr>'}
    </table>

    <h3 style="margin-top:24px">Tenants exist but career path unknown (first 50)</h3>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#f39c12;color:white">
        <th>Slug</th><th>Instance</th><th>Note</th>
      </tr>
      {partial_rows or '<tr><td colspan="3">None.</td></tr>'}
    </table>

    <p style="font-size:12px;color:#888;margin-top:16px">
      Probed {len(WD_INSTANCES)} Workday data-center prefixes
      ({', '.join(WD_INSTANCES)}) × {len(CAREER_PATHS)} known career paths +
      dynamic patterns derived from each tenant slug.<br>
      Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}.
    </p>
    """

    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "html"))

        part = MIMEBase("application", "octet-stream")
        part.set_payload(csv_path.read_bytes())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{csv_path.name}"',
        )
        msg.attach(part)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, EMAIL_TO, msg.as_string())
        print(f"[+] Email sent → {EMAIL_TO} ({len(hits)} hits, "
              f"{csv_path.stat().st_size} bytes)")
    except Exception as e:
        print(f"[!] Email failed: {e}")


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--in", dest="infile", default="_workday_candidates.txt",
                   help="One slug per line")
    p.add_argument("--out", dest="outfile", default="_workday_hits.csv")
    p.add_argument("--concurrency", type=int, default=50,
                   help="Max simultaneous in-flight requests (default 50)")
    p.add_argument("--start", type=int, default=0, help="Skip first N slugs")
    p.add_argument("--limit", type=int, default=0, help="Process only N slugs")
    p.add_argument("--timeout", type=float, default=15.0,
                   help="Per-request total timeout in seconds")
    p.add_argument("--email", action="store_true",
                   help="Email the results CSV after probing finishes")
    p.add_argument("--send-email-only", action="store_true",
                   help="Skip probing, just email an existing CSV (use --out as input)")
    args = p.parse_args()

    if args.send_email_only:
        send_email(Path(args.outfile))
        return

    infile = Path(args.infile)
    if not infile.exists():
        sys.exit(f"Input file not found: {infile}")

    all_slugs = [
        s.strip() for s in infile.read_text(encoding="utf-8").splitlines()
        if s.strip() and not s.startswith("#")
    ]
    seen = set()
    slugs = [s for s in all_slugs if not (s in seen or seen.add(s))]

    if args.start:
        slugs = slugs[args.start:]
    if args.limit:
        slugs = slugs[:args.limit]

    print(f"Input: {len(all_slugs)} lines, {len(slugs)} after dedupe+slice")
    print(f"Probing × {len(WD_INSTANCES)} instances × {len(CAREER_PATHS)} career paths max "
          f"(worst case ~{len(slugs) * len(WD_INSTANCES) * len(CAREER_PATHS)} requests, "
          f"typical ~{len(slugs) * len(WD_INSTANCES)})")
    print(f"Concurrency: {args.concurrency}  Timeout: {args.timeout}s")
    print()

    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    results: list[tuple] = []
    progress = {"done": 0, "total": len(slugs), "t0": time.time()}

    timeout = aiohttp.ClientTimeout(total=args.timeout, connect=10)
    connector = aiohttp.TCPConnector(
        limit=args.concurrency * 2,
        ttl_dns_cache=300,
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        headers=API_HEADERS,
    ) as session:
        tasks = [
            probe_company(session, s, sem, results, progress, lock)
            for s in slugs
        ]
        await asyncio.gather(*tasks)

    out = Path(args.outfile)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["slug", "instance", "career", "status", "careers_url"])
        for r in sorted(results):
            w.writerow(r)

    elapsed = time.time() - progress["t0"]
    hits = sum(1 for r in results if r[3] == "200")
    partial = sum(1 for r in results if r[3] == "TENANT_EXISTS")
    print()
    print(f"Done in {elapsed/60:.1f}min. {hits} confirmed hits, "
          f"{partial} tenants-exist, {len(results)-hits-partial} no-hits "
          f"-> {out}")

    if args.email:
        send_email(out)


if __name__ == "__main__":
    asyncio.run(main())
