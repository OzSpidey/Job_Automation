"""
workday_notify.py

Checks the DA/DE/BI side-car CSVs for new unapplied roles, sends a Telegram
message with inline Apply/Skip buttons, waits for the user's response, then
triggers workday_autoapply.py for approved roles.

Env vars:
  TELEGRAM_BOT_TOKEN   — bot token from @BotFather
  TELEGRAM_CHAT_ID     — your personal chat ID
  WD_APPLICANT_INFO    — same as autoapply
  WORKDAY_PASSWORD     — same as autoapply
  WD_RESUME_PATH       — same as autoapply
  WD_VERIFY_IMAP_USER  — same as autoapply
  WD_VERIFY_IMAP_PASSWORD — same as autoapply
  HEADLESS             — "true" for CI
  WD_CONCURRENCY       — parallel applies (default 2)
  APPROVAL_TIMEOUT     — seconds to wait for Telegram reply (default 300)
"""

import csv, json, os, re, sys, time, subprocess, tempfile
from datetime import datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT          = Path(__file__).parent
APPLIED_LOG   = ROOT / "json" / "workday_applied.json"
NOTIFIED_LOG  = ROOT / "json" / "workday_notified.json"
CSV_DIR       = ROOT / "csv"

BOT_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID          = os.environ.get("TELEGRAM_CHAT_ID", "")
APPROVAL_TIMEOUT = int(os.environ.get("APPROVAL_TIMEOUT", "300"))
MAX_AGE_DAYS     = int(os.environ.get("WD_MAX_AGE_DAYS", "1"))

IGNORED = {"booz allen", "guidehouse", "leidos"}

_ROLE_SLUG = {
    "da": "data_analyst",
    "de": "data_engineer",
    "bi": "business_intelligence",
}

# ── Telegram helpers ───────────────────────────────────────────────────────────

def tg(method: str, **kwargs) -> dict:
    import urllib.request, urllib.parse
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(kwargs).encode()
    req = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def send_message(text: str, reply_markup: dict | None = None) -> int:
    kwargs = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    resp = tg("sendMessage", **kwargs)
    return resp["result"]["message_id"]

def edit_message(msg_id: int, text: str, reply_markup: dict | None = None):
    kwargs = {"chat_id": CHAT_ID, "message_id": msg_id,
              "text": text, "parse_mode": "HTML"}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    try:
        tg("editMessageText", **kwargs)
    except Exception:
        pass

def get_updates(offset: int = 0) -> list:
    try:
        r = tg("getUpdates", offset=offset, timeout=5, allowed_updates=["callback_query"])
        return r.get("result", [])
    except Exception:
        return []

def answer_callback(callback_id: str):
    try:
        tg("answerCallbackQuery", callback_query_id=callback_id)
    except Exception:
        pass

# ── Queue helpers ──────────────────────────────────────────────────────────────

def load_applied() -> set:
    if APPLIED_LOG.exists():
        try:
            d = json.loads(APPLIED_LOG.read_text())
            return set(d) if isinstance(d, list) else set(d.keys())
        except Exception:
            pass
    return set()

def load_notified() -> set:
    if NOTIFIED_LOG.exists():
        try:
            return set(json.loads(NOTIFIED_LOG.read_text()))
        except Exception:
            pass
    return set()

def save_notified(links: set):
    NOTIFIED_LOG.parent.mkdir(exist_ok=True)
    NOTIFIED_LOG.write_text(json.dumps(sorted(links)))

def fresh_roles() -> list[dict]:
    """Return unapplied, un-notified, today-fresh DA/DE/BI roles."""
    applied  = load_applied()
    notified = load_notified()
    cutoff   = datetime.now() - timedelta(days=MAX_AGE_DAYS)

    def is_fresh(row):
        fo = (row.get("found_on") or "")[:10]
        try:
            return datetime.strptime(fo, "%Y-%m-%d") >= cutoff
        except Exception:
            return True

    roles = []
    seen_links: set = set()
    for slug in ["data_analyst", "data_engineer", "business_intelligence"]:
        for path in sorted(CSV_DIR.glob(f"workday_jobs_{slug}*.csv")):
            try:
                for row in csv.DictReader(open(path, encoding="utf-8")):
                    link = (row.get("link") or "").strip()
                    comp = (row.get("company") or "").lower()
                    if not link or link in seen_links:
                        continue
                    if link in applied or link in notified:
                        continue
                    if any(ig in comp for ig in IGNORED):
                        continue
                    if not is_fresh(row):
                        continue
                    seen_links.add(link)
                    row["_slug"] = slug
                    roles.append(row)
            except Exception:
                continue
    return roles

# ── Main flow ──────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("[!] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        sys.exit(1)

    roles = fresh_roles()
    if not roles:
        print("[i] No new roles to notify.")
        return

    print(f"[+] {len(roles)} new role(s) found — notifying via Telegram.")

    # Build one message with per-role Apply/Skip buttons
    lines = [f"<b>🆕 {len(roles)} new Workday role(s) found:</b>\n"]
    keyboard_rows = []
    for i, r in enumerate(roles):
        title   = r.get("title", "Unknown")
        company = r.get("company", "Unknown")
        loc     = r.get("location", "")
        lines.append(f"{i+1}. <b>{title}</b>\n   {company} — {loc}")
        keyboard_rows.append([
            {"text": f"✅ Apply #{i+1} {company[:20]}", "callback_data": f"apply_{i}"},
            {"text": f"❌ Skip #{i+1}",                  "callback_data": f"skip_{i}"},
        ])

    # Global apply-all / skip-all row
    keyboard_rows.append([
        {"text": "✅ Apply ALL", "callback_data": "apply_all"},
        {"text": "❌ Skip ALL",  "callback_data": "skip_all"},
    ])

    text = "\n".join(lines) + f"\n\n<i>Waiting {APPROVAL_TIMEOUT//60} min for your reply…</i>"
    msg_id = send_message(text, reply_markup={"inline_keyboard": keyboard_rows})

    # Poll for callbacks
    decisions = {}   # index → "apply" | "skip"
    deadline  = time.time() + APPROVAL_TIMEOUT
    offset    = 0

    while time.time() < deadline and len(decisions) < len(roles):
        updates = get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            cb = upd.get("callback_query")
            if not cb:
                continue
            answer_callback(cb["id"])
            data = cb.get("data", "")
            if data == "apply_all":
                for j in range(len(roles)):
                    decisions[j] = "apply"
            elif data == "skip_all":
                for j in range(len(roles)):
                    decisions[j] = "skip"
            elif data.startswith("apply_"):
                decisions[int(data.split("_")[1])] = "apply"
            elif data.startswith("skip_"):
                decisions[int(data.split("_")[1])] = "skip"
        time.sleep(3)

    # Default: skip anything not explicitly approved
    to_apply = [roles[i] for i in range(len(roles)) if decisions.get(i) == "apply"]
    to_skip  = [roles[i] for i in range(len(roles)) if decisions.get(i) != "apply"]

    # Mark notified so we don't re-send
    notified = load_notified()
    notified.update(r.get("link", "") for r in roles)
    save_notified(notified)

    if not to_apply:
        edit_message(msg_id, text.replace("Waiting…", "")
                     + "\n\n<b>No roles approved — nothing applied.</b>")
        print("[i] No roles approved.")
        return

    # Write approved roles into a temp CSV for autoapply
    approved_csv = ROOT / "csv" / "_tg_approved.csv"
    fields = ["title", "company", "location", "posted", "experience", "link", "found_on"]
    with open(approved_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(to_apply)

    approved_names = ", ".join(r.get("company", "") for r in to_apply)
    edit_message(msg_id, text.replace("Waiting…", "")
                 + f"\n\n<b>▶️ Applying to: {approved_names}</b>")
    print(f"[+] Approved: {approved_names}")

    # Run autoapply against the approved CSV
    env = os.environ.copy()
    env["ROLES"] = "da"  # slug doesn't matter — file is directly specified
    env["WD_QUEUE_CSV"] = str(approved_csv)  # autoapply reads this if set
    env["MAX_APPLY"] = str(len(to_apply))
    env["HEADLESS"] = env.get("HEADLESS", "true")

    result = subprocess.run(
        [sys.executable, str(ROOT / "workday_autoapply.py")],
        env=env, cwd=str(ROOT),
    )

    approved_csv.unlink(missing_ok=True)

    status_text = "✅ Done!" if result.returncode == 0 else "⚠️ Completed with errors — check the logs."
    edit_message(msg_id, text.replace("Waiting…", "")
                 + f"\n\n<b>{status_text}</b>")
    print(f"[+] Autoapply finished (exit {result.returncode}).")


if __name__ == "__main__":
    main()
