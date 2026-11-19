# AYA-S Google Shopping Feed (DataFeedWatch replacement — Phase 1)

In-house generator for the Google Merchant Center product feed. A GitHub Actions
cron runs `feed_google.py`, which pulls products from Shopify's Admin GraphQL API
and writes `output/google_feed.xml`. The file is published to a stable URL that
Google Merchant Center fetches on a schedule. Same pattern as **returns-sync**.

Scope today: **Google only, main store (AYA-S), single currency.** Meta, multi-country,
and extra channels are later phases (see `../DataFeedWatch_Replacement_Scope.md`).

## Files
- `feed_google.py` — the generator (stdlib only; `requests` for the live fetch).
- `category_map.csv` — Shopify product type → Google product category (edit as the catalog grows).
- `.github/workflows/google-feed.yml` — scheduled build + publish.
- `.env.example` — config template (never commit real secrets).

## 1. One-time Shopify setup
1. Shopify admin → **Settings → Apps and sales channels → Develop apps → Create an app**.
2. Configure **Admin API scopes**: `read_products` (add `read_inventory` only if you later
   want exact stock counts). Save → **Install app**.
3. Copy the **Admin API access token** (`shpat_...`). Treat it like a password.
4. Find the store's **`.myshopify.com` domain** (Settings → Domains) — this is
   `SHOPIFY_STORE_DOMAIN`, *not* the admin handle `f260e4`.

## 2. Run locally
```bash
pip install -r requirements.txt
cp .env.example .env            # fill in real values
set -a && source .env && set +a # load env
python3 feed_google.py --self-test   # offline smoke test (no network/secrets)
python3 feed_google.py               # live: writes output/google_feed.xml
```

## 3. Deploy (GitHub Actions)
Put this folder in a repo. In the repo settings add:
- **Secrets:** `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`, and (for publishing)
  `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT`, `R2_BUCKET`.
- **Variables:** `STORE_URL`, `FEED_CURRENCY`.

The workflow regenerates the feed every 6h and uploads it to your R2/S3 bucket.
(Prefer GitHub Pages instead? Uncomment Option B in the workflow.)

## 4. Point Google at it
Google Merchant Center → **Products → Feeds → Add primary feed → Scheduled fetch**,
URL = your published `google_feed.xml`. Start the fetch daily.

> Freshness note: Google's scheduled URL fetch is ~daily. For near-real-time
> price/stock, either enable Shopify structured-data "automatic item updates", or
> upgrade to the Content API push (a later phase). Daily is fine to launch.

## 5. Cut over from DataFeedWatch safely
1. Run this feed as a **separate** Merchant Center feed alongside DFW's.
2. Compare item counts + disapprovals for a few days.
3. When clean, make this the primary feed, then **uninstall DFW *and* cancel the
   DFW account** (two separate steps — uninstalling the app alone keeps billing).

## Known limitations (by design, this phase)
- Google only; single currency/market; no custom-label bidding rules yet.
- Products with >100 variants would need nested variant pagination (rare for apparel).
- Category map is manual — keep `category_map.csv` current.
