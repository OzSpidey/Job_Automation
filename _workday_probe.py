#!/usr/bin/env python3
"""Probe a list of slugs for live Workday career portals.

For each slug, tries every Workday data-center instance (wd1, wd3, wd5, wd12,
wd14, wd501) and records the first one that returns a Workday page. Writes
hits to CSV.

Usage
-----
    # Full sweep (designed for GitHub Actions runner)
    python _workday_probe.py --in _workday_candidates.txt --out _workday_hits.csv

    # Local smoke test - first 500 slugs, low concurrency, safer
    python _workday_probe.py --in _workday_candidates.txt --limit 500 --concurrency 20

    # Resume from offset
    python _workday_probe.py --in _workday_candidates.txt --start 10000 --limit 5000

Notes
-----
- DNS misses (slug doesn't exist as a Workday tenant) return NXDOMAIN locally
  and never hit Workday's infrastructure. Only confirmed hits make an HTTPS
  round-trip to Workday's CDN.
- A "hit" = HTTP 200 with `myworkdayjobs.com` in the final URL after redirects.
- Output CSV columns: slug, instance, status, final_url
"""
from __future__ import annotations

import argparse
import asyncio
import csv
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
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")


async def probe_one(session: aiohttp.ClientSession, slug: str, instance: str,
                    sem: asyncio.Semaphore) -> tuple | None:
    url = f"https://{slug}.{instance}.myworkdayjobs.com/"
    async with sem:
        try:
            async with session.get(url, allow_redirects=True) as resp:
                final = str(resp.url)
                if resp.status == 200 and "myworkdayjobs.com" in final:
                    return (slug, instance, resp.status, final)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            pass
    return None


async def probe_company(session, slug, sem, results, progress, lock):
    final_hit = None
    for instance in WD_INSTANCES:
        hit = await probe_one(session, slug, instance, sem)
        if hit:
            final_hit = hit
            break
    async with lock:
        if final_hit:
            results.append(final_hit)
            print(f"  HIT  {slug:<40s} {final_hit[1]}", flush=True)
        else:
            results.append((slug, "", "NO_HIT", ""))
        progress["done"] += 1
        hits_so_far = sum(1 for r in results if r[2] != "NO_HIT")
        if progress["done"] % 200 == 0:
            elapsed = time.time() - progress["t0"]
            rate = progress["done"] / max(elapsed, 0.1)
            eta = (progress["total"] - progress["done"]) / max(rate, 0.1)
            print(
                f"[{progress['done']}/{progress['total']}] "
                f"hits={hits_so_far} rate={rate:.1f}/s eta={eta/60:.1f}min",
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
    hits = [r for r in data if r[2] not in ("NO_HIT", "")]
    misses = len(data) - len(hits)

    sample_rows = "".join(
        f"<tr><td>{r[0]}</td><td>{r[1]}</td>"
        f"<td><a href='{r[3]}'>{r[3]}</a></td></tr>"
        for r in hits[:100]
    )
    subject = (
        f"Workday Tenant Discovery — {len(hits)} hits / {len(data)} probed "
        f"({datetime.now().strftime('%b %d %H:%M')})"
    )
    body = f"""
    <h2 style="font-family:sans-serif">Workday Tenant Discovery</h2>
    <p><b>{len(hits)}</b> live Workday tenants found out of <b>{len(data)}</b>
       slugs probed ({misses} misses).</p>
    <p>Full results attached as <code>{csv_path.name}</code>. Showing first
       100 hits below:</p>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#0a66c2;color:white">
        <th>Slug</th><th>Instance</th><th>Career Portal URL</th>
      </tr>
      {sample_rows or '<tr><td colspan="3">No hits.</td></tr>'}
    </table>
    <p style="font-size:12px;color:#888;margin-top:16px">
      Probed {len(WD_INSTANCES)} Workday data-center prefixes
      ({', '.join(WD_INSTANCES)}). Generated
      {datetime.now().strftime('%Y-%m-%d %H:%M')}.
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
    p.add_argument("--timeout", type=float, default=10.0,
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
    # Dedupe while preserving order
    seen = set()
    slugs = [s for s in all_slugs if not (s in seen or seen.add(s))]

    if args.start:
        slugs = slugs[args.start:]
    if args.limit:
        slugs = slugs[:args.limit]

    print(f"Input: {len(all_slugs)} lines, {len(slugs)} after dedupe+slice")
    print(f"Probing × {len(WD_INSTANCES)} instances "
          f"({len(slugs) * len(WD_INSTANCES)} requests max)")
    print(f"Concurrency: {args.concurrency}  Timeout: {args.timeout}s")
    print()

    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    results: list[tuple] = []
    progress = {"done": 0, "total": len(slugs), "t0": time.time()}

    timeout = aiohttp.ClientTimeout(total=args.timeout, connect=5)
    connector = aiohttp.TCPConnector(
        limit=args.concurrency * 2,
        ttl_dns_cache=300,
        ssl=False,  # Skip cert validation - faster, we only care about status
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        headers={"User-Agent": UA, "Accept": "text/html,application/json"},
    ) as session:
        tasks = [
            probe_company(session, s, sem, results, progress, lock)
            for s in slugs
        ]
        await asyncio.gather(*tasks)

    out = Path(args.outfile)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["slug", "instance", "status", "final_url"])
        for r in sorted(results):
            w.writerow(r)

    elapsed = time.time() - progress["t0"]
    hits = sum(1 for r in results if r[2] != "NO_HIT")
    print()
    print(f"Done in {elapsed/60:.1f}min. {hits} hits / {len(results)} probed -> {out}")

    if args.email:
        send_email(out)


if __name__ == "__main__":
    asyncio.run(main())
