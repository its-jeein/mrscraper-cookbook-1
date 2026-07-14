"""
Price monitor — MrScraper cookbook recipe.

One file. Reads a list of product URLs from config.json, scrapes each one with
MrScraper, compares the result against the last run, and prints a short summary
of what changed: price drops, price increases, and stock transitions.

State lives in one JSON file (snapshot.json), keyed by product URL so a product
is tracked even if its title drifts between runs. No database, no SQL.

Run:
    pip install mrscraper-sdk python-dotenv
    cp .env.example .env          # paste your token
    python monitor.py             # first run = baseline, second run = first diff
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # .env loading is optional; selftest.py runs with stdlib only

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.json"))
SNAPSHOT_PATH = Path(os.environ.get("SNAPSHOT_PATH", "data/snapshot.json"))

# US-formatted money only: "1299", "1299.99", "1,299", "1,299.00". Anything else
# (decimal-comma locales, malformed grouping) is rejected rather than mis-parsed.
_US_MONEY = re.compile(r"^\d{1,3}(,\d{3})*(\.\d+)?$|^\d+(\.\d+)?$")

# Hard ceiling per scrape. Pages run ~30-75s; this stops a single hung request
# from stalling the whole sequential run. Tune if your targets are slower.
REQUEST_TIMEOUT_S = 180

_TOKEN_HELP = (
    "MRSCRAPER_API_TOKEN is not set. Add it to .env (MRSCRAPER_API_TOKEN=atk_...) "
    "or as a GitHub Actions secret. Get a token at https://app.mrscraper.com"
)

# Plain-English extraction prompt. Edit the field descriptions to tune what
# MrScraper pulls back. Keys must stay as-is; the diff logic reads them by name.
PROMPT = (
    "Extract this product. Return ONLY a JSON object with exactly these keys, "
    "and use null for anything not shown on the page. Do not guess.\n"
    '{ "name": "the product title", '
    '"price": "current selling price after any discount, as a decimal number in dollars with cents kept, '
    'for example 295.00 or 1299.99, no currency symbol; null if not shown", '
    '"currency": "currency code shown, e.g. USD; null if not shown", '
    '"in_stock": "true if it can be bought now, false if sold out or unavailable" }\n'
    "Return JSON only. No markdown, no commentary."
)


# ---------------------------------------------------------------------------
# Scrape: one product per URL via MrScraper create-and-run (General Agent).
# ---------------------------------------------------------------------------
async def _fetch_one(client, url: str, proxy_country: str) -> dict | None:
    """Scrape a single product page. Returns a normalized dict or None on failure."""
    from mrscraper.exceptions import APIError, AuthenticationError, NetworkError

    try:
        result = await asyncio.wait_for(
            client.create_scraper(
                url=url, message=PROMPT, agent="general", proxy_country=proxy_country
            ),
            timeout=REQUEST_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        print(f"  ! scrape timed out after {REQUEST_TIMEOUT_S}s")
        return None
    except (AuthenticationError, APIError, NetworkError) as e:
        print(f"  ! scrape failed: {e}")
        return None

    fields = _extract_fields(result)
    if fields is None:
        return None
    return _normalize(fields)


def _extract_fields(result) -> dict | None:
    """Pull the extracted-product dict out of the create-and-run envelope.

    Primary (verified live): result["data"]["data"]["data"]. We also accept the
    leaf as a JSON string, and fall back to shallower levels if a future SDK
    build flattens the shape. If nothing usable is found we print the envelope's
    structure so a live run shows exactly where the data sits, instead of failing
    silently. That breadcrumb turns an unexpected shape into a one-line fix.
    """
    if not isinstance(result, dict):
        return None

    # Candidate locations, most-specific first.
    d1 = result.get("data")
    d2 = d1.get("data") if isinstance(d1, dict) else None
    d3 = d2.get("data") if isinstance(d2, dict) else None

    for candidate in (d3, d2, d1):
        leaf = candidate
        if isinstance(leaf, str):
            try:
                leaf = json.loads(leaf)
            except json.JSONDecodeError:
                continue
        if isinstance(leaf, dict) and _looks_like_product(leaf):
            return leaf

    print(f"  ! unexpected response shape; keys seen: {_shape(result)}")
    return None


def _looks_like_product(d: dict) -> bool:
    """True if the dict carries any field our prompt asks for (or a close alias),
    including the nested marketplace shape's top-level keys."""
    keys = {k.lower() for k in d.keys()}
    return bool(keys & {
        "name", "price", "in_stock", "currency", "title", "product_name",
        "product_info", "stock_status", "current_price",
    })


def _shape(obj, depth: int = 0) -> str:
    """Compact description of a nested dict's keys, for debugging a live miss."""
    if isinstance(obj, dict):
        if depth >= 3:
            return "{...}"
        return "{" + ", ".join(f"{k}: {_shape(v, depth + 1)}" for k, v in obj.items()) + "}"
    if isinstance(obj, list):
        return f"[{len(obj)} items]"
    return type(obj).__name__


async def _scrape_all(targets: list[dict], proxy_country: str) -> dict[str, dict]:
    """Scrape every target one at a time. Returns {url: normalized_product}.

    Sequential on purpose: it stays within free-tier limits, fails one product
    at a time instead of a whole batch, and is easy to follow in the logs. With
    5-10 products at 30-75s each, a run is a few minutes, well under the CI cap.
    """
    from mrscraper import MrScraper

    token = os.environ.get("MRSCRAPER_API_TOKEN", "")
    if not token:
        sys.exit(_TOKEN_HELP)

    client = MrScraper(token=token)
    out: dict[str, dict] = {}
    for i, t in enumerate(targets, 1):
        retailer = t.get("retailer", "?")
        print(f"  [{i}/{len(targets)}] {retailer}: {t['url'][:60]}")
        try:
            product = await _fetch_one(client, t["url"], proxy_country)
        except Exception as e:  # noqa: BLE001 - one bad page must not abort the batch
            print(f"      unexpected error, skipped: {e}")
            continue
        if product is None:
            print("      no usable data this run (skipped, keeps last value)")
            continue
        out[t["url"]] = product
    return out


def _flatten_marketplace(raw: dict) -> dict:
    """Some MrScraper paths (e.g. the Best Buy marketplace extractor) return a
    richer nested shape: {product_info:{name}, price:{current_price, currency},
    stock_status:{status}}. Map it onto our flat name/price/currency/in_stock so
    the same diff logic works no matter which path produced the data. A flat
    record passes through unchanged.
    """
    if not isinstance(raw, dict):
        return raw
    price = raw.get("price")
    is_nested = isinstance(price, dict) or "product_info" in raw or "stock_status" in raw
    if not is_nested:
        return raw
    info = raw.get("product_info") if isinstance(raw.get("product_info"), dict) else {}
    stock = raw.get("stock_status") if isinstance(raw.get("stock_status"), dict) else {}
    return {
        "name": info.get("name") or raw.get("name"),
        "price": price.get("current_price") if isinstance(price, dict) else price,
        "currency": price.get("currency") if isinstance(price, dict) else raw.get("currency"),
        "in_stock": stock.get("status") if stock else raw.get("in_stock"),
    }


def _normalize(raw: dict) -> dict | None:
    """Coerce loose field types into a stable shape, or return None for a
    blocked/empty render so it isn't stored as a real product.

    A legitimate product page returns a title even when sold out. So we treat a
    record with no real name AND no price AND no explicit stock signal as a
    failed scrape, not a product. That stops a blank/blocked page from
    overwriting good snapshot data or firing a phantom alert next run.
    """
    raw = _flatten_marketplace(raw)
    name = str(raw.get("name") or "").strip()
    price = _to_price(raw.get("price"))
    in_stock, explicit_stock = _to_bool(raw.get("in_stock"))

    name_is_real = bool(name) and name.lower() not in (
        "unknown", "null", "none", "the product title",
    )
    if not name_is_real and price is None and not explicit_stock:
        return None

    return {
        "name": name if name_is_real else "Unknown",
        "price": price,
        "currency": (raw.get("currency") or "USD"),
        "in_stock": in_stock,
    }


def _to_price(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        cleaned = value
        for ch in ("$", "€", "£", "¥", "USD", "US", "\u00a0"):
            cleaned = cleaned.replace(ch, "")
        cleaned = cleaned.strip()
        # We pin proxy_country="US", so prices come back US-formatted (comma =
        # thousands, dot = decimal). Only trust that exact shape. Anything else,
        # like the decimal-comma "1.299,00", is rejected rather than silently
        # mangled into a wrong-by-1000x number. A rejected value reads as "no
        # data" and is reported, which is far safer than a fabricated price.
        if not _US_MONEY.match(cleaned):
            return None
        n = float(cleaned.replace(",", ""))
        return n if n > 0 else None
    return None


def _to_bool(value) -> tuple[bool, bool]:
    """Returns (in_stock, was_explicit). was_explicit is False when the page
    gave us nothing to go on, which the caller uses to detect blank renders."""
    if isinstance(value, bool):
        return value, True
    if isinstance(value, str):
        v = value.lower().strip().replace("_", " ")  # so "out_of_stock" reads as "out of stock"
        if any(k in v for k in ("out of stock", "sold out", "unavailable", "false", "no")):
            return False, True
        if any(k in v for k in ("in stock", "available", "true", "add to cart", "buy")):
            return True, True
        return True, False  # unrecognized text: default in-stock, but not a real signal
    return True, False


# ---------------------------------------------------------------------------
# Diff: compare this run against the last snapshot, by URL.
# ---------------------------------------------------------------------------
# An order-of-magnitude price move in either direction is almost always a data
# error (a dropped decimal read 295.00 -> 29500, a mis-scraped figure), not a
# real sale. We quarantine those instead of alerting. Real retail discounts top
# out well under 10x, so this won't suppress a genuine clearance.
SANITY_RATIO = 10.0


def diff(previous: dict, current: dict, targets: list[dict], threshold_pct: float) -> dict:
    """Compare two {url: product} maps. Returns events + an unchanged count.

    A product is only compared when it appears in both runs. Stock transitions
    are reported even when price is missing (out-of-stock pages often hide price).
    Price moves are only reported when both prices are present and positive.
    """
    label = {t["url"]: t.get("retailer", t["url"]) for t in targets}
    events: list[dict] = []
    compared = 0

    for url, curr in current.items():
        prev = previous.get(url)
        if prev is None:
            continue  # first time we've seen this product; nothing to compare yet
        compared += 1

        # Stock transition takes priority — it's the clearest signal.
        if prev["in_stock"] and not curr["in_stock"]:
            events.append(_event("out_of_stock", url, label, curr, prev))
            continue
        if not prev["in_stock"] and curr["in_stock"]:
            events.append(_event("back_in_stock", url, label, curr, prev))
            continue

        # Price move — only when we have real numbers on both sides AND the
        # currency is the same. A currency flip (locale drift) would otherwise
        # compare unlike units and report a garbage percentage.
        old_p, new_p = prev.get("price"), curr.get("price")
        same_currency = prev.get("currency") == curr.get("currency")
        if old_p and new_p and same_currency and new_p != old_p:
            # Sanity guard: an order-of-magnitude jump in either direction is a
            # data error, not a real price move. Quarantine it as suspect rather
            # than fire a confident fake alert. The prompt makes this rarer; this
            # bounds the damage when the LLM inevitably drifts again.
            if max(new_p / old_p, old_p / new_p) >= SANITY_RATIO:
                events.append(_event("suspect_data", url, label, curr, prev,
                                     (new_p - old_p) / old_p * 100))
                continue
            pct = (new_p - old_p) / old_p * 100
            if abs(pct) >= threshold_pct:
                kind = "price_drop" if pct < 0 else "price_increase"
                events.append(_event(kind, url, label, curr, prev, pct))

    unchanged = compared - len(events)
    return {"events": events, "unchanged": max(unchanged, 0)}


def _event(kind, url, label, curr, prev, pct=None) -> dict:
    return {
        "type": kind,
        "url": url,
        "retailer": label.get(url, ""),
        "name": curr["name"],
        "old_price": prev.get("price"),
        "new_price": curr.get("price"),
        "currency": curr.get("currency", "USD"),
        "pct": pct,
    }


# ---------------------------------------------------------------------------
# Output: a short, screenshot-friendly summary.
# ---------------------------------------------------------------------------
HEADERS = {
    "price_drop": "PRICE DROP",
    "price_increase": "PRICE INCREASE",
    "out_of_stock": "OUT OF STOCK",
    "back_in_stock": "BACK IN STOCK",
    "suspect_data": "SUSPECT DATA — not alerted",
}
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}


def _money(currency: str, amount: float) -> str:
    symbol = SYMBOLS.get((currency or "USD").upper())
    return f"{symbol}{amount:,.2f}" if symbol else f"{currency} {amount:,.2f}"


def format_summary(result: dict, missing: int = 0) -> str:
    events, unchanged = result["events"], result["unchanged"]
    lines = ["Run Summary", ""]

    for e in events:
        bell = "⚠️" if e["type"] == "suspect_data" else "🔔"
        lines.append(f"{bell} {HEADERS[e['type']]}")
        lines.append(f"{e['name']} ({e['retailer']})")
        if e["type"] in ("price_drop", "price_increase"):
            old_s = _money(e["currency"], e["old_price"])
            new_s = _money(e["currency"], e["new_price"])
            lines.append(f"{old_s} → {new_s}  ({e['pct']:+.1f}%)")
        elif e["type"] == "suspect_data":
            old_s = _money(e["currency"], e["old_price"])
            new_s = _money(e["currency"], e["new_price"])
            lines.append(f"{old_s} → {new_s}  ({e['pct']:+.1f}%) — implausible jump, skipped. Check the scrape, not the price.")
        lines.append("")

    word = "other " if events else ""
    lines.append(f"No changes detected on {unchanged} {word}product(s).")
    if missing:
        lines.append(f"Could not read {missing} product(s) this run.")
    return "\n".join(lines).rstrip()


def write_github_summary(text: str) -> None:
    """Render the summary in the GitHub Actions run UI, if we're in CI."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        Path(path).open("a").write("```\n" + text + "\n```\n")


# ---------------------------------------------------------------------------
# State + entry point.
# ---------------------------------------------------------------------------
def load_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        return {}
    try:
        data = json.loads(SNAPSHOT_PATH.read_text())
        products = data.get("products", {})
        return products if isinstance(products, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        # A truncated cache write (e.g. CI killed mid-save) must not break the run.
        # Treat it as no prior state: this run becomes a fresh baseline.
        print(f"  ! could not read previous snapshot ({e}); starting a new baseline.")
        return {}


def save_snapshot(products: dict) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "products": products},
        indent=2,
    )
    # Write to a temp file in the same directory, then atomically replace. A
    # crash mid-write leaves the previous snapshot intact instead of a
    # truncated, unreadable file.
    tmp = SNAPSHOT_PATH.with_suffix(SNAPSHOT_PATH.suffix + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, SNAPSHOT_PATH)


# ---------------------------------------------------------------------------
# Demo mode: `python monitor.py --demo`
# Narrates the alert flow on staged data, but runs the REAL diff() detection and
# the real summary renderer, so what's on screen is the genuine logic, not
# hardcoded prints. No token, no network, deterministic for recording. It never
# touches the live snapshot.
# ---------------------------------------------------------------------------
DEMO_THRESHOLD_PCT = 5.0  # same as the production default — the demo shows real behavior, not a lowered bar
DEMO_TARGETS = [
    {"retailer": "Newegg", "url": "demo://rtx-5070"},
    {"retailer": "Amazon", "url": "demo://990-pro-2tb"},
]
DEMO_PREVIOUS = {
    "demo://rtx-5070":    {"name": "ASUS PRIME GeForce RTX 5070 12GB", "price": 649.99, "currency": "USD", "in_stock": True},
    "demo://990-pro-2tb": {"name": "Samsung 990 PRO SSD 2TB NVMe",     "price": 389.99, "currency": "USD", "in_stock": True},
}
DEMO_CURRENT = {
    "demo://rtx-5070":    {"name": "ASUS PRIME GeForce RTX 5070 12GB", "price": 549.99, "currency": "USD", "in_stock": True},   # -15.4%: the hero drop
    "demo://990-pro-2tb": {"name": "Samsung 990 PRO SSD 2TB NVMe",     "price": 389.99, "currency": "USD", "in_stock": True},   # unchanged: exercises the "N others" line
}


def demo(pause: float = 0.5) -> None:
    print("DEMO MODE — staged prices, real detection logic\n")
    for t in DEMO_TARGETS:
        prev, curr = DEMO_PREVIOUS[t["url"]], DEMO_CURRENT[t["url"]]
        print(f"🔍 Checking {curr['name']} ({t['retailer']})...")
        time.sleep(pause)
        was = _money(prev["currency"], prev["price"])
        now = _money(curr["currency"], curr["price"])
        print(f"   was {was}  →  now {now}")
        time.sleep(pause)

    result = diff(DEMO_PREVIOUS, DEMO_CURRENT, DEMO_TARGETS, DEMO_THRESHOLD_PCT)
    print()
    if result["events"]:
        print("🚨 Change detected\n")
        time.sleep(pause)
        print(format_summary(result))
    else:
        print("✅ No change above threshold.")


def main() -> None:
    if not CONFIG_PATH.exists():
        sys.exit(f"Config not found at {CONFIG_PATH}. Copy config.json and add product URLs.")
    try:
        config = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"config.json is not valid JSON: {e}")

    targets = config.get("retailers")
    if not isinstance(targets, list) or not targets:
        sys.exit("config.json needs a non-empty 'retailers' list. See the README for the shape.")
    for i, t in enumerate(targets, 1):
        if not isinstance(t, dict) or not isinstance(t.get("url"), str) or not t["url"].strip():
            sys.exit(f"config.json: retailer #{i} is missing a valid 'url'. Each entry needs a product URL.")
    proxy_country = config.get("proxy_country", "US")
    try:
        threshold = float(config.get("threshold_pct", 5.0))
    except (TypeError, ValueError):
        sys.exit("config.json: 'threshold_pct' must be a number, e.g. 5.0 for 5%.")
    if threshold < 0:
        sys.exit("config.json: 'threshold_pct' must be zero or positive.")

    if not os.environ.get("MRSCRAPER_API_TOKEN"):
        sys.exit(_TOKEN_HELP)

    print(f"Scraping {len(targets)} product(s)...")
    current = asyncio.run(_scrape_all(targets, proxy_country))
    missing = len(targets) - len(current)
    print(f"\nGot clean data for {len(current)}/{len(targets)} product(s).\n")

    previous = load_snapshot()
    result = diff(previous, current, targets, threshold)

    summary = format_summary(result, missing=missing)
    print(summary)
    write_github_summary(summary)

    # Text one SMS per genuine price change. Opt-in: a no-op without Twilio
    # credentials, so offline runs and the schedule keep working unchanged.
    from notify import send_price_alerts

    send_price_alerts(result["events"])

    # Surface quarantined values as a CI warning so a run with suspect data can't
    # slip by unnoticed in a sea of green. We don't fail the run on it — the rest
    # of the products are still valid — but the annotation makes it impossible to
    # miss, and keeps a fabricated number off the recorded demo.
    suspects = [e for e in result["events"] if e["type"] == "suspect_data"]
    if suspects and os.environ.get("GITHUB_ACTIONS"):
        for e in suspects:
            print(f"::warning title=Suspect price data::{e['name']} ({e['retailer']}) "
                  f"moved {e['pct']:+.0f}% — quarantined, not alerted. Re-check the scrape.")

    if not current:
        # Every scrape failed. Don't overwrite good state, and fail loudly so a
        # scheduled run can't go green while reading nothing real.
        sys.exit(
            f"All {len(targets)} scrape(s) failed this run — no usable data. "
            "Check the token, network, or whether the targets are blocking."
        )

    # Carry forward last-known state for any product that didn't return this run,
    # so one transient empty render doesn't look like a change next time.
    merged = {**previous, **current}
    save_snapshot(merged)


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        main()