import csv
import time
import re
import sys
from datetime import datetime
from ddgs import DDGS

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

COMPANY = "Snowflake"  # CHANGE THIS to target a different company

ROLE_CATEGORIES = {
    "Recruiter / Talent Acquisition": [
        "recruiter",
        "technical recruiter",
        "talent acquisition",
        "talent acquisition manager",
        "talent acquisition specialist",
        "talent acquisition partner",
        "corporate recruiter",
        "university recruiter",
        "talent partner",
        "recruiting manager",
        "head of talent",
        "people operations",
        "talent lead",
        "sourcer",
        "recruiting coordinator",
        "hr manager",
        "head of recruiting",
        "director of recruiting",
        "VP of talent acquisition",
        "people partner",
    ],
    "Analytics Manager": [
        # Manager/director titles
        "analytics manager",
        "manager of analytics",
        "senior analytics manager",
        "director of analytics",
        "head of analytics",
        "analytics lead",
        "VP of analytics",
        "analytics engineering manager",
        "data analytics manager",
        # Individual contributor titles Snowflake employees actually use
        "analytics engineer",
        "senior analytics engineer",
        "principal analytics engineer",
        "staff analytics engineer",
        "data scientist",
        "senior data scientist",
        "machine learning engineer",
        "AI engineer",
    ],
    "Data Engineering": [
        # Manager titles
        "data engineering manager",
        "director of data engineering",
        "head of data engineering",
        "data platform manager",
        "VP of data engineering",
        # Individual contributor titles
        "data engineer",
        "senior data engineer",
        "staff data engineer",
        "principal data engineer",
        "data architect",
        "data cloud engineer",
        "senior software engineer data",
        "data platform engineer",
        "data infrastructure engineer",
    ],
    "Business Intelligence": [
        # Manager titles
        "business intelligence manager",
        "BI manager",
        "director of business intelligence",
        "head of business intelligence",
        # Individual contributor titles
        "business intelligence analyst",
        "BI analyst",
        "data analyst",
        "senior data analyst",
        "BI developer",
        "BI engineer",
        "Tableau developer",
        "Looker developer",
        "data visualization analyst",
        "reporting analyst",
    ],
}

MAX_PER_CATEGORY = 30

# ── US location detection ──────────────────────────────────────────────────────
US_STATES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming",
}
US_METROS = {
    "Bay Area", "Silicon Valley", "San Francisco Bay Area",
    "Greater Seattle", "Greater New York", "Greater Boston",
    "Greater Chicago", "Greater Atlanta", "Greater Denver",
    "Greater Dallas", "Greater Houston", "Greater Austin",
    "Greater Los Angeles", "Greater San Francisco", "Greater San Diego",
    "Greater Phoenix", "Greater Miami", "Greater Portland",
    "Greater Salt Lake City", "Greater Raleigh", "Greater Charlotte",
    "Greater Nashville", "Greater Detroit", "Greater Philadelphia",
}
US_CITIES = {
    "San Francisco", "New York", "Seattle", "Chicago", "Boston", "Austin",
    "Denver", "Atlanta", "Los Angeles", "San Jose", "San Diego", "Portland",
    "Phoenix", "Dallas", "Houston", "Miami", "Minneapolis", "Detroit",
    "Philadelphia", "Charlotte", "Nashville", "Raleigh", "Salt Lake City",
    "Bellevue", "Redwood City", "Menlo Park", "Mountain View", "Palo Alto",
    "Sunnyvale", "Santa Clara", "San Mateo", "Bozeman", "San Ramon",
    "Pleasanton", "Oakland", "Sacramento", "Irvine", "Tempe", "Scottsdale",
    "Las Vegas", "Pittsburgh", "Columbus", "Indianapolis", "San Antonio",
    "Jacksonville", "Memphis", "Louisville", "Richmond", "Durham",
    "Baltimore", "Washington DC", "Washington, D.C",
}

# Non-US signals — if any appear in the body, the profile is NOT in the US
NON_US_BODY_SIGNALS = {
    "India", "United Kingdom", "Australia", "Canada", "Germany", "France",
    "Singapore", "Netherlands", "Brazil", "Mexico", "Japan", "China",
    "Ireland", "South Africa", "New Zealand", "Spain", "Italy", "Poland",
    "Sweden", "Norway", "Denmark", "Finland", "Switzerland", "Belgium",
    "Romania", "Ukraine", "Pakistan", "Philippines", "Indonesia", "Malaysia",
    "Maharashtra", "Karnataka", "Bengaluru", "Mumbai", "Delhi", "Hyderabad",
    "Pune", "Chennai", "Kolkata", "Noida", "Gurgaon",
    "London", "Sydney", "Toronto", "Vancouver", "Melbourne", "Dublin",
    "Amsterdam", "Paris", "Berlin", "Warsaw", "Bangalore",
    "APAC", "EMEA", "APJ",
}

# 2-letter country code subdomains that mean non-US (e.g. in., uk., au., za., ca., ie.)
NON_US_URL_PATTERN = re.compile(r"https://([a-z]{2})\.linkedin\.com/", re.I)


def is_non_us_url(url: str) -> bool:
    """Reject URLs like in.linkedin.com, za.linkedin.com, etc."""
    m = NON_US_URL_PATTERN.match(url)
    return bool(m)   # any 2-letter subdomain prefix = non-US


def is_us_location(body: str) -> bool:
    if not body:
        return False
    # Explicit positive signals
    if "United States" in body:
        return True
    for label in (*US_STATES, *US_METROS, *US_CITIES):
        if label in body:
            return True
    return False


def has_non_us_signal(body: str) -> bool:
    for signal in NON_US_BODY_SIGNALS:
        if signal in body:
            return True
    return False


def is_company_employee(company: str, title: str, body: str) -> bool:
    co = company.lower()
    t = title.lower()
    b = body.lower()
    # Headline: "Role @ Company"
    if f"@ {co}" in t:
        return True
    # Title: "Name - Role - Company | LinkedIn"
    if f"- {co} |" in t or f"- {co}\n" in t:
        return True
    # Body: "Experience: Company" (LinkedIn DDG snippet pattern)
    if f"experience: {co}" in b:
        return True
    # Body: "Company ·" location separator LinkedIn uses
    if re.search(rf'\b{re.escape(co)}\s*·', b):
        return True
    # Body: "at Company" / "@ Company"
    if re.search(rf'(at|@)\s+{re.escape(co)}\b', b, re.I):
        return True
    return False


# ── Helpers ────────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    url = url.rstrip("/")
    # Strip country subdomain for dedup purposes only
    url = re.sub(r"https://[a-z]{2}\.linkedin\.com/", "https://www.linkedin.com/", url)
    return url.lower()


def ddg_search(query: str, max_results: int = 15, retries: int = 3):
    for attempt in range(retries):
        try:
            hits = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results * 5):
                    link = r.get("href", "")
                    if "linkedin.com/in/" in link:
                        hits.append((r.get("title", ""), link, r.get("body", "")))
                    if len(hits) >= max_results:
                        break
            return hits
        except Exception as e:
            msg = str(e).lower()
            if "ratelimit" in msg or "202" in msg or "blocked" in msg or "no results" in msg.lower():
                if "no results" in msg.lower():
                    return []
                wait = 12 * (attempt + 1)
                print(f"  [rate limited] waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  Search error: {e}")
                return []
    return []


def clean_name(title: str) -> str:
    name = title.split(" - ")[0].strip()
    name = re.sub(r"\s+\|.*", "", name)
    name = re.sub(r"\s+on LinkedIn.*", "", name, flags=re.IGNORECASE)
    return name.strip()


def extract_job_title(page_title: str, fallback: str) -> str:
    parts = page_title.split(" - ")
    if len(parts) > 1:
        t = re.split(r"\s+at\s+|\s+\||\s+@\s+", parts[1])[0].strip()
        if t:
            return t
    return fallback.title()


def is_valid_profile(company: str, title: str, link: str, body: str) -> tuple[bool, str]:
    """Returns (valid, reason). Reason is shown when invalid."""
    if is_non_us_url(link):
        return False, "non-US URL"
    if has_non_us_signal(body):
        return False, "non-US body"
    if not is_company_employee(company, title, body):
        return False, "not employee"
    if not is_us_location(body):
        return False, "not US"
    return True, ""


# ── Core search ────────────────────────────────────────────────────────────────

def collect_profiles(company: str, category_name: str, keywords: list, existing_links: set) -> list:
    found = []
    print(f"\n--- Searching: {category_name} ---")

    for kw in keywords:
        if len(found) >= MAX_PER_CATEGORY:
            break

        # Phase 1: strict – "@ Company" headline match (highest confidence)
        queries = [
            f'site:linkedin.com/in "@ {company}" "{kw}"',
            # Phase 2: broader – company name + keyword + US location (more results, validated by body)
            f'site:linkedin.com/in "{company}" "{kw}" "United States"',
        ]

        for query in queries:
            if len(found) >= MAX_PER_CATEGORY:
                break

            print(f"  Query: {query}")
            results = ddg_search(query, max_results=15)

            for title, link, body in results:
                norm = normalize_url(link)
                if norm in existing_links:
                    continue

                valid, reason = is_valid_profile(company, title, link, body)
                if not valid:
                    print(f"  [skip – {reason}] {title[:70]}")
                    continue

                existing_links.add(norm)
                name = clean_name(title)
                job_title = extract_job_title(title, kw)
                found.append({
                    "Name": name,
                    "Title": job_title,
                    "Category": category_name,
                    "LinkedIn": link,
                    "Company": company,
                })
                print(f"  Found: {name} | {job_title}")

                if len(found) >= MAX_PER_CATEGORY:
                    break

            time.sleep(2)

    return found


def main():
    company = COMPANY

    unique_links = set()
    all_profiles = []

    for category_name, keywords in ROLE_CATEGORIES.items():
        profiles = collect_profiles(company, category_name, keywords, unique_links)
        all_profiles.extend(profiles)

    print("\n" + "=" * 60)
    print(f"RESULTS FOR: {company.upper()} (United States only)")
    print("=" * 60)

    current_cat = None
    for p in all_profiles:
        if p["Category"] != current_cat:
            current_cat = p["Category"]
            print(f"\n[ {current_cat} ]")
        print(f"  {p['Name']}")
        print(f"  {p['LinkedIn']}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{company.replace(' ', '_')}_people_{ts}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Name", "Title", "Category", "Company", "LinkedIn"])
        writer.writeheader()
        writer.writerows(all_profiles)

    print(f"\n{'=' * 60}")
    print(f"Total profiles found: {len(all_profiles)}")
    print(f"Saved to: {filename}")


if __name__ == "__main__":
    main()
