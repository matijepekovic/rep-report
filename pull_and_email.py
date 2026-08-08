"""
Rep Report Mailer — signs in with your PAT, downloads the Rep Totals view
as a vector PDF (Unspecified page size = no ### truncation), emails it to you.

Setup:  pip install requests   (email uses Python's built-in smtplib)
Test:   python pull_and_email.py
Schedule it with cron (Pi) or Task Scheduler (Windows) for daily delivery.
"""

import os
import requests
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

# ============ TABLEAU CONFIG ============
# Values come from GitHub Actions secrets (environment variables);
# for a local test you can temporarily replace the defaults below.

SERVER = os.getenv("TABLEAU_SERVER", "https://10ay.online.tableau.com")
SITE_CONTENT_URL = os.getenv("TABLEAU_SITE", "dabella")
API_VERSION = "3.22"

PAT_NAME = os.getenv("TABLEAU_PAT_NAME", "undisputed")
PAT_SECRET = os.getenv("TABLEAU_PAT_SECRET", "PASTE_TOKEN_SECRET_HERE")

VIEW_NAME = "Rep Totals"

# PDF options — Unspecified sizes the page to fit the whole view (kills the ###)
PDF_TYPE = "Unspecified"
PDF_ORIENTATION = "Landscape"
# Render at the same pixel size as your screen view (from your embed code).
# If columns are still tight, raise VIZ_WIDTH to 2400 or 3000.
VIZ_WIDTH = 1800
VIZ_HEIGHT = 917

VIEW_FILTERS = {
    "Lead Branch": "WA-OLY",
    "Regional": "Brody Hess",
    # If the PDF shows reps who aren't yours, uncomment the line below and list
    # your crew EXACTLY as spelled in the Sales Rep filter dropdown.
    # Mahonri's full last name needs checking — I only saw "Mahonri La.." truncated.
    # "Sales Rep": "Dennis Hambleton,Jernias Tafia,Lazarus Williams,Mahonri LASTNAME,Matije Pekovic",
}

# ============ EMAIL CONFIG ============
# Gmail:   SMTP_HOST="smtp.gmail.com"      — needs an App Password
#          (Google Account > Security > 2-Step Verification > App passwords)
# Outlook: SMTP_HOST="smtp.office365.com"  — password or app password per your org

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.office365.com")
SMTP_PORT = 587
SMTP_USER = os.getenv("SMTP_USER", "you@yourcompany.com")
SMTP_PASS = os.getenv("SMTP_PASS", "APP_PASSWORD_HERE")

EMAIL_TO = os.getenv("EMAIL_TO", "you@yourcompany.com").split(",")  # comma-separated list
EMAIL_SUBJECT = "Rep Totals — Daily"

# ========================================


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}")


def fetch_pdf() -> bytes:
    base = f"{SERVER}/api/{API_VERSION}"
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
    site_id = creds["site"]["id"]
    log("Signed in.")

    r = s.get(f"{base}/sites/{site_id}/views", params={"pageSize": 1000})
    r.raise_for_status()
    views = r.json().get("views", {}).get("view", [])
    target = next((v for v in views
                   if v["name"].strip().lower() == VIEW_NAME.strip().lower()), None)
    if not target:
        log(f"View '{VIEW_NAME}' not found. Available views:")
        for v in views[:50]:
            log(f"  - {v['name']}")
        sys.exit(1)
    log(f"Found view: {target['name']}")

    params = {"type": PDF_TYPE, "orientation": PDF_ORIENTATION, "maxAge": "1",
              "vizWidth": str(VIZ_WIDTH), "vizHeight": str(VIZ_HEIGHT)}
    for field, value in VIEW_FILTERS.items():
        params[f"vf_{field}"] = value
    r = s.get(f"{base}/sites/{site_id}/views/{target['id']}/pdf", params=params)
    if r.status_code != 200:
        log(f"PDF pull failed ({r.status_code}): {r.text[:300]}")
        sys.exit(1)
    log(f"Got PDF ({len(r.content)//1024} KB)")

    s.post(f"{base}/auth/signout")
    return r.content


def send_email(pdf_bytes: bytes):
    msg = EmailMessage()
    today = datetime.now().strftime("%b %d, %Y")
    msg["Subject"] = f"{EMAIL_SUBJECT} — {today}"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(EMAIL_TO)
    msg.set_content(f"Rep Totals for {today}. PDF attached.\n\n— Automated by the rep board.")
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf",
                       filename=f"Rep_Totals_{datetime.now():%Y-%m-%d}.pdf")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)
    log(f"Emailed to {', '.join(EMAIL_TO)}")


if __name__ == "__main__":
    pdf = fetch_pdf()
    Path(__file__).with_name("rep_totals_latest.pdf").write_bytes(pdf)  # local copy too
    send_email(pdf)
    log("Done.")


