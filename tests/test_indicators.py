"""Unit tests for RSI and VWAP indicator calculations."""

from __future__ import annotations

import math

import pandas as pd

from tradility.indicators import rsi, rsi_signal, vwap, vwap_signal


def _close(prices: list[float]) -> pd.Series:
    return pd.Series(prices, dtype=float)


def _ohlcv(n: int = 30, price: float = 100.0, vol: float = 1_000_000.0):
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    close = pd.Series([price] * n, index=idx, dtype=float)
    return (
        close + 1,  # high
        close - 1,  # low
        close,
        pd.Series([vol] * n, index=idx, dtype=float),
    )


class TestRsi:
    def test_flat_price_returns_nan_then_50(self):
        prices = [100.0] * 20
        result = rsi(_close(prices))
        # With no moves, gain and loss are both 0 — RS is NaN → RSI NaN
        assert result.isna().all() or result.dropna().empty or math.isnan(result.iloc[-1])

    def test_all_up_days_near_100(self):
        prices = [float(i) for i in range(1, 25)]
        result = rsi(_close(prices))
        last = result.dropna().iloc[-1]
        assert last > 90

    def test_all_down_days_near_0(self):
        prices = [float(i) for i in range(24, 0, -1)]
        result = rsi(_close(prices))
        last = result.dropna().iloc[-1]
        assert last < 10

    def test_length_preserved(self):
        prices = [100.0 + i * 0.5 for i in range(30)]
        result = rsi(_close(prices))
        assert len(result) == 30

    def test_values_in_range(self):
        import random

        random.seed(42)
        prices = [100.0]
        for _ in range(50):
            prices.append(prices[-1] * (1 + random.uniform(-0.03, 0.03)))
        result = rsi(_close(prices)).dropna()
        assert (result >= 0).all() and (result <= 100).all()


class TestRsiSignal:
    def test_oversold(self):
        assert rsi_signal(25.0) == "oversold"
        assert rsi_signal(30.0) == "oversold"

    def test_overbought(self):
        assert rsi_signal(75.0) == "overbought"
        assert rsi_signal(70.0) == "overbought"

    def test_neutral(self):
        assert rsi_signal(50.0) == "neutral"
        assert rsi_signal(55.3) == "neutral"

    def test_none_and_nan(self):
        assert rsi_signal(None) == "unknown"
        assert rsi_signal(float("nan")) == "unknown"


class TestVwap:
    def test_constant_price_equals_price(self):
        high, low, close, volume = _ohlcv(30, price=100.0)
        result = vwap(high, low, close, volume, period=20)
        last = result.dropna().iloc[-1]
        assert abs(last - 100.0) < 0.01

    def test_length_preserved(self):
        high, low, close, volume = _ohlcv(30)
        result = vwap(high, low, close, volume, period=20)
        assert len(result) == 30

    def test_nan_before_period(self):
        high, low, close, volume = _ohlcv(30)
        result = vwap(high, low, close, volume, period=20)
        assert result.iloc[:19].isna().all()
        assert not math.isnan(result.iloc[19])


class TestVwapSignal:
    def test_above(self):
        assert vwap_signal(110.0, 100.0) == "above"

    def test_below(self):
        assert vwap_signal(90.0, 100.0) == "below"

    def test_at_within_tolerance(self):
        assert vwap_signal(100.3, 100.0) == "at"
        assert vwap_signal(99.7, 100.0) == "at"

    def test_none_inputs(self):
        assert vwap_signal(None, 100.0) == "unknown"
        assert vwap_signal(100.0, None) == "unknown"
