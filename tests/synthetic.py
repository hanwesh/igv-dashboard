"""TEST ONLY: invented holdings and prices, never observations of IGV or any security."""

import math
from datetime import UTC, datetime

import igv_snapshot as app

TEST_NOW = datetime(2031, 6, 15, 12, tzinfo=UTC)
TEST_END = "2031-05"


def holdings(month: str, *, allocation_delta: float = 0) -> bytes:
    seed = sum(map(int, month.split("-")))
    weights = [(20 - index) * 100 / 210 for index in range(20)]
    weights[0] += 0.1 * math.sin(seed)
    weights[-1] -= 0.1 * math.sin(seed)
    weights[0] += allocation_delta
    rows = [
        {
            "ticker": f"TEST{index:02d}",
            "name": f"Synthetic Test Company {index:02d}",
            "weight_pct": weight,
            "asset_class": "Synthetic Equity",
            "market_value_usd": weight * 12345,
            "isin": f"TEST-ISIN-{index:02d}",
            "cusip": f"TEST-ID-{index:02d}",
        }
        for index, weight in enumerate(weights)
    ]
    points = {
        key: {"value": [row[field] for row in reversed(rows)]} for field, key in app.FIELDS.items()
    }
    points["asOfDate"] = {"value": int(app.snapshot_date(month).strftime("%Y%m%d"))}
    return app.json_bytes(
        {
            "_synthetic_test_only": True,
            "productId": app.PRODUCT_ID,
            "componentsByNameMap": {
                "holdings": {"containersByNameMap": {"all": {"dataPointsByNameMap": points}}}
            },
        }
    )


def prices(end: str, *, splits: dict[str, float] | None = None) -> bytes:
    first, last = app.price_bounds(end)
    dates = app.trading_days(first, last)
    splits = splits or {}
    quote = {field: [] for field in app.PRICE_FIELDS}
    timestamps, adjusted = [], []
    for day in dates:
        # Absolute date, not a window-relative index: overlaps are reproducible across refreshes.
        phase = day.toordinal()
        close = 100 + 14 * math.sin(phase / 43) + 0.001 * phase
        opening = close + 0.2 * math.cos(phase)
        factor = math.prod(splits.values())
        quote["open"].append(opening / factor)
        quote["close"].append(close / factor)
        quote["high"].append((max(opening, close) + 1.25) / factor)
        quote["low"].append((min(opening, close) - 1.25) / factor)
        quote["volume"].append(100000 + phase % 17000)
        # Deliberately different: the implementation must not use dividend-adjusted prices.
        adjusted.append(close / factor * (0.75 + 0.0000001 * phase))
        timestamps.append(
            int(datetime(day.year, day.month, day.day, 16, tzinfo=app.NY).timestamp())
        )
    events = {}
    for day, ratio in splits.items():
        if first.isoformat() <= day <= last.isoformat():
            event_date = datetime.fromisoformat(day).replace(hour=9, minute=30, tzinfo=app.NY)
            stamp = int(event_date.timestamp())
            events[str(stamp)] = {
                "date": stamp,
                "numerator": ratio,
                "denominator": 1,
                "splitRatio": f"{ratio}:1",
            }
    return app.json_bytes(
        {
            "_synthetic_test_only": True,
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"symbol": app.SYMBOL, "currency": "USD"},
                        "timestamp": timestamps,
                        "indicators": {"quote": [quote], "adjclose": [{"adjclose": adjusted}]},
                        "events": {"splits": events},
                    }
                ],
            },
        }
    )


class Provider:
    """An in-memory fixture generator. Any unexpected URL fails instead of using a network."""

    def __init__(self, end=TEST_END, *, warning_month=None, splits=None):
        self.end = end
        self.warning_month = warning_month
        self.splits = splits
        self.calls = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        prefix = "https://example.invalid/synthetic/holdings/"
        if url.startswith(prefix):
            month = url.removeprefix(prefix)[:7]
            assert url == app.source_url(month, app.SYNTHETIC)
            return holdings(month, allocation_delta=2 if month == self.warning_month else 0)
        prefix = "https://example.invalid/synthetic/prices/"
        assert url.startswith(prefix), "No real network in synthetic tests"
        end = url.removeprefix(prefix)
        assert end <= self.end and url == app.price_url(end, app.SYNTHETIC)
        return prices(end, splits=self.splits)


def seed(root, *, now=TEST_NOW, provider=None):
    provider = provider or Provider(app.completed_month(now))
    app.refresh_archive(root, provider, kind=app.SYNTHETIC, clock=lambda: now, sleep=lambda _: None)
    return provider
