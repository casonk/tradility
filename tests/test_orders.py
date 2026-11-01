"""Read-only broker orders: normalisation, signing, windows, failure handling and the export."""

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError

from tradility import orders as o
from tradility._compat import UTC

NOW = datetime(2026, 11, 1, tzinfo=UTC)

SCHWAB_ORDER = {
    "orderId": 11,
    "status": "FILLED",
    "orderType": "LIMIT",
    "price": 100.0,
    "quantity": 4,
    "filledQuantity": 4,
    "closeTime": "2026-10-02T14:00:00+0000",
    "orderLegCollection": [{"instruction": "BUY", "instrument": {"symbol": "AAPL"}}],
    "orderActivityCollection": [
        {"executionLegs": [{"quantity": 1, "price": 98.0}, {"quantity": 3, "price": 102.0}]}
    ],
}
SCHWAB_OPEN = {
    "orderId": 12,
    "status": "WORKING",
    "orderType": "STOP_LIMIT",
    "price": 90.0,
    "quantity": 2,
    "enteredTime": "2026-10-30T10:00:00+0000",
    "orderLegCollection": [{"instruction": "SELL", "instrument": {"symbol": "AAPL"}}],
}


def test_schwab_orders_are_normalised_with_the_average_fill_price_and_status():
    got = o.schwab_orders([SCHWAB_ORDER, SCHWAB_OPEN, {"status": "FILLED"}])["AAPL"]
    filled = next(x for x in got if x["id"] == "schwab-11")
    assert filled["status"] == "filled" and filled["side"] == "buy" and filled["kind"] == "limit"
    assert filled["quantity"] == 4 and filled["price"] == 101.0 and filled["date"] == "2026-10-02"
    working = next(x for x in got if x["id"] == "schwab-12")
    assert (working["status"], working["kind"], working["side"]) == ("open", "stop_limit", "sell")
    assert o.schwab_status("CANCELED") == o.schwab_status("REJECTED") == "cancelled"


BINANCE_OPEN = {
    "symbol": "BTCUSD",
    "orderId": 5,
    "status": "NEW",
    "type": "LIMIT",
    "side": "BUY",
    "price": "40000",
    "origQty": "0.5",
    "executedQty": "0",
    "cummulativeQuoteQty": "0",
    "time": 1790000000000,
}
BINANCE_FILLED = {
    "symbol": "ETHUSDT",
    "orderId": 6,
    "status": "FILLED",
    "type": "MARKET",
    "side": "SELL",
    "price": "0",
    "origQty": "2",
    "executedQty": "2",
    "cummulativeQuoteQty": "6000",
    "time": 1780000000000,
}


def test_binance_orders_map_pairs_to_analysis_tickers_and_use_the_fill_price():
    got = o.binance_orders([BINANCE_OPEN, BINANCE_FILLED, {"symbol": "BTCEUR", "orderId": 7}])
    assert set(got) == {"BTC-USD", "ETH-USD"}
    assert got["BTC-USD"][0]["status"] == "open" and got["BTC-USD"][0]["price"] == 40000.0
    eth = got["ETH-USD"][0]
    assert eth["kind"] == "market" and eth["price"] == 3000.0 and eth["side"] == "sell"


def test_signing_matches_the_published_binance_example():
    query = (
        "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
        "&recvWindow=5000&timestamp=1499827319559"
    )
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    assert (
        o.sign(secret, query) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    )


def test_schwab_is_read_in_sixty_day_windows_for_every_account():
    urls = []

    def get(url, headers):
        urls.append(url)
        assert headers["Authorization"] == "Bearer tok"
        if url.endswith("accountNumbers"):
            return [
                {"accountNumber": "1", "hashValue": "H1"},
                {"accountNumber": "2", "hashValue": "H2"},
            ]
        return (
            [SCHWAB_ORDER]
            if "H1" in url and url == next(u for u in urls if "H1/orders" in u)
            else []
        )

    got = o.fetch_schwab("tok", get, lookback_days=365, now=NOW)
    windows = [u for u in urls if "/orders?" in u]
    assert len(windows) == 2 * 7  # 365 days / 60 = 7 windows per account
    assert len(got["AAPL"]) == 1
    assert got["AAPL"][0]["account"] == "1"  # the account's last digits, so a cancel can name it


def test_binance_requests_are_signed_read_only_and_cover_held_symbols_once():
    urls = []

    def get(url, headers):
        urls.append(url)
        assert headers == {"X-MBX-APIKEY": "key"}
        return [BINANCE_OPEN] if "openOrders" in url or "BTCUSD" in url else []

    got, skipped = o.fetch_binance("key", "secret", ["BTCUSD", "ETHUSD"], get, clock=lambda: 1.0)
    assert skipped == 0
    assert [u.split("?")[0].rsplit("/", 1)[1] for u in urls] == [
        "openOrders",
        "allOrders",
        "allOrders",
    ]
    assert all(re.search(r"signature=[0-9a-f]{64}$", u) for u in urls)
    assert len(got["BTC-USD"]) == 1  # the same order from two calls is kept once


def test_held_symbols_come_from_crypto_holdings_only():
    holdings = [
        {"ticker": "BTC", "asset_class": "crypto"},
        {"ticker": "ETH-USD", "asset_class": "Crypto"},
        {"ticker": "AAPL", "asset_class": "equity"},
        {"ticker": "BTC", "asset_class": "crypto"},
    ]
    assert o.held_binance_symbols(holdings) == ["BTCUSD", "ETHUSD"]


def test_a_failing_source_is_reported_by_class_only_and_does_not_stop_the_other():
    def get(url, headers):
        if "schwab" in url:
            raise HTTPError(url, 401, "bad secret-token-value", {}, None)
        return [BINANCE_OPEN]

    env = {
        "TRADILITY_SCHWAB_ACCESS_TOKEN": "secret-token-value",
        "TRADILITY_BINANCE_API_KEY": "k",
        "TRADILITY_BINANCE_API_SECRET": "s",
    }
    result = o.collect([], get=get, env=env, now=NOW)
    assert result["sources"]["schwab"] == {"ok": False, "error": "request failed: HTTPError"}
    assert result["sources"]["binance"]["ok"] is True
    assert "secret-token-value" not in json.dumps(result)
    assert result["orders"]["BTC-USD"][0]["source"] == "binance"


def test_unconfigured_sources_say_so_and_the_export_is_owner_only(tmp_path):
    result = o.collect([], get=lambda *a: [], env={}, now=NOW)
    assert not result["sources"]["schwab"]["ok"] and not result["sources"]["binance"]["ok"]
    target = tmp_path / "x" / "orders.json"
    o.write_private(target, result)
    assert (target.stat().st_mode & 0o777) == 0o600
    assert json.loads(target.read_text())["version"] == 1


def test_this_module_can_only_read():
    source = (Path(o.__file__)).read_text()
    assert not re.search(r"method\s*=|\.post\(|\bPOST\b|\bDELETE\b|\bPUT\b", source)


def test_a_symbol_binance_us_does_not_list_is_skipped_but_other_errors_still_fail():
    def get(url, headers):
        if "NOPEUSD" in url:
            raise HTTPError(url, 400, "Invalid symbol", {}, None)
        if "BOOMUSD" in url:
            raise HTTPError(url, 401, "no", {}, None)
        return [BINANCE_OPEN] if "openOrders" in url else []

    got, skipped = o.fetch_binance("k", "s", ["NOPEUSD", "BTCUSD"], get, clock=lambda: 1.0)
    assert skipped == 1 and len(got["BTC-USD"]) == 1
    try:
        o.fetch_binance("k", "s", ["BOOMUSD"], get, clock=lambda: 1.0)
    except HTTPError as exc:
        assert exc.code == 401
    else:
        raise AssertionError("a 401 must not be swallowed")
