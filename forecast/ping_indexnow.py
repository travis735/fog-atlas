#!/usr/bin/env python3
"""Ping IndexNow (Bing / Copilot / DuckDuckGo / Seznam / Naver ecosystem)
with the URLs whose content changed in today's bake.

The sitemap index's children carry an honest lastmod (today only for pages
whose answer text changed: a forecast feed, a fresh TAF or a fresh
observation). The change-list is every URL with lastmod == today in the
PRIORITY child only (public cohort + their cities + demand cities + hubs,
~540 URLs): Bing reads the whole sitemap index daily and prioritises by
lastmod on its own, and submitting all ~6,400 daily-changed URLs in one
scheduled POST is exactly what Bing Webmaster Tools flagged as IndexNow
"batch mode" (2026-09-26), which slows indexing instead of speeding it. A
legacy flat sitemap submits everything. IndexNow accepts up to 10,000 URLs
per POST.

The key is not a secret — the protocol requires it to be publicly served at
the key location; possessing it only lets someone ask engines to recrawl
fogatlas.org URLs.

Runs from the repo root after `vite build` (reads the sitemaps out of dist/).
Exit status is the real outcome: the workflow step marks it with
continue-on-error and fails the run at the end, after the KV upload, so a
bad ping is visible without ever losing an issuance.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

KEY = "a94ea7fb1a87935bb7c2ec7dc6976f62"
HOST = "fogatlas.org"
DIST = "app/dist"
ENDPOINT = "https://api.indexnow.org/indexnow"


def changed_urls() -> list[str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    idx = open(f"{DIST}/sitemap.xml").read()
    children = re.findall(r"<loc>https://fogatlas\.org/(sitemap-[a-z]+\.xml)</loc>", idx)
    if not children:
        return re.findall(r"<loc>(https://fogatlas\.org[^<]*)</loc>", idx)
    # priority child only (see module docstring); every child if the index
    # has no priority child, which the report line makes visible by count
    if "sitemap-priority.xml" in children:
        children = ["sitemap-priority.xml"]
    urls = []
    for fn in children:
        for u, lm in re.findall(r"<url><loc>([^<]+)</loc><lastmod>([^<]+)</lastmod>", open(f"{DIST}/{fn}").read()):
            if lm == today:
                urls.append(u)
    return urls


def post(urls: list[str]) -> int:
    payload = {"host": HOST, "key": KEY, "keyLocation": f"https://{HOST}/{KEY}.txt", "urlList": urls}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status


def report(line: str) -> None:
    print(line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"- {line}\n")


def main() -> int:
    urls = changed_urls()
    if not urls:
        report("IndexNow: no URLs changed today — nothing submitted")
        return 0
    status = None
    for i in range(0, len(urls), 10000):
        chunk = urls[i:i + 10000]
        for attempt in range(2):
            try:
                status = post(chunk)
                break
            except urllib.error.HTTPError as e:
                status = e.code
                if e.code == 429 and attempt == 0:
                    time.sleep(60)
                    continue
                report(f"IndexNow: HTTP {e.code} for {len(chunk)} URLs — {e.read()[:200]!r}")
                return 1
            except urllib.error.URLError as e:
                report(f"IndexNow: network error — {e}")
                return 1
    report(f"IndexNow: HTTP {status} for {len(urls)} URLs")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"status={status}\nurls={len(urls)}\n")
    return 0 if status in (200, 202) else 1


if __name__ == "__main__":
    sys.exit(main())
