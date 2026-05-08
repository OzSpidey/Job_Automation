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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────────
SENDER_EMAIL    = os.environ.get("EMAIL_SENDER", "")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENTS      = [e.strip() for e in os.environ.get("EMAIL_TO", "").split(",") if e.strip()]

SEEN_FILE    = Path(__file__).parent / "json" / "ashby_seen_jobs.json"
CONCURRENCY  = 20
MAX_AGE_DAYS = 30  # wider window — Ashby is lower volume than Lever

ALLOWED_TITLES = re.compile(
    r"\b(analyst|data\s+scientist|engineer|developer)\b",
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
    # ─── Additional verified Ashby companies (auto-sourced batch) ───
    # AI / LLM / Agents / Applied AI
    "agi-inc"                       : "AGI Inc",
    "alterion"                      : "Alterion",
    "auctor"                        : "Auctor",
    "bobyard"                       : "Bobyard",
    "brainco"                       : "Brain Co",
    "cantina"                       : "Cantina",
    "cluely"                        : "Cluely",
    "compscience"                   : "CompScience",
    "comulate"                      : "Comulate",
    "connecthum"                    : "ConnectHum",
    "crosby"                        : "Crosby",
    "demandbase"                    : "Demandbase",
    "Distyl"                        : "Distyl AI",
    "edra"                          : "Edra",
    "eigen-labs"                    : "Eigen Labs",
    "ello"                          : "Ello",
    "eloquentai"                    : "Eloquent AI",
    "ema"                           : "Ema",
    "embedding-vc"                  : "Embedding (FLORA)",
    "fabrion"                       : "Fabrion",
    "falconer"                      : "Falconer",
    "farsight"                      : "Farsight AI",
    "fastino-ai"                    : "Fastino AI",
    "firecrawl"                     : "Firecrawl",
    "flox"                          : "Flox",
    "fluidstack"                    : "FluidStack",
    "fundamental"                   : "Fundamental",
    "genpeach"                      : "GenPeach AI",
    "graphite"                      : "Graphite",
    "Graphitehq"                    : "Graphite",
    "gumloop"                       : "Gumloop",
    "hedra"                         : "Hedra",
    "horizon3ai"                    : "Horizon3 AI",
    "hyperexponential"              : "hyperexponential",
    "inference"                     : "Inference",
    "inflectionio"                  : "Inflection.io",
    "inkeep"                        : "Inkeep",
    "kestra"                        : "Kestra",
    "linera.io"                     : "Linera",
    "liquid-ai"                     : "Liquid AI",
    "mai"                           : "MAI",
    "mainstay"                      : "Mainstay Labs",
    "maven-agi"                     : "Maven AGI",
    "mem0"                          : "Mem0",
    "metamorphic"                   : "Metamorphic",
    "mirage"                        : "Mirage",
    "multiverse"                    : "Multiverse",
    "nabucasa"                      : "Nabu Casa",
    "norm-ai"                       : "Norm AI",
    "openrouter"                    : "OpenRouter",
    "paraform"                      : "Paraform",
    "Paragon"                       : "Paragon",
    "photoroom"                     : "Photoroom",
    "prior-labs"                    : "Prior Labs",
    "reach"                         : "Reach Security",
    "reka"                          : "Reka AI",
    "rhoda-ai"                      : "Rhoda AI",
    "rillet"                        : "Rillet",
    "roo-code"                      : "Roo Code",
    "Sciforium"                     : "Sciforium",
    "sola"                          : "Sola",
    "solace"                        : "Solace",
    "spaitial"                      : "SpAItial",
    "tako"                          : "Tako",
    "truelogic"                     : "Truelogic",
    "whitecircle"                   : "White Circle",
    "Wisdom AI"                     : "Wisdom AI",
    "wordware.ai"                   : "Wordware",
    "zello"                         : "Zello",
    "adaption"                      : "Adaption",
    "algo1"                         : "Algo1",
    "basis-ai"                      : "Basis AI",
    "canals"                        : "Canals",
    "carnot-ai"                     : "Carnot AI",
    "coram-ai"                      : "Coram AI",
    "datatonic"                     : "Datatonic",
    "datologyai"                    : "DatologyAI",
    "david-ai"                      : "David AI",
    "doe"                           : "doe",
    "faculty"                       : "Faculty",
    "foxelligroup"                  : "Foxelli Group",
    "gorgias"                       : "Gorgias",
    "hcompany"                      : "H Company",
    "iacollaborative"               : "IA Collaborative",
    "jasper ai"                     : "Jasper AI",
    "kled-ai"                       : "Kled AI",
    "known"                         : "Known",
    "leanlayer"                     : "Lean Layer",
    "lilt-production"               : "LILT",
    "neocognition"                  : "NeoCognition",
    "netic"                         : "Netic",
    "raylu-ai"                      : "Raylu AI",
    "recraft"                       : "Recraft",
    "restream"                      : "Restream",
    "salient"                       : "Salient",
    "spara"                         : "Spara",
    "squadai"                       : "Squad",
    "tandem"                        : "Tandem",
    "vitvio"                        : "VitVio",
    "adaptive-ml"                   : "Adaptive ML",
    "aim4hire"                      : "Aim4Hire",
    "automat"                       : "Automat",
    "cbai"                          : "Cambridge Boston Alignment Initiative",
    "chatbase"                      : "Chatbase",
    "contextual"                    : "Contextual",
    "cortea"                        : "Cortea AI",
    "decart-ai"                     : "Decart AI",
    "emerald-ai"                    : "Emerald AI",
    "faction"                       : "Faction",
    "fermi ai"                      : "Fermi AI",
    "hebbia-ai"                     : "Hebbia",
    "mem"                           : "Mem",
    "parallel"                      : "Parallel",
    "raspberry"                     : "Raspberry AI",
    "roam"                          : "Roam",
    "sieve"                         : "Sieve",
    "steel"                         : "Steel",
    "take2"                         : "Take2",
    "tilderesearch"                 : "Tilde Research",
    "trove"                         : "Trove",
    "trychroma"                     : "Chroma",
    # Fintech / Finance / Insurance / VC
    "acorns"                        : "Acorns",
    "airwallex"                     : "Airwallex",
    "allica-bank"                   : "Allica Bank",
    "antaiventures"                 : "Antai Ventures",
    "Aspora"                        : "Aspora",
    "bankjoy"                       : "Bankjoy",
    "benepass"                      : "Benepass",
    "bestow"                        : "Bestow",
    "better-mortgage"               : "Better Mortgage",
    "brigit"                        : "Brigit",
    "cointracker"                   : "CoinTracker",
    "coldstartventures"             : "Cold Start Ventures",
    "compound"                      : "Compound Planning",
    "dave"                          : "Dave",
    "expa"                          : "Expa",
    "finary"                        : "Finary",
    "firstbaseio"                   : "Firstbase",
    "firstround"                    : "First Round Capital",
    "forum-ventures"                : "Forum Ventures",
    "gridcare"                      : "GridCARE",
    "hauler-hero"                   : "Hauler Hero",
    "hopper"                        : "Hopper",
    "kin"                           : "Kin Insurance",
    "koho"                          : "KOHO",
    "lazer"                         : "Lazer",
    "lendable"                      : "Lendable",
    "loancrate"                     : "Loancrate",
    "method"                        : "Method Financial",
    "monarchmoney"                  : "Monarch Money",
    "nivoda"                        : "Nivoda",
    "pear-vc"                       : "Pear VC",
    "pluralfinance"                 : "Plural",
    "profound"                      : "Profound",
    "rogo"                          : "Rogo",
    "sentilink"                     : "SentiLink",
    "sequoia"                       : "Sequoia One",
    "shiftsmart"                    : "Shiftsmart",
    "Silver"                        : "Silver.dev",
    "socure"                        : "Socure",
    "superdial"                     : "SuperDial",
    "sydecar"                       : "Sydecar",
    "valon"                         : "Valon",
    "ValonVM"                       : "Valon Mortgage",
    "wealthsimple"                  : "Wealthsimple",
    "9fin"                          : "9fin",
    "ambrook"                       : "Ambrook",
    "artemisanalytics"              : "Artemis",
    "atob"                          : "AtoB",
    "bullpen-talent"                : "Bullpen Talent",
    "bunch"                         : "bunch",
    "compa"                         : "Compa",
    "convey"                        : "Convey",
    "costanoavc"                    : "Costanoa Ventures",
    "decimal"                       : "Decimal",
    "imprint"                       : "Imprint",
    "jerry.ai"                      : "Jerry.ai",
    "junipersquare"                 : "Juniper Square",
    "kinetic"                       : "Kinetic",
    "ladder"                        : "Ladder",
    "lemonade"                      : "Lemonade",
    "m13"                           : "M13",
    "marshmallow"                   : "Marshmallow",
    "onramp"                        : "OnRamp",
    "oscilar"                       : "Oscilar",
    "payabli"                       : "Payabli",
    "playground global"             : "Playground Global",
    "river"                         : "River",
    "savvymoney"                    : "SavvyMoney",
    "shepherd"                      : "Shepherd",
    "stash"                         : "Stash",
    "talos-trading"                 : "Talos",
    "thndr"                         : "Thndr",
    "a16z-crypto"                   : "a16z crypto",
    "campfire"                      : "Campfire",
    "cardless"                      : "Cardless",
    "caribou"                       : "Caribou",
    "dualentry"                     : "DualEntry",
    "layer"                         : "Layer",
    "layerfi"                       : "Layer",
    "puzzle.io"                     : "Puzzle",
    "seccl"                         : "Seccl",
    "standinsurance"                : "Stand Insurance",
    "stealth fintech"               : "Stealth FinTech",
    "tradeify"                      : "Tradeify",
    # Healthcare / Bio / Therapeutics
    "a-place-for-mom"               : "A Place for Mom",
    "a16z-new-media"                : "a16z New Media",
    "ami"                           : "AMI",
    "arbiter-ai"                    : "Arbiter AI",
    "astera"                        : "Astera Institute",
    "axiombio"                      : "Axiom Bio",
    "bayesianhealth"                : "Bayesian Health",
    "bio"                           : "BIO",
    "blueberrypediatrics"           : "Blueberry Pediatrics",
    "casap"                         : "Casap",
    "chaidiscovery"                 : "Chai Discovery",
    "chainalysis-careers"           : "Chainalysis",
    "codes-health"                  : "Codes Health",
    "Commure"                       : "Commure",
    "crete-professionals-alliance"  : "Crete Professionals Alliance",
    "develop-health"                : "Develop Health",
    "edisyl"                        : "Edisyl",
    "emora-health"                  : "Emora Health",
    "equip"                         : "Equip Health",
    "fanvue.com"                    : "Fanvue",
    "farmraise"                     : "FarmRaise",
    "fetch-pet-health"              : "Fetch Pet Health",
    "fira-health"                   : "Fira Health",
    "freetrade"                     : "Freetrade",
    "genomics"                      : "Genomics",
    "gt-bio"                        : "GT Bio",
    "HealthMatch"                   : "HealthMatch",
    "hellobrightline"               : "Brightline",
    "hims-and-hers"                 : "Hims & Hers",
    "hinge-health"                  : "Hinge Health",
    "hivehealth"                    : "Hive Health",
    "homera-health"                 : "Homera Health",
    "humans-and"                    : "humans&",
    "ideal dental"                  : "Ideal Dental",
    "kindred"                       : "Kindred",
    "lawhive"                       : "Lawhive",
    "legionhealth"                  : "Legion Health",
    "maximustribe"                  : "Maximus Health",
    "medraai"                       : "Medra",
    "metriport"                     : "Metriport",
    "mithrl"                        : "Mithrl",
    "neko-health"                   : "Neko Health",
    "nosisbio"                      : "Nosis Bio",
    "odyssey"                       : "Odyssey",
    "odysseyml"                     : "Odyssey ML",
    "oneapp"                        : "OnePay",
    "onoshealth"                    : "Onos Health",
    "paradox"                       : "Paradox",
    "pearlhealth"                   : "Pearl Health",
    "percepta"                      : "Percepta",
    "Phil"                          : "PHIL",
    "plasmidsaurus"                 : "Plasmidsaurus",
    "positive-development"          : "Positive Development",
    "protege"                       : "Protege",
    "radar"                         : "Radar",
    "reinforce-labs-inc"            : "Reinforce Labs",
    "sailorhealth"                  : "Sailor Health",
    "simon-lever"                   : "Simon Lever",
    "sprinter-health"               : "Sprinter Health",
    "sully-ai"                      : "Sully AI",
    "symbiotic"                     : "Symbiotic",
    "tandem-health"                 : "Tandem Health",
    "trading212"                    : "Trading 212",
    "turquoise-health"              : "Turquoise Health",
    "virtahealth"                   : "Virta Health",
    "aios"                          : "Aios Medical",
    "anglehealth"                   : "Angle Health",
    "anima"                         : "Anima",
    "assorthealth"                  : "Assort Health",
    "axion"                         : "Axion",
    "camber"                        : "Camber",
    "cellular-intelligence"         : "Cellular Intelligence",
    "hippocratic ai"                : "Hippocratic AI",
    "kubera-health"                 : "Kubera Health",
    "nautilus biotechnology"        : "Nautilus Biotechnology",
    "nuna"                          : "Nuna",
    "parallel-bio"                  : "Parallel Bio",
    "phoenix"                       : "Phoenix",
    "prima mente"                   : "Prima Mente",
    "solstice-health"               : "Solstice Health",
    "withwisdom"                    : "Wisdom",
    "basata"                        : "Basata",
    "bravehealth"                   : "Brave Health",
    "clarion"                       : "Clarion",
    "genesis-molecular-ai"          : "Genesis Molecular AI",
    "healthleap"                    : "HealthLeap",
    "iambic-therapeutics"           : "Iambic Therapeutics",
    "meetmarvin"                    : "Marvin Behavioral Health",
    "montu-uk"                      : "Montu UK",
    "nabla"                         : "Nabla",
    "nelly"                         : "Nelly Solutions",
    "quantum"                       : "Quantum",
    # Defense / Aerospace / Robotics / Hardware
    "8vc"                           : "8VC",
    "aerovect"                      : "AeroVect",
    "airspace-intelligence.com"     : "Air Space Intelligence",
    "allspice"                      : "AllSpice",
    "Antares"                       : "Antares",
    "anysignal"                     : "AnySignal",
    "apex-technology-inc"           : "Apex Technology",
    "AtomicSemi"                    : "Atomic Semi",
    "augmodo"                       : "Augmodo",
    "blacksemiconductor"            : "Black Semiconductor",
    "circuithub"                    : "CircuitHub",
    "flux"                          : "Flux",
    "foundry-robotics"              : "Foundry Robotics",
    "gecko-robotics"                : "Gecko Robotics",
    "hadrian-automation"            : "Hadrian Automation",
    "happyrobot.ai"                 : "HappyRobot",
    "lumilens"                      : "Lumilens",
    "M-KOPA"                        : "M-KOPA",
    "mach"                          : "Mach Industries",
    "matter-intelligence"           : "Matter Intelligence",
    "Merge Labs"                    : "Merge Labs",
    "meshy"                         : "Meshy",
    "myedspacecareers"              : "MyEdSpace",
    "nidus-technologies"            : "Nidus Technologies",
    "NormalComputing"               : "Normal Computing",
    "NorthwoodSpace"                : "Northwood Space",
    "orchard"                       : "Orchard Robotics",
    "panoptyc"                      : "Panoptyc",
    "picogrid"                      : "Picogrid",
    "pivotrobotics"                 : "Pivot Robotics",
    "quartermaster"                 : "Quartermaster",
    "ReflexRobotics"                : "Reflex Robotics",
    "Ricursive Intelligence"        : "Ricursive Intelligence",
    "runetech"                      : "Rune Technologies",
    "sensmore"                      : "Sensmore",
    "sereact"                       : "Sereact",
    "serverobotics"                 : "Serve Robotics",
    "simspace-corporation"          : "SimSpace",
    "StandardBots"                  : "Standard Bots",
    "sunday"                        : "Sunday Robotics",
    "tessera-labs"                  : "Tessera Labs",
    "Verne Robotics"                : "Verne Robotics",
    "vertical-aerospace"            : "Vertical Aerospace",
    "verticalsemi"                  : "Vertical Semiconductor",
    "voltai.careers"                : "Voltai",
    "wraithwatch"                   : "Wraithwatch",
    "Zeromark"                      : "Zeromark",
    "charge-robotics"               : "Charge Robotics",
    "corvus-robotics"               : "Corvus Robotics",
    "flink"                         : "Flink Robotics",
    "mindrobotics"                  : "Mind Robotics",
    "netgear"                       : "NETGEAR",
    "odys-aviation"                 : "Odys Aviation",
    "reflect-orbital"               : "Reflect Orbital",
    "reframesystems"                : "Reframe Systems",
    "rivianvw.tech"                 : "Rivian-VW Group Technologies",
    "swan"                          : "Swan",
    "cobot"                         : "Cobot",
    "d-matrix"                      : "d-Matrix",
    "droyd"                         : "Droyd",
    "dyna-robotics"                 : "Dyna Robotics",
    "equal1"                        : "Equal1",
    "extropic"                      : "Extropic",
    "furiosa-ai"                    : "FuriosaAI",
    "goventi"                       : "Venti Technologies",
    "kraken-kinetics"               : "Kraken Kinetics",
    "logiqal"                       : "Logiqal",
    "maticrobots"                   : "Matic Robots",
    "obviant"                       : "Obviant",
    "olix"                          : "OLIX",
    "quantware"                     : "QuantWare",
    "se3"                           : "SE3 Labs",
    "second-front-systems"          : "Second Front Systems",
    "starpath.space"                : "Starpath",
    "sygaldry-technologies"         : "Sygaldry Technologies",
    # Climate / Energy / Sustainability
    "apollo-information-systems"    : "Apollo Information Systems",
    "artemis"                       : "Artemis",
    "aureliussystems"               : "Aurelius Systems",
    "aurorasolar"                   : "Aurora Solar",
    "axle-careers"                  : "Axle Energy",
    "axle-mobility"                 : "Axle Mobility",
    "base-power"                    : "Base Power",
    "blue-energy"                   : "Blue Energy",
    "cuspai"                        : "CuspAI",
    "davidenergy"                   : "David Energy",
    "endurance-energy"              : "Endurance Energy",
    "enode"                         : "Enode",
    "Epistemix"                     : "Epistemix",
    "helion"                        : "Helion",
    "isometric"                     : "Isometric",
    "patch.io"                      : "Patch",
    "periodic-labs"                 : "Periodic Labs",
    "radiant-industries"            : "Radiant Industries",
    "sylvera"                       : "Sylvera",
    "tem"                           : "tem",
    "tempo-xyz"                     : "Tempo",
    "twelve-labs"                   : "TwelveLabs",
    "climate-finance-solutions"     : "Climate Finance Solutions",
    "equal-ventures"                : "Equal Ventures",
    "gravityclimate"                : "Gravity",
    "pulsora inc"                   : "Pulsora",
    "agreena"                       : "Agreena",
    "carbonx"                       : "CarbonX",
    "fuse"                          : "Fuse",
    "proxima-fusion"                : "Proxima Fusion",
    "reonic"                        : "Reonic",
    # Legal Tech
    "legora"                        : "Legora",
    "finch-legal"                   : "Finch Legal",
    "katapult-labs"                 : "Katapult Labs",
    "spellbook.legal"               : "Spellbook",
    "spotdraft"                     : "SpotDraft",
    "wilson"                        : "WilsonAI",
    # Security / Compliance / Identity
    "Adaptive"                      : "Adaptive",
    "adaptivesecurity"              : "Adaptive Security",
    "assured"                       : "Assured",
    "cape"                          : "Cape",
    "cloudscaler"                   : "Cloudscaler",
    "fable"                         : "Fable Security",
    "GPTZero"                       : "GPTZero",
    "illumio"                       : "Illumio",
    "infisical"                     : "Infisical",
    "prophet-security"              : "Prophet Security",
    "sosafe"                        : "SoSafe",
    "stronghold"                    : "Stronghold",
    "xbowcareers"                   : "XBOW",
    "binalyze"                      : "Binalyze",
    "blackpoint cyber"              : "Blackpoint Cyber",
    "eye-security"                  : "Eye Security",
    "nord-security"                 : "Nord Security",
    "oneleet"                       : "Oneleet",
    "safety"                        : "Safety",
    "tenex"                         : "TENEX.AI",
    "zip"                           : "Zip",
    "breaker"                       : "Breaker",
    "bureau"                        : "Bureau",
    "edgesource corporation"        : "Edgesource Corporation",
    "goanagram"                     : "Anagram",
    "haast"                         : "Haast",
    "hiya"                          : "Hiya",
    "human"                         : "HUMAN",
    "trulioo"                       : "Trulioo",
    # Analytics / Data Tooling / Observability
    "Aphex"                         : "Aphex",
    "cubesoftware"                  : "Cube Software",
    "doctronic"                     : "Doctronic",
    "hudu"                          : "Hudu",
    "kaizenlabs"                    : "Kaizen Labs",
    "polaranalytics"                : "Polar Analytics",
    "revenuebase-inc"               : "RevenueBase",
    "sift"                          : "Sift",
    "tigerdata"                     : "Tiger Data",
    "unify"                         : "Unify",
    "dash0"                         : "Dash0",
    "snowflake"                     : "Snowflake",
    "motherduck"                    : "MotherDuck",
    # Productivity / SaaS / Collaboration
    "Archy"                         : "Archy",
    "everfield"                     : "Everfield",
    "fathom.video"                  : "Fathom Video",
    "glide"                         : "Glide",
    "ideals"                        : "Ideals",
    "klue"                          : "Klue",
    "meter"                         : "Meter",
    "Nango"                         : "Nango",
    "oden-technologies"             : "Oden Technologies",
    "omnea"                         : "Omnea",
    "phia"                          : "Phia",
    "plane"                         : "Plane",
    "prompt"                        : "Prompt",
    "pylon-labs"                    : "Pylon",
    "sequence"                      : "Sequence",
    "silktide"                      : "Silktide",
    "Superhuman Platform Inc"       : "Superhuman Platform",
    "superplane"                    : "SuperPlane",
    "superpower"                    : "Superpower",
    "taktile"                       : "Taktile",
    "vibe"                          : "Vibe",
    "vibecode"                      : "VibeCode",
    "wrapbook"                      : "Wrapbook",
    "mazedesign"                    : "Maze",
    "procurify"                     : "Procurify",
    "sanity"                        : "Sanity",
    "span.app"                      : "Span",
    "the browser company"           : "The Browser Company",
    "tin-can"                       : "Tin Can",
    "duvo"                          : "Duvo",
    "gitbook"                       : "GitBook",
    "jump"                          : "Jump",
    "kittl"                         : "Kittl",
    "kodex"                         : "Kodex",
    "nylas"                         : "Nylas",
    "parabola-io"                   : "Parabola",
    # Sales / Marketing / Growth / Recruiting
    "1mind"                         : "1mind",
    "agentio"                       : "Agentio",
    "b2spin"                        : "B2Spin",
    "beamery"                       : "Beamery",
    "careerswift.ai"                : "CareerSwift",
    "creatoriq"                     : "CreatorIQ",
    "directive"                     : "Directive",
    "doss"                          : "Doss",
    "dovetail"                      : "Dovetail",
    "feedbackfruits"                : "FeedbackFruits",
    "fitt"                          : "Fitt Talent Partners",
    "getnextstep"                   : "GetNextStep",
    "gorilla"                       : "Gorilla",
    "hilberts"                      : "Hilbert's AI",
    "hirehangar"                    : "Hire Hangar",
    "inbeat-agency"                 : "inBeat Agency",
    "leavitt"                       : "Leavitt Group",
    "lgads"                         : "LG Ad Solutions",
    "newform"                       : "Newform",
    "onhires"                       : "OnHires",
    "polymarket"                    : "Polymarket",
    "rollstack"                     : "Rollstack",
    "rwazi"                         : "Rwazi",
    "sanguinesa"                    : "Sanguine",
    "snowball"                      : "Snowball",
    "thumbtack"                     : "Thumbtack",
    "venatus.com"                   : "Venatus",
    "a-team"                        : "A.Team",
    "beamimpact"                    : "Beamimpact",
    "clera"                         : "Clera",
    "eventual"                      : "Eventual",
    "go-nimbly"                     : "Go Nimbly",
    "hive.co"                       : "Hive",
    "hockeystack"                   : "HockeyStack",
    "kpr"                           : "KP Reddy",
    "lavendo"                       : "Lavendo",
    "nooks"                         : "Nooks",
    "parker"                        : "Parker Group",
    "pergolux"                      : "Pergolux",
    "reflow"                        : "Reflow",
    "revenuevessel"                 : "Revenue Vessel",
    "rilla"                         : "Rilla",
    "scale army careers"            : "Scale Army",
    "serval"                        : "Serval",
    "splitmetrics"                  : "SplitMetrics",
    "whippy"                        : "Whippy",
    "beam"                          : "Beam",
    "billups"                       : "Billups",
    "everis"                        : "Everis",
    "fonzi"                         : "Fonzi",
    "inspiration-commerce-group"    : "Inspiration Commerce Group",
    "latamcent"                     : "LatamCent",
    "lightspeedhq"                  : "Lightspeed Commerce",
    "medialicious"                  : "Medialicious",
    "twenty"                        : "Twenty",
    # HR / People / Hiring / Workforce
    "coinhako"                      : "Coinhako",
    "fleek"                         : "Fleek",
    "higharc"                       : "Higharc",
    "HighlightTA"                   : "Highlight",
    "hiring-pros"                   : "Hiring Pros",
    "outcapped"                     : "Outcapped",
    "people-culture-talent"         : "People Culture Talent",
    "tapblaze"                      : "TapBlaze",
    "workwhilejobs"                 : "WorkWhile",
    "andela"                        : "Andela",
    "doinstruct"                    : "doinstruct",
    "livinghr"                      : "livingHR",
    "peoplepath"                    : "PeoplePath",
    # Media / Consumer / Creator / Gaming
    "arcade"                        : "Arcade",
    "flosports"                     : "FloSports",
    "gamechanger"                   : "GameChanger",
    "intro"                         : "Intro",
    "lap"                           : "LAP Coffee",
    "livekit"                       : "LiveKit",
    "markarch"                      : "Marketing Architects",
    "MUBI"                          : "MUBI",
    "Onyx Games LLC"                : "Onyx Games",
    "opusclip"                      : "OpusClip",
    "redlygames"                    : "Redly Games",
    "seconddinner"                  : "Second Dinner",
    "stream"                        : "Stream",
    "swishbreaks"                   : "Swish Breaks",
    "thatgamecompany"               : "thatgamecompany",
    "triumph-arcade"                : "Triumph Arcade",
    "voldex"                        : "Voldex Games",
    "volka"                         : "Volka Games",
    "voodoo"                        : "Voodoo",
    "windranger"                    : "Windranger Labs",
    "eightsleep"                    : "Eight Sleep",
    "emberos"                       : "Emberos",
    "genies"                        : "Genies",
    "hyperhug"                      : "HyperHug",
    "pch-digital"                   : "PCH Digital",
    "rothys"                        : "Rothy's",
    "strava"                        : "Strava",
    "tldr.tech"                     : "TLDR",
    "voicemod"                      : "Voicemod",
    "doji"                          : "Doji",
    "genmo"                         : "Genmo",
    "grindr llc"                    : "Grindr",
    "hawkeyeinnovations"            : "Hawk-Eye Innovations",
    "partyhat"                      : "Partyhat",
    "playson"                       : "Playson",
    "poshmark"                      : "Poshmark",
    "tonal"                         : "Tonal",
    "traction-wellness-group"       : "SweatHouz / Traction Wellness",
    "whoop"                         : "WHOOP",
    # Real Estate / PropTech / Construction
    "american-housing"              : "American Housing",
    "arch.co"                       : "Arch",
    "buildout"                      : "Buildout",
    "devsavant"                     : "DevSavant",
    "greenlitecareers"              : "GreenLite",
    "homebase"                      : "Homebase",
    "homebound"                     : "Homebound",
    "homevision"                    : "HomeVision",
    "permitflow"                    : "PermitFlow",
    "Ridealso"                      : "ALSO",
    "roompricegenie"                : "RoomPriceGenie",
    "snapdocs"                      : "Snapdocs",
    "withpulley"                    : "Pulley",
    "workyard"                      : "Workyard",
    "airgarage"                     : "Airgarage",
    "industrious"                   : "Industrious",
    "miter"                         : "Miter",
    "orbital"                       : "Orbital",
    "togal-ai"                      : "Togal AI",
    "theflex"                       : "The Flex",
    # Education / EdTech
    "instructure"                   : "Instructure",
    "reducto"                       : "Reducto",
    "prodigy-education"             : "Prodigy Education",
    "cambly"                        : "Cambly",
    "scaler"                        : "Scaler",
    "schoolhouse-world"             : "Schoolhouse",
    "stepful"                       : "Stepful",
    # Food / Pet / Restaurant / Vertical SaaS
    "farmersd40"                    : "Farmers Insurance",
    "owner"                         : "Owner.com",
    "Peppr"                         : "Peppr",
    "Spoton"                        : "SpotOn",
    "Vetcove"                       : "Vetcove",
    "barkbus"                       : "Barkbus",
    "deliveroo"                     : "Deliveroo",
    "hoxtonfarms"                   : "Hoxton Farms",
    "diversified-botanics"          : "Diversified Botanics",
    "flowhub"                       : "Flowhub",
    "nestveterinary"                : "Nest Veterinary",
    "synergy pet group"             : "Synergy Pet Group",
    # GovTech / Public Sector
    "opengov"                       : "OpenGov",
    "promise"                       : "Promise",
    "quorum"                        : "Quorum",
    "flock safety"                  : "Flock Safety",
    "govworx"                       : "Govworx",
    "govsignals"                    : "GovSignals",
    "govwell"                       : "GovWell",
    # Travel / Hospitality
    "Bounce"                        : "Bounce",
    "wetravel"                      : "WeTravel",
    "trainline"                     : "Trainline",
    # Crypto / Web3 / Blockchain
    "allium"                        : "Allium",
    "Blockworks"                    : "Blockworks",
    "Conduit"                       : "Conduit",
    "cow-dao"                       : "CoW DAO",
    "cryptio"                       : "Cryptio",
    "cyber.fund"                    : "cyber.Fund",
    "DoubleZero"                    : "DoubleZero",
    "Goldsky"                       : "Goldsky",
    "kraken.com"                    : "Kraken",
    "monad.foundation"              : "Monad Foundation",
    "OpenSea"                       : "OpenSea",
    "oplabs"                        : "OP Labs",
    "ostium"                        : "Ostium Labs",
    "p2p.org"                       : "P2P.org",
    "PaxosLabs"                     : "Paxos Labs",
    "polygon-labs"                  : "Polygon Labs",
    "spruceid"                      : "Spruce",
    "superduper"                    : "Superduper",
    "trust-wallet"                  : "Trust Wallet",
    "yolabs"                        : "Yo Labs",
    "0x"                            : "0x",
    "anagram"                       : "Anagram",
    "binance.us"                    : "Binance.US",
    "dune"                          : "Dune",
    "ether.fi"                      : "Ether.fi",
    "lightspark"                    : "Lightspark",
    "mystenlabs"                    : "Mysten Labs",
    "seifoundation"                 : "Sei Foundation",
    "solana foundation"             : "Solana Foundation",
    "unit410"                       : "Unit 410",
    "walrusfi"                      : "Walrus",
    "0g"                            : "0G Labs",
    "halliday"                      : "Halliday",
    "morpho"                        : "Morpho",
    "nexus.xyz"                     : "Nexus",
    "pluralis-research"             : "Pluralis Research",
    "sigp"                          : "Sigma Prime",
    "starknetfoundation"            : "Starknet Foundation",
    "sui foundation"                : "Sui Foundation",
    "tempo"                         : "Tempo",
    "wormholelabs"                  : "Wormhole Labs",
    # Developer Tools / Infra / Cloud
    "10xteam"                       : "10x Team",
    "9-mothers"                     : "9 Mothers",
    "abound"                        : "Abound",
    "acquisition"                   : "Acquisition.com",
    "adapt"                         : "Adapt API",
    "adaptyv"                       : "Adaptyv",
    "aghanim"                       : "Aghanim",
    "airapps"                       : "Air Apps",
    "airops"                        : "AirOps",
    "alan"                          : "Alan",
    "alchemy"                       : "Alchemy",
    "ambral"                        : "Ambral",
    "anyone-ai"                     : "Anyone AI",
    "appliedlabs"                   : "Applied Labs",
    "april"                         : "April",
    "arq"                           : "ARQ",
    "artisan"                       : "Artisan",
    "assembledhq"                   : "Assembled",
    "atticus"                       : "Atticus",
    "avoca"                         : "Avoca",
    "axiom-co"                      : "Axiom",
    "benchling"                     : "Benchling",
    "bespokelabs"                   : "Bespoke Labs",
    "bitvavo"                       : "Bitvavo",
    "bland"                         : "Bland AI",
    "brightwheel"                   : "Brightwheel",
    "brinc"                         : "BRINC",
    "camunda"                       : "Camunda",
    "ceartas"                       : "Ceartas",
    "claylabs"                      : "Clay",
    "clipboard"                     : "Clipboard",
    "close"                         : "Close",
    "coderabbit"                    : "CodeRabbit",
    "coefficientgiving"             : "Coefficient Giving",
    "coframe"                       : "Coframe",
    "comfy-org"                     : "Comfy",
    "confido"                       : "Confido",
    "confluent"                     : "Confluent",
    "contra"                        : "Contra",
    "corti"                         : "Corti",
    "counsel"                       : "Counsel Health",
    "CourseCareers"                 : "CourseCareers",
    "credo.ai"                      : "Credo AI",
    "dandy"                         : "Dandy",
    "deepjudge"                     : "DeepJudge",
    "DeepL"                         : "DeepL",
    "deepslate"                     : "Deepslate",
    "deeptune"                      : "Deeptune",
    "delinea"                       : "Delinea",
    "deposco"                       : "Deposco",
    "diagrid"                       : "Diagrid",
    "ditto"                         : "Ditto",
    "duck-duck-go"                  : "DuckDuckGo",
    "dust"                          : "Dust",
    "easyllama.com"                 : "EasyLlama",
    "elicit"                        : "Elicit",
    "eliseai"                       : "EliseAI",
    "ernest"                        : "Ernest",
    "espresso"                      : "Espresso AI",
    "everai"                        : "EverAI",
    "everops"                       : "EverOps",
    "featherlessai"                 : "Featherless AI",
    "fin"                           : "Fin",
    "finalroundai"                  : "Final Round AI",
    "fonio"                         : "fonio",
    "formance"                      : "Formance",
    "frontcareers"                  : "Front Careers",
    "g2"                            : "G2",
    "garage"                        : "Garage",
    "generalintelligencecompany"    : "General Intelligence Company",
    "getscope"                      : "Scope AI",
    "gigaml"                        : "Giga",
    "givebutter"                    : "Givebutter",
    "glacier"                       : "Glacier",
    "grvt"                          : "GRVT",
    "gt-hq"                         : "GT (Grid Dynamics)",
    "hackerone"                     : "HackerOne",
    "handshake"                     : "Handshake",
    "harmattan-ai"                  : "Harmattan AI",
    "harperinsure"                  : "Harper",
    "httpie"                        : "HTTPie",
    "hud"                           : "HUD",
    "hyperspell"                    : "Hyperspell",
    "i3d"                           : "i3D.net",
    "improbable"                    : "Improbable",
    "intus"                         : "IntusCare",
    "january"                       : "January",
    "jump-app"                      : "Jump",
    "junction"                      : "Junction",
    "junior"                        : "Junior AI",
    "kalshi"                        : "Kalshi",
    "keyrock"                       : "Keyrock",
    "konvu"                         : "Konvu",
    "LAI"                           : "Lead Allies",
    "laurel"                        : "Laurel",
    "li.fi"                         : "LI.FI",
    "lyric"                         : "Lyric",
    "makai-labs"                    : "Makai Labs",
    "maki"                          : "Maki People",
    "mapbox"                        : "Mapbox",
    "megazone"                      : "Megazone Cloud",
    "meridianlink"                  : "MeridianLink",
    "mimica"                        : "Mimica",
    "moment"                        : "Moment Technology",
    "momentic"                      : "Momentic",
    "moovx"                         : "Moovx",
    "mux"                           : "Mux",
    "nava-benefits"                 : "Nava Benefits",
    "nectar-social"                 : "Nectar Social",
    "nerdwallet"                    : "NerdWallet",
    "neuroscale"                    : "Neuroscale AI",
    "newfront"                      : "Newfront",
    "ninjatech.ai"                  : "NinjaTech AI",
    "notable"                       : "Notable",
    "omni"                          : "Omni",
    "one-pass-solutions"            : "One Pass Solutions",
    "onebrief"                      : "Onebrief",
    "openevidence"                  : "OpenEvidence",
    "optro"                         : "Optro",
    "parspec"                       : "Parspec",
    "peec"                          : "Peec AI",
    "Playground"                    : "Playground",
    "popl"                          : "Popl",
    "pragmatike"                    : "Pragmatike",
    "primary"                       : "Primary VC",
    "PrimeIntellect"                : "Prime Intellect",
    "primer"                        : "Primer",
    "proofofplay"                   : "Proof of Play",
    "proxima"                       : "Proxima",
    "quora"                         : "Quora",
    "raft"                          : "Raft",
    "ready"                         : "Ready",
    "regard"                        : "Regard",
    "relationrx"                    : "Relation Therapeutics",
    "relevanceai"                   : "Relevance AI",
    "renuity"                       : "Renuity",
    "revenuecat"                    : "RevenueCat",
    "risklabs"                      : "Risk Labs",
    "robco"                         : "RobCo",
    "ropes"                         : "Ropes",
    "ruby-labs"                     : "Ruby Labs",
    "runlayer"                      : "Runlayer",
    "runway-ml"                     : "Runway",
    "scalera"                       : "Scalera",
    "scan-com"                      : "Scan.com",
    "ScribdInc"                     : "Scribd",
    "seamflow"                      : "Seamflow",
    "SearchApi"                     : "SearchApi",
    "seismic-change.com"            : "Seismic",
    "sewer-ai"                      : "SewerAI",
    "sfcompute"                     : "SF Compute Company",
    "SigNoz"                        : "SigNoz",
    "SKELAR"                        : "SKELAR",
    "Skydropx"                      : "Skydropx",
    "slingshotai"                   : "Slingshot AI",
    "smallpdf"                      : "smallpdf",
    "source-multiplier"             : "Source Multiplier",
    "southgeeks"                    : "South Geeks",
    "spaice-tech"                   : "SPAICE",
    "span"                          : "SPAN",
    "squad"                         : "Squad",
    "stack-ai"                      : "Stack AI",
    "statista"                      : "Statista",
    "stealth-startup"               : "Stealth Startup",
    "strategic-growth-partners"     : "Strategic Growth Partners",
    "sweedpos.com"                  : "Sweed",
    "Swoop"                         : "Swoop Technologies",
    "syndica"                       : "Syndica",
    "tacto"                         : "Tacto",
    "tailor"                        : "Tailor",
    "tavily"                        : "Tavily",
    "teamworks"                     : "Teamworks",
    "techtorch"                     : "Techtorch",
    "telli"                         : "telli",
    "tennr"                         : "Tennr",
    "terac"                         : "Terac",
    "terminal-industries"           : "Terminal Industries",
    "ternary"                       : "Ternary",
    "the job sauce"                 : "The Job Sauce",
    "the-flex"                      : "The Flex",
    "the-global-talent-co"          : "The Global Talent Co",
    "theydo"                        : "TheyDo",
    "tilthq"                        : "Tilt Finance",
    "titan-msp"                     : "Titan MSP",
    "tracer"                        : "Tracer Cloud",
    "traversal"                     : "Traversal",
    "tremendous"                    : "Tremendous",
    "trovy"                         : "Trovy",
    "trueshort"                     : "TrueShort",
    "trunk tools"                   : "Trunk Tools",
    "uipath"                        : "UiPath",
    "uniswap"                       : "Uniswap Labs",
    "unwrap"                        : "Unwrap",
    "upcodes"                       : "UpCodes",
    "upside"                        : "Upside",
    "valthos"                       : "Valthos",
    "vantage"                       : "Vantage",
    "varick-agents"                 : "Varick Agents",
    "vendelux"                      : "Vendelux",
    "virtue-AI"                     : "Virtue AI",
    "wagmo"                         : "Wagmo",
    "weave"                         : "Weave",
    "webai"                         : "webAI",
    "whatnot"                       : "Whatnot",
    "wirescreen"                    : "WireScreen",
    "withclutch"                    : "Clutch",
    "workweave"                     : "Weave Engineering",
    "worldly"                       : "Worldly",
    "xenon"                         : "Xenon Pharmaceuticals",
    "xero"                          : "Xero",
    "ycombinator"                   : "Y Combinator",
    "yondr"                         : "Yondr",
    "yotta"                         : "Yotta Labs",
    "yumaai"                        : "Yuma AI",
    "zefir"                         : "Zefir",
    "zefr"                          : "Zefr",
    "Zero"                          : "Zero RFI",
    "zowie"                         : "Zowie",
    "agility.io"                    : "Agility IO",
    "buildwithfern"                 : "Fern",
    "chromatic"                     : "Chromatic",
    "coder"                         : "Coder",
    "docker"                        : "Docker",
    "lucidlink"                     : "LucidLink",
    "luxor"                         : "Luxor",
    "nash"                          : "Nash",
    "ocra"                          : "Ocra",
    "replo"                         : "Replo",
    "rescale"                       : "Rescale",
    "siftstack"                     : "Sift",
    "space44"                       : "SPACE44",
    "blacksmith"                    : "Blacksmith",
    "bunny"                         : "Bunny",
    "flutterflow"                   : "FlutterFlow",
    "netboxlabs"                    : "NetBox Labs",
    "vcluster"                      : "vCluster Labs",
    "wa.technology"                 : "WA.Technology",
    # Misc / Uncategorized
    "mobasi"                        : "Mobasi",
    "reevo"                         : "Reevo",
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

    if not new_ids:
        print("\n  No new roles since last run — skipping email.")
    else:
        print(f"\n  Sending email ({len(new_ids)} new, {len(matched)} total)...")
        send_email(matched, new_ids)
        save_seen(previously_seen | new_ids)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
