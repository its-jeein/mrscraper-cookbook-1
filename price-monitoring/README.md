# Price Monitor

Track a set of products across retailers. Each run scrapes every product, compares it to the last run, and prints a short summary of what changed: price drops, price increases, and stock going in or out. Runs on a 6-hour schedule with GitHub Actions and no server.

## What you'll build

A single script, `monitor.py`, that you point at a list of product URLs. It returns a screenshot-ready summary like this:

```
Run Summary

🔔 PRICE DROP
Sony WH-1000XM5 Wireless Noise Canceling Headphones (Walmart)
$348.00 → $278.00  (-20.1%)

🔔 OUT OF STOCK
Apple AirPods Pro 2 (Walmart)

No changes detected on 9 other product(s).
```

State lives in one JSON file. No database, no SQL, nothing to host.

## Try it in 2 minutes

See the alert format right now, no account and no token, fully offline:

```bash
python monitor.py --demo
```

That runs the real change-detection logic against staged data and prints a full summary. It costs zero tokens because nothing is scraped. When you're ready to run it for real, follow the setup below.

## Who it's for

Ecommerce operators watching competitor/own-catalog prices, founders tracking a few SKUs, and developers who want a price/stock feed for a dashboard, alert, or LLM workflow. You do not need to write application code to run it. *You do need to use the terminal, paste URLs into `config.json`, and add your API token.*

## When to use it

When you care about *change over time* on pages you don't control: a competitor drops a price, a popular item goes out of stock, a deal ends. One-off extraction is the Playground. This recipe is the scheduled version of that same move.

## What you need

1. A free MrScraper account. Sign up at https://app.mrscraper.com and copy your API token from the dashboard. The free tier includes 1000 tokens — enough to learn the recipe and watch it run (see Cost below).
2. Python 3.10+.
3. The SDK and one helper, installed from `requirements.txt`:

   ```bash
   pip install -r requirements.txt        # installs mrscraper-sdk and python-dotenv
   ```

   Without the SDK, the first line of the script (`from mrscraper import MrScraper`) fails with an import error.
4. Your token in a `.env` file, never committed:

   ```bash
   cp .env.example .env
   # then edit .env:  MRSCRAPER_API_TOKEN=your_token_here
   ```
5. A GitHub account, only if you want the schedule. Local runs need neither GitHub nor the schedule.

## Quickstart

```bash
cd price-monitoring
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python monitor.py --demo      # see the alert format — no account, no tokens spent
python selftest.py            # proves the change-detection logic, no token needed
cp .env.example .env          # paste your MrScraper token
python monitor.py             # first run = baseline; run again later for the first diff
```

## Cost

MrScraper's free tier includes 1000 tokens. The offline paths (`--demo`, `selftest.py`, `pytest`) spend none of them, so you can preview the alert format and test the change-detection logic for free.

Live runs spend tokens. MrScraper bills one token per 30 seconds of runtime, and these product pages take roughly 30–75 seconds each, so a single scrape runs about one to three tokens. The default `config.json` tracks 11 products on a 6-hour schedule — 44 scrapes a day, very roughly 50–130 tokens. Failed scrapes aren't billed, so a blocked page costs you nothing.

In practice the free tier is enough to test the recipe and watch it work, but it covers only about a day or two of the default schedule, not weeks. To stretch it, trim the product list or widen the cron interval in `.github/workflows/price-monitor.yml`. For ongoing monitoring, use fewer URLs, run less often, or move to a paid plan.

## MrScraper setup

There is no dashboard setup required. The recipe uses the same create-and-run pattern as the Playground: give it a URL and a plain-English field list, get clean JSON back. The extraction prompt lives in `monitor.py`, so any change to what you extract is a reviewable diff.

`agent="general"` because each URL is one product page. `proxy_country="US"` so US storefronts return US pricing. Omitting the country silently returns wrong-locale prices, which is one of the most common ways these runs go wrong.

One important caveat: `proxy_country="US"` sets the locale you *want*, but some retailer pages may still block, localize, or return incomplete data. For best results, enable proxy/residential routing when available, and confirm your target URLs in Playground before relying on scheduled runs.

## Example fields to extract

```json
{
  "name": "the product title",
  "price": "current selling price as a number, after any discount; null if not shown",
  "currency": "currency code shown, e.g. USD; null if not shown",
  "in_stock": "true if it can be bought now, false if sold out"
}
```

Four fields. Add more if you want (rating, seller, SKU), but these four are all the diff needs.

## API/SDK workflow

1. Read `config.json` for the product list, proxy country, and threshold.
2. Scrape every URL, one at a time, with `create_scraper(url=..., message=PROMPT, agent="general", proxy_country=...)`.
3. Load the previous run from `data/snapshot.json`.
4. Diff current against previous, keyed by URL. Emit events for stock transitions and for price moves at or above the threshold.
5. Print the summary, write it to the GitHub Actions run page if in CI, and save the new snapshot.

## Clean code sample

The whole monitor is `monitor.py`. The two parts that matter:

The live call, using the response path this recipe relies on:

```python
result = await client.create_scraper(
    url=url, message=PROMPT, agent="general", proxy_country=proxy_country
)

record = result["data"]["data"]
fields = record["data"]            # product fields returned by the agent
```

The diff, keyed by URL so a drifting product title never breaks tracking:

```python
for url, curr in current.items():
    prev = previous.get(url)
    if prev is None:
        continue  # first sighting, nothing to compare
    if prev["in_stock"] and not curr["in_stock"]:
        emit("out_of_stock", ...)
    elif not prev["in_stock"] and curr["in_stock"]:
        emit("back_in_stock", ...)
    elif prev["price"] and curr["price"] and prev["currency"] == curr["currency"]:
        pct = (curr["price"] - prev["price"]) / prev["price"] * 100
        if curr["price"] != prev["price"] and abs(pct) >= threshold:
            emit("price_drop" if pct < 0 else "price_increase", ...)
```

The change-detection logic is proven offline:

```bash
python selftest.py                # quick synthetic before/after, no API, no token
pip install pytest && pytest -q   # 90+ offline tests covering the core paths
```

## Expected output

The first run creates the baseline, so it does not alert. Real alerts start on the second run, once there is previous state to compare against. Every run after the first compares against the one before it and prints the summary shown at the top.

To show a full summary on demand for a video or screenshot, use the offline demo. It's deterministic, needs no token, and spends no tokens:

```bash
python monitor.py --demo
```

If you'd rather stage changes against real scraped data:

```bash
python monitor.py            # build a baseline
python seed_demo.py          # stage a price move and a stock change
python monitor.py            # the run shows the staged alerts
python seed_demo.py --reset  # start clean
```

## Run on a schedule

A GitHub Action runs the monitor every 6 hours (`.github/workflows/price-monitor.yml`). Add your token as the `MRSCRAPER_API_TOKEN` repository secret and it runs on its own. The schedule fires only from the default branch, so merge to `main` before you expect overnight runs.

To fire a run yourself without waiting for the schedule, for example right after setup to confirm it works:

```bash
gh workflow run price-monitor.yml
```

That needs the GitHub CLI and a `workflow_dispatch:` trigger in the workflow's `on:` block (included here).

## SMS alerts (Twilio, optional)

The monitor can text you one concise SMS per genuine price change (a drop or an
increase past your threshold). Stock-only changes and quarantined "suspect" data
are not texted. Each message contains the product name, retailer, previous price,
current price, percentage change, and the product URL.

It's fully opt-in. With no Twilio credentials set, the monitor behaves exactly as
before — it just prints the summary and skips texting.

1. Install deps (Twilio is now in `requirements.txt`):

   ```bash
   pip install -r requirements.txt
   ```

2. Add your Twilio settings to `.env` (see `.env.example`). All four are required
   to send:

   ```bash
   TWILIO_ACCOUNT_SID=ACxxxxxxxx        # Twilio Console
   TWILIO_AUTH_TOKEN=your_auth_token    # Twilio Console
   TWILIO_FROM_NUMBER=+15550000000      # a Twilio number you own
   ALERT_TO_NUMBER=+15551111111         # where alerts go
   ```

3. Preview the exact SMS without sending anything (dry run):

   ```bash
   NOTIFY_DRY_RUN=1 python monitor.py
   ```

   This prints each message body it *would* send. It never contacts Twilio and
   never records anything, so a later real run still sends.

Duplicate protection: once a specific change is texted, it's recorded in
`data/notified.json` (gitignored) so the same move can't alert twice. Missing
credentials and Twilio API errors are reported on the run's output and never
crash the monitor.

For the scheduled GitHub Action, add `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_FROM_NUMBER`, and `ALERT_TO_NUMBER` as repository secrets. The workflow
already passes them through; without them the scheduled run just skips SMS.

## License

MIT