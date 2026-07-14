"""
End-to-end and unit tests for the price monitor. Runs fully offline:
the live scrape is monkeypatched, everything else is exercised for real.

    pip install pytest
    pytest -q
"""

import asyncio
import json

import pytest

import monitor as m


# ---------------------------------------------------------------------------
# _extract_fields: pull product data out of the create-and-run envelope.
# ---------------------------------------------------------------------------
def test_extract_verified_triple_nested():
    env = {"data": {"data": {"data": {"name": "X", "price": 10, "in_stock": True}}}}
    assert _extract(env)["name"] == "X"


def test_extract_json_string_leaf():
    env = {"data": {"data": {"data": json.dumps({"name": "X", "price": 10})}}}
    assert _extract(env)["price"] == 10


def test_extract_flattened_two_levels():
    env = {"data": {"data": {"name": "X", "price": 10, "in_stock": True}}}
    assert _extract(env)["name"] == "X"


def test_extract_shallow_one_level():
    env = {"data": {"name": "X", "price": 10}}
    assert _extract(env)["name"] == "X"


def test_extract_scraper_info_only_returns_none(capsys):
    # Envelope with the scraper record but no product fields (the PyPI-docstring shape).
    env = {"data": {"data": {"id": "abc", "scraperId": "xyz", "status": "Finished"}}}
    assert _extract(env) is None
    # And it prints the shape so a live miss is debuggable, not silent.
    assert "unexpected response shape" in capsys.readouterr().out


def test_extract_garbage_and_non_dict():
    assert _extract({"foo": "bar"}) is None
    assert _extract(None) is None
    assert _extract("nope") is None


def _extract(env):
    return m._extract_fields(env)


# ---------------------------------------------------------------------------
# _normalize / coercion: blocked renders, schema echo, real out-of-stock.
# ---------------------------------------------------------------------------
def test_blocked_render_rejected():
    assert m._normalize({"name": None, "price": None, "in_stock": None}) is None


def test_schema_echo_rejected():
    assert m._normalize({"name": "the product title", "price": "number, digits only", "in_stock": None}) is None


def test_real_out_of_stock_kept_with_hidden_price():
    p = m._normalize({"name": "Real Item", "price": None, "in_stock": "Out of stock"})
    assert p is not None and p["in_stock"] is False and p["price"] is None


def test_normal_product_kept():
    p = m._normalize({"name": "Real Item", "price": "1,299.00", "in_stock": "In stock"})
    assert p["price"] == 1299.0 and p["in_stock"] is True


@pytest.mark.parametrize("raw,expected", [
    ("$1,299.00", 1299.0), ("USD 249.99", 249.99), ("249", 249.0),
    ("0", None), ("-5", None), ("number, digits only", None),
    (100, 100.0), (99.99, 99.99), (0, None), (-1, None), (None, None),
    # P0 fix 1: non-US formats are rejected, not mangled by 100-1000x.
    ("1.299,00", None), ("169,99", None), ("1.234.567", None), (True, None),
])
def test_price_parsing(raw, expected):
    assert m._to_price(raw) == expected


# P0 fix 2: a currency flip must not produce a phantom price move.
def test_currency_mismatch_no_phantom_move():
    prev = {"u0": {"name": "X", "price": 5418.99, "currency": "TWD", "in_stock": True}}
    curr = {"u0": {"name": "X", "price": 249.00, "currency": "USD", "in_stock": True}}
    d = m.diff(prev, curr, TARGETS, 5.0)
    assert d["events"] == []          # no garbage "drop"
    assert d["unchanged"] == 1


def test_same_currency_move_still_fires():
    prev = {"u0": {"name": "X", "price": 100.0, "currency": "USD", "in_stock": True}}
    curr = {"u0": {"name": "X", "price": 80.0, "currency": "USD", "in_stock": True}}
    assert m.diff(prev, curr, TARGETS, 5.0)["events"][0]["type"] == "price_drop"


# P0 fix 3: the nested marketplace schema is parsed, not dropped.
def test_marketplace_schema_parsed():
    nested = {
        "product_info": {"name": "Galaxy A16"},
        "price": {"current_price": 169.99, "currency": "USD"},
        "stock_status": {"status": "in_stock"},
    }
    p = m._normalize(nested)
    assert p is not None
    assert p["name"] == "Galaxy A16" and p["price"] == 169.99 and p["in_stock"] is True


def test_marketplace_out_of_stock_parsed():
    nested = {
        "product_info": {"name": "Sold Item"},
        "price": {"current_price": 50.0, "currency": "USD"},
        "stock_status": {"status": "out_of_stock"},
    }
    p = m._normalize(nested)
    assert p["in_stock"] is False


@pytest.mark.parametrize("raw,expected", [
    (True, (True, True)), (False, (False, True)),
    ("In Stock", (True, True)), ("Out of Stock", (False, True)),
    ("Sold Out", (False, True)), ("Unavailable", (False, True)),
    ("Add to Cart", (True, True)), ("mystery text", (True, False)), (None, (True, False)),
])
def test_stock_parsing(raw, expected):
    assert m._to_bool(raw) == expected


# ---------------------------------------------------------------------------
# diff: the core change-detection logic.
# ---------------------------------------------------------------------------
TARGETS = [{"retailer": f"R{i}", "url": f"u{i}"} for i in range(8)]


def P(price, in_stock=True):
    return {"name": "Item", "price": price, "currency": "USD", "in_stock": in_stock}


def test_price_drop_and_increase():
    prev = {"u0": P(100.0), "u1": P(100.0)}
    curr = {"u0": P(80.0), "u1": P(130.0)}
    d = m.diff(prev, curr, TARGETS, 5.0)
    kinds = {e["type"]: e for e in d["events"]}
    assert kinds["price_drop"]["pct"] == pytest.approx(-20.0)
    assert kinds["price_increase"]["pct"] == pytest.approx(30.0)


def test_below_threshold_is_unchanged():
    d = m.diff({"u0": P(100.0)}, {"u0": P(104.0)}, TARGETS, 5.0)
    assert d["events"] == [] and d["unchanged"] == 1


def test_exactly_at_threshold_fires():
    d = m.diff({"u0": P(100.0)}, {"u0": P(105.0)}, TARGETS, 5.0)
    assert len(d["events"]) == 1 and d["events"][0]["type"] == "price_increase"


def test_stock_transitions():
    prev = {"u0": P(100.0, True), "u1": P(100.0, False)}
    curr = {"u0": P(100.0, False), "u1": P(100.0, True)}
    kinds = sorted(e["type"] for e in m.diff(prev, curr, TARGETS, 5.0)["events"])
    assert kinds == ["back_in_stock", "out_of_stock"]


def test_stock_change_wins_over_price_change():
    # Price also moved, but stock transition should be the single reported event.
    d = m.diff({"u0": P(100.0, True)}, {"u0": P(50.0, False)}, TARGETS, 5.0)
    assert len(d["events"]) == 1 and d["events"][0]["type"] == "out_of_stock"


def test_new_product_no_event():
    d = m.diff({}, {"u0": P(10.0)}, TARGETS, 5.0)
    assert d["events"] == [] and d["unchanged"] == 0


def test_missing_price_no_crash_no_phantom():
    # Price disappeared but stock unchanged: no event, no ZeroDivision, no crash.
    d = m.diff({"u0": P(100.0, True)}, {"u0": P(None, True)}, TARGETS, 5.0)
    assert d["events"] == [] and d["unchanged"] == 1


def test_zero_prev_price_is_safe():
    d = m.diff({"u0": P(0.0, True)}, {"u0": P(50.0, True)}, TARGETS, 5.0)
    assert d["events"] == []  # can't compute a pct from 0; no crash


# ---------------------------------------------------------------------------
# format_summary: screenshot output.
# ---------------------------------------------------------------------------
def test_summary_no_changes():
    out = m.format_summary({"events": [], "unchanged": 5}, missing=0)
    assert "No changes detected on 5 product(s)." in out


def test_summary_full_with_missing():
    ev = [{"type": "price_drop", "retailer": "BestBuy", "name": "Sony",
           "old_price": 449.99, "new_price": 399.99, "currency": "USD", "pct": -11.1}]
    out = m.format_summary({"events": ev, "unchanged": 4}, missing=2)
    assert "🔔 PRICE DROP" in out
    assert "$449.99 → $399.99" in out
    assert "other product(s)" in out
    assert "Could not read 2 product(s)" in out


@pytest.mark.parametrize("cur,sym", [("USD", "$"), ("EUR", "€"), ("GBP", "£"), ("JPY", "¥")])
def test_currency_symbols(cur, sym):
    assert m._money(cur, 10.0) == f"{sym}10.00"


def test_currency_fallback_to_code():
    assert m._money("TWD", 5418.99) == "TWD 5,418.99"


# ---------------------------------------------------------------------------
# Snapshot IO: corruption tolerance + round trip.
# ---------------------------------------------------------------------------
def test_load_missing_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "SNAPSHOT_PATH", tmp_path / "nope.json")
    assert m.load_snapshot() == {}


def test_load_corrupt_snapshot_does_not_crash(tmp_path, monkeypatch, capsys):
    f = tmp_path / "snap.json"
    f.write_text('{"products": {"u0": {"name": "X"  TRUNCATED')
    monkeypatch.setattr(m, "SNAPSHOT_PATH", f)
    assert m.load_snapshot() == {}
    assert "could not read previous snapshot" in capsys.readouterr().out


def test_snapshot_round_trip(tmp_path, monkeypatch):
    f = tmp_path / "snap.json"
    monkeypatch.setattr(m, "SNAPSHOT_PATH", f)
    products = {"u0": P(100.0)}
    m.save_snapshot(products)
    assert m.load_snapshot() == products
    assert "generated_at" in json.loads(f.read_text())


# ---------------------------------------------------------------------------
# Full pipeline: two runs, coverage gap, snapshot carry-forward.
# ---------------------------------------------------------------------------
def test_two_run_pipeline(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "proxy_country": "US", "threshold_pct": 5.0,
        "retailers": [{"retailer": "A", "url": "u0"},
                      {"retailer": "B", "url": "u1"},
                      {"retailer": "C", "url": "u2"}],
    }))
    monkeypatch.setattr(m, "CONFIG_PATH", cfg)
    monkeypatch.setattr(m, "SNAPSHOT_PATH", tmp_path / "snap.json")
    monkeypatch.setenv("MRSCRAPER_API_TOKEN", "dummy")  # scrape is stubbed; token just passes the guard

    state = {"n": 0}

    async def fake_scrape(targets, proxy):
        state["n"] += 1
        if state["n"] == 1:
            return {t["url"]: P(100.0) for t in targets}
        # Run 2: u0 drops, u1 goes OOS (price hidden), u2 fails to scrape entirely.
        return {"u0": P(80.0), "u1": P(None, in_stock=False)}

    monkeypatch.setattr(m, "_scrape_all", fake_scrape)

    m.main()  # baseline
    m.main()  # diff
    out = capsys.readouterr().out

    assert "🔔 PRICE DROP" in out
    assert "🔔 OUT OF STOCK" in out
    assert "Could not read 1 product(s) this run." in out  # u2 coverage gap surfaced

    # u2 kept its last good value despite failing to scrape this run.
    snap = json.loads((tmp_path / "snap.json").read_text())["products"]
    assert snap["u2"]["price"] == 100.0


def test_main_invokes_send_price_alerts_with_detected_events(tmp_path, monkeypatch):
    # main() must hand the diff's events to the SMS layer. We stub the notifier
    # (so nothing is ever sent) and capture exactly what main() passes it.
    import notify

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "proxy_country": "US", "threshold_pct": 5.0,
        "retailers": [{"retailer": "A", "url": "u0"}],
    }))
    monkeypatch.setattr(m, "CONFIG_PATH", cfg)
    monkeypatch.setattr(m, "SNAPSHOT_PATH", tmp_path / "snap.json")
    monkeypatch.setenv("MRSCRAPER_API_TOKEN", "dummy")  # scrape is stubbed

    state = {"n": 0}

    async def fake_scrape(targets, proxy):
        state["n"] += 1
        price = 100.0 if state["n"] == 1 else 80.0  # run 2 = -20% price drop
        return {t["url"]: P(price) for t in targets}

    monkeypatch.setattr(m, "_scrape_all", fake_scrape)

    calls = []
    monkeypatch.setattr(notify, "send_price_alerts", lambda events: calls.append(events))

    m.main()  # baseline: notifier is still called, with no events to alert on
    assert calls == [[]]

    m.main()  # second run: the detected price drop is passed through to the notifier
    drop = next(e for e in calls[1] if e["type"] == "price_drop")
    assert drop["url"] == "u0"          # event carries the URL the SMS needs
    assert drop["pct"] == pytest.approx(-20.0)


# ---------------------------------------------------------------------------
# Config error handling: clear exits, not raw tracebacks.
# ---------------------------------------------------------------------------
def test_missing_config_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "CONFIG_PATH", tmp_path / "missing.json")
    with pytest.raises(SystemExit):
        m.main()


def test_bad_json_config_exits(tmp_path, monkeypatch):
    f = tmp_path / "config.json"
    f.write_text("{not json")
    monkeypatch.setattr(m, "CONFIG_PATH", f)
    with pytest.raises(SystemExit):
        m.main()


def test_empty_retailers_exits(tmp_path, monkeypatch):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"retailers": []}))
    monkeypatch.setattr(m, "CONFIG_PATH", f)
    with pytest.raises(SystemExit):
        m.main()


# ---------------------------------------------------------------------------
# Runtime path: _fetch_one / _scrape_all with a mocked SDK client.
# These exercise the async scrape loop and per-product error isolation
# without any network or token.
# ---------------------------------------------------------------------------
GOOD_ENV = {"data": {"data": {"data": {
    "name": "Widget", "price": 10.0, "currency": "USD", "in_stock": True}}}}


class _FakeClient:
    """Stand-in for MrScraper: returns a scripted envelope per URL, or raises."""

    def __init__(self, script):
        self.script = script

    async def create_scraper(self, url, message, agent, proxy_country):
        beh = self.script.get(url, GOOD_ENV)
        if isinstance(beh, Exception):
            raise beh
        return beh


def test_fetch_one_sdk_errors_return_none():
    pytest.importorskip("mrscraper")
    from mrscraper.exceptions import APIError, AuthenticationError, NetworkError
    for exc in (AuthenticationError("a"), APIError("b"), NetworkError("c")):
        assert asyncio.run(m._fetch_one(_FakeClient({"u": exc}), "u", "US")) is None


@pytest.mark.parametrize("env", [
    {"success": False, "status": "Checking URL", "data": {"data": {"data": {}}}},
    {}, None, [1, 2], "err", {"data": {"data": {}}},
    {"data": {"data": {"data": {"name": None, "price": None, "currency": None, "in_stock": None}}}},
])
def test_fetch_one_malformed_envelope_returns_none(env):
    pytest.importorskip("mrscraper")
    assert asyncio.run(m._fetch_one(_FakeClient({"u": env}), "u", "US")) is None


def test_scrape_all_isolates_every_failure_mode(monkeypatch):
    mr = pytest.importorskip("mrscraper")
    script = {"u0": GOOD_ENV, "u1": {"success": False, "data": {}},
              "u2": Exception("generic sdk blowup"), "u3": TimeoutError("hang"),
              "u4": GOOD_ENV}
    monkeypatch.setattr(mr, "MrScraper", lambda token: _FakeClient(script))
    monkeypatch.setenv("MRSCRAPER_API_TOKEN", "dummy")
    out = asyncio.run(m._scrape_all(TARGETS[:5], "US"))
    assert set(out) == {"u0", "u4"}  # good ones kept; all failures isolated, no crash


# ---------------------------------------------------------------------------
# Config validation and operational signal (regressions for stress-test finds).
# ---------------------------------------------------------------------------
def _cfg(tmp_path, obj):
    f = tmp_path / "config.json"
    f.write_text(json.dumps(obj))
    return f


def test_main_missing_url_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "CONFIG_PATH", _cfg(tmp_path, {"retailers": [{"retailer": "Amazon"}]}))
    monkeypatch.setenv("MRSCRAPER_API_TOKEN", "dummy")
    with pytest.raises(SystemExit):
        m.main()


def test_main_bad_threshold_type_exits_cleanly(tmp_path, monkeypatch):
    cfg = {"retailers": [{"retailer": "R", "url": "u"}], "threshold_pct": "abc"}
    monkeypatch.setattr(m, "CONFIG_PATH", _cfg(tmp_path, cfg))
    monkeypatch.setenv("MRSCRAPER_API_TOKEN", "dummy")
    with pytest.raises(SystemExit):
        m.main()


def test_main_negative_threshold_exits_cleanly(tmp_path, monkeypatch):
    cfg = {"retailers": [{"retailer": "R", "url": "u"}], "threshold_pct": -1.0}
    monkeypatch.setattr(m, "CONFIG_PATH", _cfg(tmp_path, cfg))
    monkeypatch.setenv("MRSCRAPER_API_TOKEN", "dummy")
    with pytest.raises(SystemExit):
        m.main()


def test_main_total_scrape_failure_exits_nonzero_and_keeps_state(tmp_path, monkeypatch):
    mr = pytest.importorskip("mrscraper")
    cfg = {"retailers": [{"retailer": "R", "url": "z"}], "threshold_pct": 5.0}
    snap = tmp_path / "snap.json"
    monkeypatch.setattr(m, "CONFIG_PATH", _cfg(tmp_path, cfg))
    monkeypatch.setattr(m, "SNAPSHOT_PATH", snap)
    monkeypatch.setattr(mr, "MrScraper", lambda token: _FakeClient({"z": {"success": False, "data": {}}}))
    monkeypatch.setenv("MRSCRAPER_API_TOKEN", "dummy")
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code not in (0, None)   # loud failure, not a green run
    assert not snap.exists()                # did not overwrite good state with nothing


# ---------------------------------------------------------------------------
# P1 hardening: per-request timeout and atomic snapshot write.
# ---------------------------------------------------------------------------
def test_fetch_one_times_out_returns_none(monkeypatch):
    pytest.importorskip("mrscraper")
    monkeypatch.setattr(m, "REQUEST_TIMEOUT_S", 0.05)

    class _Hang:
        async def create_scraper(self, **kw):
            await asyncio.sleep(5)  # never completes within the timeout

    # A hung scrape resolves to None (skipped), not an exception or a stall.
    assert asyncio.run(m._fetch_one(_Hang(), "u", "US")) is None


def _prod(name, price):
    return {"name": name, "price": price, "currency": "USD", "in_stock": True}


def test_save_snapshot_is_atomic_on_failure(tmp_path, monkeypatch):
    snap = tmp_path / "data" / "snapshot.json"
    monkeypatch.setattr(m, "SNAPSHOT_PATH", snap)
    m.save_snapshot({"u": _prod("old", 1.0)})
    before = snap.read_text()

    def _boom(*a, **k):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(m.os, "replace", _boom)
    with pytest.raises(OSError):
        m.save_snapshot({"u": _prod("new", 2.0)})

    # The original snapshot is untouched and still readable, not truncated.
    assert snap.read_text() == before
    assert m.load_snapshot()["u"]["name"] == "old"


def test_save_snapshot_round_trip_after_atomic_change(tmp_path, monkeypatch):
    snap = tmp_path / "data" / "snapshot.json"
    monkeypatch.setattr(m, "SNAPSHOT_PATH", snap)
    m.save_snapshot({"u": _prod("v1", 1.0)})
    m.save_snapshot({"u": _prod("v2", 2.0)})  # second write replaces cleanly
    assert m.load_snapshot()["u"]["price"] == 2.0
    assert not snap.with_suffix(".json.tmp").exists()  # no temp left after success


def test_demo_mode_uses_real_detection(capsys):
    m.demo(pause=0)
    out = capsys.readouterr().out
    assert "🔍 Checking Tesla Model Y" in out
    assert "Previous Price: $41,998" in out
    assert "Current Price: $39,998" in out
    assert "🚨 Price Change Detected" in out
    assert "PRICE DROP" in out   # produced by the real diff() + format_summary()
    assert "-4.8%" in out        # real computed percentage, not hardcoded


# Plain-test coverage for the invariants the property tests proved (no hypothesis dep).
@pytest.mark.parametrize("junk", [
    None, True, False, "", "free", "N/A", "$", "abc", "1.2.3",
    "199,00", "1.299,00", [], {}, -5, 0, "0", float("nan"),
])
def test_to_price_never_raises_and_is_safe(junk):
    r = m._to_price(junk)
    assert r is None or (isinstance(r, float) and r > 0)


def test_identical_product_never_alerts_even_at_zero_threshold():
    p = {"u0": {"name": "X", "price": 100.0, "currency": "USD", "in_stock": True}}
    assert m.diff(p, dict(p), TARGETS, 0.0)["events"] == []