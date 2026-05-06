"""
greenhouse_nologin_scraper.py
------------------------------
Scrapes active jobs from 435+ verified companies' public Greenhouse boards
using the Greenhouse public API (no auth or browser required).

API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
"""

import asyncio
import csv
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# -- Email ---------------------------------------------------------------------
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", "")

# -- Paths ---------------------------------------------------------------------
BASE_DIR       = Path(__file__).parent
SEEN_FILE      = BASE_DIR / "json" / "greenhouse_nologin_seen.json"
CSV_FILE       = BASE_DIR / "csv"  / "greenhouse_nologin_jobs.csv"
LAST_RUN_FILE  = BASE_DIR / "greenhouse_last_run_jobs.json"

API_URL        = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
MAX_CONCURRENT = 20
TIMEOUT        = 20.0

# -- Role matching -------------------------------------------------------------
ROLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bdata\s+analyst\b',                        re.I), "Data Analyst"),
    (re.compile(r'\banalytics?\s+engineer\b',                 re.I), "Analytics Engineer"),
    (re.compile(r'\bdata\s+engineer(?:ing)?\b',               re.I), "Data Engineer"),
    (re.compile(r'\b(?:business\s+intelligence|bi)\s+(?:analyst|developer|engineer|specialist)\b',
                                                              re.I), "BI"),
    (re.compile(r'\bdata\s+scientist\b',                      re.I), "Data Scientist"),
    (re.compile(r'\bbusiness\s+analyst\b',                    re.I), "Business Analyst"),
    (re.compile(r'\breporting\s+analyst\b',                   re.I), "Reporting Analyst"),
    (re.compile(r'\bsoftware\s+engineer(?:ing)?\b',           re.I), "Software Engineer"),
    (re.compile(r'\bsoftware\s+developer\b',                  re.I), "Software Developer"),
    (re.compile(r'\b(?:machine\s+learning|ml)\s+engineer\b',  re.I), "ML Engineer"),
    (re.compile(r'\bbackend\s+engineer\b',                    re.I), "Backend Engineer"),
    (re.compile(r'\bfull[\s-]?stack\s+engineer\b',            re.I), "Full-Stack Engineer"),
]

SENIOR_RE = re.compile(
    r'\b(senior|sr\.?|lead|staff|principal|manager|director|vp|'
    r'vice\s+president|head\s+of|associate\s+director)\b',
    re.I,
)

# -- US location check ---------------------------------------------------------
_US_ST = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
    "MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|"
    "TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
_US_CITIES = (
    r"new\s+york(?:\s+city)?|nyc|san\s+francisco|bay\s+area|silicon\s+valley|"
    r"los\s+angeles|chicago|seattle|boston|austin|denver|atlanta|miami|"
    r"dallas|houston|phoenix|portland|minneapolis|nashville|san\s+diego|"
    r"san\s+jose|washington\s+d\.?c\.?|new\s+jersey|nationwide"
)
_US_RE = re.compile(
    rf'(?:united\s+states|usa|u\.s\.a?|u\.s\.|,\s*(?:{_US_ST})\b|(?:{_US_CITIES}))',
    re.I,
)
_NON_US_RE = re.compile(
    r'\b(india|canada|united\s+kingdom|\buk\b|australia|germany|france|'
    r'netherlands|singapore|japan|china|brazil|mexico|ireland|sweden|'
    r'israel|poland|romania|portugal|czech|argentina|colombia|chile|'
    r'new\s+zealand|south\s+africa|dubai|uae|united\s+arab\s+emirates|'
    r'hong\s+kong|taiwan|south\s+korea|spain|italy|belgium|denmark|'
    r'finland|norway|switzerland|austria|turkey|russia|philippines|'
    r'indonesia|vietnam|nigeria|egypt|kenya|ghana|ethiopia|morocco|'
    r'pakistan|ukraine|greece|hungary|saudi\s+arabia|qatar|kuwait|'
    r'bahrain|jordan|iraq|thailand|malaysia|myanmar|sri\s+lanka|'
    r'bangladesh|nepal|cambodia|laos|peru|venezuela|ecuador|bolivia|'
    r'paraguay|uruguay|panama|costa\s+rica|guatemala|honduras|'
    r'el\s+salvador|cuba|dominican\s+republic|jamaica|trinidad|'
    r'senegal|cameroon|tanzania|uganda|algeria|tunisia|zimbabwe|'
    # Major non-US cities
    r'london|amsterdam|berlin|toronto|sydney|melbourne|paris|tokyo|'
    r'mumbai|bombay|bangalore|bengaluru|delhi|hyderabad|chennai|pune|'
    r'tel\s+aviv|warsaw|dublin|stockholm|copenhagen|oslo|helsinki|'
    r'vienna|zurich|madrid|barcelona|rome|prague|bucharest|lisbon|'
    r'shanghai|beijing|shenzhen|guangzhou|s[ao]\s+paulo|bogot[a]|'
    r'lima|santiago|buenos\s+aires|cape\s+town|johannesburg|nairobi|'
    r'karachi|lahore|dhaka|colombo|kuala\s+lumpur|jakarta|bangkok|'
    r'ho\s+chi\s+minh|manila|taipei|seoul|osaka|auckland|abu\s+dhabi|'
    r'riyadh|doha|accra|lagos|cairo|addis\s+ababa|dar\s+es\s+salaam|'
    r'kampala|casablanca|tunis|algiers|harare|kigali|lusaka|abuja|'
    r'kathmandu|colombo|phnom\s+penh|vientiane|yangon|athens|budapest|'
    r'kyiv|kiev|bucharest|belgrade|zagreb|sofia|bratislava|ljubljana|'
    r'tallinn|riga|vilnius|reykjavik|valletta|nicosia|limassol)\b',
    re.I,
)


def _is_us(loc: str) -> bool:
    if not loc:
        return True
    if _NON_US_RE.search(loc):
        return False
    if _US_RE.search(loc):
        return True
    if re.search(r'\b(remote|work[\s-]from[\s-]home|wfh|hybrid)\b', loc, re.I):
        return True
    return False


def _classify(title: str) -> str | None:
    for pat, label in ROLE_PATTERNS:
        if pat.search(title):
            return label
    return None


# -- 177 verified Greenhouse board slugs ---------------------------------------
COMPANIES: dict[str, str] = {
    # Analytics / Data Tooling
    "amplitude":          "Amplitude",
    "mixpanel":           "Mixpanel",
    "dataiku":            "Dataiku",
    "labelbox":           "Labelbox",
    "fivetran":           "Fivetran",
    "starburst":          "Starburst",
    "assemblyai":         "AssemblyAI",
    # Cloud / Infrastructure / Security
    "fastly":             "Fastly",
    "pagerduty":          "PagerDuty",
    "wizinc":             "Wiz",
    "abnormalsecurity":   "Abnormal Security",
    "axonius":            "Axonius",
    "netlify":            "Netlify",
    "sourcegraph91":      "Sourcegraph",
    "tanium":             "Tanium",
    # Enterprise SaaS / CRM / Marketing
    "hubspot":            "HubSpot",
    "intercom":           "Intercom",
    "gongio":             "Gong",
    "salesloft":          "SalesLoft",
    "iterable":           "Iterable",
    "attentive":          "Attentive",
    "klaviyo":            "Klaviyo",
    "typeform":           "Typeform",
    "contentful":         "Contentful",
    "movableink":         "Movable Ink",
    "yotpo":              "Yotpo",
    "impact":             "Impact.com",
    "appian":             "Appian",
    "smartsheet":         "Smartsheet",
    # Productivity / Collaboration
    "asana":              "Asana",
    "airtable":           "Airtable",
    "lattice":            "Lattice",
    "gleanwork":          "Glean",
    "moveworks":          "Moveworks",
    # Fintech / Payments / Insurance
    "affirm":             "Affirm",
    "brex":               "Brex",
    "robinhood":          "Robinhood",
    "carta":              "Carta",
    "marqeta":            "Marqeta",
    "betterment":         "Betterment",
    "ethoslife":          "Ethos Life",
    "adyen":              "Adyen",
    "riskified":          "Riskified",
    "amwins":             "Amwins",
    # HR Tech / Workforce
    "gusto":              "Gusto",
    "justworks":          "Justworks",
    # Healthcare / Health Tech
    "doximity":           "Doximity",
    "modernhealth":       "Modern Health",
    "flatironhealth":     "Flatiron Health",
    "transcarent":        "Transcarent",
    "oscar":              "Oscar Health",
    "calm":               "Calm",
    # E-commerce / Marketplace
    "faire":              "Faire",
    "poshmark":           "Poshmark",
    "opentable":          "OpenTable",
    "rebag":              "Rebag",
    "instacart":          "Instacart",
    # Travel / Mobility / Hospitality
    "lyft":               "Lyft",
    "tripadvisor":        "TripAdvisor",
    "vacasa":             "Vacasa",
    # Real Estate / PropTech
    "opendoor":           "Opendoor",
    "orchard":            "Orchard",
    # Logistics / Supply Chain
    "flexport":           "Flexport",
    "project44":          "Project44",
    "samsara":            "Samsara",
    # Media / Education / Consumer
    "reddit":             "Reddit",
    "discord":            "Discord",
    "udemy":              "Udemy",
    "coursera":           "Coursera",
    "duolingo":           "Duolingo",
    "nextdoor":           "Nextdoor",
    "voxmedia":           "Vox Media",
    "medium":             "Medium",
    # AI / ML Platforms
    "scaleai":            "Scale AI",
    "togetherai":         "Together AI",
    # General Tech / Software
    "airbnb":             "Airbnb",
    "doordashusa":        "DoorDash",
    "dropbox":            "Dropbox",
    "figma":              "Figma",
    "postman":            "Postman",
    "twilio":             "Twilio",
    "okta":               "Okta",
    "databricks":         "Databricks",
    "lob":                "Lob",
    "sendbird":           "Sendbird",
    "upwork":             "Upwork",
    "checkr":             "Checkr",
    "truework":           "Truework",
    "unqork":             "Unqork",
    "observeai":          "Observe.AI",
    "pendo":              "Pendo",
    "elastic":            "Elastic",
    # AI / ML
    "anthropic":          "Anthropic",
    # Dev Tools / Infrastructure
    "gitlab":             "GitLab",
    "vercel":             "Vercel",
    "grafanalabs":        "Grafana Labs",
    "temporal":           "Temporal",
    "redpandadata":       "Redpanda",
    "harnessinc":         "Harness",
    "launchdarkly":       "LaunchDarkly",
    "pulumicorporation":  "Pulumi",
    "dagsterlabs":        "Dagster Labs",
    "merge":              "Merge",
    "warp":               "Warp",
    "cribl":              "Cribl",
    "clickhouse":         "ClickHouse",
    "acquia":             "Acquia",
    # Data / Analytics
    "dbtlabsinc":         "dbt Labs",
    "sigmacomputing":     "Sigma Computing",
    "hightouch":          "Hightouch",
    "metronome":          "Metronome",
    "alphasense":         "AlphaSense",
    "yipitdata":          "YipitData",
    "guidepoint":         "Guidepoint Global",
    # Security
    "zscaler":            "Zscaler",
    "chainguard":         "Chainguard",
    "spycloud":           "SpyCloud",
    "verkada":            "Verkada",
    "recordedfuture":     "Recorded Future",
    "arkoselabs":         "Arkose Labs",
    "orca":               "Orca Security",
    # Marketing / AdTech
    "braze":              "Braze",
    "stackadapt":         "StackAdapt",
    # SaaS / Productivity
    "calendly":           "Calendly",
    "webflow":            "Webflow",
    "qualtrics":          "Qualtrics",
    "lumos":              "Lumos",
    # HR Tech
    "remotecom":          "Remote",
    # Finance / Fintech
    "stripe":             "Stripe",
    "sofi":               "SoFi",
    "mercury":            "Mercury",
    "figure":             "Figure",
    "lithic":             "Lithic",
    "upstart":            "Upstart",
    "ocrolusinc":         "Ocrolus",
    "enova":              "Enova",
    "smartasset":         "SmartAsset",
    "humaninterest":      "Human Interest",
    "block":              "Block",
    "stockx":             "StockX",
    "ripple":             "Ripple",
    "payoneer":           "Payoneer",
    "toast":              "Toast",
    "monzo":              "Monzo",
    "fireblocks":         "Fireblocks",
    # Healthcare
    "springhealth66":     "Spring Health",
    "omadahealth":        "Omada Health",
    "coherehealth":       "Cohere Health",
    "garnerhealth":       "Garner Health",
    "natera":             "Natera",
    "oura":               "Oura",
    "carrotfertility":    "Carrot Fertility",
    "zocdoc":             "Zocdoc",
    "cerebral":           "Cerebral",
    # Media / Entertainment
    "thenewyorktimes":    "The New York Times",
    "axios":              "Axios",
    "twitch":             "Twitch",
    "pinterest":          "Pinterest",
    "scopely":            "Scopely",
    "hudl":               "Hudl",
    "renttherunway":      "Rent the Runway",
    "speechify":          "Speechify",
    "newsbreak":          "NewsBreak",
    "gofundme":           "GoFundMe",
    # Defense / Hard Tech
    "andurilindustries":  "Anduril Industries",
    "flyzipline":         "Zipline",
    # E-commerce / Commerce
    "loop":               "Loop Returns",
    "afresh":             "Afresh",
    "thedutchie":         "Dutchie",
    # Transportation
    "via":                "Via",
    "fleetio":            "Fleetio",
    # Biotech / Life Sciences
    "ginkgobioworks":     "Ginkgo Bioworks",
    # Research / Nonprofit
    "a16z":               "Andreessen Horowitz",
    "ithaka":             "ITHAKA",
    # Storage / Communications
    "purestorage":        "Pure Storage",
    "bandwidth":          "Bandwidth",
    "ujet":               "UJET",
    # AI / LLM / Research
    "xai":                          "xAI",
    "deepmind":                     "Google DeepMind",
    "thealleninstitute":            "Allen Institute for AI",
    "fireworksai":                  "Fireworks AI",
    "lightningai":                  "Lightning AI",
    "hebbia":                       "Hebbia",
    "snorkelai":                    "Snorkel AI",
    # Crypto / Web3
    "okx":                          "OKX",
    "blockchain":                   "Blockchain.com",
    "galaxydigitalservices":        "Galaxy Digital Services",
    "gemini":                       "Gemini",
    # Fintech / Payments / Banking
    "chime":                        "Chime",
    "tripactions":                  "Navan",
    "nubank":                       "Nubank",
    "selffinancial":                "Self Financial",
    "kikoff":                       "Kikoff",
    "icapitalnetwork":              "iCapital Network",
    "galileofinancialtechnologies": "Galileo Financial Technologies",
    "convera":                      "Convera",
    "wisetack":                     "Wisetack",
    "earnin":                       "EarnIn",
    "dvtrading":                    "DV Trading",
    "pdtpartners":                  "PDT Partners",
    "sixthstreet":                  "Sixth Street",
    "schonfeld":                    "Schonfeld",
    "point72":                      "Point72",
    "genevatrading":                "Geneva Trading",
    "ctccampusboard":               "Chicago Trading Company",
    "forafinancial":                "Fora Financial",
    # Healthcare Tech / Digital Health
    "letsgetchecked":               "LetsGetChecked",
    "billiontoone":                 "BillionToOne",
    "interwellhealth":              "Interwell Health",
    "smarterdx":                    "SmarterDx",
    "coverahealth":                 "Covera Health",
    "truveta":                      "Truveta",
    "smithrx":                      "SmithRx",
    "alma":                         "Alma",
    "betterhelpcom":                "BetterHelp",
    "growtherapy":                  "Grow Therapy",
    "tomorrowhealth":               "Tomorrow Health",
    "usenourish":                   "Nourish",
    "parachutehealth":              "Parachute Health",
    "eleoshealth":                  "Eleos Health",
    "sparkadvisors":                "Spark Advisors",
    "abacusinsights":               "Abacus Insights",
    "hs":                           "Headspace",
    # Biotech / Genomics
    "formationbio":                 "Formation Bio",
    "biohub":                       "Chan Zuckerberg Biohub",
    "flagshippioneeringinc":        "Flagship Pioneering",
    # Dev Tools / SaaS
    "datadog":                      "Datadog",
    "cloudflare":                   "Cloudflare",
    "workato":                      "Workato",
    "singlestore":                  "SingleStore",
    "bloomreach":                   "Bloomreach",
    "everlaw":                      "Everlaw",
    "lucidsoftware":                "Lucid Software",
    "customerio":                   "Customer.io",
    "commercetools":                "commercetools",
    "devrev":                       "DevRev",
    "tekion":                       "Tekion",
    "boulevard":                    "Boulevard",
    "upkeep":                       "UpKeep",
    "vts":                          "VTS",
    "6sense":                       "6sense",
    "nextroll":                     "NextRoll",
    "northbeam":                    "Northbeam",
    "workera":                      "Workera",
    "mitratech":                    "Mitratech",
    "quillbot":                     "QuillBot",
    "rithum":                       "Rithum",
    "recharge":                     "Recharge",
    "dhigroupinc":                  "DHI Group",
    "life360":                      "Life360",
    "fossainc":                     "FOSSA",
    "opensesame":                   "OpenSesame",
    "gomotive":                     "Motive",
    # Cybersecurity
    "armissecurity":                "Armis Security",
    "securityscorecard":            "SecurityScorecard",
    "keepersecurity":               "Keeper Security",
    "censys":                       "Censys",
    "pingidentity":                 "Ping Identity",
    "opswat":                       "OPSWAT",
    # Autonomous Vehicles / Robotics / Aerospace
    "spacex":                       "SpaceX",
    "waymo":                        "Waymo",
    "nuro":                         "Nuro",
    "lucidmotors":                  "Lucid Motors",
    "aurorainnovation":             "Aurora Innovation",
    "appliedintuition":             "Applied Intuition",
    "relativity":                   "Relativity Space",
    "nimblerobotics":               "Nimble Robotics",
    "maymobility":                  "May Mobility",
    "diligentrobotics":             "Diligent Robotics",
    "archer56":                     "Archer Aviation",
    # Energy / Clean Tech
    "energyhub":                    "EnergyHub",
    "oklo":                         "Oklo",
    "clearwayjobs":                 "Clearway Energy",
    # Data / Analytics
    "moloco":                       "Moloco",
    "enigmaio":                     "Enigma",
    "consumeredge":                 "Consumer Edge",
    "khanacademy":                  "Khan Academy",
    "planetlabs":                   "Planet Labs",
    "soci":                         "SOCi",
    "acuitymd":                     "AcuityMD",
    # Enterprise / Professional Services
    "capco":                        "Capco",
    "charlesriverassociates":       "Charles River Associates",
    "cision":                       "Cision",
    "brandwatch":                   "Brandwatch",
    "canonical":                    "Canonical",
    "smartlyio":                    "Smartly",
    "oportun":                      "Oportun",
    "hut8":                         "Hut 8",
    "inmobi":                       "InMobi",
    # E-commerce / Marketplace
    "iherb":                        "iHerb",
    "shipmonk":                     "ShipMonk",
    "goatgroup":                    "GOAT Group",
    "glossgenius":                  "GlossGenius",
    "instawork":                    "Instawork",
    "hungryroot":                   "Hungryroot",
    "paperlesspost":                "Paperless Post",
    "homeward":                     "Homeward",
    # Media / Entertainment
    "sonyinteractiveentertainmentglobal": "PlayStation / SIE",
    "nationalpublicradioinc":       "NPR",
    "later":                        "Later",
    "newsela":                      "Newsela",
    "forbes":                       "Forbes",
    "sonymusicentertainment":       "Sony Music Entertainment",
    # EdTech
    "2u":                           "2U / edX",
    "correlationone":               "Correlation One",
    # Defense / Gov Tech
    "defenseunicorns":              "Defense Unicorns",
    "redcellpartners":              "Red Cell Partners",
    # AI Hardware
    "cerebrassystems":              "Cerebras Systems",
    "applovin":                     "AppLovin",
    # Proptech
    "apartmentiq":                  "ApartmentIQ",
    "entera":                       "Entera",
    # Fintech / Payments / Crypto
    "upgrade":          "Upgrade",
    "tabapay":          "TabaPay",
    "blend":            "Blend",
    "highradius":       "HighRadius",
    "taxbit":           "TaxBit",
    "pleo":             "Pleo",
    "n26":              "N26",
    "tide":             "Tide",
    "bitgo":            "BitGo",
    "binance":          "Binance",
    "consensys":        "ConsenSys",
    "nansen":           "Nansen",
    "truepill":         "TruePill",
    "viagogo":          "Viagogo",
    "branchmetrics":    "Branch Metrics",
    "appsflyer":        "AppsFlyer",
    "branch":           "Branch",
    # HR Tech
    "workstream":       "Workstream",
    # Cybersecurity
    "tines":            "Tines",
    "cybereason":       "Cybereason",
    "exabeam":          "Exabeam",
    "torq":             "Torq",
    "huntress":         "Huntress",
    "kaseya":           "Kaseya",
    "solarwinds":       "SolarWinds",
    "knowbe4":          "KnowBe4",
    "forter":           "Forter",
    "jumio":            "Jumio",
    "alloy":            "Alloy",
    "netskope":         "Netskope",
    "lookout":          "Lookout",
    "veracode":         "Veracode",
    "blackduck":        "Black Duck",
    "jfrog":            "JFrog",
    "nexus":            "Nexus",
    # Data / Databases / Infra
    "cockroachlabs":    "Cockroach Labs",
    "mongodb":          "MongoDB",
    "neo4j":            "Neo4j",
    "planetscale":      "PlanetScale",
    "imply":            "Imply",
    "materialize":      "Materialize",
    "sumologic":        "Sumo Logic",
    "newrelic":         "New Relic",
    "honeycomb":        "Honeycomb",
    "kentik":           "Kentik",
    "sisense":          "Sisense",
    "phdata":           "phData",
    "stitch":           "Stitch",
    "celonis":          "Celonis",
    "yugabyte":         "Yugabyte",
    "catchpoint":       "Catchpoint",
    # Dev Tools / Testing / CI
    "jetbrains":        "JetBrains",
    "circleci":         "CircleCI",
    "buildkite":        "Buildkite",
    "mabl":             "Mabl",
    "saucelabs":        "Sauce Labs",
    "testlio":          "Testlio",
    "cortex":           "Cortex",
    "roadie":           "Roadie",
    "rubrik":           "Rubrik",
    "druva":            "Druva",
    "commvault":        "Commvault",
    # CMS / Web
    "storyblok":        "Storyblok",
    "contentstack":     "Contentstack",
    # Communications / CPaaS
    "zenvia":           "Zenvia",
    "vonage":           "Vonage",
    "bird":             "Bird",
    "five9":            "Five9",
    "nice":             "NICE",
    "dialpad":          "Dialpad",
    # E-commerce / SaaS
    "squarespace":      "Squarespace",
    "trustpilot":       "Trustpilot",
    "zuora":            "Zuora",
    "pandadoc":         "PandaDoc",
    "surveymonkey":     "SurveyMonkey",
    "alchemer":         "Alchemer",
    # Sales / Marketing / Analytics
    "apollo":           "Apollo.io",
    "comet":            "Comet ML",
    "showpad":          "Showpad",
    "disco":            "DISCO Legal",
    "cognism":          "Cognism",
    "bombora":          "Bombora",
    "zoominfo":         "ZoomInfo",
    "hootsuite":        "Hootsuite",
    "constantcontact":  "Constant Contact",
    "listrak":          "Listrak",
    "aha":              "Aha!",
    "grin":             "Grin",
    "planable":         "Planable",
    "wrike":            "Wrike",
    "airship":          "Airship",
    "liftoff":          "Liftoff",
    # Ops / Scheduling / Restaurant Tech
    "sterling":         "Sterling",
    "bloomerang":       "Bloomerang",
    "flodesk":          "Flodesk",
    "7shifts":          "7shifts",
    "revel":            "Revel Systems",
    "touchbistro":      "TouchBistro",
    # Insurance / Legal / Construction
    "pieinsurance":     "Pie Insurance",
    "openly":           "Openly Insurance",
    "counterpart":      "Counterpart Insurance",
    "knock":            "Knock",
    "fieldwire":        "Fieldwire",
    "siteline":         "Siteline",
    # Health / Wellness / Fitness
    "lightmatter":      "Lightmatter",
    "pathai":           "PathAI",
    "ritual":           "Ritual",
    "daylight":         "Daylight Health",
    "zwift":            "Zwift",
    "gympass":          "Gympass",
    "peloton":          "Peloton",
    "classpass":        "ClassPass",
    "mindbody":         "Mindbody",
    "arizeai":          "Arize AI",
    # Gaming / Sports / Entertainment
    "riotgames":        "Riot Games",
    "naughtydog":       "Naughty Dog",
    "insomniac":        "Insomniac Games",
    "2k":               "2K Games",
    "bungie":           "Bungie",
    "gearbox":          "Gearbox Software",
    "seatgeek":         "SeatGeek",
    "fanduel":          "FanDuel",
    "buzzfeed":         "BuzzFeed",
    # Clean Energy / EV / Quantum
    "sunnova":          "Sunnova",
    "redwoodmaterials": "Redwood Materials",
    "chargepoint":      "ChargePoint",
    "faradayfuture":    "Faraday Future",
    "archer":           "Archer Aviation",
    "ionq":             "IonQ",
    "psiquantum":       "PsiQuantum",
    # Biotech
    "abcellera":        "AbCellera",
    "atomwise":         "Atomwise",
    "caribou":          "Caribou Biosciences",
    "verve":            "Verve Therapeutics",
    "resilience":       "Resilience Bio",
    "absci":            "Absci",
    "seer":             "Seer Bio",
    "twistbioscience":  "Twist Bioscience",
    # Cloud / Infrastructure / IT
    "coreweave":        "CoreWeave",
    "automox":          "Automox",
    "connectwise":      "ConnectWise",
    "jamf":             "Jamf",
    "make":             "Make",
    "orkes":            "Orkes",
    "symphony":         "Symphony",
    # EdTech
    "springboard":      "Springboard",
}


# -- Async scraping ------------------------------------------------------------

async def _get_office_location(client: httpx.AsyncClient, slug: str, job_id: str) -> str:
    """Fetch individual job detail and return a combined offices location string."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
    try:
        resp = await client.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        parts = []
        for office in data.get("offices", []):
            oloc = ((office.get("location") or {}).get("name") or "")
            oname = office.get("name", "")
            if oloc:
                parts.append(oloc)
            elif oname:
                parts.append(oname)
        return " | ".join(parts)
    except Exception:
        return ""


async def _fetch(
    client: httpx.AsyncClient,
    slug: str,
    company: str,
    sem: asyncio.Semaphore,
) -> list[dict]:
    async with sem:
        url = API_URL.format(slug=slug)
        try:
            resp = await client.get(url, timeout=TIMEOUT)
        except Exception as exc:
            print(f"[!] {company} ({slug}): {exc}")
            return []

        if resp.status_code == 404:
            print(f"[-] {company} ({slug}): board not found")
            return []
        if resp.status_code != 200:
            print(f"[!] {company} ({slug}): HTTP {resp.status_code}")
            return []

        try:
            data = resp.json()
        except Exception:
            return []

        hits: list[dict] = []
        for job in data.get("jobs", []):
            title   = (job.get("title") or "").strip()
            loc     = ((job.get("location") or {}).get("name") or "").strip()
            job_id  = str(job.get("id", ""))
            job_url = job.get("absolute_url", "")
            raw_fp  = job.get("first_published") or job.get("updated_at") or ""
            try:
                posted = datetime.fromisoformat(raw_fp).strftime("%Y-%m-%d") if raw_fp else ""
            except Exception:
                posted = raw_fp[:10] if raw_fp else ""

            role = _classify(title)
            if not role:
                continue
            if SENIOR_RE.search(title):
                continue
            if _NON_US_RE.search(loc):
                continue
            if _NON_US_RE.search(title):
                continue
            if not _is_us(loc):
                # Ambiguous location (e.g. "In-Office", "Hybrid") -- check offices
                office_loc = await _get_office_location(client, slug, job_id)
                if office_loc and _NON_US_RE.search(office_loc):
                    continue  # offices confirm non-US

            hits.append({
                "job_id":   job_id,
                "title":    title,
                "company":  company,
                "location": loc,
                "role":     role,
                "url":      job_url,
                "posted":   posted,
            })

        if hits:
            print(f"[+] {company}: {len(hits)} match(es)")
        return hits


async def _scrape_all() -> list[dict]:
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; greenhouse-nologin-scraper/1.0)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tasks = [
            _fetch(client, slug, name, sem)
            for slug, name in COMPANIES.items()
        ]
        batches = await asyncio.gather(*tasks, return_exceptions=True)
    return [job for batch in batches if isinstance(batch, list) for job in batch]


# -- Email ---------------------------------------------------------------------

def _send_email(jobs: list[dict]) -> None:
    if not EMAIL_PASSWORD:
        print("[!] GMAIL_APP_PASSWORD not set -- skipping email.")
        return

    by_role: dict[str, list[dict]] = {}
    for j in jobs:
        by_role.setdefault(j["role"], []).append(j)

    sorted_jobs = sorted(jobs, key=lambda j: j.get("posted") or "", reverse=True)
    rows = ""
    for j in sorted_jobs:
        new_badge = (
            "<span style='background:#22c55e;color:#fff;font-size:10px;"
            "font-weight:bold;padding:2px 5px;border-radius:3px;margin-left:5px'>NEW</span>"
            if j.get("is_new") else ""
        )
        rows += (
            f"<tr>"
            f"<td>{j['title']}{new_badge}</td>"
            f"<td>{j['company']}</td>"
            f"<td>{j['location'] or '--'}</td>"
            f"<td>{j['role']}</td>"
            f"<td>{j.get('posted') or '--'}</td>"
            f"<td><a href='{j['url']}'>Link</a></td>"
            f"</tr>"
        )

    body = f"""
    <h2>Greenhouse No-Login Scraper -- {len(jobs)} new job(s) (posted &le;3 days)</h2>
    <p>Scraped {len(COMPANIES)} company boards. Showing only new postings from the last 3 days.</p>
    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;font-family:sans-serif;font-size:13px">
      <tr style="background:#e0e0e0">
        <th>Title</th><th>Company</th><th>Location</th><th>Role</th><th>Posted</th><th>Link</th>
      </tr>
      {rows}
    </table>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Greenhouse No-Login: {len(jobs)} new job(s) (<=3 days old)"
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, EMAIL_TO, msg.as_string())
        print(f"[+] Email sent to {EMAIL_TO}")
    except Exception as exc:
        print(f"[!] Email failed: {exc}")


# -- Main ----------------------------------------------------------------------

def main() -> None:
    seen: set[str] = set()
    if SEEN_FILE.exists():
        seen = set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))

    last_run_ids: set[str] = set()
    if LAST_RUN_FILE.exists():
        last_run_ids = set(json.loads(LAST_RUN_FILE.read_text(encoding="utf-8")))

    print(f"[i] {len(seen)} previously seen IDs | scraping {len(COMPANIES)} boards...")

    all_jobs = asyncio.run(_scrape_all())
    for job in all_jobs:
        job["is_new"] = job["job_id"] not in last_run_ids
    print(f"[i] Total matching: {len(all_jobs)}")

    new_jobs = [j for j in all_jobs if j["job_id"] not in seen]
    print(f"[i] New this run:   {len(new_jobs)}")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    recent_jobs = [j for j in new_jobs if (j.get("posted") or "") >= cutoff]
    print(f"[i] Posted <=3 days: {len(recent_jobs)}")

    if new_jobs:
        CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_header = not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0
        fieldnames = ["job_id", "title", "company", "location", "role", "posted", "url", "found_at", "is_new"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        new_jobs_sorted = sorted(new_jobs, key=lambda j: j.get("posted") or "", reverse=True)
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            for j in new_jobs_sorted:
                writer.writerow({**j, "found_at": now})
        print(f"[+] Appended to {CSV_FILE}")

    # Always update seen with everything observed this run (including already-seen)
    updated_seen = seen | {j["job_id"] for j in all_jobs}
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(updated_seen)), encoding="utf-8")

    LAST_RUN_FILE.write_text(json.dumps(sorted({j["job_id"] for j in all_jobs})), encoding="utf-8")

    if recent_jobs:
        _send_email(recent_jobs)
    else:
        print("[i] No recent new jobs -- skipping email.")


if __name__ == "__main__":
    main()
