#!/usr/bin/env python3
"""
Google Shopping feed generator for AYA-S (DataFeedWatch replacement — Phase 1).

Pulls products from the Shopify Admin GraphQL API and writes a Google Merchant
Center product feed (RSS 2.0 / g: namespace) to an XML file. A scheduled fetch
in Google Merchant Center then pulls that file's hosted URL.

Config comes from environment variables (see .env.example). No secrets live in
this file. Only dependency is `requests` (and only for the live fetch — the
offline --self-test needs nothing but the Python stdlib).

Usage:
    python3 feed_google.py                 # live: fetch Shopify -> write feed
    python3 feed_google.py --self-test     # offline: build feed from a sample product
    python3 feed_google.py --out feed.xml  # override output path
"""

import argparse
import html
import os
import sys
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

GOOGLE_NS = "http://base.google.com/ns/1.0"
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2025-10")
PAGE_SIZE = int(os.environ.get("FEED_PAGE_SIZE", "20"))

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_dotenv(path=".env"):
    """Load .env ourselves so there's no need to `source` it (values may contain
    &, >, spaces or inline `#` comments that would break the shell)."""
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


def cfg(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"ERROR: required environment variable {name} is not set.")
    return val


def load_category_map(path):
    """CSV: shopify_product_type,google_product_category  -> dict keyed lowercase."""
    mapping = {}
    if not path or not os.path.exists(path):
        return mapping
    import csv
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("shopify_product_type") or "").strip().lower()
            cat = (row.get("google_product_category") or "").strip()
            if key and cat:
                mapping[key] = cat
    return mapping


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def strip_html(raw, limit=4900):
    if not raw:
        return ""
    s = _Stripper()
    s.feed(raw)
    text = html.unescape(" ".join("".join(s.parts).split()))
    return text[:limit]


def numeric_id(gid):
    """gid://shopify/ProductVariant/44123456 -> 44123456"""
    return gid.rsplit("/", 1)[-1] if gid else ""


def looks_like_gtin(code):
    return bool(code) and code.isdigit() and len(code) in (8, 12, 13, 14)


# ---------------------------------------------------------------------------
# Shopify fetch (live)
# ---------------------------------------------------------------------------

PRODUCTS_QUERY = """
query Products($cursor: String, $n: Int!) {
  products(first: $n, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        handle
        descriptionHtml
        vendor
        productType
        tags
        onlineStoreUrl
        featuredImage { url }
        images(first: 10) { edges { node { url } } }
        variants(first: 100) {
          edges {
            node {
              id
              title
              sku
              barcode
              price
              compareAtPrice
              availableForSale
              image { url }
            }
          }
        }
      }
    }
  }
}
"""


def fetch_all_products():
    import requests  # imported here so --self-test has zero dependencies

    domain = cfg("SHOPIFY_STORE_DOMAIN", required=True)   # e.g. your-store.myshopify.com
    token = cfg("SHOPIFY_ADMIN_TOKEN", required=True)
    url = f"https://{domain}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    cursor = None
    while True:
        payload = {"query": PRODUCTS_QUERY, "variables": {"cursor": cursor, "n": PAGE_SIZE}}
        for attempt in range(6):
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                # Cost/throttle errors -> back off and retry; otherwise fail loudly.
                msg = str(data["errors"])
                if "THROTTLED" in msg.upper():
                    time.sleep(2 * (attempt + 1))
                    continue
                sys.exit(f"Shopify GraphQL error: {msg}")
            break
        else:
            sys.exit("Shopify GraphQL kept throttling — aborting.")

        block = data["data"]["products"]
        for edge in block["edges"]:
            yield edge["node"]
        if block["pageInfo"]["hasNextPage"]:
            cursor = block["pageInfo"]["endCursor"]
        else:
            break


# ---------------------------------------------------------------------------
# Transform: Shopify product node -> list of Google feed item dicts
# ---------------------------------------------------------------------------

def product_to_items(node, settings):
    store_url = settings["store_url"].rstrip("/")
    currency = settings["currency"]
    brand_default = settings["brand_default"]
    cat_map = settings["category_map"]
    default_cat = settings["default_category"]

    handle = node.get("handle")
    featured = (node.get("featuredImage") or {}).get("url")
    images = [e["node"]["url"] for e in (node.get("images") or {}).get("edges", [])]
    vendor = node.get("vendor") or brand_default
    ptype = node.get("productType") or ""
    gcat = cat_map.get(ptype.strip().lower(), default_cat)
    description = strip_html(node.get("descriptionHtml"))

    items = []
    for ve in (node.get("variants") or {}).get("edges", []):
        v = ve["node"]
        price = v.get("price")
        if not price or float(price) <= 0:
            continue  # skip unpriced variants

        vid = numeric_id(v["id"])
        image = (v.get("image") or {}).get("url") or featured
        if not handle or not image:
            continue  # Google requires a link + image

        compare = v.get("compareAtPrice")
        on_sale = compare and float(compare) > float(price)

        barcode = v.get("barcode") or ""
        sku = v.get("sku") or ""

        item = {
            "id": vid,
            "item_group_id": numeric_id(node["id"]),
            "title": _compose_title(node.get("title", ""), v.get("title")),
            "description": description,
            "link": f"{store_url}/products/{handle}?variant={vid}",
            "image_link": image,
            "additional_image_link": [u for u in images if u != image][:10],
            "availability": "in_stock" if v.get("availableForSale") else "out_of_stock",
            "price": f"{float(compare if on_sale else price):.2f} {currency}",
            "sale_price": f"{float(price):.2f} {currency}" if on_sale else None,
            "brand": vendor,
            "condition": "new",
            "google_product_category": gcat,
            "product_type": ptype or None,
            "gtin": barcode if looks_like_gtin(barcode) else None,
            "mpn": sku or None,
        }
        if not item["gtin"] and not item["mpn"]:
            item["identifier_exists"] = "no"
        items.append(item)
    return items


def _compose_title(product_title, variant_title):
    if variant_title and variant_title.lower() != "default title":
        return f"{product_title} – {variant_title}"
    return product_title


# ---------------------------------------------------------------------------
# Build the RSS 2.0 XML
# ---------------------------------------------------------------------------

G_ATTRS = {  # attribute -> True if it belongs in the g: namespace
    "id": True, "title": False, "description": False, "link": False,
    "image_link": True, "additional_image_link": True, "availability": True,
    "price": True, "sale_price": True, "brand": True, "condition": True,
    "google_product_category": True, "product_type": True, "gtin": True,
    "mpn": True, "item_group_id": True, "identifier_exists": True,
}


def build_feed(items, title="AYA-S Google Feed", link="https://aya-s.com"):
    ET.register_namespace("g", GOOGLE_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link
    ET.SubElement(channel, "description").text = "AYA-S product feed for Google Shopping"

    for it in items:
        node = ET.SubElement(channel, "item")
        for key, in_g in G_ATTRS.items():
            val = it.get(key)
            if val in (None, "", []):
                continue
            tag = f"{{{GOOGLE_NS}}}{key}" if in_g else key
            if isinstance(val, list):
                for v in val:
                    ET.SubElement(node, tag).text = v
            else:
                ET.SubElement(node, tag).text = str(val)
    return ET.ElementTree(rss)


def write_feed(tree, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Sample product for --self-test (mirrors the GraphQL node shape)
# ---------------------------------------------------------------------------

SAMPLE = {
    "id": "gid://shopify/Product/7890",
    "title": "Wool Trousers",
    "handle": "wool-trousers",
    "descriptionHtml": "<p>Tailored <b>wool</b> trousers.</p>",
    "vendor": "AYA-S",
    "productType": "Trousers",
    "tags": ["AW26"],
    "onlineStoreUrl": "https://aya-s.com/products/wool-trousers",
    "featuredImage": {"url": "https://cdn.shopify.com/img/main.jpg"},
    "images": {"edges": [
        {"node": {"url": "https://cdn.shopify.com/img/main.jpg"}},
        {"node": {"url": "https://cdn.shopify.com/img/alt.jpg"}},
    ]},
    "variants": {"edges": [
        {"node": {"id": "gid://shopify/ProductVariant/44123456", "title": "Black / M",
                  "sku": "WT-BLK-M", "barcode": "5712345678901", "price": "799.00",
                  "compareAtPrice": "999.00", "availableForSale": True,
                  "image": {"url": "https://cdn.shopify.com/img/black.jpg"}}},
        {"node": {"id": "gid://shopify/ProductVariant/44123457", "title": "Black / L",
                  "sku": "WT-BLK-L", "barcode": "", "price": "799.00",
                  "compareAtPrice": None, "availableForSale": False,
                  "image": None}},
    ]},
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate the Google Shopping feed.")
    ap.add_argument("--self-test", action="store_true",
                    help="Build the feed from a built-in sample product (no network).")
    ap.add_argument("--out", default=None, help="Output path (else OUTPUT_PATH env or default).")
    ap.add_argument("--env", default=".env", help="Which .env file to load (e.g. .env.ww).")
    args = ap.parse_args()

    _load_dotenv(args.env)
    out = args.out or os.environ.get("OUTPUT_PATH", "output/google_feed.xml")
    settings = {
        "store_url": cfg("STORE_URL", "https://aya-s.com"),
        "currency": cfg("FEED_CURRENCY", "DKK"),
        "brand_default": cfg("BRAND_DEFAULT", "AYA-S"),
        "default_category": cfg("DEFAULT_GOOGLE_CATEGORY",
                                "Apparel & Accessories > Clothing"),
        "category_map": load_category_map(cfg("CATEGORY_MAP_PATH", "category_map.csv")),
    }

    sources = [SAMPLE] if args.self_test else fetch_all_products()

    items, products = [], 0
    for node in sources:
        products += 1
        items.extend(product_to_items(node, settings))

    tree = build_feed(items, link=settings["store_url"])
    write_feed(tree, out)
    print(f"Wrote {len(items)} items from {products} products -> {out}")

    if args.self_test:
        with open(out, encoding="utf-8") as fh:
            print("\n--- feed preview ---")
            print(fh.read())


if __name__ == "__main__":
    main()
