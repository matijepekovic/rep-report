"""
UNDISPUTED Leaderboard Mailer.

Pulls the UNDISPUTED view's DATA (not PDF) from Tableau, injects it into the
STATS leaderboard page, screenshots the rendered board with headless Chromium,
and emails the PNG.

Flow:
  1. Sign in to Tableau (same PAT secrets as pull_and_email.py).
  2. Find the saved custom view, resolve its underlying view.
  3. GET /views/{id}/data  -> summary data as CSV.
  4. Map CSV columns -> leaderboard fields (wide or Measure Names/Values long
     format both supported). CSV headers are always logged.
  5. Write data.json into the STATS checkout, serve it, screenshot 1920x1080
     (viewport auto-grows so every rep row is visible).
  6. Email the PNG. DRY_RUN=1 skips the email and just leaves artifacts.
"""

import csv
import io
import json
import os
import re
import smtplib
import subprocess
import sys
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import requests

# ============ TABLEAU CONFIG ============

SERVER = os.getenv("TABLEAU_SERVER", "https://10ay.online.tableau.com")
SITE_CONTENT_URL = os.getenv("TABLEAU_SITE", "dabella")
API_VERSION = "3.22"

PAT_NAME = os.getenv("TABLEAU_PAT_NAME", "undisputed")
PAT_SECRET = os.getenv("TABLEAU_PAT_SECRET", "PASTE_TOKEN_SECRET_HERE")

CUSTOM_VIEW_NAME = os.getenv("CUSTOM_VIEW_NAME", "UNDISPUTED")

# Optional view filters for the data query, e.g. "Region=West;Week=Last"
# (applied as vf_<field>=<value> query params).
VIZ_FILTERS = os.getenv("TABLEAU_VIZ_FILTERS", "")

# ============ BOARD / RENDER CONFIG ============

STATS_DIR = Path(os.getenv("STATS_DIR", "stats-board"))  # checkout of matijepekovic/STATS
SHOT_WIDTH = int(os.getenv("SHOT_WIDTH", "1920"))
SHOT_HEIGHT = int(os.getenv("SHOT_HEIGHT", "1080"))
OUT_PNG = Path(os.getenv("OUT_PNG", "board.png"))

# Week range label; override with REPORT_RANGE="MAY 19 - MAY 25, 2025"
REPORT_RANGE = os.getenv("REPORT_RANGE", "")

DRY_RUN = os.getenv("DRY_RUN", "0").lower() in ("1", "true", "yes")

# ============ EMAIL CONFIG ============

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = 587
SMTP_USER = os.getenv("SMTP_USER", "you@gmail.com")
SMTP_PASS = os.getenv("SMTP_PASS", "APP_PASSWORD_HERE")

EMAIL_TO = os.getenv("EMAIL_TO", "you@yourcompany.com").split(",")
EMAIL_SUBJECT = os.getenv("EMAIL_SUBJECT", "UNDISPUTED Leaderboard")

# ============ CSV -> BOARD FIELD MAPPING ============
# Aliases are matched against normalized headers (lowercase, alphanumerics only).
# Adjust the alias lists if the first run's logged headers differ.

FIELD_ALIASES = {
    "issuedLeads":    ["issuedleads", "issued", "leadsissued", "leads"],
    "pitchedLeads":   ["pitchedleads", "pitched", "leadspitched"],
    "pitchRate":      ["pitchrate", "pitchpct", "pitchpercent"],
    "soldLeads":      ["soldleads", "sold", "sales", "leadssold"],
    "closeRate":      ["closerate", "closepct", "closingrate", "closepercent"],
    "grossSplit":     ["grosssplit", "gross", "grosssales", "grossvolume"],
    "netSplit":       ["netsplit", "net", "netsales", "netvolume"],
    "dpl":            ["dpl", "dollarsperlead", "perlead"],
    "salesRetention": ["salesretention", "retention", "retentionrate"],
    "avgGrossPerRep": ["avggrossperrep", "avggross", "averagegrossperrep"],
    "avgNetPerRep":   ["avgnetperrep", "avgnet", "averagenetperrep"],
}
REP_ALIASES = ["rep", "repname", "salesrep", "salesperson", "name", "employee",
               "assignedrep", "repbranch"]
BRANCH_ALIASES = ["branch", "office", "location", "market", "team"]
MEASURE_NAME_COLS = ["measurenames", "measure"]
MEASURE_VALUE_COLS = ["measurevalues", "value"]
TOTAL_ROW_NAMES = {"grand total", "total", "totals", "team total", "team totals"}

PCT_FIELDS = {"pitchRate", "closeRate", "salesRetention"}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def clean_number(raw, pct=False):
    """'$48,750' -> 48750.0 ; '82%' -> 82.0 ; '' -> None."""
    if raw is None:
        return None
    s = str(raw).strip().replace("$", "").replace(",", "").replace("%", "")
    if s in ("", "-", "—", "n/a", "null", "None"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


# ---------------------------------------------------------------- Tableau

def tableau_signin():
    api_ver = API_VERSION
    try:
        info = requests.get(f"{SERVER}/api/{API_VERSION}/serverinfo",
                            headers={"Accept": "application/json"}, timeout=30).json()
        api_ver = info["serverInfo"]["restApiVersion"]
    except Exception:
        pass
    log(f"Using API version {api_ver}")
    base = f"{SERVER}/api/{api_ver}"
    s = requests.Session()
    s.headers["Accept"] = "application/json"
    r = s.post(f"{base}/auth/signin", json={
        "credentials": {
            "personalAccessTokenName": PAT_NAME,
            "personalAccessTokenSecret": PAT_SECRET,
            "site": {"contentUrl": SITE_CONTENT_URL},
        }
    })
    if r.status_code != 200:
        log(f"Sign-in failed ({r.status_code}): {r.text[:300]}")
        sys.exit(1)
    creds = r.json()["credentials"]
    s.headers["X-Tableau-Auth"] = creds["token"]
    log("Signed in.")
    return s, base, creds["site"]["id"]


def fetch_view_csv(s, base, site_id) -> str:
    # Locate the saved custom view (same as the PDF bot), then use its
    # underlying view for the data query.
    r = s.get(f"{base}/sites/{site_id}/customviews", params={"pageSize": 1000})
    r.raise_for_status()
    cvs = r.json().get("customViews", {}).get("customView", [])
    cv = next((c for c in cvs
               if c["name"].strip().lower() == CUSTOM_VIEW_NAME.strip().lower()), None)
    if not cv:
        log(f"Custom view '{CUSTOM_VIEW_NAME}' not found. Visible custom views:")
        for c in cvs[:50]:
            log(f"  - {c['name']}")
        sys.exit(1)
    view_id = cv.get("view", {}).get("id")
    log(f"Custom view '{cv['name']}' -> underlying view {view_id}")
    if not view_id:
        log("Custom view carries no underlying view id; cannot query data.")
        sys.exit(1)

    params = {"maxAge": "1"}
    for pair in [p for p in VIZ_FILTERS.split(";") if "=" in p]:
        k, v = pair.split("=", 1)
        params[f"vf_{k.strip()}"] = v.strip()

    r = s.get(f"{base}/sites/{site_id}/views/{view_id}/data", params=params)
    if r.status_code != 200:
        log(f"Data pull failed ({r.status_code}): {r.text[:300]}")
        sys.exit(1)
    log(f"Got view data CSV ({len(r.content)//1024} KB)")
    return r.content.decode("utf-8-sig")


# ---------------------------------------------------------------- Mapping

def match_field(header_norm):
    for field, aliases in FIELD_ALIASES.items():
        if header_norm in aliases:
            return field
    return None


def rows_to_board(csv_text: str):
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    log(f"CSV headers: {headers}")
    hmap = {h: norm(h) for h in headers}

    rep_col = next((h for h, n in hmap.items() if n in REP_ALIASES), None)
    branch_col = next((h for h, n in hmap.items() if n in BRANCH_ALIASES), None)
    mn_col = next((h for h, n in hmap.items() if n in MEASURE_NAME_COLS), None)
    mv_col = next((h for h, n in hmap.items() if n in MEASURE_VALUE_COLS), None)
    if not rep_col:
        log("No rep-name column recognized. Add its header to REP_ALIASES.")
        sys.exit(1)
    log(f"Rep column: '{rep_col}'  Branch column: '{branch_col}'  "
        f"Long format: {bool(mn_col and mv_col)}")

    reps = {}   # name -> dict
    order = []
    for row in reader:
        name = (row.get(rep_col) or "").strip()
        if not name:
            continue
        rec = reps.setdefault(name, {"name": name})
        if name not in order:
            order.append(name)
        if branch_col and row.get(branch_col):
            rec["branch"] = row[branch_col].strip()
        if mn_col and mv_col:                      # long format: one measure per row
            field = match_field(norm(row.get(mn_col, "")))
            if field:
                rec[field] = clean_number(row.get(mv_col), field in PCT_FIELDS)
        else:                                       # wide format: measures as columns
            for h, n in hmap.items():
                field = match_field(n)
                if field:
                    rec[field] = clean_number(row.get(h), field in PCT_FIELDS)

    # Split out a totals row if Tableau included one
    totals = None
    for name in list(reps):
        if name.strip().lower() in TOTAL_ROW_NAMES:
            totals = reps.pop(name)
            totals.pop("name", None)
            totals.pop("branch", None)
            order.remove(name)

    rep_list = [reps[n] for n in order]
    if not rep_list:
        log("No rep rows parsed — check the header mapping above.")
        sys.exit(1)

    if totals is None:
        totals = compute_totals(rep_list)
        log("No total row in CSV; computed totals from rep rows.")

    log(f"Parsed {len(rep_list)} reps. Fields on first rep: "
        f"{sorted(k for k in rep_list[0] if k not in ('name', 'branch'))}")
    return rep_list, totals


def compute_totals(rep_list):
    def total(key):
        vals = [r.get(key) for r in rep_list if r.get(key) is not None]
        return sum(vals) if vals else None

    def mean(key):
        vals = [r.get(key) for r in rep_list if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    t = {
        "issuedLeads": total("issuedLeads"),
        "pitchedLeads": total("pitchedLeads"),
        "soldLeads": total("soldLeads"),
        "grossSplit": total("grossSplit"),
        "netSplit": total("netSplit"),
        "dpl": mean("dpl"),
        "salesRetention": mean("salesRetention"),
    }
    if t["issuedLeads"] and t["pitchedLeads"]:
        t["pitchRate"] = t["pitchedLeads"] / t["issuedLeads"]
    if t["pitchedLeads"] and t["soldLeads"]:
        t["closeRate"] = t["soldLeads"] / t["pitchedLeads"]
    return t


def default_range():
    now = datetime.now()
    mon = now - timedelta(days=now.weekday())
    sun = mon + timedelta(days=6)
    if mon.month == sun.month:
        return f"{mon:%b %d} – {sun:%d, %Y}".upper()
    return f"{mon:%b %d} – {sun:%b %d, %Y}".upper()


# ---------------------------------------------------------------- Render

def render_board(data: dict) -> Path:
    if not (STATS_DIR / "index.html").exists():
        log(f"STATS checkout not found at '{STATS_DIR}'.")
        sys.exit(1)
    (STATS_DIR / "data.json").write_text(json.dumps(data, indent=1))
    log(f"Wrote {STATS_DIR}/data.json")

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8123", "--bind", "127.0.0.1"],
        cwd=STATS_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            exe = os.getenv("CHROMIUM_PATH")  # optional override
            browser = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
            page = browser.new_page(viewport={"width": SHOT_WIDTH, "height": SHOT_HEIGHT})
            page.goto("http://127.0.0.1:8123/index.html", wait_until="networkidle")
            page.wait_for_timeout(700)
            # Grow the viewport until every rep row is visible (board scrolls
            # internally when there are more reps than fit).
            for _ in range(6):
                need = page.evaluate(
                    "()=>{const b=document.getElementById('board');"
                    "return b? b.scrollHeight-b.clientHeight : 0}")
                if need <= 2:
                    break
                h = min(page.viewport_size["height"] + need + 40, 3000)
                page.set_viewport_size({"width": SHOT_WIDTH, "height": h})
                page.wait_for_timeout(300)
            page.screenshot(path=str(OUT_PNG))
            browser.close()
    finally:
        server.terminate()
    log(f"Screenshot saved: {OUT_PNG} ({OUT_PNG.stat().st_size//1024} KB)")
    return OUT_PNG


# ---------------------------------------------------------------- Email

def send_email(png: Path):
    msg = EmailMessage()
    today = datetime.now().strftime("%b %d, %Y")
    msg["Subject"] = f"{EMAIL_SUBJECT} — {today}"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(EMAIL_TO)
    msg.set_content(f"UNDISPUTED leaderboard for {today}. Attached.\n\n"
                    "— Automated by the rep board.")
    msg.add_attachment(png.read_bytes(), maintype="image", subtype="png",
                       filename=f"Undisputed_{datetime.now():%Y-%m-%d}.png")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)
    log(f"Emailed to {', '.join(EMAIL_TO)}")


# ---------------------------------------------------------------- Main

if __name__ == "__main__":
    s, base, site_id = tableau_signin()
    csv_text = fetch_view_csv(s, base, site_id)
    Path("view_data.csv").write_text(csv_text)
    s.post(f"{base}/auth/signout")

    rep_list, totals = rows_to_board(csv_text)
    data = {
        "dateRange": REPORT_RANGE or default_range(),
        "reps": rep_list,
        "totals": totals,
    }
    png = render_board(data)

    if DRY_RUN:
        log("DRY_RUN set — skipping email. Inspect board.png / view_data.csv.")
    else:
        send_email(png)
    log("Done.")
