"""
Ashby Jobs Scanner
------------------
Scans verified Ashby companies for target roles.
No login or account required — uses the public Ashby job board API.

API: https://api.ashbyhq.com/posting-api/job-board/{slug}

To add a company:
  1. Open jobs.ashbyhq.com/{slug} in a browser
  2. If the page loads with job listings, the slug is valid
  3. Add slug -> display name to COMPANIES below

Run: python ashby_jobs_scanner.py
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
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────────
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENTS      = [e.strip() for e in os.environ.get("EMAIL_TO", "").split(",") if e.strip()]

SEEN_FILE    = Path(__file__).parent / "json" / "ashby_seen_jobs.json"
MASTER_CSV   = Path(__file__).parent / "csv"  / "new_jobs.csv"
CONCURRENCY  = 20
MAX_AGE_DAYS = 30  # wider window — Ashby is lower volume than Lever

# -- Master CSV ----------------------------------------------------------------
_MASTER_COLS    = ["source", "job_id", "title", "company", "location", "role", "posted", "url", "found_at"]
_MASTER_ROLE_RE = re.compile(r'data\s+analyst|data\s+engineer|business\s+intelligence', re.I)

def _classify_master_role(title: str) -> str:
    if re.search(r'data\s+engineer',         title, re.I): return "Data Engineer"
    if re.search(r'data\s+analyst',          title, re.I): return "Data Analyst"
    if re.search(r'business\s+intelligence', title, re.I): return "Business Intelligence"
    return ""

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

ALLOWED_TITLES = re.compile(
    r"\b(data\s+engineer|data\s+analyst|analytics\s+engineer|analytics\s+analyst"
    r"|business\s+intelligence\s+analyst|machine\s+learning\s+engineer"
    r"|data\s+scientist|ai\s+engineer|software\s+developer|software\s+engineer)\b",
    re.I
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

# ── Verified Ashby companies ──────────────────────────────────────────────────────
# Slugs confirmed via jobs.ashbyhq.com/{slug}.
# Invalid slugs return HTTP 404 and are silently skipped — safe to keep speculative entries.
COMPANIES: dict[str, str] = {
    # Fintech / Finance / Payments
    "ramp":             "Ramp",
    "mercury":          "Mercury",
    "plaid":            "Plaid",
    "moderntreasury":   "Modern Treasury",
    "unit":             "Unit",
    "deel":             "Deel",
    "persona":          "Persona",
    "sardine":          "Sardine",
    "column":           "Column",
    "tripactions":      "Navan (TripActions)",
    "expensify":        "Expensify",
    "pleo":             "Pleo",
    "spendesk":         "Spendesk",
    "extend":           "Extend",
    "rho":              "Rho",
    "relay":            "Relay",
    "relayfi":          "Relay Financial",
    "found":            "Found",
    "novo":             "Novo",
    "paddle":           "Paddle",
    "lago":             "Lago",
    "orb":              "Orb",
    "stigg":            "Stigg",
    "creem":            "Creem",
    "anrok":            "Anrok",
    "numeral":          "Numeral",
    "fonoa":            "Fonoa",
    "kintsugi":         "Kintsugi",
    "marqeta":          "Marqeta",
    "synctera":         "Synctera",
    "modernfi":         "ModernFi",
    "ledger":           "Ledger",
    # Productivity / SaaS / Collaboration
    "retool":           "Retool",
    "notion":           "Notion",
    "superhuman":       "Superhuman",
    "linear":           "Linear",
    "airtable":         "Airtable",
    "attio":            "Attio",
    "clickup":          "ClickUp",
    "fellow":           "Fellow",
    "loom":             "Loom",
    "mural":            "Mural",
    "tldraw":           "tldraw",
    "frontapp":         "Front",
    "helpscout":        "Help Scout",
    "kustomer":         "Kustomer",
    "pylon":            "Pylon",
    "plain":            "Plain",
    "gamma":            "Gamma",
    "scribe":           "Scribe",
    "tango":            "Tango",
    "buffer":           "Buffer",
    "hatch":            "Hatch",
    "slab":             "Slab",
    "thinkific":        "Thinkific",
    "metaview":         "Metaview",
    "qualified":        "Qualified",
    "chilipiper":       "Chili Piper",
    "motion":           "Motion",
    # Sales / Marketing / Growth
    "lemlist":          "Lemlist",
    "salesloft":        "Salesloft",
    "commonroom":       "Common Room",
    "mutiny":           "Mutiny",
    "talkdesk":         "Talkdesk",
    "replicant":        "Replicant",
    "level":            "Level AI",
    # Analytics / Data Tooling
    "posthog":          "PostHog",
    "wandb":            "Weights & Biases",
    "hex":              "Hex",
    "preset":           "Preset",
    "chalk":            "Chalk",
    "arize":            "Arize AI",
    "deepnote":         "Deepnote",
    "cube":             "Cube",
    "lightdash":        "Lightdash",
    "atlan":            "Atlan",
    "montecarlodata":   "Monte Carlo",
    "anomalo":          "Anomalo",
    "datafold":         "Datafold",
    "airbyte":          "Airbyte",
    "hightouch":        "Hightouch",
    "prefect":          "Prefect",
    "astronomer":       "Astronomer",
    "windmill":         "Windmill",
    "materialize":      "Materialize",
    "rerun":            "Rerun",
    # AI / LLM / Generative
    "cohere":           "Cohere",
    "perplexity":       "Perplexity AI",
    "elevenlabs":       "ElevenLabs",
    "runway":           "Runway",
    "adept":            "Adept AI",
    "cognition":        "Cognition",
    "descript":         "Descript",
    "pika":             "Pika",
    "captions":         "Captions AI",
    "heygen":           "HeyGen",
    "harvey":           "Harvey AI",
    "sierra":           "Sierra AI",
    "anysphere":        "Cursor",
    "luma":             "Luma AI",
    "imbue":            "Imbue",
    "poolside":         "Poolside",
    "openai":           "OpenAI",
    "mistral":          "Mistral AI",
    "character":        "Character.AI",
    "writer":           "Writer",
    "ideogram":         "Ideogram",
    "krea":             "Krea AI",
    "openart":          "OpenArt",
    "suno":             "Suno",
    "tavus":            "Tavus",
    "synthesia":        "Synthesia",
    "decagon":          "Decagon",
    "lindy":            "Lindy AI",
    "rasa":             "Rasa",
    "cartesia":         "Cartesia",
    "rev":              "Rev",
    "deepgram":         "Deepgram",
    "lambda":           "Lambda Labs",
    "anyscale":         "Anyscale",
    "rewind":           "Rewind AI",
    "owkin":            "Owkin",
    "cradlebio":        "Cradle",
    "rain":             "Rain AI",
    "lightning":        "Lightning AI",
    "mercor":           "Mercor",
    "speak":            "Speak",
    "factory":          "Factory AI",
    "lovable":          "Lovable",
    "synthflow":        "Synthflow AI",
    "vapi":             "Vapi",
    "axelera":          "Axelera AI",
    # AI Infra / Search / RAG / Agents
    "langchain":        "LangChain",
    "llamaindex":       "LlamaIndex",
    "pinecone":         "Pinecone",
    "weaviate":         "Weaviate",
    "exa":              "Exa",
    "context":          "Context",
    "unstructured":     "Unstructured",
    "browserbase":      "Browserbase",
    "e2b":              "E2B",
    "n8n":              "n8n",
    "zapier":           "Zapier",
    "anon":             "Anon",
    "encord":           "Encord",
    "roboflow":         "Roboflow",
    "braintrust":       "Braintrust",
    "langfuse":         "Langfuse",
    "etched":           "Etched",
    # Developer Tools / Editors
    "raycast":          "Raycast",
    "warp":             "Warp",
    "cursor":           "Cursor",
    "continue":         "Continue",
    "kilocode":         "Kilo Code",
    "stainlessapi":     "Stainless",
    "speakeasy":        "Speakeasy",
    # Developer Infrastructure / Hosting / DB
    "supabase":         "Supabase",
    "replit":           "Replit",
    "modal":            "Modal Labs",
    "clerk":            "Clerk",
    "workos":           "WorkOS",
    "stytch":           "Stytch",
    "resend":           "Resend",
    "prisma":           "Prisma",
    "expo":             "Expo",
    "mintlify":         "Mintlify",
    "convex":           "Convex",
    "convex-dev":       "Convex (Dev)",
    "inngest":          "Inngest",
    "zed":              "Zed",
    "baseten":          "Baseten",
    "codeium":          "Codeium",
    "railway":          "Railway",
    "vercel":           "Vercel",
    "render":           "Render",
    "neon":             "Neon",
    "temporal":         "Temporal",
    "restate":          "Restate",
    "polar":            "Polar",
    "depot":            "Depot",
    "namespace":        "Namespace Labs",
    "wundergraph":      "WunderGraph",
    "apollo-graphql":   "Apollo GraphQL",
    "kong":             "Kong",
    "saturn":           "Saturn Cloud",
    "crusoe":           "Crusoe",
    "tensorwave":       "TensorWave",
    "qawolf":           "QA Wolf",
    # Observability / Monitoring / Analytics
    "sentry":           "Sentry",
    "fullstory":        "FullStory",
    "amplitude":        "Amplitude",
    "freshpaint":       "Freshpaint",
    "statsig":          "Statsig",
    "launchdarkly":     "LaunchDarkly",
    "knock":            "Knock",
    # Security / Compliance / Identity
    "vanta":            "Vanta",
    "drata":            "Drata",
    "snyk":             "Snyk",
    "semgrep":          "Semgrep",
    "socket":           "Socket",
    "wiz":              "Wiz",
    "orca":             "Orca Security",
    "1password":        "1Password",
    "savvy":            "Savvy Security",
    "nudge":            "Nudge Security",
    "materialsecurity": "Material Security",
    "cyberhaven":       "Cyberhaven",
    "blink":            "Blink Ops",
    "castle":           "Castle",
    "feathr":           "Feathr",
    # Healthcare / Bio / Therapeutics
    "medallion":        "Medallion",
    "insitro":          "Insitro",
    "asimov":           "Asimov",
    "abridge":          "Abridge",
    "ambiencehealthcare":"Ambience Healthcare",
    "atomic":           "Atomic AI",
    "radai":            "Rad AI",
    "candidhealth":     "Candid Health",
    "talkiatry":        "Talkiatry",
    "headway":          "Headway",
    "rula":             "Rula",
    "grow-therapy":     "Grow Therapy",
    "sondermind":       "SonderMind",
    "lyrahealth":       "Lyra Health",
    # Climate / Energy / Sustainability
    "watershed":        "Watershed",
    "sweep":            "Sweep",
    "twelve":           "Twelve",
    "formenergy":       "Form Energy",
    "heirloomcarbon":   "Heirloom",
    # Media / Consumer / Creator
    "beehiiv":          "Beehiiv",
    "substack":         "Substack",
    "ghost":            "Ghost",
    "kit":              "Kit (ConvertKit)",
    "patreon":          "Patreon",
    "passes":           "Passes",
    "partiful":         "Partiful",
    # E-commerce / Retail
    "bolt":             "Bolt",
    "rebuy":            "Rebuy",
    "verkada":          "Verkada",
    "attentivemobile":  "Attentive",
    "recharge":         "Recharge",
    "tapcart":          "Tapcart",
    # HR / People / Hiring / Workforce
    "oyster":           "Oyster",
    "g2i":              "G2i",
    "humaans":          "Humaans",
    "charthop":         "ChartHop",
    "leapsome":         "Leapsome",
    "betterup":         "BetterUp",
    "mosey":            "Mosey",
    "merge":            "Merge",
    "finch":            "Finch",
    "rutter":           "Rutter",
    "codat":            "Codat",
    # Productivity / Knowledge / Meetings
    "tactiq":           "Tactiq",
    "fathom":           "Fathom",
    "maven":            "Maven",
    # Defense / Aerospace / Robotics / Mobility
    "saronic":          "Saronic",
    "skydio":           "Skydio",
    "applied":          "Applied Intuition",
    "horizon":          "Horizon",
    "physicalintelligence":"Physical Intelligence",
    "1x":               "1X Technologies",
    "figure":           "Figure AI",
    "sanctuary":        "Sanctuary AI",
    "plaud":            "Plaud AI",
    "monumental":       "Monumental",
    "boom":             "Boom Supersonic",
    # Legal Tech
    "ironcladhq":       "Ironclad",
    "robin-ai":         "Robin AI",
    "kira":             "Kira Systems",
    # FinOps / Operations / Workflow
    "causal":           "Causal",
    "abacum":           "Abacum",
    "mosaic":           "Mosaic",
    "float":            "Float",
    "timely":           "Timely",
    "atlas":            "Atlas",
    "vector":           "Vector",
    # Self-referential
    "ashby":            "Ashby",
}

COMPANIES = dict(COMPANIES)  # ensure no accidental duplicates


# ── Persistence ───────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(ids: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


# ── Helpers ───────────────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None


def is_recent(date_str: str) -> bool:
    dt = _parse_date(date_str)
    if not dt:
        return False
    return (datetime.now(timezone.utc) - dt).days <= MAX_AGE_DAYS


def posted_label(date_str: str) -> str:
    dt = _parse_date(date_str)
    if not dt:
        return "Unknown"
    days = (datetime.now(timezone.utc) - dt).days
    if days == 0:
        return "Today"
    if days == 1:
        return "1 day ago"
    if days < 7:
        return f"{days} days ago"
    if days < 14:
        return "1 week ago"
    return f"{days // 7} weeks ago"


def _extract_location(raw: dict) -> str:
    loc = raw.get("location", "")
    if isinstance(loc, dict):
        return loc.get("locationStr", "") or loc.get("city", "")
    return loc or ""


def is_allowed_title(title: str) -> bool:
    if SKIP_TITLE_RE.search(title):
        return False
    return bool(ALLOWED_TITLES.search(title))


def is_us_location(location: str, is_remote: bool) -> bool:
    if is_remote:
        return True
    if not location.strip():
        return True  # blank = assume US or worldwide
    return bool(US_LOCATION_RE.search(location))


# ── Fetching ──────────────────────────────────────────────────────────────────────

async def fetch_company(
    client: httpx.AsyncClient, slug: str, sem: asyncio.Semaphore
) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    async with sem:
        try:
            resp = await client.get(url, timeout=10)
            if resp.status_code != 200:
                return []
            data = resp.json()
            jobs = []
            for j in data.get("jobs", []):
                jobs.append({**j, "_slug": slug})
            return jobs
        except Exception:
            return []


async def fetch_all() -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; job-scanner/1.0)"}

    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        results = await asyncio.gather(
            *[fetch_company(client, slug, sem) for slug in COMPANIES]
        )

    jobs = []
    for postings in results:
        jobs.extend(postings)
    return jobs


# ── Email ─────────────────────────────────────────────────────────────────────────

def send_email(all_jobs: list[dict], new_ids: set) -> None:
    new_count  = len(new_ids)
    seen_count = len(all_jobs) - new_count
    subject    = f"[Ashby Scanner] {new_count} New Role(s) Found"

    all_jobs = sorted(
        all_jobs,
        key=lambda j: (j["id"] in new_ids, j.get("publishedAt", "")),
        reverse=True,
    )

    rows_html = []
    for j in all_jobs:
        slug      = j["_slug"]
        company   = COMPANIES.get(slug, slug.replace("-", " ").title())
        location  = _extract_location(j)
        dept      = j.get("department", "") or ""
        is_new    = j["id"] in new_ids
        pub_date  = j.get("publishedAt", "")
        apply_url = j.get("applyUrl", "") or j.get("jobUrl", "") or \
                    f"https://jobs.ashbyhq.com/{slug}/{j['id']}/application"

        new_badge = (
            '<span style="background:#2ecc71;color:#fff;padding:2px 7px;'
            'border-radius:4px;font-size:11px;font-weight:bold;margin-left:6px;">NEW</span>'
            if is_new else ""
        )
        row_bg = 'background:#f0fff4;' if is_new else ''
        rows_html.append(
            f'<tr style="{row_bg}">'
            f'<td style="padding:8px;border:1px solid #ddd;">{j["title"]}{new_badge}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{company}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{location}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{dept}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">{posted_label(pub_date)}</td>'
            f'<td style="padding:8px;border:1px solid #ddd;">'
            f'<a href="{apply_url}">Apply</a></td>'
            f'</tr>'
        )

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333">
    <h2 style="color:#4a4a4a">Ashby Jobs — Digest</h2>
    <p><strong style="color:#2ecc71">{new_count} new</strong> role(s) &nbsp;|&nbsp;
       {seen_count} already seen &nbsp;|&nbsp;
       Last {MAX_AGE_DAYS} days &nbsp;|&nbsp; US / Remote</p>
    <p style="font-size:12px;color:#666;">
       Data Engineer &nbsp;·&nbsp; Data Analyst &nbsp;·&nbsp; Analytics Engineer &nbsp;·&nbsp;
       Analytics Analyst &nbsp;·&nbsp; BI Analyst &nbsp;·&nbsp; ML Engineer &nbsp;·&nbsp;
       Data Scientist &nbsp;·&nbsp; AI Engineer &nbsp;·&nbsp; Software Developer &nbsp;·&nbsp;
       Software Engineer</p>
    <table style="border-collapse:collapse;width:100%;max-width:1300px">
      <tr style="background:#4a4a4a;color:#fff">
        <th style="padding:10px;border:1px solid #555;text-align:left;">Role</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Company</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Location</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Department</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Posted</th>
        <th style="padding:10px;border:1px solid #555;text-align:left;">Link</th>
      </tr>
      {"".join(rows_html)}
    </table>
    <p style="font-size:12px;color:#888;margin-top:20px">
      Source: Ashby ATS · {len(COMPANIES)} companies scanned
    </p>
    </body></html>
    """
    plain = f"Ashby Jobs — {new_count} new role(s) this run ({len(all_jobs)} total in last {MAX_AGE_DAYS} days):\n\n"
    for j in all_jobs:
        slug    = j["_slug"]
        company = COMPANIES.get(slug, slug.replace("-", " ").title())
        tag     = "[NEW] " if j["id"] in new_ids else "      "
        apply_url = j.get("applyUrl", "") or j.get("jobUrl", "") or \
                    f"https://jobs.ashbyhq.com/{slug}/{j['id']}/application"
        plain += (
            f"{tag}{j['title']} @ {company} "
            f"| {_extract_location(j)} "
            f"| {posted_label(j.get('publishedAt', ''))}\n  {apply_url}\n\n"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.sendmail(SENDER_EMAIL, RECIPIENTS, msg.as_string())

    print(f"[email] Sent — {new_count} new role(s).")


# ── Main ──────────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 55)
    print("  Ashby Jobs Scanner")
    print(f"  Scanning {len(COMPANIES)} companies...")
    print("=" * 55)

    all_postings    = await fetch_all()
    previously_seen = load_seen()

    print(f"\n  Total postings fetched: {len(all_postings)}")

    matched   = []
    seen_keys: set = set()

    for p in all_postings:
        title     = p.get("title", "").strip()
        location  = _extract_location(p)
        is_remote = bool(p.get("isRemote", False))
        job_id    = p.get("id", "")
        slug      = p["_slug"]
        dedup_key = f"{slug}|{title.lower()}"

        if not is_allowed_title(title):
            continue
        if not is_recent(p.get("publishedAt", "")):
            continue
        if not is_us_location(location, is_remote):
            continue
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        matched.append(p)

    new_ids = {p["id"] for p in matched if p["id"] not in previously_seen}

    print(f"  Matched (title + US, {MAX_AGE_DAYS} days): {len(matched)}")
    print(f"  Already seen:                   {len(matched) - len(new_ids)}")
    print(f"  New (not sent before):          {len(new_ids)}")

    for p in matched:
        slug    = p["_slug"]
        company = COMPANIES.get(slug, slug.replace("-", " ").title())
        tag     = "[NEW]" if p["id"] in new_ids else "     "
        print(f"\n  {tag} {p['title']}")
        print(f"    Company:  {company}")
        print(f"    Location: {_extract_location(p)}{' (Remote)' if p.get('isRemote') else ''}")
        print(f"    Posted:   {posted_label(p.get('publishedAt', ''))}")
        apply_url = p.get("applyUrl", "") or p.get("jobUrl", "") or \
                    f"https://jobs.ashbyhq.com/{slug}/{p['id']}/application"
        print(f"    URL:      {apply_url}")

    # Append DA/DE/BI new jobs posted <24h to master CSV
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    now_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    master_rows = []
    for p in matched:
        if p["id"] not in new_ids:
            continue
        title = p.get("title", "")
        if not _MASTER_ROLE_RE.search(title):
            continue
        pub = p.get("publishedAt", "")
        try:
            posted_dt = datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(timezone.utc)
            if posted_dt < cutoff_24h:
                continue
        except Exception:
            continue
        slug      = p["_slug"]
        apply_url = p.get("applyUrl", "") or p.get("jobUrl", "") or \
                    f"https://jobs.ashbyhq.com/{slug}/{p['id']}/application"
        master_rows.append({
            "source":   "ashby",
            "job_id":   p["id"],
            "title":    title,
            "company":  COMPANIES.get(slug, slug.replace("-", " ").title()),
            "location": _extract_location(p),
            "role":     _classify_master_role(title),
            "posted":   posted_dt.isoformat(),
            "url":      apply_url,
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
