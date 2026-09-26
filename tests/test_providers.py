import copy
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.message import Message
from urllib.error import HTTPError, URLError
from xml.etree.ElementTree import fromstring, tostring

import pytest

import sec_nport as sec
from tests.synthetic import TEST_END, TEST_NOW, Provider, filing, holdings, identity, index, mapping

NS = "{" + sec.NAMESPACE + "}"


def xml_root(raw=None):
    return fromstring(raw or holdings(filing()))


def node(root, name):
    return root.find(".//" + NS + name)


def parse(root=None, *, item=None, raw=None):
    item = item or filing()
    raw = raw if raw is not None else tostring(root if root is not None else xml_root())
    return sec.normalize_filing(
        raw,
        item,
        identity(),
        sec.index_metadata(index(item), item, sec.SYNTHETIC),
        TEST_NOW,
        sec.SYNTHETIC,
    )


def test_only_normalized_facts_survive():
    result = parse()
    serialized = sec.json_bytes(result)
    assert b"SYNTHETIC NARRATIVE" not in serialized
    assert b"<edgarSubmission" not in serialized
    assert b"signature" not in serialized
    assert set(result) == sec.SNAPSHOT_KEYS
    assert result["source_row_count"] == 22
    assert result["net_assets_usd"] == "1000000.123456789012"
    for field in ("source_sha256", "filing_index_sha256"):
        sec.check_hash(result["provenance"][field])


def test_exact_source_weights_and_unrounded_ranking():
    root = xml_root()
    rows = node(root, "invstOrSecs")
    node(rows[0], "pctVal").text = "30.000000000002"
    node(rows[1], "pctVal").text = "30.000000000003"
    result = parse(root)
    ranked = sec.ranked_snapshot(result, "a" * 64)
    assert [row["source_row"] for row in ranked["top10"][:2]] == [2, 1]
    assert ranked["top10"][0]["weight_pct"] == "30.000000000003"
    assert Decimal(ranked["top10_weight_pct"]) == sum(
        (Decimal(row["weight_pct"]) for row in ranked["top10"]), Decimal(0)
    )


def test_ranking_does_not_round_36_digit_values():
    result = parse()
    first, second = result["holdings"][:2]
    first["weight_pct"] = "100000000000000000000000.000000000001"
    second["weight_pct"] = "100000000000000000000000.000000000002"
    assert sec.ranked_snapshot(result, "a" * 64)["top10"][0]["source_row"] == 2


def test_ties_use_value_then_source_order_without_ticker_assumptions():
    result = parse()
    for row in result["holdings"]:
        row["weight_pct"] = "1"
        row["market_value_usd"] = "100"
    result["holdings"][5]["market_value_usd"] = "101"
    ranked = sec.ranked_snapshot(result, "a" * 64)
    assert [row["source_row"] for row in ranked["top10"][:3]] == [6, 1, 2]


def test_missing_historic_tickers_and_non_top_identifiers_are_preserved():
    result = parse()
    assert result["holdings"][1]["ticker"] == []
    assert result["holdings"][1]["isin"] == ["TEST00000001"]
    assert result["holdings"][-1]["cusip"] is None
    assert result["holdings"][-1]["lei"] is None
    assert result["holdings"][-1]["isin"] == []
    assert result["holdings"][-1]["weight_pct"] == "-0.25"
    assert result["holdings"][-2]["weight_pct"] == "0"
    assert "no reported ticker" in " ".join(sec.ranked_snapshot(result, "a" * 64)["quality_notes"])


def test_unusual_totals_are_reported_without_renormalization():
    item = filing()
    result = parse(raw=holdings(item, allocation_delta="2"))
    ranked = sec.ranked_snapshot(result, "a" * 64)
    assert abs(Decimal(ranked["portfolio_weight_pct"]) - 102) < Decimal("0.000000001")
    assert "without normalization" in ranked["quality_notes"][0]
    assert ranked["top10"][0]["weight_pct"] == result["holdings"][0]["weight_pct"]


def test_conditional_categories_currency_and_row_types():
    root = xml_root()
    row = node(root, "invstOrSec")
    for simple, conditional, attrs in [
        ("assetCat", "assetConditional", {"assetCat": "OTHER", "desc": "Synthetic test asset"}),
        ("issuerCat", "issuerConditional", {"issuerCat": "OTHER", "desc": "Synthetic test issuer"}),
        ("curCd", "currencyConditional", {"curCd": "EUR", "exchangeRt": "1.1"}),
    ]:
        element = node(row, simple)
        element.tag, element.attrib, element.text = NS + conditional, attrs, None
    result = parse(root)
    assert result["holdings"][0]["asset_category"] == "OTHER"
    assert result["holdings"][0]["currency"] == "EUR"
    assert "Synthetic test asset" not in json.dumps(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("regCik", "9999"),
        ("seriesId", "S000999998"),
        ("seriesName", "Another fund"),
        ("repPdDate", "2031-10-31"),
        ("repPdEnd", "2033-10-31"),
        ("submissionType", "NPORT-NP"),
        ("isFinalFiling", "Y"),
        ("netAssets", "0"),
        ("pctVal", "NaN"),
        ("pctVal", "Infinity"),
        ("pctVal", "1e2"),
        ("valUSD", "not a number"),
        ("balance", ""),
        ("name", ""),
    ],
)
def test_source_mismatches_and_malformed_fields_fail(field, value):
    root = xml_root()
    node(root, field).text = value
    with pytest.raises(sec.DataError):
        parse(root)


@pytest.mark.parametrize("field", ["genInfo", "fundInfo", "invstOrSecs", "pctVal", "identifiers"])
def test_missing_or_duplicate_required_elements_fail(field):
    root = xml_root()
    parent = next(
        parent for parent in root.iter() if any(child.tag == NS + field for child in parent)
    )
    parent.remove(node(parent, field))
    with pytest.raises(sec.DataError):
        parse(root)
    root = xml_root()
    parent = next(
        parent for parent in root.iter() if any(child.tag == NS + field for child in parent)
    )
    parent.append(copy.deepcopy(node(parent, field)))
    with pytest.raises(sec.DataError):
        parse(root)


def test_duplicate_rows_and_nondisseminated_rows_fail():
    root = xml_root()
    schedule = node(root, "invstOrSecs")
    schedule.append(copy.deepcopy(schedule[0]))
    with pytest.raises(sec.DataError, match="Duplicate complete"):
        parse(root)
    root = xml_root()
    row = node(root, "invstOrSec")
    row.append(fromstring(f'<notDissem xmlns="{sec.NAMESPACE}">true</notDissem>'))
    with pytest.raises(sec.DataError, match="nondisseminated"):
        parse(root)


@pytest.mark.parametrize(
    "raw",
    [
        b"not XML",
        b"<html>access denied</html>",
        b'<!DOCTYPE x [<!ENTITY x "expanded">]><x>&x;</x>',
        b'<!DOCTYPE x SYSTEM "file:///etc/passwd"><x/>',
    ],
)
def test_bad_or_unsafe_xml_is_not_a_portfolio(raw):
    with pytest.raises(sec.DataError):
        parse(raw=raw)


def test_source_bounds_and_future_dates():
    with pytest.raises(sec.DataError, match="limit"):
        parse(raw=b"x" * (sec.MAX_BYTES + 1))
    item = filing()
    with pytest.raises(sec.DataError, match="Future"):
        sec.normalize_filing(
            holdings(item),
            item,
            identity(),
            sec.index_metadata(index(item), item, sec.SYNTHETIC),
            datetime(2031, 9, 1, tzinfo=UTC),
            sec.SYNTHETIC,
        )


def test_synthetic_markers_cannot_be_upgraded_to_production():
    item = filing()
    with pytest.raises(sec.DataError, match="cannot be mixed"):
        sec.resolve_identity(mapping(), TEST_NOW, sec.PRODUCTION)
    with pytest.raises(sec.DataError, match="cannot be mixed"):
        sec.normalize_filing(
            holdings(item),
            item,
            identity(),
            sec.index_metadata(index(item), item, sec.SYNTHETIC),
            TEST_NOW,
            sec.PRODUCTION,
        )


@pytest.mark.parametrize(
    "case", ["wrong-cik", "duplicate", "no-match", "wrong-series", "misaligned"]
)
def test_fund_mapping_needs_one_exact_class(case):
    data = json.loads(mapping())
    if case == "wrong-cik":
        data["data"][1][0] = 9999
    elif case == "duplicate":
        data["data"].append(data["data"][1])
    elif case == "no-match":
        data["data"] = data["data"][:1]
    elif case == "wrong-series":
        data["data"][1][1] = "IGV"
    else:
        data["data"][0].pop()
    with pytest.raises(sec.DataError):
        sec.resolve_identity(sec.json_bytes(data), TEST_NOW, sec.SYNTHETIC)


def test_official_index_acceptance_is_eastern_converted_to_utc():
    item = filing()
    metadata = sec.index_metadata(index(item), item, sec.SYNTHETIC)
    assert metadata["accepted_at_utc"] == "2031-09-14T16:00:00Z"
    winter = filing("2030-10-31")
    assert sec.index_metadata(index(winter), winter, sec.SYNTHETIC)["accepted_at_utc"].endswith(
        "17:00:00Z"
    )


def test_acceptance_can_precede_official_filing_date():
    item = filing()
    raw = index(item).replace(b"2031-09-14 12:00:00", b"2031-09-13 18:30:00")
    metadata = sec.index_metadata(raw, item, sec.SYNTHETIC)
    assert metadata["accepted_at_utc"] == "2031-09-13T22:30:00Z"
    sec.normalize_filing(holdings(item), item, identity(), metadata, TEST_NOW, sec.SYNTHETIC)


@pytest.mark.parametrize("case", ["date", "period", "accepted", "link", "duplicate-acceptance"])
def test_index_provenance_is_required(case):
    item = filing()
    raw = index(item)
    if case == "date":
        raw = raw.replace(b"2031-09-14", b"2031-09-13")
    elif case == "period":
        raw = raw.replace(TEST_END.encode(), b"2031-10-31")
    elif case == "accepted":
        raw = raw.replace(b"12:00:00", b"not a timestamp")
    elif case == "link":
        raw = raw.replace(b"primary_doc.xml", b"other.xml")
    else:
        raw += b'<div class="infoHead">Accepted</div><div class="info">2031-09-14 12:00:00</div>'
    with pytest.raises(sec.DataError):
        sec.index_metadata(raw, item, sec.SYNTHETIC)


def test_discovery_paginates_and_returns_amendments():
    provider = Provider(count=120)
    provider.add(TEST_END, amendment=1, filed="2031-09-15")
    found = sec.discover_filings(provider, identity(), "1990-01-01", "2031-09-15", sec.SYNTHETIC)
    assert len(found) == 121 and found[-1].form == "NPORT-P/A"
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "case", ["timeout", "truncated", "broad", "missing", "wrong-fund", "duplicate"]
)
def test_discovery_fails_closed_on_incomplete_or_wrong_results(case):
    provider = Provider()

    def malformed(url):
        result = json.loads(provider(url))
        if case == "timeout":
            result["timed_out"] = True
        elif case == "truncated":
            result["hits"]["total"]["relation"] = "gte"
        elif case == "broad":
            result["hits"]["total"]["value"] = 501
        elif case == "missing":
            result["hits"]["hits"].pop()
        elif case == "wrong-fund":
            result["hits"]["hits"][0]["_source"]["ciks"] = ["9999"]
        else:
            result["hits"]["hits"][1] = result["hits"]["hits"][0]
        return sec.json_bytes(result)

    with pytest.raises(sec.DataError):
        sec.discover_filings(malformed, identity(), "2025-01-01", "2031-09-15", sec.SYNTHETIC)


@pytest.mark.parametrize(
    "url",
    [
        "https://query1.finance.yahoo.com/v8/finance/chart/IGV",
        "https://www.blackrock.com/varnish-api/data",
        "https://www.ishares.com/us/products/239771",
        "https://example.invalid/synthetic/file.xml",
        "https://www.sec.gov.evil.invalid/Archives/edgar/data/1100663/file.xml",
        "http://www.sec.gov/files/company_tickers_mf.json",
        "https://www.sec.gov/Archives/edgar/data/999/123456789012345678/primary_doc.xml",
    ],
)
def test_network_allowlist_rejects_all_other_sources(url):
    client = sec.SECClient("igv-dashboard synthetic operator@example.invalid")
    with pytest.raises(sec.DataError, match="outside"):
        client(url)


def http_error(code, retry=None):
    headers = Message()
    if retry is not None:
        headers["Retry-After"] = retry
    return HTTPError(sec.MAPPING_URL, code, "Synthetic HTTP error", headers, None)


def client_for(responses):
    calls, waits = [], []
    responses = iter(responses)

    def open_url(request, timeout):
        calls.append(request.full_url)
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response)

    return (
        sec.SECClient(
            "igv-dashboard synthetic operator@example.invalid",
            open_url=open_url,
            sleep=waits.append,
            monotonic=lambda: 0,
            clock=lambda: TEST_NOW,
        ),
        calls,
        waits,
    )


def test_transient_retries_preserve_request_and_retry_after():
    client, calls, waits = client_for([http_error(429, "12"), URLError("test"), b"{}"])
    assert client(sec.MAPPING_URL) == b"{}"
    assert calls == [sec.MAPPING_URL] * 3
    assert waits == [12, 1, 4, 1]


def test_retry_after_http_date():
    later = (TEST_NOW + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    client, calls, waits = client_for([http_error(503, later), b"{}"])
    assert client(sec.MAPPING_URL) == b"{}"
    assert waits[0] == 30 and len(calls) == 2


@pytest.mark.parametrize("code", [401, 403, 404])
def test_permanent_errors_never_retry_or_change_identity(code):
    client, calls, waits = client_for([http_error(code)])
    with pytest.raises(sec.DataError):
        client(sec.MAPPING_URL)
    assert len(calls) == 1 and waits == []


@pytest.mark.parametrize("retry", ["121", "not a delay"])
def test_retry_after_beyond_budget_or_invalid_defers_without_early_request(retry):
    client, calls, waits = client_for([http_error(429, retry)])
    with pytest.raises(sec.DataError, match="Retry-After"):
        client(sec.MAPPING_URL)
    assert len(calls) == 1 and waits == []


def test_bounded_retries_reads_and_throttle():
    client, calls, _ = client_for([TimeoutError()] * 3)
    with pytest.raises(sec.DataError, match="three"):
        client(sec.MAPPING_URL)
    assert len(calls) == 3
    client, calls, _ = client_for([b"x" * (sec.MAX_BYTES + 1)])
    with pytest.raises(sec.DataError, match="limit"):
        client(sec.MAPPING_URL)
    client, calls, waits = client_for([b"{}", b"{}"])
    client(sec.MAPPING_URL)
    client(sec.MAPPING_URL)
    assert waits == [1]


def test_no_identification_fallback_or_redirects():
    for value in ["", "impersonated-browser", "igv-dashboard " + "\r\n" + "injected"]:
        with pytest.raises(sec.BlockedError):
            sec.SECClient(value)
    with pytest.raises(sec.DataError, match="redirect"):
        sec.NoRedirects().redirect_request(None, None, 302, "", {}, "https://example.invalid")


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "1e9", "1,000", "1.0000000000001", "9" * 37, 1, None]
)
def test_numeric_contract(value):
    with pytest.raises(sec.DataError):
        sec.decimal(value)
