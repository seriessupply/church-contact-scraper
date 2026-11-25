"""Shared helpers: secrets, fingerprints, Google Sheet client."""
import hashlib
import json
import os
import time

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
OUTPUT_DIR = os.path.join(ROOT, "output")

COLUMNS = [
    "added", "status", "tier", "church", "city", "state_province", "country",
    "website", "first_name", "full_name", "role", "email", "email_source_url",
    "youth_page_url", "phone",
]

USER_AGENT = "SeriesSupplyContactBot/1.0 (+https://seriessupply.com; youth ministry resources)"


def secret(name):
    """Read a secret from the environment, stripping stray spaces, newlines and quotes."""
    v = os.environ.get(name, "") or ""
    v = v.strip().strip('"').strip("'").strip()
    return v


def fingerprint(value):
    """SHA-256 fingerprint (first 20 hex chars) of a lowercased email or domain."""
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:20]


def load_set(path):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_lines(path, lines):
    if not lines:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


class Sheet:
    """Talks to the Apps Script web app. Every failure is logged loudly, never raised."""

    def __init__(self):
        self.url = secret("SHEET_URL")
        self.token = secret("SHEET_TOKEN")
        self.enabled = bool(self.url and self.token)
        if not self.enabled:
            print("!! SHEET_URL / SHEET_TOKEN not set. Rows will only be saved to the CSV backup.")

    def _post(self, payload):
        payload = dict(payload, token=self.token)
        last = None
        for attempt in range(3):
            try:
                r = requests.post(self.url, data=json.dumps(payload),
                                  headers={"Content-Type": "text/plain;charset=utf-8"},
                                  timeout=60, allow_redirects=True)
                try:
                    data = r.json()
                except ValueError:
                    snippet = r.text[:200].replace("\n", " ")
                    last = (f"HTTP {r.status_code}, not JSON. Usually means the web app isn't deployed "
                            f"with access 'Anyone', or SHEET_URL isn't the /exec URL. Got: {snippet}")
                    time.sleep(3)
                    continue
                if data.get("ok"):
                    return data
                last = f"sheet refused: {data.get('error')}"
                break  # a refusal (bad token etc.) won't fix itself on retry
            except requests.RequestException as e:
                last = f"network error: {e}"
                time.sleep(3)
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f"!! GOOGLE SHEET POST FAILED: {last}")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        return None

    def add_rows(self, rows):
        """Returns True if the sheet accepted the rows."""
        if not self.enabled or not rows:
            return False
        ok = True
        for i in range(0, len(rows), 100):
            chunk = rows[i:i + 100]
            res = self._post({"action": "add", "rows": [[r.get(c, "") for c in COLUMNS] for r in chunk]})
            if res is None:
                ok = False
            else:
                print(f"Sheet: added {res.get('added')} rows, skipped {res.get('skipped')} duplicates.")
        return ok

    def get_rows(self, status="new", tier="", limit=500):
        if not self.enabled:
            return None
        params = {"token": self.token, "status": status, "tier": tier, "limit": str(limit)}
        try:
            r = requests.get(self.url, params=params, timeout=60)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"!! GOOGLE SHEET READ FAILED: {e}")
            return None
        if not data.get("ok"):
            print(f"!! GOOGLE SHEET READ REFUSED: {data.get('error')}")
            return None
        return data["rows"]

    def mark(self, emails, status):
        res = self._post({"action": "mark", "emails": emails, "status": status})
        return res is not None
