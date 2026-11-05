"""Live indicators: the stored bars plus a live price give the numbers a fresh run would."""

import pandas as pd
import pytest

from tradility.analyze import indicator_fields, live_bars
from tradility.live import live_indicators


def _frame(days: int = 400) -> pd.DataFrame:
    index = pd.bdate_range("2025-01-01", periods=days)
    close = pd.Series([50 + (i % 17) * 0.7 + i * 0.05 for i in range(days)], index=index)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Volume": 1000 + close * 3,
        }
    )


def _record(df: pd.DataFrame) -> dict:
    fields = indicator_fields(df["Close"], df["High"], df["Low"], df["Volume"])
    return {"ticker": "T", "live_bars": live_bars(df), **fields}


def test_a_live_bar_for_a_new_day_matches_a_full_recompute():
    df = _frame()
    record = _record(df)
    price = 61.25
    day = {"high": 62.0, "low": 60.0, "volume": 5000.0}
    live = live_indicators(record, price, day, today={"2099-01-01"})

    index = df.index.append(pd.DatetimeIndex([df.index[-1] + pd.Timedelta(days=1)]))
    grown = pd.concat(
        [
            df,
            pd.DataFrame(
                {"Open": price, "High": 62.0, "Low": 60.0, "Close": price, "Volume": 5000.0},
                index=[index[-1]],
            ),
        ]
    )
    full = indicator_fields(grown["Close"], grown["High"], grown["Low"], grown["Volume"])
    assert live["last_price"] == price
    for key in ("rsi_14", "vwap_20", "sma_50", "sma_200"):
        assert live[key] == pytest.approx(full[key], abs=1e-3), key  # bars are stored to 4 places
    for key in ("long_term_trend", "technical_rating", "vwap_20_signal", "rsi_14_signal"):
        assert live[key] == full[key], key
    for kind, values in full["indicator_variants"].items():
        assert live["indicator_variants"][kind] == pytest.approx(values, abs=1e-2)


def test_an_in_progress_bar_dated_today_is_replaced_not_duplicated():
    df = _frame()
    record = _record(df)
    today = {record["live_bars"]["date"]}
    live = live_indicators(record, 70.0, today=today)
    shorter = df.copy()
    shorter.iloc[-1, shorter.columns.get_loc("Close")] = 70.0
    shorter.iloc[-1, shorter.columns.get_loc("High")] = max(float(df["High"].iloc[-1]), 70.0)
    full = indicator_fields(shorter["Close"], shorter["High"], shorter["Low"], shorter["Volume"])
    assert live["rsi_14"] == pytest.approx(full["rsi_14"], abs=1e-2)
    assert live["sma_50"] == pytest.approx(full["sma_50"], abs=1e-3)


def test_without_session_volume_vwap_keeps_its_stored_value_but_the_signal_follows_price():
    df = _frame()
    record = _record(df)
    live = live_indicators(record, 200.0, today={"2099-01-01"})
    assert live["vwap_20"] == record["vwap_20"]
    assert live["vwap_20_signal"] == "above"  # 200 is far over the stored VWAP
    assert live["rsi_14"] != record["rsi_14"]


def test_records_without_stored_bars_have_no_live_indicators():
    assert live_indicators({"ticker": "OLD"}, 10.0) is None


def test_missing_highs_lows_and_volumes_never_become_nan_and_stay_valid_json():
    import json

    df = _frame()
    df.iloc[-3, df.columns.get_loc("High")] = float("nan")
    df.iloc[-2, df.columns.get_loc("Low")] = float("nan")
    df.iloc[-1, df.columns.get_loc("Volume")] = float("nan")
    bars = live_bars(df)
    json.dumps(bars, allow_nan=False)  # raises on NaN: browsers cannot parse it
    assert bars["high"][-3] == bars["close"][-3] and bars["low"][-2] == bars["close"][-2]
    assert bars["volume"][-1] == 0


def test_an_old_export_with_null_bars_still_gives_live_indicators():
    df = _frame()
    record = _record(df)
    record["live_bars"]["high"][-3] = None
    record["live_bars"]["low"][-2] = None
    record["live_bars"]["volume"][-1] = None
    live = live_indicators(
        record, 61.0, {"high": 62.0, "low": 60.0, "volume": 4000.0}, today={"2099-01-01"}
    )
    assert live is not None and live["vwap_20"] == live["vwap_20"]  # not NaN
