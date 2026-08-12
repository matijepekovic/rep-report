"""
One-off probe: can Close Rate by Product be read out of Tableau over REST?

It answers four questions and nothing else:

  1. Does this PAT reach the RegionalSaleswFLOW workbook at all?
  2. Which view serves the close-rate-by-product numbers? The browser URL
     points at a dashboard, and /views/{id}/data needs the sheet behind it.
  3. What shape is the REST summary CSV? The .xlsx exports are crosstabs;
     REST returns something else, and the parser has to match it.
  4. Which filter key pins the result to Olympia? REST applies the view's
     *saved* filter state, not what a browser has selected, so the scope
     must be sent explicitly -- and the rep workbook already proved the key
     is the underlying field name, not the filter card's title.

This repository is PUBLIC, so its Actions logs are world-readable. The probe
therefore prints STRUCTURE ONLY: column headers, product labels, row counts,
and a boolean for whether rates arrive as fractions. No close rates, no
dollar amounts and no rep names are ever written to the log.

Temporary. Delete this file and .github/workflows/tableau-probe.yml once the
answers are recorded.
"""

import csv
import io
import os
import sys
from datetime import date, timedelta

from pull_render_email import log, norm, tableau_signin

WORKBOOK = os.getenv("PROBE_WORKBOOK", "RegionalSaleswFLOW")
MARKET = os.getenv("PROBE_MARKET", "Olympia")

# The five lines that would go on the board, plus the three that must be
# dropped. Listed so the log can report which of them the view exposes
# without echoing anything else out of the data.
KNOWN_PRODUCTS = {
    "bath", "baths", "gutter", "gutters", "roof", "roofs", "siding",
    "window", "windows", "door", "doors", "solar", "walkintub", "walkintubs",
}

PRODUCT_COL_HINTS = ("productcalc", "product")
RATE_COL_HINTS = ("closerate", "pitchedrate", "pitchrate")

# The rep workbook needed "USER-Home Branch" rather than its card title
# "Home Branch", so try the field key and the plausible variants.
MARKET_FILTER_KEYS = [
    "LEAD-Market__c",
    "LEAD-Market",
    "Market",
]


def month_range():
    """First and last day of the current month, ISO -- what the app sends."""
    today = date.today()
    first = today.replace(day=1)
    nxt = (first.replace(year=first.year + 1, month=1)
           if first.month == 12 else first.replace(month=first.month + 1))
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


START = os.getenv("PROBE_START", "") or month_range()[0]
END = os.getenv("PROBE_END", "") or month_range()[1]


def is_product(label):
    return norm(label) in KNOWN_PRODUCTS


def number(raw):
    s = str(raw or "").strip().replace("$", "").replace(",", "").replace("%", "")
    if s in ("", "-", "n/a", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def scale_of(values):
    """Fractions or percents -- reported as a label, never as a value."""
    seen = [v for v in values if v is not None]
    if not seen:
        return "no numeric values"
    if max(abs(v) for v in seen) <= 1.0:
        return "fractions (0-1), so the parser must multiply by 100"
    return "already percent (0-100)"


def describe(csv_text):
    """Summarize a CSV's structure. Deliberately returns no measure values."""
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    rows = list(reader)

    log(f"    columns ({len(headers)}): {headers}")
    log(f"    data rows: {len(rows)}")

    product_cols = [h for h in headers
                    if any(hint in norm(h) for hint in PRODUCT_COL_HINTS)]
    product_headers = [h for h in headers if is_product(h)]

    if product_cols:
        col = product_cols[0]
        labels = []
        for r in rows:
            v = str(r.get(col) or "").strip()
            if v and v not in labels:
                labels.append(v)
        log(f"    SHAPE: long -- one row per product, dimension '{col}'")
        log(f"    product labels ({len(labels)}): {labels[:20]}")
        rate_cols = [h for h in headers
                     if any(hint in norm(h) for hint in RATE_COL_HINTS)]
        for rc in rate_cols:
            log(f"    '{rc}' scale: {scale_of([number(r.get(rc)) for r in rows])}")
        if not rate_cols:
            log("    NO close-rate column found by name; full header list above")
        return "long"

    if product_headers:
        log("    SHAPE: wide -- one row, products as columns")
        log(f"    product columns ({len(product_headers)}): {product_headers}")
        vals = [number(rows[0].get(h)) for h in product_headers] if rows else []
        log(f"    value scale: {scale_of(vals)}")
        return "wide"

    log("    SHAPE: unrecognized -- no product dimension or product columns")
    return "unknown"


def main():
    s, base, site_id = tableau_signin()

    # ---------------------------------------------------------- 1. workbook
    log(f"Looking up workbook '{WORKBOOK}'...")
    r = s.get(f"{base}/sites/{site_id}/workbooks/{WORKBOOK}", params={"key": "contentUrl"})
    if r.status_code != 200:
        log(f"ANSWER 1: NO -- this PAT cannot reach '{WORKBOOK}' (HTTP {r.status_code}).")
        log("The fix is granting the existing token access to that workbook, not code.")
        sys.exit(1)
    workbook_id = r.json().get("workbook", {}).get("id", "")
    log(f"ANSWER 1: YES -- workbook resolves (id {workbook_id}).")

    # ------------------------------------------------------------- 2. views
    r = s.get(f"{base}/sites/{site_id}/workbooks/{workbook_id}/views")
    if r.status_code != 200:
        log(f"Could not list views (HTTP {r.status_code}).")
        sys.exit(1)
    views = r.json().get("views", {}).get("view", [])
    if isinstance(views, dict):
        views = [views]
    log(f"ANSWER 2: workbook exposes {len(views)} views:")
    for v in views:
        log(f"  - name={v.get('name')!r}  contentUrl={v.get('contentUrl')!r}  id={v.get('id')}")

    candidates = [v for v in views if "closerate" in norm(v.get("contentUrl") or "")
                  or "closerate" in norm(v.get("name") or "")]
    if not candidates:
        log("No view name or contentUrl mentions close rate; trying every view.")
        candidates = views

    # --------------------------------------------- 3 & 4. shape and filters
    for v in candidates:
        vid = v.get("id")
        log("")
        log(f"=== {v.get('name')!r}  [{v.get('contentUrl')}] ===")

        attempts = [("dates only", {})]
        for key in MARKET_FILTER_KEYS:
            attempts.append((f"dates + vf_{key}={MARKET}", {f"vf_{key}": MARKET}))

        for label, extra in attempts:
            params = {"maxAge": "1", "vf_Start": START, "vf_End": END, **extra}
            r = s.get(f"{base}/sites/{site_id}/views/{vid}/data", params=params)
            if r.status_code != 200:
                log(f"  {label}: HTTP {r.status_code}")
                continue
            text = r.content.decode("utf-8-sig", errors="replace")
            log(f"  {label}: HTTP 200, {len(r.content)} bytes")
            describe(text)

    log("")
    log(f"Date range probed: {START} to {END}")
    log("Done. No measure values were printed.")


if __name__ == "__main__":
    main()
