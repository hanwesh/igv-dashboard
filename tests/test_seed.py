"""Offline regression evidence for the original SEC factual seed, never network fixtures."""

from decimal import Decimal

import pytest

import igv_snapshot as app
import sec_nport as sec

# Independently checked official accessions; future refreshes retain these immutable versions.
BASELINE = [
    ("2019-09-30", "0001752724-19-177737", 97, "108.5865"),
    ("2019-12-31", "0001752724-20-038765", 107, "110.4006"),
    ("2020-03-31", "0001752724-20-112086", 105, "113.1009"),
    ("2020-06-30", "0001752724-20-176956", 104, "105.9586"),
    ("2020-09-30", "0001752724-20-247717", 104, "107.3419"),
    ("2020-12-31", "0001752724-21-040710", 115, "103.9814"),
    ("2021-03-31", "0001752724-21-116364", 115, "103.9448"),
    ("2021-06-30", "0001752724-21-186103", 126, "105.1196"),
    ("2021-09-30", "0001752724-21-255883", 123, "103.2285"),
    ("2021-12-31", "0001752724-22-046384", 131, "104.3986"),
    ("2022-03-31", "0001752724-22-122715", 131, "104.3764"),
    ("2022-06-30", "0001752724-22-193713", 125, "107.1610"),
    ("2022-09-30", "0001752724-22-268669", 121, "104.8048"),
    ("2022-12-31", "0001752724-23-039241", 124, "107.8556"),
    ("2023-03-31", "0001752724-23-123225", 122, "104.1062"),
    ("2023-06-30", "0001752724-23-191500", 120, "106.1218"),
    ("2023-09-30", "0001752724-23-264259", 120, "108.0859"),
    ("2023-12-31", "0001752724-24-043111", 119, "107.8656"),
    ("2024-03-31", "0001752724-24-123329", 118, "106.3891"),
    ("2024-06-30", "0001752724-24-194361", 119, "103.9873"),
    ("2024-09-30", "0001752724-24-269945", 118, "106.3968"),
    ("2024-12-31", "0001752724-25-043802", 126, "106.8706"),
    ("2025-03-31", "0001752724-25-119782", 121, "106.7387"),
    ("2025-06-30", "0001752724-25-210397", 119, "103.6657"),
    ("2025-09-30", "0002071691-25-007647", 117, "106.8149"),
    ("2025-09-30", "0002071691-26-013333", 117, "107.8421"),
    ("2025-12-31", "0002071691-26-004222", 119, "103.7245"),
    ("2026-03-31", "0002071691-26-012490", 116, "105.3258"),
    ("2026-06-30", "0002071691-26-019778", 111, "105.8792"),
]


@pytest.fixture(scope="module")
def actual_seed():
    root = app.ROOT / "data"
    manifest = app.read_manifest(root)
    return manifest, app.validate_manifest(manifest, root, require_current=False)


@pytest.mark.parametrize(("reported", "accession", "count", "total"), BASELINE)
def test_official_seed_counts_and_reported_weights(actual_seed, reported, accession, count, total):
    manifest, validated = actual_seed
    item = validated["all_filings"][accession]
    assert item["reported_as_of"] == reported
    assert item["source_row_count"] == count
    assert len(item["holdings"]) == count
    weights = Decimal(sec.decimal_sum(row["weight_pct"] for row in item["holdings"]))
    assert weights.quantize(Decimal("0.0001")) == Decimal(total)
    assert Decimal(103) < weights < Decimal(114)
    assert (
        sum(row["security_lending"]["is_cash_collateral"] is True for row in item["holdings"]) == 1
    )
    assert all(row["ticker"] == [] for row in item["holdings"])
    assert item["fiscal_year_end"].endswith("-03-31")
    assert manifest["identity"]["series_id"] == "S000004355"
    assert manifest["identity"]["class_id"] == "C000012085"
    assert manifest["identity"]["registrant_cik"] == "0001100663"


def test_original_seed_coverage_and_amendment_history(actual_seed):
    _, validated = actual_seed
    baseline = {accession: validated["all_filings"][accession] for _, accession, _, _ in BASELINE}
    quarters = app.active_quarters(baseline)
    assert len(baseline) == 29 and len(quarters) == 28
    assert min(quarters) == "2019-09" and max(quarters) == "2026-06"
    assert quarters["2025-09"] == "0002071691-26-013333"
    amended = baseline[quarters["2025-09"]]["provenance"]
    original = baseline["0002071691-25-007647"]["provenance"]
    assert amended["form"] == "NPORT-P/A" and amended["filing_date"] == "2026-06-18"
    assert original["form"] == "NPORT-P" and original["filing_date"] == "2025-11-26"
    assert sec.parse_utc(amended["accepted_at_utc"]) > sec.parse_utc(original["accepted_at_utc"])
    assert amended["source_sha256"] != original["source_sha256"]


def test_latest_seed_top_ten_match_unrounded_sec_facts(actual_seed):
    _, validated = actual_seed
    item = validated["all_filings"]["0002071691-26-019778"]
    top = sec.ranked_snapshot(item, "a" * 64)["top10"]
    assert [Decimal(row["weight_pct"]) for row in top] == [
        Decimal(value)
        for value in [
            "10.371175712740",
            "8.072074228397",
            "7.704553749721",
            "7.284173247396",
            "6.261257673112",
            "5.868089075690",
            "4.805820433672",
            "4.757717397859",
            "3.914032737324",
            "3.871236258607",
        ]
    ]
    assert top[5]["title"] == "BlackRock Cash Funds: Institutional, SL Agency Shares"
    assert top[5]["security_lending"]["is_cash_collateral"] is True
    assert top[5]["asset_category"] == "STIV" and top[5]["issuer_category"] == "RF"
    assert sum(row["security_lending"]["is_loan_by_fund"] is True for row in item["holdings"]) == 57
    for accession in ["0001752724-20-038765", "0001752724-20-112086"]:
        top_holding = sec.ranked_snapshot(validated["all_filings"][accession], "a" * 64)["top10"][0]
        assert top_holding["security_lending"]["is_cash_collateral"] is True


def test_seed_no_tickers_are_a_neutral_source_property(actual_seed):
    _, validated = actual_seed
    for _, accession, _, _ in BASELINE:
        ranked = sec.ranked_snapshot(validated["all_filings"][accession], "a" * 64)
        assert "supplies no exchange tickers" in ranked["identifier_note"]
        assert not any("ticker" in note for note in ranked["quality_notes"])


def test_seed_cross_quarter_continuity_is_cusip_not_issuer_spelling(actual_seed):
    _, validated = actual_seed
    originals = {accession: validated["all_filings"][accession] for _, accession, _, _ in BASELINE}
    quarters = app.active_quarters(originals)
    snapshots = [
        sec.ranked_snapshot(originals[quarters[period]], "a" * 64)
        for period in sorted(quarters)[-20:]
    ]
    groups = app.security_groups(snapshots)
    assert len(groups) == 17
    counts = {group["cusip"]: group["appearances"] for group in groups}
    assert {
        cusip: counts[cusip]
        for cusip in [
            "79466L302",
            "594918104",
            "68389X105",
            "81762P102",
            "00724F101",
            "461202103",
            "697435105",
        ]
    } == {
        "79466L302": 20,
        "594918104": 20,
        "68389X105": 20,
        "81762P102": 20,
        "00724F101": 19,
        "461202103": 19,
        "697435105": 18,
    }
    salesforce = next(group for group in groups if group["cusip"] == "79466L302")
    assert {"salesforce.com Inc", "Salesforce Inc", "Salesforce, Inc."} <= set(
        salesforce["search_terms"]
    )
