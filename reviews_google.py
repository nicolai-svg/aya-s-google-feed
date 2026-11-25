#!/usr/bin/env python3
"""
Generate a Google Merchant Center *product reviews* feed from Judge.me.

This replaces Judge.me's paid ($15/mo) Google Shopping review syndication with a
self-hosted feed we control. It pulls all published product reviews from the
Judge.me API and emits XML in Google's product reviews schema 2.3, which is the
format Google ingests for Product Ratings (the star ratings on Shopping ads and
free listings).

Enrollment note: to actually show, the feed must be submitted to Merchant Center
AND the account enrolled in Product Ratings (interest form). Needs >=50 reviews
(we have thousands) and each review matched to a product by URL / identifiers.

Config (env or .env.reviews, loaded without needing to `source` it):
  JUDGEME_PRIVATE_TOKEN   Judge.me private API token
  JUDGEME_SHOP_DOMAIN     e.g. f260e4.myshopify.com
  STORE_URL               storefront base, e.g. https://aya-s.dk (for product URLs)
  REVIEWS_PUBLISHER       publisher name shown in the feed (default: AYA-S)
  REVIEWS_OUTPUT_PATH     output file (default: output/google_reviews.xml)
  REVIEWS_ENRICH          "1" -> look products up in Shopify (needs SHOPIFY_STORE_DOMAIN +
                          SHOPIFY_ADMIN_TOKEN) and add GTINs + variant IDs so Google can
                          match reviews to offers by identifier, not just by URL.

The same script serves several stores: pass --env to pick the config file, e.g.
  python3 reviews_google.py                      # DK  (.env.reviews + .env)
  python3 reviews_google.py --env .env.reviews.ww  # EU / Worldwide store
"""

import argparse
import os
import sys
import time
import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

API_BASE = "https://judge.me/api/v1/reviews.json"
SCHEMA = "http://www.google.com/shopping/reviews/schema/product/2.3/product_reviews.xsd"


def _load_dotenv(path):
    """Load a .env-style file ourselves (values may contain &, >, spaces, #)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            if " #" in val:
                val = val.split(" #", 1)[0]
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val and key not in os.environ:
                os.environ[key] = val


def need(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing {name} — set it in the environment or .env.reviews.")
    return val


def fetch_all_reviews(token, shop, per_page=100, max_pages=200):
    """Page through the Judge.me reviews API until a page comes back empty."""
    out = []
    for page in range(1, max_pages + 1):
        qs = urllib.parse.urlencode(
            {"api_token": token, "shop_domain": shop, "per_page": per_page, "page": page}
        )
        req = urllib.request.Request(f"{API_BASE}?{qs}", headers={"User-Agent": "aya-reviews-feed"})
        for attempt in range(3):
            try:
                import json
                data = json.load(urllib.request.urlopen(req, timeout=60))
                break
            except Exception as e:  # transient — back off and retry
                if attempt == 2:
                    sys.exit(f"Judge.me API failed on page {page}: {e}")
                time.sleep(2 * (attempt + 1))
        reviews = data.get("reviews", [])
        if not reviews:
            break
        out.extend(reviews)
        if len(reviews) < per_page:
            break
        time.sleep(0.3)  # be gentle on the API
    return out


def is_product_review(r):
    """Keep only published, non-hidden reviews tied to a real product."""
    pid = str(r.get("product_external_id") or "0")
    if pid == "0":
        return False  # store review, not a product review
    if str(r.get("published")).lower() not in ("true", "1"):
        return False
    if str(r.get("hidden")).lower() in ("true", "1"):
        return False
    try:
        rating = int(r.get("rating") or 0)
    except (TypeError, ValueError):
        return False
    return 1 <= rating <= 5


def sub(parent, tag, text=None, **attrib):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrib.items()})
    if text is not None:
        el.text = str(text)
    return el


def fetch_product_ids():
    """Map Shopify product id -> {"gtins": [...], "variants": [...]} (active products)."""
    from feed_google import fetch_all_products, numeric_id, looks_like_gtin

    out = {}
    for node in fetch_all_products():
        gtins, variants = [], []
        for edge in node["variants"]["edges"]:
            v = edge["node"]
            variants.append(numeric_id(v["id"]))
            code = (v.get("barcode") or "").strip()
            if looks_like_gtin(code) and code not in gtins:
                gtins.append(code)
        out[numeric_id(node["id"])] = {"gtins": gtins, "variants": variants}
    return out


def build_feed(reviews, store_url, publisher, product_ids=None):
    product_ids = product_ids or {}
    store_url = store_url.rstrip("/")
    feed = ET.Element(
        "feed",
        {
            "xmlns:vc": "http://www.w3.org/2007/XMLSchema-versioning",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": SCHEMA,
        },
    )
    sub(feed, "version", "2.3")
    pub = sub(feed, "publisher")
    sub(pub, "name", publisher)
    reviews_el = sub(feed, "reviews")

    kept = 0
    for r in reviews:
        if not is_product_review(r):
            continue
        handle = (r.get("product_handle") or "").strip()
        if not handle:
            continue
        product_url = f"{store_url}/products/{handle}"
        reviewer = r.get("reviewer") or {}
        name = (reviewer.get("name") or "Anonymous").strip() or "Anonymous"
        reviewer_id = str(reviewer.get("id") or r.get("id"))
        body = (r.get("body") or "").strip()
        if not body:
            continue  # Google requires review content
        title = (r.get("title") or "").strip()
        rating = int(r.get("rating"))
        ts = (r.get("created_at") or "").strip()

        rev = sub(reviews_el, "review")
        sub(rev, "review_id", r.get("id"))
        rvr = sub(rev, "reviewer")
        sub(rvr, "name", name)
        sub(rvr, "reviewer_id", reviewer_id)
        if ts:
            sub(rev, "review_timestamp", ts)
        if title:
            sub(rev, "title", title)
        sub(rev, "content", body)
        sub(rev, "review_url", product_url, type="singleton")
        ratings = sub(rev, "ratings")
        sub(ratings, "overall", rating, min="1", max="5")
        products = sub(rev, "products")
        product = sub(products, "product")
        pids = sub(product, "product_ids")
        pid = str(r.get("product_external_id"))
        extra = product_ids.get(pid, {})
        if extra.get("gtins"):
            gtins = sub(pids, "gtins")
            for g in extra["gtins"]:
                sub(gtins, "gtin", g)
        skus = sub(pids, "skus")
        sub(skus, "sku", pid)
        for vid in extra.get("variants", []):
            sub(skus, "sku", vid)  # variant id == g:id in our product feeds
        brands = sub(pids, "brands")
        sub(brands, "brand", publisher)
        sub(product, "product_name", (r.get("product_title") or handle).strip())
        sub(product, "product_url", product_url)
        sub(rev, "is_spam", "false")  # schema order: is_spam before transaction_id
        if str(r.get("verified") or "").startswith("verified") or r.get("verified") == "buyer":
            sub(rev, "transaction_id", f"jm-{r.get('id')}")
        kept += 1

    return feed, kept


def main():
    ap = argparse.ArgumentParser(description="Generate the Google product reviews feed.")
    ap.add_argument("--env", action="append",
                    help="config file(s) to load (default: .env.reviews then .env)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    for name in args.env or [".env.reviews", ".env"]:
        _load_dotenv(os.path.join(here, name))

    token = need("JUDGEME_PRIVATE_TOKEN")
    shop = need("JUDGEME_SHOP_DOMAIN")
    store_url = os.environ.get("STORE_URL", "https://aya-s.dk")
    publisher = os.environ.get("REVIEWS_PUBLISHER", "AYA-S")
    out_path = os.environ.get("REVIEWS_OUTPUT_PATH", os.path.join(here, "output", "google_reviews.xml"))

    print(f"Fetching reviews from Judge.me ({shop}) ...")
    reviews = fetch_all_reviews(token, shop)
    print(f"  pulled {len(reviews)} total reviews")

    product_ids = {}
    if os.environ.get("REVIEWS_ENRICH") == "1":
        print("Looking up GTINs / variant IDs in Shopify ...")
        product_ids = fetch_product_ids()
        print(f"  {len(product_ids)} active products")

    feed, kept = build_feed(reviews, store_url, publisher, product_ids)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    ET.indent(feed, space="  ")
    ET.ElementTree(feed).write(out_path, encoding="utf-8", xml_declaration=True)
    print(f"  wrote {kept} product reviews -> {out_path}")
    if product_ids:
        matched = sum(1 for r in feed.iter("review") if r.find(".//gtin") is not None)
        print(f"  {matched}/{kept} reviews carry a GTIN")
    if kept < 50:
        print(f"  WARNING: only {kept} product reviews (Google Product Ratings needs >=50)")


if __name__ == "__main__":
    main()
