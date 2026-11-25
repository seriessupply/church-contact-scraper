"""Write an Instantly CSV of status=new Tier A contacts and mark them "Instantly".

Usage:
  python export_instantly.py               # up to 300 Tier A contacts
  python export_instantly.py --limit 60
  python export_instantly.py --include-b   # also include the Tier B tab (youth@ inboxes)
  python export_instantly.py --upload      # also add them straight to the Instantly campaign (nightly, Nov 23 2026)

--upload: contacts go to the campaign through the Series Supply Supabase function "instantly-import" (it skips
anyone already in the Instantly workspace). Only after Instantly accepts them are the rows marked "Instantly" in the
sheet, so a failed night just leaves them "new" for the next run. Needs CRM_EXPORT_KEY.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime

import requests

from common import OUTPUT_DIR, STATE_DIR, Sheet, append_lines, fingerprint, load_set

INSTANTLY_COLS = ["email", "firstName", "companyName", "city", "state", "role", "website"]
IMPORT_URL = "https://ykhpapnkprtlgczdawse.supabase.co/functions/v1/instantly-import"
SENT_FILE = os.path.join(STATE_DIR, "exported.txt")  # fingerprints of emails handed to Instantly

# Words that end up as a "first name" when a staff page lists a department or title instead of a person
# (Nov 19, 2026: "Fun", "ADULTS", "Handbell", "Missions"...). The campaign says "Hey {{firstName}}," with no
# fallback, so a bad name goes out as "Hey Handbell,". Those get "there" ("Hey there,") instead.
NOT_NAMES = {
    "adult", "adults", "audio", "deacon", "deaconess", "handbell", "handbells", "leads", "lead", "missions", "mission",
    "religious", "scavenger", "way", "fun", "youth", "student", "students", "ministry", "ministries", "kids", "children",
    "family", "families", "worship", "music", "media", "office", "admin", "info", "contact", "team", "staff", "pastor",
    "pastors", "church", "parish", "formation", "outreach", "welcome", "events", "middle", "high", "school", "director",
    "coordinator", "the", "our", "new", "life", "teen", "teens", "jr", "sr", "junior", "senior", "small", "groups",
    "group", "care", "connect", "communications", "tech", "production", "sound", "video", "camp", "general", "main",
}
# Real names that are also ordinary words: keep them only when the email backs them up (faith@ yes, greg@ no).
WORD_NAMES = {"faith", "grace", "hope", "joy", "angel", "fox", "lake", "sage", "arrow", "way", "glory", "praise"}


def clean_first_name(first, email):
    """Return a safe first name for "Hey {{firstName}},". Never invents a name; falls back to "there"."""
    f = (first or "").strip()
    local = (email or "").split("@")[0].lower()
    low = f.lower()
    if not f or not f.replace("-", "").replace("'", "").isalpha() or low in NOT_NAMES:
        return "there"
    if low in WORD_NAMES and low[:3] not in local:
        return "there"
    if f.isupper() and len(f) > 2:
        return "there" if low in NOT_NAMES else f.title()
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=int(os.environ.get("EXPORT_LIMIT") or 300))
    ap.add_argument("--include-b", action="store_true",
                    default=os.environ.get("INCLUDE_B", "").lower() in ("1", "true", "yes"))
    ap.add_argument("--upload", action="store_true")
    args = ap.parse_args()

    sheet = Sheet()
    if not sheet.enabled:
        print("SHEET_URL and SHEET_TOKEN are required.")
        return 1
    tiers = "AB" if args.include_b else "A"
    rows = sheet.get_rows(status="new", tier=tiers, limit=args.limit * 2)
    if rows is None:
        return 1
    already = load_set(SENT_FILE)
    picked = []
    for r in rows:  # already sorted best-first by the sheet
        fp = fingerprint(r["email"])
        if fp in already:
            continue
        picked.append(r)
        already.add(fp)
        if len(picked) >= args.limit:
            break
    if not picked:
        print("No new contacts to export.")
        return 0

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"instantly_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(INSTANTLY_COLS)
        for r in picked:
            w.writerow([r["email"], clean_first_name(r["first_name"], r["email"]), r["church"], r["city"], r["state_province"],
                        r["role"], r["website"]])

    emails = [r["email"] for r in picked]
    if args.upload:
        key = os.environ.get("CRM_EXPORT_KEY", "")
        if not key:
            print("CRM_EXPORT_KEY is not set yet, so nothing was uploaded or marked. The rows stay 'new'.")
            return 0
        leads = [{"email": r["email"], "first_name": clean_first_name(r["first_name"], r["email"]),
                  "company_name": r["church"], "website": r["website"], "city": r["city"],
                  "state": r["state_province"], "role": r["role"]} for r in picked]
        try:
            resp = requests.post(IMPORT_URL, json={"key": key, "leads": leads}, timeout=280)
            res = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"!! Instantly upload failed ({e}). Nothing marked; the rows stay 'new' for next run.")
            return 1
        inner = res.get("result") or {}
        ok = resp.ok and (inner.get("status") == "success" or res.get("mode") == "single" and not res.get("failed_count"))
        if not ok:
            print("!! Instantly did not accept the upload. Nothing marked; the rows stay 'new' for next run.")
            print("Instantly said:", str(res.get("error") or (res.get("result") or {}).get("status")), str(res.get("body", ""))[:300])
            return 1
        print(f"Instantly: uploaded {inner.get('leads_uploaded', res.get('added'))}, "
              f"already there {inner.get('duplicated_leads', 0) or inner.get('skipped_count', 0)}, "
              f"invalid {inner.get('invalid_email_count', 0)}, room left in plan {inner.get('remaining_in_plan', '?')}")
    if not sheet.mark(emails, "Instantly"):
        print("!! The CSV was written but the sheet did not mark the rows. Don't upload it twice.")
        return 1
    append_lines(SENT_FILE, [fingerprint(e) for e in emails])
    a = sum(1 for r in picked if r["tier"] == "A")
    print(f"Exported {len(picked)} contacts (Tier A: {a}, other: {len(picked) - a}) -> {path}")
    print("Those rows are now marked 'Instantly' in the sheet.")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"Exported **{len(picked)}** contacts (Tier A: {a}). Download the CSV from Artifacts below.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
