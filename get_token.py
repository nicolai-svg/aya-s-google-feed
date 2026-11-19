#!/usr/bin/env python3
"""
One-time helper: mint a permanent (offline) Admin API access token for the
dedicated Shopify app, so the feed cron can authenticate.

Dev Dashboard apps are OAuth-based — there is no "copy static token" button like
the old admin-created custom apps. This script does the one-time OAuth handshake:
it opens Shopify's approval page, receives the callback on localhost, exchanges
the code for an OFFLINE (non-expiring) token, and appends it to .env.

Run it ONCE, locally, in the browser profile where you're logged into Shopify.

Prerequisites
-------------
1. The app's "Allowed redirection URL(s)" must include:  http://localhost:3456/callback
2. Set these (env or .env):
     SHOPIFY_STORE_DOMAIN   e.g. f260e4.myshopify.com
     APP_CLIENT_ID          the app's Client ID (not secret)
     APP_CLIENT_SECRET      the app's Client secret  (secret — you provide it)
     SCOPES                 optional, default: read_products

Usage
-----
    set -a && source .env && set +a
    python3 get_token.py
"""

import argparse
import http.server
import os
import secrets
import sys
import urllib.parse
import webbrowser

import requests

REDIRECT_HOST, REDIRECT_PORT = "localhost", 3456
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"


def _load_dotenv(path=".env"):
    """Load .env ourselves so there's no need to `source` it. Values may contain
    &, >, spaces or inline `#` comments that would otherwise break the shell."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            if " #" in val:                      # drop trailing inline comment
                val = val.split(" #", 1)[0]
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val and key not in os.environ:
                os.environ[key] = val


def need(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing {name} — set it in the environment or .env.")
    return val


def main():
    ap = argparse.ArgumentParser(description="Mint an offline Admin API token.")
    ap.add_argument("--env", default=".env", help="Which .env file to load/append (e.g. .env.ww).")
    args = ap.parse_args()
    _load_dotenv(args.env)
    shop = need("SHOPIFY_STORE_DOMAIN")
    client_id = need("APP_CLIENT_ID")
    client_secret = need("APP_CLIENT_SECRET")
    scopes = os.environ.get("SCOPES", "read_products")
    state = secrets.token_urlsafe(16)

    auth_url = f"https://{shop}/admin/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        # no grant_options[] -> offline (permanent) token, which is what a cron needs
    })

    with open("authorize_url.txt", "w", encoding="utf-8") as fh:
        fh.write(auth_url + "\n")

    holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            query = urllib.parse.urlparse(self.path).query
            holder.update({k: v[0] for k, v in urllib.parse.parse_qs(query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("All done — close this tab and return to the terminal.".encode())

    print("Open this URL in the browser where you're logged into Shopify, then approve:\n")
    print(auth_url + "\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print(f"Waiting for the approval redirect on {REDIRECT_URI} ...")
    httpd = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), Handler)
    httpd.handle_request()  # serve exactly one request (the OAuth callback)

    if holder.get("state") != state:
        sys.exit(f"State mismatch — aborting for safety. Got: {holder}")
    code = holder.get("code")
    if not code:
        sys.exit(f"No authorization code in callback: {holder}")

    resp = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        json={"client_id": client_id, "client_secret": client_secret, "code": code},
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        sys.exit(f"No access_token in response: {resp.text[:200]}")

    with open(args.env, "a", encoding="utf-8") as fh:
        fh.write(f"\nSHOPIFY_ADMIN_TOKEN={token}\n")

    print(f"\nOffline token minted and appended to {args.env} (ends ...{token[-4:]}).")
    print("Next: copy that value from .env into your GitHub secret SHOPIFY_ADMIN_TOKEN,")
    print("then run:  python3 feed_google.py")


if __name__ == "__main__":
    main()
