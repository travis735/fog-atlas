#!/usr/bin/env python3
"""Ping IndexNow (Bing / Copilot / DuckDuckGo / Seznam / Naver ecosystem)
with the URLs the daily bake just redeployed.

Every baked page genuinely changes each day (forecast answer + lastmod), so
the full sitemap is a legitimate change-list. IndexNow accepts up to 10,000
URLs per POST; we are ~6,800. The key is not a secret — the protocol requires
it to be publicly served at the key location; possessing it only lets someone
ask engines to recrawl fogatlas.org URLs.

Runs from the repo root after `vite build` (reads the sitemap out of dist/).
Failure is non-fatal upstream — crawlers still find changes via sitemap lastmod.
"""
import json
import re
import sys
import urllib.request

KEY = "a94ea7fb1a87935bb7c2ec7dc6976f62"
HOST = "fogatlas.org"
SITEMAP = "app/dist/sitemap.xml"
ENDPOINT = "https://api.indexnow.org/indexnow"

urls = re.findall(r"<loc>(https://fogatlas\.org[^<]*)</loc>", open(SITEMAP).read())
if not urls:
    sys.exit(f"no URLs parsed from {SITEMAP} — refusing to ping")
if len(urls) > 10000:
    print(f"warning: {len(urls)} URLs exceeds the 10k/POST cap — truncating")
    urls = urls[:10000]

payload = {
    "host": HOST,
    "key": KEY,
    "keyLocation": f"https://{HOST}/{KEY}.txt",
    "urlList": urls,
}
req = urllib.request.Request(
    ENDPOINT,
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json; charset=utf-8"},
)
with urllib.request.urlopen(req, timeout=60) as resp:
    print(f"IndexNow: HTTP {resp.status} for {len(urls)} URLs")
