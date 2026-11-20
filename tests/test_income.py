from datetime import UTC, datetime

from tradility.income import attribute, collect, kind_of, match_ticker, name_tokens, schwab_income

NAMES = {
    "UPS": "United Parcel Service, Inc.",
    "TXN": "Texas Instruments Incorporated",
    "APD": "Air Products and Chemicals, Inc.",
    "IBM": "International Business Machines Corporation",
    "DIS": "The Walt Disney Company",
    "T": "AT&T Inc.",
    "ABT": "Abbott Laboratories",
    "VOO": "Vanguard S&P 500 ETF",
    "VOOV": "Vanguard S&P 500 Value Index Fund ETF Shares",
    "INTU": "Intuit Inc.",
    "ISRG": "Intuitive Surgical, Inc.",
}


def test_schwab_abbreviations_find_their_company():
    assert match_ticker("UNITED PARCEL SVC INC CLASS CLASS B", NAMES) == "UPS"
    assert match_ticker("TEXAS INSTRS INC", NAMES) == "TXN"
    assert match_ticker("AIR PRODS & CHEMS INC", NAMES) == "APD"
    assert match_ticker("IBM CORP", NAMES) == "IBM"  # an acronym
    assert match_ticker("DISNEY WALT CO", NAMES) == "DIS"  # words in another order
    assert match_ticker("AT&T INC", NAMES) == "T"  # not every name that has an "AT" in it


def test_an_ambiguous_or_unknown_name_is_not_guessed():
    assert match_ticker("VANGUARD S&P 500 ETF", {"VOO": NAMES["VOO"], "VOOV": NAMES["VOOV"]}) in {
        "VOO",
        None,
    }
    assert match_ticker("INTUIT", NAMES) == "INTU"  # a whole word beats a prefix of a longer name
    assert match_ticker("SOME UNKNOWN COMPANY", NAMES) is None


def test_name_tokens_drop_legal_and_share_class_boilerplate():
    assert name_tokens("NIKE INC CLASS CLASS B") == ["NIKE"]
    assert name_tokens("ABB LTD FSPONSORED ADR 1 ADR REPS 1 ORD SHS") == ["ABB", "FSPONSORED"]


def test_cash_interest_is_told_apart_from_dividends():
    assert kind_of("SCHWAB GOVERNMENT MONEY INV") == "interest"
    assert kind_of("SCHWAB1 INT 09/29-10/28") == "interest"
    assert kind_of("TEXAS INSTRS INC") == "dividend"


def _raw(description, amount, symbol=None, status="VALID"):
    instrument = (
        {"assetType": "EQUITY", "symbol": symbol}
        if symbol
        else {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"}
    )
    return {
        "description": description,
        "netAmount": amount,
        "status": status,
        "tradeDate": "2026-11-06T05:00:00+0000",
        "transferItems": [{"instrument": instrument, "amount": amount}],
        "qualifiedDividend": True,
    }


def test_a_schwab_payment_becomes_a_flat_row_and_invalid_ones_are_dropped():
    row = schwab_income(_raw("TEXAS INSTRS INC", 1.5), "6427")
    assert row["date"] == "2026-11-06" and row["amount"] == 1.5 and row["kind"] == "dividend"
    assert row["symbol"] == "" and row["account"] == "6427"
    assert schwab_income(_raw("X", 1.0, symbol="ABC"), "6427")["symbol"] == "ABC"
    assert schwab_income(_raw("X", 1.0, status="CANCELED"), "6427") is None
    assert schwab_income({"description": "X", "netAmount": "n/a"}, "1") is None


def test_attribute_uses_a_symbol_when_given_and_leaves_interest_alone():
    rows = [
        {"kind": "dividend", "description": "TEXAS INSTRS INC", "symbol": "", "amount": 2.0},
        {"kind": "dividend", "description": "ANYTHING", "symbol": "ABT", "amount": 1.0},
        {
            "kind": "interest",
            "description": "SCHWAB GOVERNMENT MONEY INV",
            "symbol": "",
            "amount": 3.0,
        },
    ]
    out = attribute(rows, NAMES)
    assert [r["ticker"] for r in out] == ["TXN", "ABT", None]


def test_collect_reports_a_missing_credential_without_rows():
    out = collect(env={}, now=datetime(2026, 11, 9, tzinfo=UTC))
    assert out["sources"]["schwab"]["ok"] is False and out["income"] == []
