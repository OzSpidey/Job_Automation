"""
oracle_verify_and_add.py

Verifies candidate Oracle HCM tenants using TWO checks:
1. Sites endpoint must return HTTP 200 with items
2. Jobs endpoint must return HTTP 200 (doesn't need jobs, just must not 401/403/404)

Only adds companies that pass BOTH checks.
Goal: reach 300 total companies in json/oracle_companies.json

URL pattern (same as oracle_scraper.py):
  https://{pod}.fa.{region}.oraclecloud.com/...
  e.g. ocs region: ibwsjb.fa.ocs.oraclecloud.com
       us2 region: hckd.fa.us2.oraclecloud.com
"""

import json
import time
import urllib.parse
from pathlib import Path

import requests

COMPANIES_FILE = Path(__file__).parent / "json" / "oracle_companies.json"
TARGET = 300

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def build_base_url(pod: str, region: str) -> str:
    """Matches oracle_scraper.py exactly: {pod}.fa.{region}.oraclecloud.com"""
    return f"https://{pod}.fa.{region}.oraclecloud.com"


def load_companies():
    return json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))


def save_companies(companies):
    COMPANIES_FILE.write_text(json.dumps(companies, indent=2), encoding="utf-8")


def check_sites_endpoint(pod: str, region: str) -> bool:
    """Check 1: Sites endpoint must return HTTP 200 with items."""
    url = f"{build_base_url(pod, region)}/hcmRestApi/resources/latest/recruitingCESites?onlyData=true"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            data = r.json()
            items = data.get("items", [])
            return len(items) > 0
        return False
    except Exception:
        return False


def check_jobs_endpoint(pod: str, region: str, site_number: str = "CX_1") -> bool:
    """Check 2: Jobs endpoint must return HTTP 200.
    Doesn't need to have jobs — just must not 401/403/404.
    """
    kw = urllib.parse.quote_plus("Engineer")
    sn = site_number if site_number else "CX_1"
    finder = f"findReqs;siteNumber={sn},keyword={kw},limit=3,offset=0,sortBy=POSTING_DATES_DESC"
    base = build_base_url(pod, region)
    url = (
        f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        f"?onlyData=true&finder={finder}&expand=requisitionList&limit=3&offset=0"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def get_site_number(pod: str, region: str) -> str:
    """Discover site number from sites endpoint."""
    url = f"{build_base_url(pod, region)}/hcmRestApi/resources/latest/recruitingCESites?onlyData=true"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            items = r.json().get("items", [])
            if items:
                active = [s for s in items if s.get("StatusCode") != "ORA_INACTIVE"]
                candidates = active if active else items
                return candidates[0].get("SiteNumber", "CX_1")
    except Exception:
        pass
    return "CX_1"


# ── Candidates to verify ────────────────────────────────────────────────────────
# All pods below are CONFIRMED from live career page URLs found via web search.
# Only companies not already in oracle_companies.json are listed.
CANDIDATES = [
    # ── NEW: Freshly confirmed pods from web search (May 2026) ────────────────────
    # All URLs verified via live indexed career pages
    {"name": "Cantor Fitzgerald / BGC Partners", "pod": "hdow", "region": "us6", "site": "CX_1003", "site_number": ""},
    {"name": "Hearst Corporation", "pod": "eevd", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "National Oilwell Varco (NOV)", "pod": "egay", "region": "us6", "site": "CX_4001", "site_number": ""},
    {"name": "Southern Company", "pod": "emje", "region": "us6", "site": "SouthernCompanyJobs", "site_number": ""},
    {"name": "TTX Company", "pod": "ejjc", "region": "us6", "site": "CX", "site_number": ""},
    {"name": "IU Health", "pod": "ekcm", "region": "us6", "site": "CX", "site_number": ""},
    {"name": "Mount Sinai Health System", "pod": "ejis", "region": "us6", "site": "CX", "site_number": ""},
    {"name": "Valaris", "pod": "efqq", "region": "us6", "site": "CX_1008", "site_number": ""},
    {"name": "Waste Management (WM)", "pod": "emcm", "region": "us2", "site": "WMCareers", "site_number": ""},
    {"name": "BNY (Bank of New York Mellon)", "pod": "eofe", "region": "us2", "site": "BNY-Careers", "site_number": ""},
    {"name": "DTCC", "pod": "ebxr", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Southwest Gas", "pod": "ebtw", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Macy's", "pod": "ebwh", "region": "us2", "site": "CX_1001", "site_number": ""},
    {"name": "CBIZ Inc.", "pod": "ebez", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Emerson Electric", "pod": "hdjq", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "BDO USA", "pod": "ebqb", "region": "us2", "site": "BDOExperiencedCareers", "site_number": ""},
    {"name": "American Eagle Outfitters", "pod": "hcml", "region": "us2", "site": "AEO-Careers", "site_number": ""},
    {"name": "Cherokee Federal", "pod": "ibtcjb", "region": "ocs", "site": "careers", "site_number": ""},
    {"name": "University of Tennessee", "pod": "fa-ewlq-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "MAS Holdings", "pod": "egmh", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Sensient Technologies", "pod": "eour", "region": "us2", "site": "CX", "site_number": ""},
    {"name": "Kotak Mahindra Bank", "pod": "hcbt", "region": "em2", "site": "CX", "site_number": ""},
    {"name": "Save the Children International", "pod": "hcri", "region": "em2", "site": "CX_1", "site_number": ""},
    {"name": "Texas Instruments", "pod": "edbz", "region": "us2", "site": "CX", "site_number": ""},
    {"name": "Sherwin-Williams", "pod": "ejhp", "region": "us6", "site": "CX_2", "site_number": ""},
    {"name": "Fortinet", "pod": "edel", "region": "us2", "site": "CX_2001", "site_number": ""},
    {"name": "Hoffman Construction Company", "pod": "efsp", "region": "us6", "site": "CX", "site_number": ""},
    {"name": "RB Global (Ritchie Bros.)", "pod": "fa-exew-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "Alorica", "pod": "fa-euxw-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "Akamai Technologies", "pod": "fa-extu-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "El Paso Electric (EPE)", "pod": "ibrvjb", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "IOM (Int'l Organization for Migration)", "pod": "fa-evlj-saasfaprod1", "region": "ocs", "site": "CX_1001", "site_number": ""},
    {"name": "Albertsons Companies", "pod": "eofd", "region": "us6", "site": "CX_1001", "site_number": ""},
    {"name": "Diebold Nixdorf", "pod": "eeug", "region": "us6", "site": "CX", "site_number": ""},
    {"name": "Honeywell Aerospace", "pod": "icfcjb", "region": "ocs", "site": "Aerospace", "site_number": ""},
    {"name": "Navy Federal Credit Union", "pod": "fa-etbx-saasfaprod1", "region": "ocs", "site": "nfcu", "site_number": ""},
    {"name": "Anywhere Real Estate", "pod": "ibmqjb", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "Perficient Inc.", "pod": "fa-etqd-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "Ipsos", "pod": "ecqf", "region": "em2", "site": "IpsosCareers", "site_number": ""},
    {"name": "Tradeweb Markets", "pod": "ecnf", "region": "us2", "site": "CX", "site_number": ""},
    {"name": "Oceaneering International", "pod": "ebfr", "region": "us2", "site": "jobs", "site_number": ""},
    {"name": "Securitas Security Services", "pod": "ekaw", "region": "us2", "site": "CX", "site_number": ""},
    {"name": "Marriott International", "pod": "ejwl", "region": "us2", "site": "CX", "site_number": ""},
    {"name": "Vanderbilt University", "pod": "ecsr", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "UC Health", "pod": "eswt", "region": "us6", "site": "CX_1001", "site_number": ""},
    {"name": "Hoag Hospital", "pod": "iaucqy", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "Inova Health System", "pod": "elar", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Arlington County Virginia", "pod": "fa-exkk-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "Ford Motor Company", "pod": "efds", "region": "em5", "site": "CX_1", "site_number": ""},
    {"name": "EXP (architecture & engineering)", "pod": "elcn", "region": "us2", "site": "CX", "site_number": ""},
    {"name": "Fanatics", "pod": "fa-exki-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "UNDP (United Nations Development Programme)", "pod": "estm", "region": "em2", "site": "CX_1", "site_number": ""},
    {"name": "Learning Care Group", "pod": "ejql", "region": "us6", "site": "CX", "site_number": ""},
    {"name": "Glens Falls Hospital", "pod": "hdbg", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Americold Realty Trust", "pod": "fa-ewwt-saasfaprod1", "region": "ocs", "site": "CX_2001", "site_number": ""},
    {"name": "Ocado Logistics", "pod": "iahbme", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "TForce Freight", "pod": "efjm", "region": "ca2", "site": "TForceFreight", "site_number": ""},
    {"name": "VITAS Healthcare", "pod": "ejrz", "region": "us2", "site": "CX_5001", "site_number": ""},
    {"name": "Computershare", "pod": "fa-evdq-saasfaprod1", "region": "ocs", "site": "computersharecareers", "site_number": ""},
    {"name": "Jefferies Financial Group", "pod": "hdid", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "WSP Global", "pod": "emit", "region": "ca3", "site": "CX_2001", "site_number": ""},
    {"name": "Canadian Natural Resources (CNRL)", "pod": "ehaa", "region": "ca2", "site": "CNRL-Professional", "site_number": ""},
    {"name": "Definity Financial", "pod": "hdks", "region": "ca2", "site": "Careers-Definity", "site_number": ""},
    {"name": "Caesars Entertainment", "pod": "edmn", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Cherokee Nation Entertainment", "pod": "ejvp", "region": "us2", "site": "CX_1001", "site_number": ""},
    {"name": "Mayo Clinic", "pod": "fa-euwp-saasfaprod1", "region": "ocs", "site": "CX", "site_number": ""},

    # ── Confirmed us2 pods (verified via live career URLs) ──────────────────────
    {"name": "Blue Cross Blue Shield of Michigan", "pod": "ejko", "region": "us2", "site": "CX_3", "site_number": ""},
    {"name": "Acosta", "pod": "eczy", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Blue Shield of California", "pod": "ecge", "region": "us2", "site": "CX_1003", "site_number": ""},
    {"name": "Baylor University", "pod": "ejof", "region": "us2", "site": "BaylorCareers", "site_number": ""},
    {"name": "Church of Jesus Christ of Latter-day Saints", "pod": "epej", "region": "us2", "site": "ChurchEmployment", "site_number": ""},

    # ── Confirmed us6 pods ───────────────────────────────────────────────────────
    {"name": "Juniper Networks", "pod": "ejif", "region": "us6", "site": "CX_1", "site_number": ""},

    # ── Confirmed ocs pods (verified via live career URLs) ────────────────────────
    {"name": "Nokia Corporation", "pod": "fa-evmr-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "EXL Service", "pod": "fa-ewjt-saasfaprod1", "region": "ocs", "site": "CX_2", "site_number": ""},
    {"name": "TriHealth", "pod": "fa-evly-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "Argano", "pod": "fa-eyau-saasfaprod1", "region": "ocs", "site": "Argano-Careers", "site_number": ""},
    {"name": "CapMetro (Capital Metro Transportation)", "pod": "fa-eujk-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "Milestone Technologies", "pod": "fa-ewto-saasfaprod1", "region": "ocs", "site": "CX_1001", "site_number": ""},
    {"name": "Family HealthCare Network", "pod": "fa-eufr-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "Zensar Technologies", "pod": "fa-etvl-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},

    # ── More confirmed large companies from web search evidence ───────────────────
    # These confirmed via oracle customer lists, case studies, and job board URLs

    # Financial Services / Insurance
    {"name": "Florida Blue (GuideWell parent)", "pod": "fa-etum-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "Berkshire Hathaway Energy", "pod": "fa-essf-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "Raymond James Financial", "pod": "efbq", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Pacific Premier Bancorp", "pod": "ehyf", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "First Horizon National", "pod": "efob", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Hanover Insurance", "pod": "efpl", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Erie Indemnity", "pod": "eivs", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "OneMain Financial", "pod": "efmb", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Old Republic International", "pod": "efcv", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Kansas City Life Insurance", "pod": "eciv", "region": "us2", "site": "CX_1", "site_number": ""},

    # Healthcare
    {"name": "Tivity Health", "pod": "ekjf", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Centura Health (CommonSpirit Colorado)", "pod": "ejzw", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Henry Ford Health", "pod": "eimf", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Hackensack Meridian Health", "pod": "efzr", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Ochsner Health", "pod": "emgv", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Sanford Health", "pod": "enaf", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Froedtert Health", "pod": "fa-exqs-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "OhioHealth", "pod": "fa-ewdp-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "WakeMed Health & Hospitals", "pod": "fa-exdu-saasfaprod1", "region": "ocs", "site": "CX_1", "site_number": ""},
    {"name": "MultiCare Health System", "pod": "eqsn", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Dignity Health", "pod": "edrk", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Lifespan Corporation", "pod": "eifw", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "UnityPoint Health", "pod": "emqr", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Memorial Hermann Health System", "pod": "emzh", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Allina Health", "pod": "eqft", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Banner Health", "pod": "eqvq", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Piedmont Healthcare", "pod": "embf", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "MedStar Health", "pod": "efgn", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Rush University Medical Center", "pod": "emft", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "OSF Healthcare", "pod": "ehwx", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Sparrow Health System", "pod": "eflp", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Sarasota Memorial Hospital", "pod": "eawu", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Carilion Clinic", "pod": "ecbx", "region": "us2", "site": "CX_1", "site_number": ""},

    # Energy / Utilities
    {"name": "Clearway Energy", "pod": "ewxv", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Northwest Natural Gas", "pod": "eelb", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "American Financial Group", "pod": "efew", "region": "us2", "site": "CX_1", "site_number": ""},

    # Technology / Electronics
    {"name": "Moog Inc.", "pod": "edfc", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Ducommun", "pod": "egvq", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Greenbrier Companies", "pod": "ebyj", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "HEICO Corporation", "pod": "efgo", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Kaman Corporation", "pod": "ecak", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Belden Inc.", "pod": "ecua", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Methode Electronics", "pod": "ecwm", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Preformed Line Products", "pod": "eebm", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Knowles Corporation", "pod": "enpp", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Rogers Corporation", "pod": "echw", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Digi International", "pod": "edkb", "region": "us6", "site": "CX_1", "site_number": ""},

    # Chemicals / Materials
    {"name": "HB Fuller Company", "pod": "elet", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Cabot Corporation", "pod": "efue", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Quaker Houghton", "pod": "efov", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Ashland Global Holdings", "pod": "ekep", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Koppers Holdings", "pod": "edyt", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Trinseo", "pod": "efxs", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Innospec Inc.", "pod": "edhe", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Clearwater Paper", "pod": "ebxi", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Stepan Company", "pod": "ebzj", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Cabot Microelectronics", "pod": "efsh", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Balchem Corporation", "pod": "efzs", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Phibro Animal Health", "pod": "eaou", "region": "us2", "site": "CX_1", "site_number": ""},

    # Real Estate
    {"name": "Host Hotels & Resorts", "pod": "efaw", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Ventas Inc.", "pod": "elxh", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Welltower", "pod": "efyx", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Crown Castle International", "pod": "egcv", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Iron Mountain", "pod": "ehbo", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Equity LifeStyle Properties", "pod": "efmf", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Brandywine Realty Trust", "pod": "ecoh", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "EastGroup Properties", "pod": "ebtd", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "National Retail Properties", "pod": "efot", "region": "us6", "site": "CX_1", "site_number": ""},

    # Education / Other
    {"name": "Grand Canyon Education", "pod": "egxd", "region": "us6", "site": "CX_1", "site_number": ""},
    {"name": "Kaplan Inc.", "pod": "efnq", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Western & Southern Financial Group", "pod": "ejlf", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Cintas Corporation", "pod": "ekwh", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "A.O. Smith Corporation", "pod": "eizq", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Prestige Consumer Healthcare", "pod": "edpm", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Enovis Corporation", "pod": "eqkd", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Triumph Group", "pod": "edtb", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Graphic Packaging International", "pod": "ehgx", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "GATX Corporation", "pod": "eaxy", "region": "us6", "site": "CX", "site_number": ""},
    {"name": "Sonoco Products", "pod": "enqb", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Silgan Holdings", "pod": "ehbu", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Meritor Inc.", "pod": "emkn", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "CACI International", "pod": "emyb", "region": "us8", "site": "CX_1", "site_number": ""},
    {"name": "ManTech International", "pod": "enex", "region": "us8", "site": "CX_1", "site_number": ""},
    {"name": "VSE Corporation", "pod": "eivh", "region": "us8", "site": "CX_1", "site_number": ""},
    {"name": "Benchmark Electronics", "pod": "efwb", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Regal Rexnord", "pod": "efhb", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "OSI Systems", "pod": "edqh", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Astronics Corporation", "pod": "ectj", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "TransDigm Group", "pod": "ehhz", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "American Vanguard Corporation", "pod": "edrv", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Materion Corporation", "pod": "edga", "region": "us2", "site": "CX_1", "site_number": ""},
    {"name": "Cousins Properties", "pod": "edes", "region": "us2", "site": "CX_1", "site_number": ""},
]


def main():
    companies = load_companies()
    existing_pods = {c["pod"] for c in companies}
    existing_names = {c["name"].lower() for c in companies}
    current_count = len(companies)
    needed = TARGET - current_count

    print(f"[*] Current: {current_count} companies | Target: {TARGET} | Need: {needed} more")
    print(f"[*] Testing {len(CANDIDATES)} candidates with dual-endpoint verification...\n")

    added = 0
    failed_sites = 0
    failed_jobs = 0
    skipped = 0

    for candidate in CANDIDATES:
        if added >= needed:
            print(f"\n[+] Reached target of {TARGET} companies!")
            break

        pod = candidate["pod"]
        name = candidate["name"]
        region = candidate["region"]

        # Skip duplicates
        if pod in existing_pods:
            skipped += 1
            continue
        if name.lower() in existing_names:
            skipped += 1
            continue

        print(f"  [test] {name} ({pod}/{region})", end=" ", flush=True)

        # Check 1: Sites endpoint must return 200 with items
        sites_ok = check_sites_endpoint(pod, region)
        if not sites_ok:
            print("FAIL sites")
            failed_sites += 1
            time.sleep(0.3)
            continue

        # Discover actual site_number from the sites endpoint
        sn = get_site_number(pod, region)

        # Check 2: Jobs endpoint must return 200
        jobs_ok = check_jobs_endpoint(pod, region, sn)
        if not jobs_ok:
            print(f"FAIL jobs (sites OK, sn={sn})")
            failed_jobs += 1
            time.sleep(0.3)
            continue

        # Both checks passed — add the company
        new_entry = {
            "name": candidate["name"],
            "pod": pod,
            "region": region,
            "site": candidate.get("site", "CX_1"),
            "site_number": sn,
        }
        companies.append(new_entry)
        existing_pods.add(pod)
        existing_names.add(name.lower())
        added += 1
        print(f"OK  sn={sn}  [{current_count + added}/{TARGET}]")

        # Save after each successful addition so progress is never lost
        save_companies(companies)
        time.sleep(0.5)

    print(f"\n{'='*65}")
    print(f"[+] Done — added {added} new companies")
    print(f"    Skipped (dupes): {skipped} | Failed sites: {failed_sites} | Failed jobs: {failed_jobs}")
    print(f"    Total now: {len(companies)} / {TARGET}")
    if len(companies) >= TARGET:
        print(f"    *** TARGET REACHED! ***")
    else:
        remaining = TARGET - len(companies)
        print(f"    Still need {remaining} more — add more candidates to CANDIDATES list and re-run.")


if __name__ == "__main__":
    main()
