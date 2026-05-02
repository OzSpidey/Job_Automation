# Job_Automation

Automated job scraping and auto-apply scripts for multiple job platforms, with GitHub Actions workflows for scheduled runs.

---

## Folder Structure

```
Automation_Scripts/
├── *.py                  Active scraper and auto-apply scripts
├── requirements.txt      Python dependencies
├── .env                  Local secrets (API keys, email credentials)
│
├── json/                 Seen-job ID logs, session files, and company configs
├── csv/                  Output CSVs and Excel files (applied jobs, job listings)
├── bat/                  Windows batch files for local script execution
├── txt/                  Applicant info and misc text files
├── logs/                 Runtime log files from bat launches
│
├── .github/workflows/    GitHub Actions workflows (one per scraper)
├── Unused_Scripts/       Archived scripts no longer in active use
└── earlyapply_replica/   Full EarlyApply site replica (FastAPI + Next.js)
```

---

## Scripts

| Script | Platform | What it does |
|--------|----------|--------------|
| `workday_scraper.py` | Workday | Hits Workday career APIs directly (no browser) across 400+ Fortune 1000 companies. Filters by role (DE, DA, BI, SE, etc.), skips senior titles, emails new listings. |
| `oracle_scraper.py` | Oracle Recruiting Cloud | Calls Oracle Cloud REST APIs across tracked companies. Same role/seniority filtering as workday scraper. Emails new listings. |
| `lever_jobs_scanner.py` | Lever ATS | Scans Lever-hosted company career pages for Data Engineer and Data Analyst roles. Deduplicates by job ID across runs. |
| `greenhouse_autoapply.py` | Greenhouse ATS | Auto-fills and submits job applications on Greenhouse for Data Analyst roles posted in the last 5 days. Logs applied jobs to `csv/greenhouse_applied.csv`. |
| `jobdiva_autoapply.py` | JobDiva | Auto-applies to Data Engineer postings on JobDiva via Quick Apply. Skips senior roles and jobs older than 1 day. |
| `indeed_adzuna_scraper.py` | Adzuna API | Uses the Adzuna API (Indeed data) to find jobs posted in the last 24 hours. No browser required. Emails new listings. |
| `linkedin_nologin_scraper.py` | LinkedIn | Scrapes LinkedIn's public guest job API — no login, no cookies, no account risk. Filters by role and experience level. |
| `amazon_jobs_scanner.py` | Amazon Jobs (US) | Scrapes Amazon's university recruiting page for US roles (DE, BI, BA). Sorts by most recent, scrapes 40 pages. |
| `amazon_india_jobs_scanner.py` | Amazon Jobs (India) | Same as above but targets India university roles (SWE, SDE). |
| `microsoft_jobs_scanner.py` | Microsoft Careers | Scrapes Microsoft's careers site for SWE, DE, DA, and BI roles, sorted by latest. |
| `naukri_jobs_scanner.py` | Naukri.com | Searches Naukri for C++, C, Python, and SWE roles posted in the last day. Targets India market. |
