#!/usr/bin/env python3
"""
Loro Piana size availability checker (Elite Style internal tool — step 1).

Usage:
    python check_loropiana.py "https://de.loropiana.com/en/shoes/woman/white-sole/summer-charms-walk-loafer-FAE5444_MB97.html"
    python check_loropiana.py "<url>" --sizes 38,39

Requires: pip install requests
"""

import argparse
import json
import re
import sys
from urllib.parse import urlparse

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-DE,en;q=0.9",
}


def parse_product_url(url: str):
    """Extract host, locale prefix, article code and color code from a PDP URL."""
    u = urlparse(url)
    m = re.search(r"-([A-Z0-9]+)_([A-Z0-9]+)\.html$", u.path)
    if not m:
        sys.exit("Could not extract article/color code from URL. Expected ...-FAE5444_MB97.html")
    article, color = m.group(1), m.group(2)
    # locale prefix, e.g. /en or /de
    locale = u.path.strip("/").split("/")[0]
    return u.netloc, locale, article, color


def try_json(session, url, params=None):
    try:
        r = session.get(url, params=params, headers=HEADERS, timeout=20)
        print(f"  -> GET {r.url}  [{r.status_code}]")
        if r.status_code != 200:
            return None
        ctype = r.headers.get("content-type", "")
        if "json" not in ctype and not r.text.lstrip().startswith(("{", "[")):
            return None
        return r.json()
    except Exception as e:
        print(f"  -> failed: {e}")
        return None


def extract_sizes_generic(data):
    """
    Walk any JSON structure and collect objects that look like size variants:
    something with a size-ish label and an availability-ish flag.
    Returns list of (label, available, raw).
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            keys = {k.lower() for k in node.keys()}
            label = None
            for k in ("size", "sizecode", "displayvalue", "value", "label", "name"):
                for orig in node:
                    if orig.lower() == k and isinstance(node[orig], (str, int, float)):
                        label = str(node[orig])
                        break
                if label:
                    break
            avail = None
            for orig in node:
                lk = orig.lower()
                if lk in ("available", "instock", "in_stock", "purchasable", "orderable", "isavailable"):
                    avail = bool(node[orig])
                elif lk in ("stock", "quantity", "atp", "availablequantity") and isinstance(node[orig], (int, float)):
                    avail = node[orig] > 0
                elif lk in ("availability", "stockstatus", "status") and isinstance(node[orig], str):
                    avail = node[orig].upper() not in ("OUTOFSTOCK", "OUT_OF_STOCK", "NOT_AVAILABLE", "UNAVAILABLE")
            if label is not None and avail is not None and re.fullmatch(r"\d{2}([.,]5)?|[XSML/]+", label):
                found.append((label, avail, node))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    # dedupe by label, prefer entries that say available
    dedup = {}
    for label, avail, raw in found:
        if label not in dedup or avail:
            dedup[label] = (avail, raw)
    return [(l, a) for l, (a, _) in sorted(dedup.items(), key=lambda x: float(x[0].replace(",", ".")) if x[0][0].isdigit() else 0)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--sizes", help="comma-separated sizes to watch, e.g. 38,39.5", default=None)
    ap.add_argument("--dump", action="store_true", help="print raw JSON of first successful endpoint")
    args = ap.parse_args()

    host, locale, article, color = parse_product_url(args.url)
    base = f"https://{host}"
    print(f"Host: {host} | locale: {locale} | article: {article} | color: {color}\n")

    session = requests.Session()
    # Warm up: get the PDP first so we receive session cookies (dwsid etc.)
    print("[0] Warming up session on product page...")
    try:
        r0 = session.get(args.url, headers=HEADERS, timeout=25)
        print(f"  -> [{r0.status_code}], cookies: {list(session.cookies.keys())}")
        html = r0.text if r0.status_code == 200 else ""
    except Exception as e:
        print(f"  -> failed: {e}")
        html = ""

    candidates = [
        ("LP api get-sizes", f"{base}/api/pdp/get-sizes", {"articleCode": article, "colorCode": color}),
        ("LP api get-variants", f"{base}/api/pdp/get-variants", {"articleCode": article, "colorCode": color}),
        ("LP api availability", f"{base}/api/pdp/availability", {"articleCode": article, "colorCode": color}),
        ("SFCC Product-Variation",
         f"{base}/on/demandware.store/Sites-loropiana-b2c-de-Site/{locale}/Product-Variation",
         {"pid": f"{article}_{color}", "quantity": "1"}),
        ("SFCC Product-ShowQuickView",
         f"{base}/on/demandware.store/Sites-loropiana-b2c-de-Site/{locale}/Product-ShowQuickView",
         {"pid": f"{article}_{color}"}),
    ]

    result_sizes = None
    for i, (name, url, params) in enumerate(candidates, 1):
        print(f"\n[{i}] Trying: {name}")
        data = try_json(session, url, params)
        if data is None:
            continue
        sizes = extract_sizes_generic(data)
        if sizes:
            result_sizes = sizes
            print(f"  ✓ parsed {len(sizes)} sizes from this endpoint")
            if args.dump:
                print(json.dumps(data, indent=2, ensure_ascii=False)[:5000])
            break
        else:
            print("  ~ got JSON but couldn't identify size/availability fields. Raw preview:")
            print(json.dumps(data, indent=2, ensure_ascii=False)[:3000])
            print("  (send this output back so the parser can be adapted)")

    # Fallback: look for embedded JSON in the HTML itself
    if result_sizes is None and html:
        print("\n[F] Falling back to embedded JSON in the HTML...")
        for pat in (r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});",
                    r"utag_data\s*=\s*(\{.*?\});",
                    r'type="application/ld\+json">(\{.*?\})</script>'):
            for m in re.finditer(pat, html, re.S):
                try:
                    data = json.loads(m.group(1))
                except Exception:
                    continue
                sizes = extract_sizes_generic(data)
                if sizes:
                    result_sizes = sizes
                    break
            if result_sizes:
                break

    print("\n" + "=" * 50)
    if not result_sizes:
        print("No size data found via any method.")
        print("Next step: open the page in Chrome DevTools → Network tab → change the size")
        print("in the dropdown, and note which XHR request fires. Send me its URL + response.")
        sys.exit(1)

    watch = None
    if args.sizes:
        watch = {s.strip().replace(".", ",") for s in args.sizes.split(",")}

    print(f"{article}_{color} — size availability:")
    for label, avail in result_sizes:
        mark = "✅ available" if avail else "❌ out of stock"
        star = "  ← watched" if watch and label.replace(".", ",") in watch else ""
        print(f"  {label:>5}  {mark}{star}")

    if watch:
        hits = [l for l, a in result_sizes if a and l.replace(".", ",") in watch]
        print("\nWatched result:", ("IN STOCK: " + ", ".join(hits)) if hits else "none of the watched sizes available")


if __name__ == "__main__":
    main()
