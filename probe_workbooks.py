"""
One-off probe: can this PAT list the workbooks and sheets on the site?

The Pi is going to get a "pick your report" control, and the only thing I
cannot determine from outside the office is whether a non-admin personal
access token is allowed to enumerate content. Tableau has two listing
endpoints and they behave very differently for a non-admin:

  GET /sites/{site}/workbooks               - site-wide, usually admin-only
  GET /sites/{site}/users/{user}/workbooks  - what this user can see

This checks both, and confirms 8-SalesRepLevelData shows up so the picker
can offer the report the board already uses.

This repository is PUBLIC, so its Actions logs are world-readable. The probe
prints workbook and sheet NAMES only -- no measures, no rates, no rep names,
no numbers of any kind. The secret is never echoed.

Temporary. Delete this file and .github/workflows/tableau-probe.yml once the
answers are recorded.
"""
import sys

from pull_render_email import log, tableau_signin

TARGET = "8-SalesRepLevelData"


def listing(session, url, label):
    r = session.get(url, params={"pageSize": 1000})
    if r.status_code != 200:
        log(f"  {label}: HTTP {r.status_code} -- not permitted for this token")
        return []
    books = r.json().get("workbooks", {}).get("workbook", [])
    if isinstance(books, dict):
        books = [books]
    log(f"  {label}: HTTP 200, {len(books)} workbooks")
    return books


def main():
    s, base, site_id = tableau_signin()

    # tableau_signin only returns the site id, so re-read the user id the
    # same way the sign-in response provides it.
    me = s.get(f"{base}/sites/{site_id}/users").json()
    users = me.get("users", {}).get("user", [])
    if isinstance(users, dict):
        users = [users]
    log(f"Users visible to this token: {len(users)}")

    log("")
    log("ANSWER 1: which listing endpoint works?")
    site_books = listing(s, f"{base}/sites/{site_id}/workbooks", "site-wide")

    user_books = []
    for user in users[:1]:
        uid = user.get("id")
        if uid:
            user_books = listing(
                s, f"{base}/sites/{site_id}/users/{uid}/workbooks", "per-user")

    books = site_books or user_books
    if not books:
        log("")
        log("ANSWER: NO -- this token cannot enumerate workbooks.")
        log("The picker will have to take typed names instead of a dropdown.")
        sys.exit(0)

    log("")
    log(f"ANSWER 2: {len(books)} workbooks visible. Names and content URLs:")
    for b in sorted(books, key=lambda x: str(x.get("name") or "")):
        log(f"  - {b.get('name')!r}  [{b.get('contentUrl')}]")

    log("")
    log("ANSWER 3: is the board's current workbook among them?")
    match = next((b for b in books
                  if str(b.get("contentUrl") or "").lower() == TARGET.lower()), None)
    log(f"  {TARGET}: {'FOUND' if match else 'NOT FOUND'}")

    if match:
        r = s.get(f"{base}/sites/{site_id}/workbooks/{match['id']}/views")
        if r.status_code == 200:
            views = r.json().get("views", {}).get("view", [])
            if isinstance(views, dict):
                views = [views]
            log(f"")
            log(f"ANSWER 4: {len(views)} sheets in {TARGET}:")
            for v in views:
                log(f"  - {v.get('name')!r}  [{v.get('contentUrl')}]")
        else:
            log(f"  Could not list its sheets (HTTP {r.status_code}).")

    log("")
    log("Done. No measures, rates or rep names were printed.")


if __name__ == "__main__":
    main()
