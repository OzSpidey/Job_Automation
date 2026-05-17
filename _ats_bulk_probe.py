"""Bulk ATS probe: checks every slug in _bulk_candidates.txt against
Greenhouse, Ashby, and Lever concurrently.  All three ATS platforms are
probed in parallel for each batch so total runtime is ~max(gh, ashby, lever)
not the sum.

Usage:
  python _ats_bulk_probe.py [candidates_file]

Output files (written/appended in batches so a crash loses at most one batch):
  _bulk_greenhouse.csv   slug,name,job_count
  _bulk_ashby.csv        slug,job_count,sample_titles
  _bulk_lever.csv        slug,job_count,sample_title
"""
import asyncio
import csv
import re
import sys
from pathlib import Path

import httpx

CANDIDATES_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_bulk_candidates.txt")
GH_OUT   = Path("_bulk_greenhouse.csv")
ASHBY_OUT = Path("_bulk_ashby.csv")
LEVER_OUT = Path("_bulk_lever.csv")

CONCURRENCY = 20          # per platform
BATCH_SIZE  = 150         # slugs per round-trip gather
TIMEOUT = httpx.Timeout(15.0, connect=8.0)
HEADERS = {"User-Agent": "Mozilla/5.0 (ats-discovery/1.0)"}


# ── per-platform probers ──────────────────────────────────────────────────────

async def probe_greenhouse(client: httpx.AsyncClient, slug: str, sem: asyncio.Semaphore):
    async with sem:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        try:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            data = r.json()
            jobs = data.get("jobs") or []
            if not jobs:
                return None
            name = ""
            for j in jobs:
                cn = (j.get("company_name") or "").strip()
                if cn:
                    name = cn
                    break
            return (slug, name or slug, len(jobs))
        except Exception:
            return None


async def probe_ashby(client: httpx.AsyncClient, slug: str, sem: asyncio.Semaphore):
    async with sem:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        try:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            data = r.json()
            jobs = data.get("jobs") or []
            if not jobs:
                return None
            titles = [j.get("title", "") for j in jobs[:3] if j.get("title")]
            return (slug, len(jobs), " | ".join(titles))
        except Exception:
            return None


async def probe_lever(client: httpx.AsyncClient, slug: str, sem: asyncio.Semaphore):
    async with sem:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            r = await client.get(url)
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                return None
            data = r.json()
            if not isinstance(data, list) or not data:
                return None
            sample = (data[0].get("text") or "")[:100]
            return (slug, len(data), sample)
        except Exception:
            return None


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    if not CANDIDATES_FILE.exists():
        print(f"ERROR: candidates file not found: {CANDIDATES_FILE}", file=sys.stderr)
        sys.exit(1)

    raw = CANDIDATES_FILE.read_text(encoding="utf-8").splitlines()
    slugs: list[str] = []
    seen: set[str] = set()
    for s in raw:
        s = s.strip().lower()
        if not s or s.startswith("#"):
            continue
        if not re.match(r"^[a-z0-9][a-z0-9\-]{1,58}[a-z0-9]$", s):
            continue
        if s not in seen:
            seen.add(s)
            slugs.append(s)

    total = len(slugs)
    print(f"Loaded {total} unique slugs from {CANDIDATES_FILE}", flush=True)

    # Open output files (write mode — fresh run each time)
    gh_f    = GH_OUT.open("w", newline="", encoding="utf-8")
    ashby_f = ASHBY_OUT.open("w", newline="", encoding="utf-8")
    lever_f = LEVER_OUT.open("w", newline="", encoding="utf-8")

    gh_w    = csv.writer(gh_f)
    ashby_w = csv.writer(ashby_f)
    lever_w = csv.writer(lever_f)

    gh_w.writerow(["slug", "name", "job_count"])
    ashby_w.writerow(["slug", "job_count", "sample_titles"])
    lever_w.writerow(["slug", "job_count", "sample_title"])

    gh_sem    = asyncio.Semaphore(CONCURRENCY)
    ashby_sem = asyncio.Semaphore(CONCURRENCY)
    lever_sem = asyncio.Semaphore(CONCURRENCY)

    gh_ok = ashby_ok = lever_ok = 0

    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=HEADERS
    ) as client:
        for i in range(0, total, BATCH_SIZE):
            chunk = slugs[i : i + BATCH_SIZE]

            gh_res, ashby_res, lever_res = await asyncio.gather(
                asyncio.gather(*[probe_greenhouse(client, s, gh_sem) for s in chunk]),
                asyncio.gather(*[probe_ashby(client, s, ashby_sem) for s in chunk]),
                asyncio.gather(*[probe_lever(client, s, lever_sem) for s in chunk]),
            )

            for row in gh_res:
                if row:
                    gh_w.writerow(row)
                    gh_ok += 1
            for row in ashby_res:
                if row:
                    ashby_w.writerow(row)
                    ashby_ok += 1
            for row in lever_res:
                if row:
                    lever_w.writerow(row)
                    lever_ok += 1

            # Flush after every batch so partial results survive a crash
            gh_f.flush()
            ashby_f.flush()
            lever_f.flush()

            done = i + len(chunk)
            pct = done * 100 // total
            print(
                f"[{done:>6}/{total}  {pct:>3}%]  "
                f"GH={gh_ok}  Ashby={ashby_ok}  Lever={lever_ok}",
                flush=True,
            )

    gh_f.close()
    ashby_f.close()
    lever_f.close()

    print(f"\n=== DONE ===")
    print(f"Greenhouse : {gh_ok:>5} companies  →  {GH_OUT}")
    print(f"Ashby      : {ashby_ok:>5} companies  →  {ASHBY_OUT}")
    print(f"Lever      : {lever_ok:>5} companies  →  {LEVER_OUT}")


if __name__ == "__main__":
    asyncio.run(main())
