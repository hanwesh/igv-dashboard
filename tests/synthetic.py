"""Invented N-PORT-shaped fixtures, not actual SEC filings or observations of IGV."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from urllib.parse import parse_qs, urlparse
from xml.etree.ElementTree import Element, SubElement, tostring

import igv_snapshot as app
import sec_nport as sec

TEST_NOW = datetime(2031, 9, 15, 12, tzinfo=UTC)
TEST_END = "2031-07-31"
TEST_SERIES = "S000999999"
TEST_CLASS = "C000999999"


def mapping() -> bytes:
    return sec.json_bytes(
        {
            "_synthetic_test_only": True,
            "fields": ["cik", "seriesId", "classId", "symbol"],
            "data": [
                [int(sec.CIK), "S000999998", "C000999998", "TEST-NOT-IGV"],
                [int(sec.CIK), TEST_SERIES, TEST_CLASS, sec.SYMBOL],
            ],
        }
    )


def identity(now=TEST_NOW):
    return sec.resolve_identity(mapping(), now, sec.SYNTHETIC)


def filing(period=TEST_END, *, amendment=0, filed=None) -> sec.Filing:
    as_of = sec.parse_date(period)
    accession = (
        f"0000999999-{as_of.year % 100:02d}-{as_of.month * 10000 + as_of.day * 100 + amendment:06d}"
    )
    return sec.Filing(
        accession,
        "NPORT-P/A" if amendment else "NPORT-P",
        filed or (as_of + timedelta(days=45)).isoformat(),
        "primary_doc.xml",
        period,
    )


def index(item: sec.Filing) -> bytes:
    source = sec.source_url(item.accession, item.filename, sec.SYNTHETIC)
    return (
        '<!doctype html><html><body><div class="infoHead">Filing Date</div>'
        f'<div class="info">{item.filing_date}</div><div class="infoHead">Accepted</div>'
        f'<div class="info">{item.filing_date} 12:00:00</div>'
        '<div class="infoHead">Period of Report</div>'
        f'<div class="info">{item.reported_date_hint}</div><a href="{urlparse(source).path}">'
        "Synthetic XML</a></body></html>"
    ).encode()


def holdings(
    item: sec.Filing, *, allocation_delta="0", weight_overrides: dict[int, str] | None = None
) -> bytes:
    root = Element("edgarSubmission", {"xmlns": sec.NAMESPACE, "synthetic-test-only": "true"})

    def node(parent, key, value):
        element = SubElement(parent, key)
        element.text = value
        return element

    header = SubElement(root, "headerData")
    node(header, "submissionType", item.form)
    form = SubElement(root, "formData")
    general = SubElement(form, "genInfo")
    reported = sec.parse_date(item.reported_date_hint)
    year = reported.year + (1 if reported.month > 10 else 0)
    for key, value in {
        "regCik": sec.CIK,
        "seriesId": TEST_SERIES,
        "seriesName": sec.FUND_NAME,
        "repPdEnd": f"{year}-10-31",
        "repPdDate": item.reported_date_hint,
        "isFinalFiling": "N",
    }.items():
        node(general, key, value)
    fund = SubElement(form, "fundInfo")
    node(fund, "netAssets", "1000000.123456789012")
    schedule = SubElement(form, "invstOrSecs")
    with localcontext() as context:
        context.prec = 50
        weights = [
            ((20 - number) * Decimal(100) / 210).quantize(Decimal("0.000000000001"))
            for number in range(20)
        ]
        weights[0] += Decimal("0.25") + Decimal(allocation_delta)
        weights += [Decimal("0"), Decimal("-0.25")]
        for number, weight in enumerate(weights):
            if weight_overrides and number in weight_overrides:
                weight = Decimal(weight_overrides[number])
            row = SubElement(schedule, "invstOrSec")
            for key, value in {
                "name": f"Synthetic Test Company {number:02d}",
                "lei": "N/A",
                "title": f"Synthetic Test Security {number:02d}",
                "cusip": f"TEST{number:05d}" if number < 20 else "N/A",
            }.items():
                node(row, key, value)
            identifiers = SubElement(row, "identifiers")
            if number < 20:
                SubElement(identifiers, "isin", value=f"TEST{number:08d}")
                if number % 2 == 0:
                    SubElement(identifiers, "ticker", value=f"TEST{number:02d}")
            else:
                SubElement(identifiers, "other", value="N/A", otherDesc="N/A")
            for key, value in {
                "balance": str(number * 100),
                "units": "NS",
                "curCd": "USD",
                "valUSD": format(weight * 10000, "f"),
                "pctVal": format(weight, "f"),
                "payoffProfile": "Long",
                "assetCat": "STIV" if number in {6, 20} else "EC",
                "issuerCat": "RF" if number in {6, 20} else "CORP",
            }.items():
                node(row, key, value)
            lending = SubElement(row, "securityLending")
            if number == 6:
                SubElement(
                    lending,
                    "cashCollateralCondition",
                    isCashCollateral="Y",
                    cashCollateralVal=format(weight * 10000, "f"),
                )
            else:
                node(lending, "isCashCollateral", "N")
            node(lending, "isNonCashCollateral", "N")
            if number % 2 == 0 and number not in {6, 20}:
                SubElement(lending, "loanByFundCondition", isLoanByFund="Y", loanVal="12.345")
            else:
                node(lending, "isLoanByFund", "N")
    notes = SubElement(form, "explntrNotes")
    node(notes, "note", "SYNTHETIC NARRATIVE MUST NEVER BE ARCHIVED")
    return tostring(root, encoding="utf-8", xml_declaration=True)


def normalized(item=None, *, now=TEST_NOW, allocation_delta="0"):
    item = item or filing()
    metadata = sec.index_metadata(index(item), item, sec.SYNTHETIC)
    return sec.normalize_filing(
        holdings(item, allocation_delta=allocation_delta),
        item,
        identity(now),
        metadata,
        now,
        sec.SYNTHETIC,
    )


class Provider:
    """In-memory official-route-shaped fixtures. No network fallback."""

    def __init__(
        self,
        end=TEST_END,
        *,
        count=23,
        warning_period=None,
        extra_month=False,
        weight_overrides: dict[str, dict[int, str]] | None = None,
    ):
        self.records = {}
        self.calls = []
        weight_overrides = weight_overrides or {}
        last = sec.parse_date(end)
        for offset in range(1 - count, 1):
            period = sec.month_end(sec.month_shift(last, offset * 3)).isoformat()
            self.add(
                period,
                allocation_delta="2" if period == warning_period else "0",
                weight_overrides=weight_overrides.get(period),
            )
        if extra_month:
            self.add(sec.month_end(sec.month_shift(last, -1)).isoformat())

    def add(
        self,
        period,
        *,
        amendment=0,
        filed=None,
        allocation_delta="0",
        weight_overrides: dict[int, str] | None = None,
    ):
        item = filing(period, amendment=amendment, filed=filed)
        self.records[item.accession] = {
            "filing": item,
            "xml": holdings(
                item,
                allocation_delta=allocation_delta,
                weight_overrides=weight_overrides,
            ),
            "index": index(item),
        }
        return item

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        assert url.startswith("https://example.invalid/synthetic/"), "No real financial requests"
        if url.endswith("/company_tickers_mf.json"):
            return mapping()
        if "/search-index?" in url:
            query = parse_qs(urlparse(url).query)
            assert query["q"] == [f'"{TEST_SERIES}"']
            assert query["ciks"] == [sec.CIK]
            assert query["forms"] == ["NPORT-P"]
            records = [
                record["filing"]
                for record in self.records.values()
                if query["startdt"][0] <= record["filing"].filing_date <= query["enddt"][0]
            ]
            offset = int(query["from"][0])
            return sec.json_bytes(
                {
                    "timed_out": False,
                    "hits": {
                        "total": {"value": len(records), "relation": "eq"},
                        "hits": [
                            {
                                "_id": item.accession + ":" + item.filename,
                                "_source": {
                                    "adsh": item.accession,
                                    "file_type": item.form,
                                    "ciks": [sec.CIK],
                                    "file_date": item.filing_date,
                                    "period_ending": item.reported_date_hint,
                                },
                            }
                            for item in records[offset : offset + 100]
                        ],
                    },
                }
            )
        for record in self.records.values():
            item = record["filing"]
            if url == sec.filing_url(item.accession, sec.SYNTHETIC):
                return record["index"]
            if url == item.document_url(sec.SYNTHETIC):
                return record["xml"]
        raise AssertionError("Unexpected synthetic source request")


def seed(root, *, now=TEST_NOW, provider=None):
    provider = provider or Provider()
    app.refresh_archive(root, provider, kind=sec.SYNTHETIC, clock=lambda: now, bootstrap=True)
    return provider
