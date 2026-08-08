"""
Rep Report Mailer — pulls your saved UNDISPUTED custom view from Tableau
as a PDF (vector) and emails it. Filters come from the custom view itself,
exactly as you saved them — no filter names needed.
"""

import os
import requests
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

# ============ TABLEAU CONFIG ============

SERVER = os.getenv("TABLEAU_SERVER", "https://10ay.online.tableau.com")
SITE_CONTENT_URL = os.getenv("TABLEAU_SITE", "dabella")
API_VERSION = "3.22"

PAT_NAME = os.getenv("TABLEAU_PAT_NAME", "undisputed")
PAT_SECRET = os.getenv("TABLEAU_PAT_SECRET", "PASTE_TOKEN_SECRET_HERE")

CUSTOM_VIEW_NAME = "UNDISPUTED"   # the saved custom view to export

# Render size (px) — matches your on-screen view where numbers show fully.
# Raise VIZ_WIDTH to 2400+ if columns are tight.
VIZ_WIDTH = 1800
VIZ_HEIGHT = 917
PDF_TYPE = "Unspecified"
PDF_ORIENTATION = "Landscape"

# ============ EMAIL CONFIG ============

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = 587
SMTP_USER = os.getenv("SMTP_USER", "you@gmail.com")
SMTP_PASS = os.getenv("SMTP_PASS", "APP_PASSWORD_HERE")

EMAIL_TO = os.getenv("EMAIL_TO", "you@yourcompany.com").split(",")
EMAIL_SUBJECT = "Rep Totals — Daily"

# ========================================


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}")


def fetch() -> tuple[bytes, str]:
    """Returns (file_bytes, 'pdf' or 'png')."""
    # Ask the server for the newest API version it supports — the custom-view
    # PDF endpoint only exists in recent versions.
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
    site_id = creds["site"]["id"]
    log("Signed in.")

    # Find the UNDISPUTED custom view
    r = s.get(f"{base}/sites/{site_id}/customviews", params={"pageSize": 1000})
    r.raise_for_status()
    cvs = r.json().get("customViews", {}).get("customView", [])
    cv = next((c for c in cvs
               if c["name"].strip().lower() == CUSTOM_VIEW_NAME.strip().lower()), None)
    if not cv:
        log(f"Custom view '{CUSTOM_VIEW_NAME}' not found. Custom views visible:")
        for c in cvs[:50]:
            log(f"  - {c['name']}")
        sys.exit(1)
    log(f"Found custom view: {cv['name']} ({cv['id']})")

    render = {"maxAge": "1", "vizWidth": str(VIZ_WIDTH), "vizHeight": str(VIZ_HEIGHT)}

    # Try PDF first (vector)
    r = s.get(f"{base}/sites/{site_id}/customviews/{cv['id']}/pdf",
              params={**render, "type": PDF_TYPE, "orientation": PDF_ORIENTATION})
    if r.status_code == 200:
        log(f"Got PDF ({len(r.content)//1024} KB)")
        s.post(f"{base}/auth/signout")
        return r.content, "pdf"

    # Older API versions have no custom-view PDF — fall back to high-res PNG
    log(f"PDF endpoint unavailable ({r.status_code}); falling back to image.")
    r = s.get(f"{base}/sites/{site_id}/customviews/{cv['id']}/image",
              params={"resolution": "high", "maxAge": "1"})
    if r.status_code != 200:
        log(f"Image pull failed ({r.status_code}): {r.text[:300]}")
        sys.exit(1)
    log(f"Got PNG ({len(r.content)//1024} KB)")
    s.post(f"{base}/auth/signout")
    return r.content, "png"


def send_email(data: bytes, ext: str):
    msg = EmailMessage()
    today = datetime.now().strftime("%b %d, %Y")
    msg["Subject"] = f"{EMAIL_SUBJECT} — {today}"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(EMAIL_TO)
    msg.set_content(f"Rep Totals for {today}. Attached.\n\n— Automated by the rep board.")
    maintype, subtype = ("application", "pdf") if ext == "pdf" else ("image", "png")
    msg.add_attachment(data, maintype=maintype, subtype=subtype,
                       filename=f"Rep_Totals_{datetime.now():%Y-%m-%d}.{ext}")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)
    log(f"Emailed to {', '.join(EMAIL_TO)}")


if __name__ == "__main__":
    data, ext = fetch()
    Path(__file__).with_name(f"rep_totals_latest.{ext}").write_bytes(data)
    send_email(data, ext)
    log("Done.")
