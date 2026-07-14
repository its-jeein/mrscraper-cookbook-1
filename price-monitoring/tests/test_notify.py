"""
Tests for the Twilio SMS alert layer. Fully offline: the Twilio client is
always monkeypatched, so no real message is ever sent and no network or
credentials are required.
"""

import json

import pytest

import notify as n


# ---------------------------------------------------------------------------
# Test doubles: a fake Twilio client that records calls or fails on demand.
# ---------------------------------------------------------------------------
class _Messages:
    def __init__(self, sent, fail):
        self._sent = sent
        self._fail = fail

    def create(self, body, from_, to):
        if self._fail:
            raise RuntimeError("twilio 500: simulated API failure")
        self._sent.append({"body": body, "from": from_, "to": to})


class _FakeClient:
    def __init__(self, sent, fail=False):
        self.messages = _Messages(sent, fail)


@pytest.fixture
def creds(monkeypatch):
    """Set complete, fake credentials in the environment."""
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok_fake")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550000000")
    monkeypatch.setenv("ALERT_TO_NUMBER", "+15551111111")
    monkeypatch.delenv("NOTIFY_DRY_RUN", raising=False)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(n, "NOTIFIED_PATH", tmp_path / "notified.json")
    return tmp_path / "notified.json"


def _drop(url="https://shop.example/rtx", pct=-15.4):
    return {
        "type": "price_drop", "url": url, "retailer": "Newegg",
        "name": "ASUS RTX 5070", "old_price": 649.99, "new_price": 549.99,
        "currency": "USD", "pct": pct,
    }


def _install_client(monkeypatch, sent, fail=False):
    monkeypatch.setattr(n, "_make_client", lambda creds: _FakeClient(sent, fail))


# ---------------------------------------------------------------------------
# format_sms: contains every required field.
# ---------------------------------------------------------------------------
def test_format_sms_has_all_required_fields():
    body = n.format_sms(_drop())
    assert "ASUS RTX 5070" in body               # product name
    assert "Newegg" in body                       # retailer
    assert "$649.99" in body                       # previous price
    assert "$549.99" in body                       # current price
    assert "-15.4%" in body                        # percentage change
    assert "https://shop.example/rtx" in body      # product URL


def test_format_sms_price_increase_wording():
    e = _drop()
    e.update(type="price_increase", old_price=100.0, new_price=130.0, pct=30.0)
    body = n.format_sms(e)
    assert "Price increase" in body and "+30.0%" in body


# ---------------------------------------------------------------------------
# Event selection: only genuine price moves are texted.
# ---------------------------------------------------------------------------
def test_only_price_changes_trigger_sms(creds, ledger, monkeypatch):
    sent = []
    _install_client(monkeypatch, sent)
    events = [
        _drop(),
        {"type": "out_of_stock", "url": "u2", "retailer": "R", "name": "N",
         "old_price": 10, "new_price": None, "currency": "USD", "pct": None},
        {"type": "suspect_data", "url": "u3", "retailer": "R", "name": "N",
         "old_price": 1.0, "new_price": 9999.0, "currency": "USD", "pct": 999999.0},
    ]
    n.send_price_alerts(events)
    assert len(sent) == 1
    assert "ASUS RTX 5070" in sent[0]["body"]


def test_no_price_change_sends_nothing(creds, ledger, monkeypatch):
    sent = []
    _install_client(monkeypatch, sent)
    n.send_price_alerts([{"type": "back_in_stock", "url": "u", "retailer": "R",
                          "name": "N", "old_price": None, "new_price": None,
                          "currency": "USD", "pct": None}])
    assert sent == []


# ---------------------------------------------------------------------------
# Happy path: a real send records the change and addresses it correctly.
# ---------------------------------------------------------------------------
def test_successful_send_records_ledger(creds, ledger, monkeypatch):
    sent = []
    _install_client(monkeypatch, sent)
    n.send_price_alerts([_drop()])
    assert len(sent) == 1
    assert sent[0]["from"] == "+15550000000" and sent[0]["to"] == "+15551111111"
    keys = json.loads(ledger.read_text())["keys"]
    assert keys == [n._dedup_key(_drop())]


# ---------------------------------------------------------------------------
# Duplicate prevention across runs.
# ---------------------------------------------------------------------------
def test_duplicate_is_not_resent(creds, ledger, monkeypatch):
    sent = []
    _install_client(monkeypatch, sent)
    n.send_price_alerts([_drop()])   # first run: sends
    n.send_price_alerts([_drop()])   # second run: same change, must skip
    assert len(sent) == 1


def test_different_price_move_is_sent_again(creds, ledger, monkeypatch):
    sent = []
    _install_client(monkeypatch, sent)
    n.send_price_alerts([_drop(pct=-15.4)])
    e2 = _drop(pct=-30.0)
    e2["new_price"] = 449.99           # a new, distinct move -> new key -> sends
    n.send_price_alerts([e2])
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# Dry-run: prints the exact SMS, sends nothing, writes no ledger.
# ---------------------------------------------------------------------------
def test_dry_run_prints_but_does_not_send(creds, ledger, monkeypatch, capsys):
    monkeypatch.setenv("NOTIFY_DRY_RUN", "1")
    sent = []
    _install_client(monkeypatch, sent)  # would record if ever called
    n.send_price_alerts([_drop()])
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "https://shop.example/rtx" in out   # exact body shown
    assert sent == []                           # nothing sent
    assert not ledger.exists()                  # dry-run must not suppress a real send later


# ---------------------------------------------------------------------------
# Missing / partial credentials: clear message, no crash, no send.
# ---------------------------------------------------------------------------
def test_missing_credentials_is_clear_and_safe(ledger, monkeypatch, capsys):
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                "TWILIO_FROM_NUMBER", "ALERT_TO_NUMBER", "NOTIFY_DRY_RUN"):
        monkeypatch.delenv(var, raising=False)
    called = {"n": 0}
    monkeypatch.setattr(n, "_make_client", lambda creds: called.__setitem__("n", 1))
    n.send_price_alerts([_drop()])
    assert "credentials incomplete" in capsys.readouterr().out
    assert called["n"] == 0            # never attempted to build a client


def test_partial_credentials_treated_as_missing(ledger, monkeypatch, capsys):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok_fake")
    monkeypatch.delenv("TWILIO_FROM_NUMBER", raising=False)  # missing sender
    monkeypatch.setenv("ALERT_TO_NUMBER", "+15551111111")
    monkeypatch.delenv("NOTIFY_DRY_RUN", raising=False)
    n.send_price_alerts([_drop()])
    assert "credentials incomplete" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Twilio API failure: reported clearly, run continues, not marked as sent.
# ---------------------------------------------------------------------------
def test_api_failure_is_handled_and_not_recorded(creds, ledger, monkeypatch, capsys):
    _install_client(monkeypatch, sent=[], fail=True)
    n.send_price_alerts([_drop()])    # must not raise
    assert "SMS failed" in capsys.readouterr().out
    assert not ledger.exists()         # failed send is retried next run, not silently dropped
