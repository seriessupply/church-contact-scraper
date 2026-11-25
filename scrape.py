"""Nightly church youth-contact scraper for Series Supply.

OpenStreetMap churches (Overpass) -> church websites -> published youth/student contacts
-> Google Sheet + CSV backup. Progress is saved in state/ so each night picks up where
the last one stopped.

Usage:
  python scrape.py                 # normal nightly run (75 Tier A contacts or 25 min)
  python scrape.py --limit 5       # quick test
"""
import argparse
import csv
import gzip
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from common import (COLUMNS, OUTPUT_DIR, STATE_DIR, USER_AGENT, Sheet, append_lines, fingerprint,
                    load_set)
import extract

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Edmonton")
except Exception:  # pragma: no cover
    LOCAL_TZ = None

US_STATES = [
    ("US-AL", "AL"), ("US-AK", "AK"), ("US-AZ", "AZ"), ("US-AR", "AR"), ("US-CA", "CA"), ("US-CO", "CO"),
    ("US-CT", "CT"), ("US-DE", "DE"), ("US-DC", "DC"), ("US-FL", "FL"), ("US-GA", "GA"), ("US-HI", "HI"),
    ("US-ID", "ID"), ("US-IL", "IL"), ("US-IN", "IN"), ("US-IA", "IA"), ("US-KS", "KS"), ("US-KY", "KY"),
    ("US-LA", "LA"), ("US-ME", "ME"), ("US-MD", "MD"), ("US-MA", "MA"), ("US-MI", "MI"), ("US-MN", "MN"),
    ("US-MS", "MS"), ("US-MO", "MO"), ("US-MT", "MT"), ("US-NE", "NE"), ("US-NV", "NV"), ("US-NH", "NH"),
    ("US-NJ", "NJ"), ("US-NM", "NM"), ("US-NY", "NY"), ("US-NC", "NC"), ("US-ND", "ND"), ("US-OH", "OH"),
    ("US-OK", "OK"), ("US-OR", "OR"), ("US-PA", "PA"), ("US-RI", "RI"), ("US-SC", "SC"), ("US-SD", "SD"),
    ("US-TN", "TN"), ("US-TX", "TX"), ("US-UT", "UT"), ("US-VT", "VT"), ("US-VA", "VA"), ("US-WA", "WA"),
    ("US-WV", "WV"), ("US-WI", "WI"), ("US-WY", "WY"),
]
CA_PROVINCES = [
    ("CA-AB", "AB"), ("CA-BC", "BC"), ("CA-MB", "MB"), ("CA-NB", "NB"), ("CA-NL", "NL"), ("CA-NS", "NS"),
    ("CA-ON", "ON"), ("CA-PE", "PE"), ("CA-QC", "QC"), ("CA-SK", "SK"), ("CA-NT", "NT"), ("CA-NU", "NU"),
    ("CA-YT", "YT"),
]
REGIONS = US_STATES + CA_PROVINCES

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

SKIP_HOSTS = ("facebook.com", "fb.com", "fb.me", "instagram.com", "twitter.com", "x.com", "youtube.com",
              "youtu.be", "linktr.ee", "yelp.com", "google.com", "goo.gl", "maps.app.goo.gl", "tiktok.com",
              "linkedin.com", "wikipedia.org", "findagrave.com", "yellowpages.com", "mapquest.com")
SHARED_HOSTS = ("sites.google.com", "churchcenter.com", "faithlife.com", "wixsite.com", "weebly.com",
                "blogspot.com", "wordpress.com", "squarespace.com", "umc.org", "elca.org", "lcms.org",
                "pcusa.org", "sbc.net", "catholic.org", "ag.org")
FREE_MAIL = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "me.com",
             "live.com", "msn.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "protonmail.com",
             "mac.com", "shaw.ca", "telus.net", "rogers.com", "sympatico.ca", "bell.net")

PROGRESS_FILE = os.path.join(STATE_DIR, "progress.json")
EMAILS_FILE = os.path.join(STATE_DIR, "contacted.txt")      # SHA-256 fingerprints, never plain emails
DOMAINS_FILE = os.path.join(STATE_DIR, "domains.txt")       # fingerprints of church sites already crawled
CACHE_DIR = os.path.join(STATE_DIR, "cache")
PENDING_FILE = os.path.join(OUTPUT_DIR, "pending_sheet.json")

PER_SITE_SECONDS = 75
REQUEST_TIMEOUT = 10
DELAY = 1.0
MAX_PAGES = 6
MAX_BYTES = 2_000_000


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def local_today():
    now = datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()
    return now.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- state

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"region_index": 0, "offset": 0, "finished": False}


def save_progress(p):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)
    os.replace(tmp, PROGRESS_FILE)


# ---------------------------------------------------------------- overpass

def fetch_region(code):
    """Return list of church dicts for a region, cached in state/cache. None if Overpass is down."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{code}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    query = (
        f'[out:json][timeout:300];area["ISO3166-2"="{code}"]->.a;('
        'nwr(area.a)["amenity"="place_of_worship"]["religion"="christian"]["website"];'
        'nwr(area.a)["amenity"="place_of_worship"]["religion"="christian"]["contact:website"];'
        ");out center tags;"
    )
    data = None
    give_up = time.time() + 8 * 60  # never let a bad Overpass night eat the run
    for attempt in range(2):
        for ep in OVERPASS_ENDPOINTS:
            left = give_up - time.time()
            if left < 20:
                break
            try:
                log(f"Overpass: {code} via {urlparse(ep).netloc}")
                r = requests.post(ep, data={"data": query}, timeout=(15, min(330, left)),
                                  headers={"User-Agent": USER_AGENT})
                if r.status_code == 200 and r.text.lstrip().startswith("{"):
                    data = r.json()
                    break
                log(f"Overpass {urlparse(ep).netloc} returned HTTP {r.status_code}")
            except (requests.RequestException, ValueError) as e:
                log(f"Overpass {urlparse(ep).netloc} failed: {str(e)[:120]}")
        if data or time.time() > give_up - 30:
            break
        time.sleep(20)
    if not data:
        return None
    seen, churches = set(), []
    for el in data.get("elements", []):
        t = el.get("tags", {})
        site = (t.get("website") or t.get("contact:website") or "").split(";")[0].strip()
        key = f"{el['type']}/{el['id']}"
        if not site or key in seen:
            continue
        seen.add(key)
        churches.append({
            "osm": key,
            "name": t.get("name", "").strip(),
            "city": t.get("addr:city", "").strip(),
            "state": t.get("addr:state", "").strip(),
            "website": site,
            "phone": (t.get("phone") or t.get("contact:phone") or "").split(";")[0].strip(),
        })
    churches.sort(key=lambda c: c["osm"])  # stable order so offsets stay valid
    with open(path, "w", encoding="utf-8") as f:
        json.dump(churches, f, separators=(",", ":"))
    log(f"Overpass: {code} has {len(churches)} Christian places of worship with a website")
    return churches


OVERTURE_DIR = os.path.join(STATE_DIR, "overture")


def overture_region(code):
    """Second source (Nov 19, 2026): Overture Maps churches with a website for this region, from
    state/overture/<code>.json.gz (refreshed monthly by overture_fetch.py). Empty list if not there."""
    path = os.path.join(OVERTURE_DIR, f"{code}.json.gz")
    if not os.path.exists(path):
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log(f"Overture file for {code} unreadable: {e}")
        return []


def region_churches(code, progress):
    """Pass 1: OpenStreetMap churches, then (once Overture data exists) Overture churches appended after them.
    Pass 2: Overture-only for the regions pass 1 finished before Overture was added. Sites already crawled are
    skipped by the domain dedupe, so a church in both sources is only visited once."""
    ov = overture_region(code)
    if progress.get("pass", 1) == 2:
        return ov
    osm = fetch_region(code)
    if osm is None:
        return None
    if ov and "union_from" not in progress:
        progress["union_from"] = progress["region_index"]
        log(f"Overture data found: from {code} on, each region also gets its Overture churches")
    return osm + ov if "union_from" in progress else osm


# ---------------------------------------------------------------- crawling

def normalize_site(url):
    url = url.strip()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    p = urlparse(url)
    if not p.netloc or "." not in p.netloc:
        return None
    return url


def site_key(url):
    p = urlparse(url)
    host = p.netloc.lower().split(":")[0].removeprefix("www.")
    if any(host == h or host.endswith("." + h) for h in SHARED_HOSTS):
        seg = p.path.strip("/").split("/")[0] if p.path.strip("/") else ""
        return f"{host}/{seg}".lower()
    return host


def skip_host(url):
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


class Site:
    def __init__(self, start_url):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT,
                               "Accept": "text/html,application/xhtml+xml",
                               "Accept-Language": "en-US,en;q=0.8"})
        self.start = start_url
        self.robots = None
        self.last = 0.0
        self.deadline = time.time() + PER_SITE_SECONDS

    def _wait(self):
        gap = time.time() - self.last
        if gap < DELAY:
            time.sleep(DELAY - gap + random.uniform(0, 0.3))
        self.last = time.time()

    def _get(self, url):
        self._wait()
        r = self.s.get(url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True)
        ctype = r.headers.get("Content-Type", "")
        if r.status_code != 200 or ("html" not in ctype and "text" not in ctype and ctype):
            r.close()
            return None, r.url
        chunks, size = [], 0
        for chunk in r.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                break
        r.close()
        enc = r.encoding or "utf-8"
        if enc.lower() == "iso-8859-1":
            enc = "utf-8"
        return b"".join(chunks).decode(enc, errors="replace"), r.url

    def load_robots(self, base):
        p = urlparse(base)
        rp = RobotFileParser()
        try:
            self._wait()
            r = self.s.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=8)
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code == 200 and "html" not in r.headers.get("Content-Type", ""):
                rp.parse(r.text.splitlines())
            else:
                rp.allow_all = True
        except requests.RequestException:
            rp.allow_all = True
        self.robots = rp

    def allowed(self, url):
        if self.robots is None:
            return True
        try:
            return self.robots.can_fetch("SeriesSupplyContactBot", url)
        except Exception:
            return True

    def fetch(self, url):
        if time.time() > self.deadline or not self.allowed(url):
            return None, url
        try:
            return self._get(url)
        except requests.exceptions.SSLError:
            if url.startswith("https://"):
                try:
                    return self._get("http://" + url[8:])
                except requests.RequestException:
                    return None, url
            return None, url
        except requests.RequestException:
            return None, url


def crawl_church(church, region_abbr, country):
    """Crawl one church site. Returns (contacts, info dict)."""
    url = normalize_site(church["website"])
    info = {"youth": False, "pages": 0, "keys": []}
    if not url or skip_host(url):
        return [], info
    site = Site(url)
    site.load_robots(url)
    html_text, final = site.fetch(url)
    if not html_text:
        return [], info
    host = urlparse(final).netloc.lower().removeprefix("www.")
    info["keys"] = list({site_key(url), site_key(final)})
    if site.robots and urlparse(final).netloc != urlparse(url).netloc:
        site.load_robots(final)

    pages = []  # (url, parsed)
    parsed = extract.parse_page(html_text)
    pages.append((final, parsed))
    if not church.get("name"):  # OSM had no name: use the church's own site name
        church = dict(church, name=extract.church_name_from_page(parsed, host) or host)
    queue = extract.pick_links(final, parsed.links, host)
    visited = {final.rstrip("/"), url.rstrip("/")}
    while queue and len(pages) < MAX_PAGES and time.time() < site.deadline:
        nxt = queue.pop(0)
        if nxt.rstrip("/") in visited:
            continue
        visited.add(nxt.rstrip("/"))
        text, furl = site.fetch(nxt)
        if not text:
            continue
        p = extract.parse_page(text)
        pages.append((furl, p))
        for u in extract.pick_links(furl, p.links, host):
            if u.rstrip("/") not in visited and u not in queue:
                queue.append(u)
        # keep youth/staff pages first
        queue.sort(key=lambda u: 0 if extract.PAGE_KEYWORDS[0][0].search(u) else 1)
    info["pages"] = len(pages)

    all_text = " ".join(" ".join(b[0] for b in p.blocks) + " " + " ".join(t for _, t in p.links)
                        for _, p in pages)
    if not extract.has_youth_signal(all_text):
        return [], info
    info["youth"] = True

    youth_page = ""
    for purl, p in pages:
        if extract.PAGE_KEYWORDS[0][0].search(urlparse(purl).path) or \
                extract.PAGE_KEYWORDS[0][0].search(p.title or ""):
            youth_page = purl
            break

    phone = church.get("phone", "")
    if not phone:
        for _, p in pages:
            ph = extract.first_phone(" ".join(b[0] for b in p.blocks))
            if ph:
                phone = ph
                break

    found = {}
    for purl, p in pages:
        for c in extract.contacts_from_page(purl, p):
            prev = found.get(c["email"])
            # keep the richest version of each email
            score = (2 if c["full_name"] else 0) + (1 if c["role"] else 0)
            if not prev or score > prev["_score"]:
                c["_score"] = score
                found[c["email"]] = c

    root = host.split(":")[0]
    tiered = []
    for email, c in found.items():
        edom = email.split("@")[1]
        on_site = edom == root or root.endswith("." + edom) or edom.endswith("." + root) or \
            edom.split(".")[-2:] == root.split(".")[-2:]
        free = edom in FREE_MAIL
        role_inbox = extract.is_role_inbox(email)
        general = extract.is_general_inbox(email)
        youth_prefix = role_inbox and not email.split("@")[0].startswith(("family", "families", "fam", "creative"))
        if not (on_site or free or c["role"]):
            continue  # someone else's address (web designer, denomination office, ...)
        tier = None
        if c["role"] and c["full_name"] and not role_inbox and not general \
                and extract.name_matches_email(c["full_name"], email):
            tier = "A"
        elif youth_prefix or (role_inbox and c["role"]):
            tier = "B"
        elif c["role"] and not general:
            tier = "B"
            c["full_name"] = "" if not extract.name_matches_email(c["full_name"] or "x", email) else c["full_name"]
        elif on_site or free:
            tier = "C"
        if tier:
            c["tier"] = tier
            tiered.append(c)

    a = [c for c in tiered if c["tier"] == "A"]
    b = [c for c in tiered if c["tier"] == "B"]
    cs = [c for c in tiered if c["tier"] == "C"]
    if a:
        # at most 2 people per church, pastors/directors first, so one church isn't emailed 5 times
        a.sort(key=lambda c: (0 if re.search(r"pastor|director|minister", c["role"], re.I) else 1,
                              0 if re.search(r"youth|student", c["role"], re.I) else 1))
        keep = a[:2]
    elif b:
        keep = b[:1]
    else:  # general church emails (office@, info@) are no longer saved
        keep = []

    rows = []
    for c in keep:
        full = c["full_name"] if c["tier"] != "C" else ""
        first = ""
        if full:
            for tok in full.split():
                if len(tok.strip(".")) > 1:
                    first = tok
                    break
        rows.append({
            "added": local_today(),
            "status": "new",
            "tier": c["tier"],
            "church": church.get("name", ""),
            "city": church.get("city", ""),
            "state_province": church.get("state") or region_abbr,
            "country": country,
            "website": final,
            "first_name": first,
            "full_name": full,
            "role": c["role"] or ("Youth/Student Ministry" if c["tier"] == "B" else ""),
            "email": c["email"],
            "email_source_url": c["source_url"],
            "youth_page_url": youth_page,
            "phone": phone,
        })
    return rows, info


# ---------------------------------------------------------------- output

class Output:
    def __init__(self, sheet):
        self.sheet = sheet
        self.lock = threading.Lock()
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        self.csv_path = os.path.join(OUTPUT_DIR, f"{local_today()}.csv")
        self.buffer = []
        self.emails = load_set(EMAILS_FILE)
        self.domains = load_set(DOMAINS_FILE)
        self.counts = {"A": 0, "B": 0, "C": 0}

    def seen_domain(self, key):
        return fingerprint(key) in self.domains

    def mark_domains(self, keys):
        with self.lock:
            new = [fingerprint(k) for k in keys if fingerprint(k) not in self.domains]
            self.domains.update(new)
            append_lines(DOMAINS_FILE, new)

    def add(self, rows):
        """Save rows to CSV right away (so nothing is lost), queue them for the sheet."""
        added = []
        with self.lock:
            for r in rows:
                fp = fingerprint(r["email"])
                if fp in self.emails:
                    continue
                self.emails.add(fp)
                append_lines(EMAILS_FILE, [fp])
                added.append(r)
                self.counts[r["tier"]] += 1
            if added:
                new_file = not os.path.exists(self.csv_path)
                with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=COLUMNS)
                    if new_file:
                        w.writeheader()
                    for r in added:
                        w.writerow({c: r.get(c, "") for c in COLUMNS})
                self.buffer.extend(added)
        return added

    def flush_sheet(self, force=False):
        with self.lock:
            if not self.buffer or (len(self.buffer) < 20 and not force):
                return
            rows, self.buffer = self.buffer, []
        if not self.sheet.add_rows(rows):
            save_pending(load_pending() + rows)


def load_pending():
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_pending(rows):
    if rows:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        log(f"Saved {len(rows)} rows to output/pending_sheet.json; they'll be retried next run.")
    elif os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=int(os.environ.get("TEST_LIMIT") or 0) or 75,
                    help="stop after this many new Tier A contacts")
    ap.add_argument("--max-minutes", type=float, default=float(os.environ.get("MAX_MINUTES") or 25))
    ap.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS") or 10))
    args = ap.parse_args()
    start = time.time()
    log(f"Target: {args.limit} new Tier A contacts, time budget {args.max_minutes} min")

    sheet = Sheet()
    pending = load_pending()
    if pending:
        log(f"Retrying {len(pending)} rows the sheet missed last time")
        save_pending([] if sheet.add_rows(pending) else pending)

    out = Output(sheet)
    progress = load_progress()
    stats = {"sites": 0, "youth": 0, "skipped_seen": 0}
    stop_reason = "target reached"

    def ab():  # only Tier A counts toward the nightly target
        return out.counts["A"]

    def time_left():
        return args.max_minutes * 60 - (time.time() - start)

    while ab() < args.limit:
        if progress["region_index"] >= len(REGIONS):
            if progress.get("pass", 1) == 1 and progress.get("union_from"):
                # second lap: Overture churches for the regions finished before Overture was added
                progress.update({"pass": 2, "region_index": 0, "offset": 0})
                save_progress(progress)
                log("Pass 2: Overture churches for the states done before Nov 19, 2026")
                continue
            progress["finished"] = True
            stop_reason = "every US state and Canadian province is done"
            break
        if progress.get("pass", 1) == 2 and progress["region_index"] >= progress.get("union_from", len(REGIONS)):
            progress["region_index"] = len(REGIONS)  # the rest already had Overture in pass 1
            continue
        if time_left() < 5 * 60:
            stop_reason = "time budget used"
            break
        code, abbr = REGIONS[progress["region_index"]]
        country = "US" if code.startswith("US") else "CA"
        churches = region_churches(code, progress)
        if churches is None:
            stop_reason = "Overpass is down; stopped early and saved everything found so far"
            break
        offset = progress["offset"]
        log(f"Region {code}: starting at {offset} of {len(churches)}")

        done = set()
        lock = threading.Lock()

        def advance():
            nonlocal offset
            while offset in done:
                done.discard(offset)
                offset += 1
            progress["offset"] = offset
            save_progress(progress)

        def work(idx, church):
            rows, info = crawl_church(church, abbr, country)
            return idx, church, rows, info

        idx = offset
        futures = {}
        claimed = set()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            while True:
                # keep the pool fed
                while (len(futures) < args.workers * 2 and idx < len(churches)
                       and ab() < args.limit and time_left() > PER_SITE_SECONDS + 15):
                    church = churches[idx]
                    url = normalize_site(church["website"])
                    if not url or skip_host(url) or out.seen_domain(site_key(url)):
                        stats["skipped_seen"] += 1
                        with lock:
                            done.add(idx)
                        idx += 1
                        continue
                    key = site_key(url)
                    if key in claimed:
                        stats["skipped_seen"] += 1
                        with lock:
                            done.add(idx)
                        idx += 1
                        continue
                    claimed.add(key)  # twin OSM entries for the same site don't double-crawl
                    futures[pool.submit(work, idx, church)] = idx
                    idx += 1
                with lock:
                    advance()
                if not futures:
                    break
                finished, _ = wait(list(futures), timeout=30, return_when=FIRST_COMPLETED)
                for f in finished:
                    i = futures.pop(f)
                    try:
                        _, church, rows, info = f.result()
                    except Exception as e:  # one bad site never stops the night
                        log(f"error on {churches[i].get('website')}: {e}")
                        rows, info = [], {"youth": False, "keys": []}
                    stats["sites"] += 1
                    if info.get("keys"):
                        out.mark_domains(info["keys"])
                    if info.get("youth"):
                        stats["youth"] += 1
                    new = out.add(rows)
                    for r in new:  # the repo and its logs are public: never print names or emails
                        log(f"  {r['tier']}  {r['state_province']}  {r['role'][:28]}")
                    with lock:
                        done.add(i)
                    out.flush_sheet()
                    if stats["sites"] % 25 == 0:
                        log(f"... {stats['sites']} sites crawled, A={out.counts['A']} B={out.counts['B']} "
                            f"C={out.counts['C']}, {time_left() / 60:.1f} min left")
                if ab() >= args.limit or time_left() <= PER_SITE_SECONDS + 15:
                    # drop sites that haven't started yet; they stay in the queue for next night
                    for f in list(futures):
                        if f.cancel():
                            futures.pop(f)
                    if not futures:
                        break
                if idx >= len(churches) and not futures:
                    break
            with lock:
                advance()

        if offset >= len(churches):
            log(f"Region {code} complete")
            progress["region_index"] += 1
            progress["offset"] = 0
            save_progress(progress)
            try:
                os.remove(os.path.join(CACHE_DIR, f"{code}.json"))
            except OSError:
                pass
        if ab() >= args.limit:
            stop_reason = "target reached"
            break
        if time_left() <= PER_SITE_SECONDS + 15:
            stop_reason = "time budget used"
            break

    out.flush_sheet(force=True)
    save_progress(progress)
    mins = (time.time() - start) / 60
    region = REGIONS[min(progress["region_index"], len(REGIONS) - 1)][0]
    summary = (f"Stopped: {stop_reason}\n"
               f"New contacts: A={out.counts['A']}  B={out.counts['B']}  C={out.counts['C']}  "
               f"(Tier A {ab()} of {args.limit})\n"
               f"Sites crawled: {stats['sites']}  with youth signal: {stats['youth']}  "
               f"skipped (already seen / social): {stats['skipped_seen']}\n"
               f"Now at: {region} offset {progress['offset']} (pass {progress.get('pass', 1)})\n"
               f"Run time: {mins:.1f} min\n"
               f"CSV backup: output/{os.path.basename(out.csv_path)}")
    log("\n" + summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("### Series Supply contact scraper\n```\n" + summary + "\n```\n")


if __name__ == "__main__":
    sys.exit(main())
