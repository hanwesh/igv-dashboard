import json
import math
from urllib.error import HTTPError, URLError

import pytest

import igv_snapshot as app
from tests.synthetic import TEST_END, holdings, prices


def points(data):
    return data["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"][
        "dataPointsByNameMap"
    ]


def result(data):
    return data["chart"]["result"][0]


def parsed_holding(data):
    return app.parse_holdings(json.dumps(data).encode(), TEST_END, app.SYNTHETIC)


def parsed_price(data, end=TEST_END):
    return app.parse_prices(json.dumps(data).encode(), end, app.SYNTHETIC)


def test_unrounded_rank_and_identifiers():
    data = json.loads(holdings(TEST_END))
    source = points(data)
    source["holdingPercent"]["value"][-1] = 10.000001
    source["holdingPercent"]["value"][-2] = 10.000002
    item = parsed_holding(data)
    assert item["top10"][0]["ticker"] == "TEST01"
    assert item["top10"][0]["weight_pct"] == 10.000002
    assert item["top10"][1]["ticker"] == "TEST00"
    assert item["top10_weight_pct"] == math.fsum(row["weight_pct"] for row in item["top10"])
    assert [row["rank"] for row in item["top10"]] == list(range(1, 11))
    assert item["top10"][0]["name"] == "Synthetic Test Company 01"


def test_rounding_noise_quiet_but_material_allocation_warns():
    for delta in [0, 0.00001, -0.00001, 0.004]:
        item = app.parse_holdings(
            holdings(TEST_END, allocation_delta=delta), TEST_END, app.SYNTHETIC
        )
        assert not item["quality_notes"]
        assert item["portfolio_weight_total_pct"] == pytest.approx(100 + delta)
    for delta in [2, -1, 0.01]:
        item = app.parse_holdings(
            holdings(TEST_END, allocation_delta=delta), TEST_END, app.SYNTHETIC
        )
        assert len(item["quality_notes"]) == 1
        assert "without normalization" in item["quality_notes"][0]
        assert item["portfolio_weight_total_pct"] == pytest.approx(100 + delta)


def test_cash_and_negative_non_top_positions_are_not_filtered():
    data = json.loads(holdings(TEST_END))
    source = points(data)
    for index, weight in [(0, 0), (1, -0.1)]:
        source["holdingPercent"]["value"][index] = weight
        source["marketValue"]["value"][index] = weight * 1000
        for field in ["ticker", "isin", "cusip"]:
            source[field]["value"][index] = None
    source["assetClass"]["value"][-1] = "Synthetic Cash"
    assert parsed_holding(data)["holding_count"] == 20
    assert parsed_holding(data)["top10"][0]["asset_class"] == "Synthetic Cash"


@pytest.mark.parametrize(
    "case",
    [
        "id",
        "date",
        "empty",
        "short",
        "misaligned",
        "missing",
        "nan",
        "bool",
        "text_weight",
        "bad_text",
        "empty_top",
        "duplicate",
    ],
)
def test_holdings_reject_bad_responses(case):
    data = json.loads(holdings(TEST_END))
    source = points(data)
    if case == "id":
        data["productId"] = 999999
    elif case == "date":
        source["asOfDate"]["value"] -= 1
    elif case in {"empty", "short", "misaligned"}:
        source["ticker"]["value"] = source["ticker"]["value"][
            : {"empty": 0, "short": 3, "misaligned": 19}[case]
        ]
    elif case == "missing":
        del source["holdingPercent"]
    elif case in {"nan", "bool", "text_weight"}:
        source["holdingPercent"]["value"][-1] = {
            "nan": float("nan"),
            "bool": True,
            "text_weight": "10",
        }[case]
    elif case == "bad_text":
        source["issueName"]["value"][-1] = {"malformed": "text"}
    elif case == "empty_top":
        source["ticker"]["value"][-1] = ""
    elif case == "duplicate":
        source["isin"]["value"][-2] = source["isin"]["value"][-1]
    with pytest.raises(app.DataError):
        parsed_holding(data)


def test_malformed_json_and_kind_mixing():
    for content in [b"<html>unavailable</html>", b"[]", b"null"]:
        with pytest.raises(app.DataError):
            app.parse_holdings(content, TEST_END, app.SYNTHETIC)
    with pytest.raises(app.DataError, match="mismatch"):
        app.parse_holdings(holdings(TEST_END), TEST_END, app.PRODUCTION)
    data = json.loads(holdings(TEST_END))
    del data["_synthetic_test_only"]
    with pytest.raises(app.DataError, match="mismatch"):
        parsed_holding(data)


def test_price_month_alignment_and_compounding():
    snapshots = [
        app.parse_holdings(holdings(month), month, app.SYNTHETIC)
        for month in app.display_months(TEST_END)
    ]
    parsed = app.parse_prices(prices(TEST_END), TEST_END, app.SYNTHETIC)
    series = app.performance_series(snapshots, parsed)
    assert len(series) == 60
    baseline = parsed["days"][0]
    assert series[0]["previous_close"] == baseline["close"]
    compounded = math.prod(1 + row["monthly_return_pct"] / 100 for row in series)
    assert compounded == pytest.approx(series[-1]["close"] / baseline["close"])
    assert (compounded - 1) * 100 == pytest.approx(series[-1]["cumulative_return_pct"])
    for item in series:
        days = [row for row in parsed["days"] if row["date"].startswith(item["month"])]
        assert item["trading_days"] == len(days)
        assert item["volume"] == sum(row["volume"] for row in days)
        assert item["high"] == max(row["high"] for row in days)
        assert item["low"] == min(row["low"] for row in days)
        assert item["open"] == days[0]["open"]
        assert item["close"] == days[-1]["close"]
    raw = json.loads(prices(TEST_END))
    result(raw)["indicators"]["adjclose"][0]["adjclose"] = [
        value * 0.1 for value in result(raw)["indicators"]["adjclose"][0]["adjclose"]
    ]
    assert app.performance_series(snapshots, parsed_price(raw)) == series
    snapshots[-1]["snapshot_date"] = "2031-05-01"
    with pytest.raises(app.DataError, match="mismatch"):
        app.performance_series(snapshots, parsed)


@pytest.mark.parametrize(
    "case",
    [
        "symbol",
        "currency",
        "error",
        "empty_result",
        "null_result",
        "missing_column",
        "misaligned",
        "null",
        "infinite",
        "negative",
        "ohlc",
        "volume",
        "missing_day",
        "stale",
        "missing_baseline",
        "duplicate",
        "extra_day",
        "split",
    ],
)
def test_prices_reject_incomplete_or_invalid_histories(case):
    data = json.loads(prices(TEST_END))
    res = result(data)
    quote = res["indicators"]["quote"][0]
    if case in {"symbol", "currency"}:
        res["meta"][case] = "WRONG"
    elif case == "error":
        data["chart"]["error"] = {"code": "SyntheticFailure"}
    elif case == "empty_result":
        data["chart"]["result"] = []
    elif case == "null_result":
        data["chart"]["result"] = None
    elif case == "missing_column":
        del quote["open"]
    elif case == "misaligned":
        quote["open"].pop()
    elif case in {"null", "infinite", "negative"}:
        quote["close"][3] = {"null": None, "infinite": float("inf"), "negative": -1}[case]
    elif case == "ohlc":
        quote["high"][4] = quote["low"][4] - 1
    elif case == "volume":
        quote["volume"][4] = 1.25
    elif case in {"missing_day", "stale", "missing_baseline"}:
        index = {"missing_day": 6, "stale": -1, "missing_baseline": 0}[case]
        res["timestamp"].pop(index)
        for values in quote.values():
            values.pop(index)
        res["indicators"]["adjclose"][0]["adjclose"].pop(index)
    elif case == "duplicate":
        res["timestamp"][1] = res["timestamp"][0]
    elif case == "extra_day":
        res["timestamp"][-1] += 86400
    elif case == "split":
        res["events"]["splits"] = {
            "bad": {"date": res["timestamp"][20], "numerator": 0, "denominator": 1}
        }
    with pytest.raises(app.DataError):
        parsed_price(data)


def test_split_revision_checks_entire_overlap():
    before = app.parse_prices(prices(TEST_END), TEST_END, app.SYNTHETIC)
    after_end = "2031-06"
    split_day = app.trading_days(app.month_start(after_end), app.snapshot_date(after_end))[
        5
    ].isoformat()
    after = app.parse_prices(prices(after_end, splits={split_day: 3}), after_end, app.SYNTHETIC)
    app.check_price_revision(before, after)
    broken = app.parse_prices(prices(after_end), after_end, app.SYNTHETIC)
    broken["splits"] = after["splits"]
    with pytest.raises(app.DataError, match="Incoherent"):
        app.check_price_revision(before, broken)
    after["days"][50]["open"] *= 1.001
    with pytest.raises(app.DataError, match="Incoherent"):
        app.check_price_revision(before, after)
    retro = app.parse_prices(prices(after_end, splits={split_day: 3}), after_end, app.SYNTHETIC)
    retro["splits"][before["days"][-10]["date"]] = 2
    with pytest.raises(app.DataError, match="retrospective"):
        app.check_price_revision(before, retro)


def test_initial_price_window_rejects_unadjusted_split_quotes():
    split_day = "2029-06-11"
    raw = json.loads(prices(TEST_END, splits={split_day: 3}))
    source = result(raw)
    for index, stamp in enumerate(source["timestamp"]):
        if app.timestamp_day(stamp).isoformat() < split_day:
            for field in ("open", "high", "low", "close"):
                source["indicators"]["quote"][0][field][index] *= 3
    with pytest.raises(app.DataError, match="Mixed price adjustment basis"):
        parsed_price(raw)


def test_recorded_cash_distribution_is_not_used_as_price_return():
    raw = json.loads(prices(TEST_END))
    source = result(raw)
    for index in range(30):
        source["indicators"]["adjclose"][0]["adjclose"][index] *= 0.98
    stamp = source["timestamp"][30]
    source["events"]["dividends"] = {str(stamp): {"date": stamp, "amount": 2}}
    assert (
        parsed_price(raw)["days"]
        == app.parse_prices(prices(TEST_END), TEST_END, app.SYNTHETIC)["days"]
    )
    source["events"].pop("dividends")
    with pytest.raises(app.DataError, match="Mixed price adjustment basis"):
        parsed_price(raw)


def test_numeric_overflow_is_explicit():
    raw = json.loads(holdings(TEST_END))
    points(raw)["holdingPercent"]["value"] = [1e308] * 20
    with pytest.raises(app.DataError, match="overflow"):
        parsed_holding(raw)


def test_retries_exact_request_without_fallback():
    good = holdings(TEST_END)
    responses = iter([URLError("synthetic transient error"), b"{}", good])
    seen, delays = [], []

    def fetch(url):
        seen.append(url)
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    url = app.source_url(TEST_END, app.SYNTHETIC)
    raw, item = app.fetch_validated(
        fetch,
        url,
        lambda data: app.parse_holdings(data, TEST_END, app.SYNTHETIC),
        sleep=delays.append,
    )
    assert raw == good and item["month"] == TEST_END
    assert seen == [url] * 3
    assert delays == [1, 2]


def test_permanent_http_failure_does_not_retry():
    seen = []

    def forbidden(url):
        seen.append(url)
        raise HTTPError(url, 403, "synthetic forbidden", {}, None)

    with pytest.raises(app.DataError, match="HTTP 403"):
        app.fetch_validated(
            forbidden, "https://example.invalid/", lambda _: {}, sleep=lambda _: None
        )
    assert len(seen) == 1
