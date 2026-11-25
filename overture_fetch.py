"""Second church source: Overture Maps Places (free, open data from Meta, Microsoft and others).

Pulls every Christian place of worship in the US and Canada that lists a website, and saves one file per
state/province in state/overture/<REGION>.json.gz (same shape as the OpenStreetMap lists the scraper already uses).
The nightly scrape then visits those church websites exactly like it does now: only emails a church published
on its own site are ever collected.

Usage:
  python overture_fetch.py --probe     # print the schema, a sample and the category counts (no files written)
  python overture_fetch.py             # write state/overture/*.json
Set OVERTURE_RELEASE (e.g. 2026-11-19.0) to pin a release; otherwise the newest one is used.
"""
import argparse
import gzip
import json
import os
import re
import sys

import duckdb
import requests

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
OUT_DIR = os.path.join(STATE_DIR, "overture")
BUCKET = "s3://overturemaps-us-west-2/release"
# North America only (US incl. Alaska/Hawaii, and Canada) so DuckDB can skip most of the world's files.
BBOX = (-180.0, 17.0, -50.0, 84.0)
EXCLUDE_PRIMARY = ("jehovahs_witness_place_of_worship", "monastery", "seminary", "convent", "muslim_place_of_worship")
NOT_CHURCH_RX = r"(mosque|synagogue|temple|hindu|buddhist|sikh|jewish|islamic|kingdom_hall|mormon|latter)"


def latest_release():
    if os.environ.get("OVERTURE_RELEASE"):
        return os.environ["OVERTURE_RELEASE"]
    try:
        cat = requests.get("https://stac.overturemaps.org/catalog.json", timeout=30).json()
        ids = []
        for link in cat.get("links", []):
            m = re.search(r"(\d{4}-\d{2}-\d{2}\.\d+)", link.get("href", ""))
            if m:
                ids.append(m.group(1))
        if cat.get("latest"):
            return cat["latest"]
        if ids:
            return sorted(ids)[-1]
    except Exception as e:  # pragma: no cover
        print("STAC lookup failed:", e)
    sys.exit("Couldn't find the newest Overture release. Set OVERTURE_RELEASE.")


def connect():
    con = duckdb.connect()
    for sql in ("INSTALL httpfs", "LOAD httpfs", "SET s3_region='us-west-2'"):
        con.execute(sql)
    try:
        con.execute("CREATE SECRET (TYPE S3, PROVIDER config, REGION 'us-west-2')")
    except Exception:
        pass
    return con


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    rel = latest_release()
    src = f"{BUCKET}/{rel}/theme=places/type=place/*"
    print("Overture release:", rel)
    con = connect()
    base = f"read_parquet('{src}', hive_partitioning=1)"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {base}").fetchall()}
    print("columns:", sorted(cols))
    # Nov 19, 2026 release: basic_category = christian_place_of_worship / place_of_worship / religious_organization,
    # taxonomy.primary = the specific kind (baptist_place_of_worship, monastery...).
    prim = "lower(coalesce(taxonomy.primary, ''))" if "taxonomy" in cols else "''"
    where = (f"bbox.xmin > {BBOX[0]} AND bbox.xmax < {BBOX[2]} AND bbox.ymin > {BBOX[1]} AND bbox.ymax < {BBOX[3]} "
             f"AND len(websites) > 0 AND addresses[1].country IN ('US','CA') "
             f"AND basic_category IN ('christian_place_of_worship', 'place_of_worship', 'religious_organization') "
             f"AND {prim} NOT IN ({', '.join(repr(x) for x in EXCLUDE_PRIMARY)}) "
             f"AND NOT regexp_matches(lower(coalesce(names.primary,'')), '{NOT_CHURCH_RX}')")
    cat_expr = prim
    conf = " AND (confidence IS NULL OR confidence >= 0.5)" if "confidence" in cols else ""
    q = (f"SELECT id, names.primary AS name, websites[1] AS website, "
         f"CASE WHEN len(phones) > 0 THEN phones[1] ELSE '' END AS phone, "
         f"addresses[1].locality AS city, addresses[1].region AS region, addresses[1].country AS country, "
         f"lower({cat_expr}) AS cat FROM {base} WHERE {where}{conf}")
    if args.probe:
        print(con.execute(f"SELECT cat, count(*) n FROM ({q}) GROUP BY cat ORDER BY n DESC LIMIT 40").fetchall())
        print(con.execute(f"SELECT country, count(*) FROM ({q}) GROUP BY country").fetchall())
        for r in con.execute(f"SELECT * FROM ({q}) LIMIT 8").fetchall():
            print(r)
        return
    rows = con.execute(q).fetchall()
    print("churches with a website:", len(rows))
    by_region = {}
    for id_, name, site, phone, city, region, country, _ in rows:
        reg = str(region or "").upper()
        reg = reg.split("-")[-1][:2] if reg else ""
        if not reg or not site:
            continue
        code = f"{country}-{reg}"
        by_region.setdefault(code, []).append({"osm": f"ov/{id_}", "name": (name or "").strip(),
                                               "city": (city or "").strip(), "state": reg,
                                               "website": str(site).strip(), "phone": str(phone or "").strip()})
    os.makedirs(OUT_DIR, exist_ok=True)
    for code, lst in by_region.items():
        lst.sort(key=lambda c: c["osm"])  # stable order so offsets stay valid
        with gzip.open(os.path.join(OUT_DIR, f"{code}.json.gz"), "wt", encoding="utf-8") as f:
            json.dump(lst, f, separators=(",", ":"))
    with open(os.path.join(OUT_DIR, "_release.txt"), "w") as f:
        f.write(rel + "\n")
    print({k: len(v) for k, v in sorted(by_region.items())})


if __name__ == "__main__":
    main()
