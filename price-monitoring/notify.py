"""
SMS alerts via Twilio Programmable Messaging.

Sends one concise SMS per genuine price change (a price_drop or price_increase
produced by monitor.diff()). Everything here is opt-in and side-effect-free
unless real Twilio credentials are present:

  - No / partial credentials -> prints a clear note, sends nothing (offline
                                runs, pytest, and --demo stay silent)
  - NOTIFY_DRY_RUN=1          -> prints the exact SMS body, sends nothing
  - Full credentials + change -> sends via Twilio, then records the change so
                                 the same move never alerts twice

Stock transitions and quarantined "suspect_data" are intentionally not texted:
the required SMS fields (previous price, current price, percentage change) only
exist for real price moves.

Credentials and phone numbers come from environment variables only; nothing is
hardcoded or committed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Only these event types carry old_price, new_price, and pct — the fields the
# SMS needs. Stock flips and suspect_data are handled by the on-screen summary.
PRICE_CHANGE_TYPES = ("price_drop", "price_increase")

# Ledger of already-sent changes, so a re-run against the same baseline can't
# fire a second identical text. Lives beside the snapshot; gitignored.
NOTIFIED_PATH = Path(os.environ.get("NOTIFIED_PATH", "data/notified.json"))

# Cap the ledger so it can't grow without bound over a long-lived deployment.
_MAX_LEDGER = 500


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _is_dry_run() -> bool:
    return _env("NOTIFY_DRY_RUN").lower() in ("1", "true", "yes", "on")


def _load_credentials() -> dict | None:
    """Return the four Twilio settings, or None if any is missing."""
    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    from_number = _env("TWILIO_FROM_NUMBER")
    to_number = _env("ALERT_TO_NUMBER")
    if not (sid and token and from_number and to_number):
        return None
    return {"sid": sid, "token": token, "from": from_number, "to": to_number}


def format_sms(event: dict) -> str:
    """Build the concise SMS body for one price-change event.

    Reuses monitor._money so currency formatting matches the on-screen summary.
    Imported lazily to keep monitor.py free of any import of this module.
    """
    from monitor import _money

    old_s = _money(event.get("currency", "USD"), event["old_price"])
    new_s = _money(event.get("currency", "USD"), event["new_price"])
    verb = "Price drop" if event["type"] == "price_drop" else "Price increase"
    return (
        f"{verb}: {event['name']} ({event['retailer']})\n"
        f"{old_s} -> {new_s} ({event['pct']:+.1f}%)\n"
        f"{event['url']}"
    )


def _dedup_key(event: dict) -> str:
    return f"{event['type']}|{event['url']}|{event['old_price']}|{event['new_price']}"


def _load_notified() -> list[str]:
    try:
        data = json.loads(NOTIFIED_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []  # missing or truncated ledger: treat as "nothing sent yet"
    keys = data.get("keys") if isinstance(data, dict) else None
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


def _save_notified(keys: list[str]) -> None:
    NOTIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"keys": keys[-_MAX_LEDGER:]}, indent=2)
    tmp = NOTIFIED_PATH.with_suffix(NOTIFIED_PATH.suffix + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, NOTIFIED_PATH)  # atomic: a crash can't corrupt the ledger


def _make_client(creds: dict):
    """Build a Twilio REST client. Isolated so tests monkeypatch this and never
    touch the network or import a real client."""
    from twilio.rest import Client

    return Client(creds["sid"], creds["token"])


def send_price_alerts(events: list[dict]) -> None:
    """Send one SMS per genuine price change. Never raises: any failure is
    reported and the monitor run continues."""
    changes = [e for e in events if e.get("type") in PRICE_CHANGE_TYPES]
    if not changes:
        return

    if _is_dry_run():
        for e in changes:
            print("  [dry-run] SMS that would be sent:")
            for line in format_sms(e).splitlines():
                print(f"      {line}")
        return

    creds = _load_credentials()
    if creds is None:
        print(
            "  ! SMS not sent: Twilio credentials incomplete. Set "
            "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, and "
            "ALERT_TO_NUMBER (or NOTIFY_DRY_RUN=1 to preview)."
        )
        return

    already = _load_notified()
    seen = set(already)
    to_send = [e for e in changes if _dedup_key(e) not in seen]
    if len(to_send) < len(changes):
        print(f"  {len(changes) - len(to_send)} price change(s) already texted; skipping duplicates.")
    if not to_send:
        return

    try:
        client = _make_client(creds)
    except ImportError:
        print("  ! SMS not sent: Twilio SDK not installed. Run `pip install twilio`.")
        return

    sent = list(already)
    for e in to_send:
        try:
            client.messages.create(body=format_sms(e), from_=creds["from"], to=creds["to"])
        except Exception as ex:  # noqa: BLE001 - a send failure must not abort the run
            print(f"  ! SMS failed for {e['name']} ({e['retailer']}): {ex}")
            continue
        print(f"  SMS sent: {e['name']} ({e['retailer']}) {e['pct']:+.1f}%")
        sent.append(_dedup_key(e))

    if len(sent) != len(already):
        _save_notified(sent)
