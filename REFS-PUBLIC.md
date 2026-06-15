# REFS-PUBLIC.md — Public References

> Record external public repositories, datasets, documentation, APIs, or other
> public resources that this repository utilizes or depends on.
> This file is tracked and intentionally kept free of private or local-only details.

## Public Repositories

- https://github.com/ranaroussi/yfinance — primary OHLCV data source; MIT license

## Public Datasets and APIs

- https://finance.yahoo.com — price data served via yfinance; equities, ETFs, and crypto (`*-USD` pairs)
- https://api.binance.us — planned: native crypto OHLCV (see BACKLOG.md)
- https://www.alphavantage.co — planned: fundamentals and additional technicals (see BACKLOG.md)

## Documentation and Specifications

- https://pandas.pydata.org/docs/ — DataFrame and Series operations used throughout
- https://school.stockcharts.com/doku.php?id=technical_indicators:relative_strength_index_rsi — RSI definition
- https://school.stockcharts.com/doku.php?id=technical_indicators:vwap_intraday — VWAP definition

## Notes

- yfinance data quality varies by ticker; crypto pairs use Yahoo's `SYMBOL-USD` convention.
- The rolling VWAP implemented here is a daily-bar proxy, not true intraday VWAP.
