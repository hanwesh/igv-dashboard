#!/usr/bin/env python3
"""Strict, permission-gated IGV archive and native dashboard builder.

No market data ships with this code. Tests inject independently generated data;
the network entry point is disabled unless permission and context gates pass.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

ROOT = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")
MONTH_COUNT = 60
PRODUCT_ID = 239771
SYMBOL = "IGV"
PRODUCTION = "production"
SYNTHETIC = "synthetic-test-only"
REPOSITORY = "hanwesh/igv-dashboard"
PRODUCT_URL = "https://www.ishares.com/us/products/239771/ishares-north-american-techsoftware-etf"
HOLDINGS_API = (
    "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
    "product-data/api/v2/get-product-data"
)
PRICE_API = "https://query1.finance.yahoo.com/v8/finance/chart/IGV"
FIELDS = {
    "ticker": "ticker",
    "name": "issueName",
    "weight_pct": "holdingPercent",
    "asset_class": "assetClass",
    "market_value_usd": "marketValue",
    "isin": "isin",
    "cusip": "cusip",
}
PRICE_FIELDS = ("open", "high", "low", "close", "volume")
PERFORMANCE_FIELDS = (
    "month",
    "date",
    "open",
    "high",
    "low",
    "close",
    "previous_close",
    "monthly_return_pct",
    "cumulative_return_pct",
    "volume",
    "trading_days",
)


class DataError(ValueError):
    """An archive or provider response cannot be used without guessing."""


class BlockedError(DataError):
    """Permission or a complete production archive is missing."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise DataError("Timestamps must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise DataError("Missing retrieval timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DataError("Invalid retrieval timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise DataError("Provenance timestamps must be UTC, not filesystem mtimes")
    return parsed


def month_start(month: str) -> date:
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
        raise DataError("Months must have YYYY-MM form")
    try:
        return date.fromisoformat(month + "-01")
    except ValueError as error:
        raise DataError("Invalid calendar month") from error


def shift_month(month: str, offset: int) -> str:
    start = month_start(month)
    year, zero_month = divmod(start.year * 12 + start.month - 1 + offset, 12)
    try:
        return date(year, zero_month + 1, 1).strftime("%Y-%m")
    except ValueError as error:
        raise DataError("Month outside the supported date range") from error


def completed_month(now: datetime | None = None) -> str:
    now = now or utc_now()
    if now.tzinfo is None:
        raise DataError("The refresh clock must be timezone-aware")
    return shift_month(now.astimezone(NY).strftime("%Y-%m"), -1)


def month_range(first: str, last: str) -> list[str]:
    start, end = month_start(first), month_start(last)
    count = (end.year - start.year) * 12 + end.month - start.month + 1
    if count <= 0:
        raise DataError("Reversed monthly range")
    return [shift_month(first, offset) for offset in range(count)]


def display_months(end: str) -> list[str]:
    return month_range(shift_month(end, 1 - MONTH_COUNT), end)


@lru_cache(maxsize=32)
def exchange_calendar(first_year: int, last_year: int):
    # Calendar bounds are sessions, so leave room for closed boundary dates.
    return xcals.get_calendar("XNYS", start=f"{first_year - 1}-01-01", end=f"{last_year + 1}-12-31")


def trading_days(first: date, last: date) -> list[date]:
    if first > last:
        raise DataError("Reversed trading-day range")
    try:
        exchange = exchange_calendar(first.year, last.year)
        return [stamp.date() for stamp in exchange.sessions_in_range(first, last)]
    except (ValueError, KeyError) as error:
        raise DataError("Exchange calendar cannot cover the requested range") from error


def snapshot_date(month: str) -> date:
    start = month_start(month)
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    days = trading_days(start, end)
    if not days:
        raise DataError(f"No exchange sessions in {month}")
    return days[-1]


def price_bounds(end: str) -> tuple[date, date]:
    return snapshot_date(shift_month(end, -MONTH_COUNT)), snapshot_date(end)


def source_url(month: str, kind: str = PRODUCTION) -> str:
    day = snapshot_date(month)
    if kind == SYNTHETIC:
        return f"https://example.invalid/synthetic/holdings/{day.isoformat()}"
    return (
        HOLDINGS_API
        + "?"
        + urlencode(
            {
                "appType": "PRODUCT_PAGE",
                "appSubType": "ISHARES",
                "targetSite": "us-ishares",
                "locale": "en_US",
                "portfolioId": str(PRODUCT_ID),
                "userType": "individual",
                "asOfDate": day.strftime("%Y%m%d"),
                "component": "holdings.all",
                "excludeContent": "true",
            }
        )
    )


def price_url(end: str, kind: str = PRODUCTION) -> str:
    first, _ = price_bounds(end)
    exclusive_end = month_start(shift_month(end, 1))
    if kind == SYNTHETIC:
        return f"https://example.invalid/synthetic/prices/{end}"
    return (
        PRICE_API
        + "?"
        + urlencode(
            {
                "period1": int(datetime.combine(first, datetime.min.time(), NY).timestamp()),
                "period2": int(
                    datetime.combine(exclusive_end, datetime.min.time(), NY).timestamp()
                ),
                "interval": "1d",
                "events": "div,splits",
                "includeAdjustedClose": "true",
            }
        )
    )


def publication_allowed(env: Mapping[str, str]) -> bool:
    if env.get("DATA_PUBLICATION_APPROVED") != "true":
        return False
    if env.get("GITHUB_ACTIONS") != "true":
        return True
    return (
        env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") in {"push", "schedule", "workflow_dispatch"}
    )


def require_publication() -> None:
    if not publication_allowed(os.environ):
        raise BlockedError(
            "CODE ONLY: production processing is disabled. Source redistribution permission "
            "is unresolved; DATA_PUBLICATION_APPROVED must remain false until authorized. "
            "GitHub Actions additionally requires trusted main context."
        )


def download(url: str, *, allow_network: bool = False) -> bytes:
    require_publication()
    if not allow_network:
        raise BlockedError("Live collection requires the explicit --allow-network flag")
    if not url.startswith((HOLDINGS_API + "?", PRICE_API + "?")):
        raise DataError("Refusing a URL outside the implemented provider endpoints")
    request = Request(url, headers={"User-Agent": "igv-dashboard/1.0"})
    with urlopen(request, timeout=45) as response:
        # Bound malformed responses without logging or persisting their contents.
        content = response.read(32 * 1024 * 1024 + 1)
    if len(content) > 32 * 1024 * 1024:
        raise DataError("Provider response exceeds the size limit")
    return content


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()


def decode_object(content: bytes, kind: str) -> dict:
    try:
        value = json.loads(content)
    except (ValueError, UnicodeDecodeError) as error:
        raise DataError("Malformed JSON response") from error
    if not isinstance(value, dict):
        raise DataError("Expected a JSON object")
    if kind not in {PRODUCTION, SYNTHETIC}:
        raise DataError("Unknown archive data kind")
    marked = value.get("_synthetic_test_only") is True
    if marked != (kind == SYNTHETIC):
        raise DataError("Synthetic/production content mismatch")
    return value


def finite(value: object, label: str, *, positive: bool = False) -> float | int:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or (positive and value <= 0)
    ):
        raise DataError(f"Invalid finite numeric value: {label}")
    return value


def finite_sum(values, label: str) -> float:
    try:
        return finite(math.fsum(values), label)
    except OverflowError as error:
        raise DataError(f"Numeric overflow: {label}") from error


def parse_holdings(content: bytes, month: str, kind: str = PRODUCTION) -> dict:
    data = decode_object(content, kind)
    if type(data.get("productId")) is not int or data["productId"] != PRODUCT_ID:
        raise DataError(f"{month}: unexpected fund product ID")
    day = snapshot_date(month)
    try:
        points = data["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"][
            "dataPointsByNameMap"
        ]
        actual = points["asOfDate"]["value"]
        if type(actual) is not int or actual != int(day.strftime("%Y%m%d")):
            raise DataError(
                f"{month}: requested {day}, received a mismatched as-of date; "
                "refusing a stale/latest-date fallback"
            )
        columns = {field: points[key]["value"] for field, key in FIELDS.items()}
    except (KeyError, TypeError) as error:
        raise DataError(f"{month}: missing holdings columns or dated schema") from error
    if any(not isinstance(values, list) or len(values) < 10 for values in columns.values()):
        raise DataError(f"{month}: missing, empty or short holdings columns")
    if len({len(values) for values in columns.values()}) != 1:
        raise DataError(f"{month}: misaligned holdings columns")
    rows = [
        {field: values[index] for field, values in columns.items()}
        for index in range(len(columns["ticker"]))
    ]
    for row in rows:
        for field in ("weight_pct", "market_value_usd"):
            finite(row[field], field)
        for field in ("ticker", "name", "asset_class", "isin", "cusip"):
            if row[field] is not None and not isinstance(row[field], str):
                raise DataError(f"{month}: invalid text field {field}")
    ranked = sorted(
        rows,
        key=lambda row: (-row["weight_pct"], -row["market_value_usd"], row["ticker"] or ""),
    )
    top = ranked[:10]
    if any(
        not isinstance(row["ticker"], str)
        or not row["ticker"].strip()
        or not isinstance(row["name"], str)
        or not row["name"].strip()
        or row["weight_pct"] <= 0
        for row in top
    ):
        raise DataError(f"{month}: incomplete top-ten holding")
    if len({row["isin"] or row["cusip"] or row["ticker"] for row in top}) != 10:
        raise DataError(f"{month}: duplicated top-ten security")
    for rank, row in enumerate(top, 1):
        row["rank"] = rank
    total = finite_sum((row["weight_pct"] for row in rows), "portfolio weight total")
    notes = []
    if abs(total - 100) > 0.005:
        notes.append(
            f"Full published position weights sum to {total:.10g}%, not 100%. "
            "Weights are retained as supplied, without normalization."
        )
    return {
        "month": month,
        "snapshot_date": day.isoformat(),
        "holding_count": len(rows),
        "portfolio_weight_total_pct": total,
        "quality_notes": notes,
        "top10_weight_pct": finite_sum((row["weight_pct"] for row in top), "top-ten weight total"),
        "top10": top,
    }


def timestamp_day(value: object) -> date:
    finite(value, "timestamp")
    try:
        return datetime.fromtimestamp(value, NY).date()
    except (OverflowError, OSError, ValueError) as error:
        raise DataError("Invalid source timestamp") from error


def parse_prices(content: bytes, end: str, kind: str = PRODUCTION) -> dict:
    response = decode_object(content, kind)
    try:
        chart = response["chart"]
        if chart["error"] is not None or len(chart["result"]) != 1:
            raise DataError("Historical-price source returned an error or empty result")
        result = chart["result"][0]
        meta = result["meta"]
        if meta["symbol"] != SYMBOL or meta["currency"] != "USD":
            raise DataError("Historical-price source has an unexpected symbol or currency")
        timestamps = result["timestamp"]
        indicators = result["indicators"]
        if any(
            not isinstance(indicators[key], list) or len(indicators[key]) != 1
            for key in ("quote", "adjclose")
        ):
            raise DataError("Expected one coherent daily price series")
        quote = indicators["quote"][0]
        adjusted = indicators["adjclose"][0]["adjclose"]
        arrays = [quote[field] for field in PRICE_FIELDS] + [adjusted]
        if (
            not isinstance(timestamps, list)
            or not timestamps
            or any(
                not isinstance(values, list) or len(values) != len(timestamps) for values in arrays
            )
        ):
            raise DataError("Historical-price columns are empty or have different lengths")
    except (KeyError, TypeError, IndexError) as error:
        raise DataError("Missing historical-price schema") from error
    days = []
    for index, timestamp in enumerate(timestamps):
        day = timestamp_day(timestamp)
        if days and day.isoformat() <= days[-1]["date"]:
            raise DataError("Historical-price dates are duplicated or out of order")
        row = {field: quote[field][index] for field in PRICE_FIELDS}
        for field in ("open", "high", "low", "close"):
            finite(row[field], field, positive=True)
        volume = finite(row["volume"], "volume")
        if volume < 0 or int(volume) != volume:
            raise DataError("Volume must be a nonnegative integer")
        finite(adjusted[index], "adjclose", positive=True)
        if (
            not row["low"]
            <= min(row["open"], row["close"])
            <= max(row["open"], row["close"])
            <= row["high"]
        ):
            raise DataError(f"Inconsistent OHLC prices on {day}")
        days.append({"date": day.isoformat(), **row})
    first, last = price_bounds(end)
    expected = [day.isoformat() for day in trading_days(first, last)]
    actual = [row["date"] for row in days]
    if actual != expected:
        missing, extra = sorted(set(expected) - set(actual)), sorted(set(actual) - set(expected))
        raise DataError(
            "Price coverage must include every exchange session and the prior month-end "
            f"baseline; missing={','.join(missing[:5]) or 'none'}, "
            f"unexpected={','.join(extra[:5]) or 'none'}"
        )
    splits, dividends = {}, set()
    try:
        events = result.get("events", {})
        if not isinstance(events, dict):
            raise DataError("Malformed corporate-action events")
        split_events = events.get("splits", {})
        if not isinstance(split_events, dict):
            raise DataError("Malformed split events")
        for event in split_events.values():
            split_day = timestamp_day(event["date"]).isoformat()
            numerator = finite(event["numerator"], "split numerator", positive=True)
            denominator = finite(event["denominator"], "split denominator", positive=True)
            ratio = numerator / denominator
            finite(ratio, "split ratio", positive=True)
            if "splitRatio" in event:
                left, right = event["splitRatio"].split(":")
                declared = float(left) / float(right)
                if not math.isclose(ratio, declared, rel_tol=1e-9):
                    raise DataError("Split ratio fields disagree")
            if split_day not in expected or split_day in splits:
                raise DataError("Split event has a duplicate or out-of-window date")
            splits[split_day] = ratio
        dividend_events = events.get("dividends", {})
        if not isinstance(dividend_events, dict):
            raise DataError("Malformed dividend events")
        for event in dividend_events.values():
            event_day = timestamp_day(event["date"]).isoformat()
            amount = finite(event["amount"], "dividend amount")
            if amount < 0 or event_day not in expected or event_day in dividends:
                raise DataError("Dividend event has an invalid amount or date")
            dividends.add(event_day)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, AttributeError) as error:
        raise DataError("Invalid corporate-action event") from error
    factors = [
        finite(adjusted[index] / row["close"], "adjusted/quote ratio", positive=True)
        for index, row in enumerate(days)
    ]
    for index, row in enumerate(days[1:], 1):
        if row["date"] in dividends:
            if row["date"] in splits:
                raise DataError("Same-day split/dividend basis needs manual source verification")
            continue
        if not math.isclose(factors[index], factors[index - 1], rel_tol=2e-4):
            raise DataError(
                "Mixed price adjustment basis: adjusted/quote ratio changed without a "
                "cash distribution. Refusing potentially unadjusted split prices."
            )
    return {"days": days, "splits": splits, "through_month": end}


def check_price_revision(previous: dict, current: dict) -> None:
    old_rows = {row["date"]: row for row in previous["days"]}
    old_splits, new_splits = previous["splits"], current["splits"]
    first = current["days"][0]["date"]
    old_last = previous["days"][-1]["date"]
    if first > old_last:
        raise DataError("Price windows do not overlap; intermediate history is required")
    for day, ratio in old_splits.items():
        if first <= day <= old_last and new_splits.get(day) != ratio:
            raise DataError("A previously recorded split was removed or revised")
    additions = {day: ratio for day, ratio in new_splits.items() if day not in old_splits}
    if any(day <= old_last for day in additions):
        raise DataError("Unexplained retrospective split event; manual investigation required")
    for row in current["days"]:
        old = old_rows.get(row["date"])
        if old is None:
            continue
        factor = math.prod(ratio for day, ratio in additions.items() if row["date"] < day)
        finite(factor, "cumulative split ratio", positive=True)
        for field in ("open", "high", "low", "close"):
            if not math.isclose(row[field], old[field] / factor, rel_tol=1e-5, abs_tol=1e-6):
                raise DataError(
                    "Incoherent historical price adjustment or unexplained revision: "
                    f"{row['date']} {field}. Refusing to splice price bases."
                )


def performance_series(snapshots: list[dict], prices: dict) -> list[dict]:
    days = prices["days"]
    baseline = previous = days[0]
    by_month: dict[str, list[dict]] = {}
    for row in days[1:]:
        by_month.setdefault(row["date"][:7], []).append(row)
    series = []
    for item in snapshots:
        month_days = by_month.get(item["month"], [])
        if not month_days or month_days[-1]["date"] != item["snapshot_date"]:
            raise DataError(f"Price/holdings month-end mismatch for {item['month']}")
        last = month_days[-1]
        series.append(
            {
                "month": item["month"],
                "date": last["date"],
                "open": month_days[0]["open"],
                "high": max(row["high"] for row in month_days),
                "low": min(row["low"] for row in month_days),
                "close": last["close"],
                "previous_close": previous["close"],
                "monthly_return_pct": (last["close"] / previous["close"] - 1) * 100,
                "cumulative_return_pct": (last["close"] / baseline["close"] - 1) * 100,
                "volume": sum(row["volume"] for row in month_days),
                "trading_days": len(month_days),
            }
        )
        for field in PERFORMANCE_FIELDS[2:]:
            finite(series[-1][field], f"monthly {field}")
        previous = last
    return series


def object_path(root: Path, entry: dict) -> Path:
    digest = entry.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise DataError("Invalid provenance SHA256")
    relative = f"objects/{digest}.json"
    if entry.get("object") != relative:
        raise DataError("Invalid immutable archive object path")
    path = root / relative
    if path.is_symlink() or path.parent.resolve() != root.resolve() / "objects":
        raise DataError("Archive objects must not be symlinks or external paths")
    return path


def read_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if path.is_symlink():
        raise DataError("The manifest must not be a symlink")
    if not path.is_file():
        raise BlockedError(
            "No complete authorized archive: data/ contains schema only. "
            "Refusing to create a real-looking dashboard or substitute synthetic data."
        )
    try:
        manifest = json.loads(path.read_bytes())
    except (ValueError, UnicodeDecodeError) as error:
        raise DataError("Malformed archive manifest") from error
    if not isinstance(manifest, dict):
        raise DataError("Invalid archive manifest")
    return manifest


def validate_manifest(
    manifest: dict,
    root: Path,
    *,
    now: datetime | None = None,
    kind: str = PRODUCTION,
    require_current: bool = True,
    pending: dict[str, bytes] | None = None,
) -> dict:
    pending = pending or {}
    calendar_info = manifest.get("calendar")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("data_kind") != kind
        or manifest.get("ticker") != SYMBOL
        or type(manifest.get("product_id")) is not int
        or manifest.get("product_id") != PRODUCT_ID
        or not isinstance(calendar_info, dict)
        or calendar_info.get("name") != "XNYS"
        or calendar_info.get("library") != "exchange-calendars"
        or not isinstance(calendar_info.get("version"), str)
        or not calendar_info["version"]
    ):
        raise DataError("Archive schema, identity, calendar or data kind mismatch")
    end = manifest.get("data_through")
    month_start(end)
    latest = completed_month(now)
    if end > latest:
        raise DataError("Archive includes an unfinished or future month")
    if require_current and end != latest:
        raise DataError(f"Stale archive: data through {end}; latest completed month is {latest}")
    refreshed = parse_utc(manifest.get("last_successful_data_refresh_utc"))
    if refreshed > (now or utc_now()).astimezone(UTC):
        raise DataError("Archive refresh timestamp is in the future")
    holdings, price_entries = manifest.get("holdings"), manifest.get("prices")
    if not isinstance(holdings, dict) or len(holdings) < MONTH_COUNT:
        raise DataError("A complete archive needs at least 60 monthly holdings snapshots")
    months = sorted(holdings)
    if months != month_range(months[0], end):
        raise DataError("Archive has missing, duplicate or out-of-range holdings months")
    if not isinstance(price_entries, dict) or end not in price_entries:
        raise DataError("Missing coherent price archive for the latest holdings window")
    if any(month not in holdings for month in price_entries):
        raise DataError("Price archive has an unrecognized month")
    if min(price_entries) != shift_month(months[0], MONTH_COUNT - 1):
        raise DataError("Price archive must cover the earliest retained holdings window")

    def content(entry: dict, expected_url: str) -> bytes:
        if not isinstance(entry, dict):
            raise DataError("Malformed provenance entry")
        path = object_path(root, entry)
        raw = pending[entry["sha256"]] if entry["sha256"] in pending else path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise DataError("Archive object SHA256 mismatch")
        if entry.get("source_url") != expected_url:
            raise DataError("Provenance source URL does not match the requested source")
        retrieved = parse_utc(entry.get("retrieved_at_utc"))
        if retrieved > refreshed:
            raise DataError("Retrieval time is later than the successful archive refresh")
        return raw

    parsed_holdings = {}
    for month in months:
        entry = holdings[month]
        raw = content(entry, source_url(month, kind))
        expected_date = snapshot_date(month).isoformat()
        if entry.get("requested_date") != expected_date or entry.get("as_of_date") != expected_date:
            raise DataError("Holdings provenance date mismatch")
        if parse_utc(entry["retrieved_at_utc"]).astimezone(NY).date().isoformat() < expected_date:
            raise DataError("Holdings retrieval predates the reported snapshot")
        item = parse_holdings(raw, month, kind)
        parsed_holdings[month] = {
            **item,
            "source_url": entry["source_url"],
            "source_sha256": entry["sha256"],
            "retrieved_at_utc": entry["retrieved_at_utc"],
        }
    current_prices = None
    previous_prices = None
    for month in sorted(price_entries):
        entry = price_entries[month]
        raw = content(entry, price_url(month, kind))
        first, last = price_bounds(month)
        if (
            entry.get("requested_start_date") != first.isoformat()
            or entry.get("requested_end_date") != last.isoformat()
            or entry.get("as_of_date") != last.isoformat()
            or entry.get("price_basis") != "provider-split-adjusted-quote"
        ):
            raise DataError("Price provenance dates or adjustment basis mismatch")
        parsed = parse_prices(raw, month, kind)
        if previous_prices is not None:
            check_price_revision(previous_prices, parsed)
        previous_prices = parsed
        if month == end:
            current_prices = parsed
    snapshots = [parsed_holdings[month] for month in display_months(end)]
    if current_prices is None:
        raise DataError("Missing active price window")
    series = performance_series(snapshots, current_prices)
    if len(snapshots) != 60 or sum(len(item["top10"]) for item in snapshots) != 600:
        raise DataError("Report must contain 60 months and 600 top-ten records")
    return {"snapshots": snapshots, "prices": current_prices, "series": series}


def fetch_validated(
    fetcher: Callable[[str], bytes],
    url: str,
    parse: Callable[[bytes], dict],
    *,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bytes, dict]:
    for attempt in range(attempts):
        try:
            raw = fetcher(url)
            return raw, parse(raw)
        except HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504}:
                raise DataError(f"Provider HTTP {error.code}; no alternative date used") from error
            failure = error
        except (DataError, URLError, TimeoutError, ConnectionError) as error:
            failure = error
        if attempt + 1 < attempts:
            print(f"Source attempt {attempt + 1} rejected; retrying exact request", file=sys.stderr)
            sleep(2**attempt)
    raise DataError(
        f"Source unavailable or invalid after {attempts} attempts: {failure}. "
        "Last-good archive retained; retry after the issuer publishes the exact snapshot."
    ) from failure


def provenance(raw: bytes, url: str, retrieved: datetime, **dates: str) -> dict:
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "object": f"objects/{digest}.json",
        "sha256": digest,
        "source_url": url,
        "retrieved_at_utc": utc_text(retrieved),
        **dates,
    }


def promote_archive(root: Path, manifest: dict, pending: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    added = []
    committed = False
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=root) as temporary:
        stage = Path(temporary)
        for digest, raw in pending.items():
            (stage / f"{digest}.json").write_bytes(raw)
        staged_manifest = stage / "manifest.json"
        staged_manifest.write_bytes(json_bytes(manifest))
        (root / "objects").mkdir(exist_ok=True)
        try:
            for digest, raw in pending.items():
                target = root / "objects" / f"{digest}.json"
                if target.exists():
                    if target.read_bytes() != raw:
                        raise DataError("An immutable archive object was modified")
                    continue
                os.replace(stage / f"{digest}.json", target)
                added.append(target)
            # Readers use only this pointer. Existing objects are never overwritten.
            os.replace(staged_manifest, root / "manifest.json")
            committed = True
        finally:
            if not committed:
                for path in added:
                    path.unlink()


def refresh_archive(
    root: Path,
    fetcher: Callable[[str], bytes],
    *,
    kind: str = PRODUCTION,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    if kind == PRODUCTION:
        require_publication()
    elif kind != SYNTHETIC:
        raise DataError("Unknown archive data kind")
    now = clock()
    latest = completed_month(now)
    existing = read_manifest(root) if (root / "manifest.json").exists() else None
    previous = None
    if existing is not None:
        previous = validate_manifest(existing, root, now=now, kind=kind, require_current=False)
        if existing["data_through"] == latest:
            return False
        needed = month_range(shift_month(existing["data_through"], 1), latest)
        manifest = json.loads(json_bytes(existing))
    else:
        needed = display_months(latest)
        manifest = {
            "schema_version": 1,
            "data_kind": kind,
            "ticker": SYMBOL,
            "product_id": PRODUCT_ID,
            "holdings": {},
            "prices": {},
        }
    pending = {}
    for month in needed:
        url = source_url(month, kind)
        raw, _ = fetch_validated(
            fetcher, url, lambda content: parse_holdings(content, month, kind), sleep=sleep
        )
        day = snapshot_date(month).isoformat()
        entry = provenance(raw, url, clock(), requested_date=day, as_of_date=day)
        pending[entry["sha256"]] = raw
        manifest["holdings"][month] = entry
    price_ends = []
    if existing is not None:
        boundary = shift_month(existing["data_through"], MONTH_COUNT)
        while boundary < latest:
            price_ends.append(boundary)
            boundary = shift_month(boundary, MONTH_COUNT)
    price_ends.append(latest)
    previous_prices = previous["prices"] if previous is not None else None
    for end in price_ends:
        url = price_url(end, kind)
        raw, parsed_prices = fetch_validated(
            fetcher, url, lambda content: parse_prices(content, end, kind), sleep=sleep
        )
        if previous_prices is not None:
            check_price_revision(previous_prices, parsed_prices)
        previous_prices = parsed_prices
        first, last = price_bounds(end)
        entry = provenance(
            raw,
            url,
            clock(),
            requested_start_date=first.isoformat(),
            requested_end_date=last.isoformat(),
            as_of_date=last.isoformat(),
            price_basis="provider-split-adjusted-quote",
        )
        pending[entry["sha256"]] = raw
        manifest["prices"][end] = entry
    manifest["data_through"] = latest
    manifest["last_successful_data_refresh_utc"] = utc_text(clock())
    manifest["calendar"] = {
        "name": "XNYS",
        "library": "exchange-calendars",
        "version": version("exchange-calendars"),
    }
    validate_manifest(manifest, root, now=clock(), kind=kind, pending=pending)
    promote_archive(root, manifest, pending)
    return True


def report_payload(manifest: dict, validated: dict) -> dict:
    end, kind = manifest["data_through"], manifest["data_kind"]
    first = display_months(end)[0]
    entry = manifest["prices"][end]
    baseline = validated["prices"]["days"][0]
    synthetic = kind == SYNTHETIC
    prefix = "SYNTHETIC_TEST_ONLY" if synthetic else SYMBOL
    stem = f"{prefix}_top10_monthly_{first}_to_{end}"
    return {
        "data_kind": kind,
        "ticker": "TEST ONLY" if synthetic else SYMBOL,
        "fund_name": (
            "Synthetic test fund - not IGV investment data"
            if synthetic
            else "iShares Expanded Tech-Software Sector ETF"
        ),
        "product_url": "https://example.invalid/synthetic/" if synthetic else PRODUCT_URL,
        "period_start": first,
        "period_end": end,
        "data_through": end,
        "last_successful_data_refresh_utc": manifest["last_successful_data_refresh_utc"],
        "downloads": {name: f"{stem}_{name}.csv" for name in ("wide", "long", "performance")},
        "methodology": (
            f"Last XNYS exchange session of every completed calendar month, {first} through "
            f"{end}, using exchange-calendars {manifest['calendar']['version']}. Holdings "
            "are ranked by unrounded source portfolio weight, with no asset-class exclusions. "
            "Weights are percentages of the whole fund, never normalized to the top ten. "
            "Historical tickers and names are retained as supplied. No interpolation, "
            "forward-filling, estimated months or current-holdings substitutions."
        ),
        "snapshots": validated["snapshots"],
        "performance": {
            "source_name": "Synthetic test generator" if synthetic else "Yahoo Finance",
            "source_url": entry["source_url"],
            "history_url": (
                "https://example.invalid/synthetic/"
                if synthetic
                else "https://finance.yahoo.com/quote/IGV/history/"
            ),
            "currency": "USD",
            "baseline_date": baseline["date"],
            "baseline_close": baseline["close"],
            "source_sha256": entry["sha256"],
            "retrieved_at_utc": entry["retrieved_at_utc"],
            "methodology": (
                f"Daily quote OHLC fields aggregated by exchange-local month, with the first "
                f"return measured from the {baseline['date']} closing-price baseline. "
                "Quote prices follow the provider's split-adjusted convention; dividend-"
                "adjusted adjclose is not used. Cash distributions are excluded: these are "
                "price returns, not total returns. OHLC is first open, highest high, lowest "
                "low and final close; volume sums the supplied daily field without estimation "
                "or rescaling. Every expected exchange session is required. Each refresh "
                "replaces the whole active price window, never splices adjustment bases, "
                "and checks overlapping OHLC against recorded splits. Initial adjustment "
                "semantics rely on the provider's convention, not an independent price audit."
            ),
            "series": validated["series"],
        },
    }


def csv_text(headers: list[str], rows: list[list]) -> str:
    def safe(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        if isinstance(value, str) and value.startswith(("\t", "\r", "\n")):
            return "'" + value
        return value

    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows([safe(value) for value in row] for row in rows)
    return stream.getvalue()


def export_csvs(report: dict) -> dict[str, str]:
    snapshots, kind = report["snapshots"], report["data_kind"]
    long_fields = ["rank", *FIELDS]
    long_headers = ["month", "snapshot_date", *long_fields, "source_url", "data_kind"]
    long_rows = [
        [
            item["month"],
            item["snapshot_date"],
            *[row[field] for field in long_fields],
            item["source_url"],
            kind,
        ]
        for item in snapshots
        for row in item["top10"]
    ]
    wide_headers = [
        "month",
        "snapshot_date",
        *[f"rank_{rank}_ticker_and_weight_pct" for rank in range(1, 11)],
        "top10_weight_pct",
        "source_url",
        "data_kind",
    ]
    wide_rows = [
        [
            item["month"],
            item["snapshot_date"],
            *[f"{row['ticker']} {row['weight_pct']:.2f}%" for row in item["top10"]],
            item["top10_weight_pct"],
            item["source_url"],
            kind,
        ]
        for item in snapshots
    ]
    performance_rows = [
        [*[row[field] for field in PERFORMANCE_FIELDS], report["performance"]["source_url"], kind]
        for row in report["performance"]["series"]
    ]
    return {
        "wide": csv_text(wide_headers, wide_rows),
        "long": csv_text(long_headers, long_rows),
        "performance": csv_text([*PERFORMANCE_FIELDS, "source_url", "data_kind"], performance_rows),
    }


def build_site(
    root: Path,
    output: Path,
    *,
    test_only: bool = False,
    now: datetime | None = None,
) -> dict:
    if test_only:
        if (
            output.resolve() == (ROOT / "site").resolve()
            or ROOT.resolve() in output.resolve().parents
        ):
            raise BlockedError(
                "Synthetic sites must be built in a temporary directory outside the repo"
            )
    else:
        require_publication()
    if output.resolve() == root.resolve() or root.resolve() in output.resolve().parents:
        raise DataError("Site output must be separate from the source archive")
    kind = SYNTHETIC if test_only else PRODUCTION
    manifest = read_manifest(root)
    validated = validate_manifest(manifest, root, now=now, kind=kind)
    report = report_payload(manifest, validated)
    template = (ROOT / "igv_snapshot_template.html").read_text(encoding="utf-8")
    title_prefix = "SYNTHETIC TEST ONLY" if test_only else SYMBOL
    replacements = {
        "__TEST_VISIBILITY__": "" if test_only else "hidden",
        "__IGV_DATA__": json.dumps(report, ensure_ascii=True, allow_nan=False).replace(
            "<", "\\u003c"
        ),
        "__IGV_TITLE__": html.escape(
            f"{title_prefix} | Performance and monthly holdings | "
            f"{report['period_start']} - {report['period_end']}"
        ),
        **{f"__CSV_{key.upper()}__": value for key, value in report["downloads"].items()},
    }
    for marker, replacement in replacements.items():
        if template.count(marker) != 1:
            raise DataError(f"HTML template needs exactly one {marker} placeholder")
        template = template.replace(marker, replacement)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".site-stage-", dir=output.parent) as temporary:
        stage = Path(temporary)
        for kind, text in export_csvs(report).items():
            (stage / report["downloads"][kind]).write_text(text, encoding="utf-8-sig", newline="")
        (stage / ".nojekyll").write_text("", encoding="utf-8")
        (stage / "index.html").write_text(template, encoding="utf-8")
        output.mkdir(parents=True, exist_ok=True)
        for filename in [*report["downloads"].values(), ".nojekyll", "index.html"]:
            # The entry point is promoted last, only after all its downloads exist.
            os.replace(stage / filename, output / filename)
    return report


def print_summary(manifest: dict, validated: dict) -> None:
    print(
        f"Data through {manifest['data_through']}; 60 months / 600 ranked records. "
        f"Last successful data refresh: {manifest['last_successful_data_refresh_utc']}"
    )
    for item in validated["snapshots"]:
        for note in item["quality_notes"]:
            print(f"WARNING {item['month']}: {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("refresh", "validate", "build"))
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=ROOT / "site")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--test-only", action="store_true", help="Offline synthetic archives only")
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            if args.test_only:
                raise BlockedError("Synthetic refresh is test-injected only; no network allowed")
            require_publication()
            if not args.allow_network:
                raise BlockedError("Live collection requires --allow-network")
            changed = refresh_archive(
                args.data_dir, lambda url: download(url, allow_network=args.allow_network)
            )
            print(
                "Validated archive updated." if changed else "No new completed month; no changes."
            )
        elif args.allow_network:
            raise BlockedError("--allow-network is only valid with refresh")
        if args.command == "build":
            build_site(args.data_dir, args.output, test_only=args.test_only)
            print("Built index.html and three relative CSV downloads.")
        manifest = read_manifest(args.data_dir)
        validated = validate_manifest(
            manifest, args.data_dir, kind=SYNTHETIC if args.test_only else PRODUCTION
        )
        print_summary(manifest, validated)
        return 0
    except (DataError, OSError) as error:
        label = "BLOCKED" if isinstance(error, BlockedError) else "FAILED"
        print(f"{label}: {error}", file=sys.stderr)
        return 2 if isinstance(error, BlockedError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
