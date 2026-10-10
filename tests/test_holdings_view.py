"""Two holdings views over one archive: as filed by default, operating companies by flag only."""

import copy
import csv
import io
from decimal import Decimal

import pytest

import igv_snapshot as app
import sec_nport as sec
from tests.synthetic import TEST_NOW, normalized

SHA = "a" * 64


@pytest.fixture
def filing():
    return copy.deepcopy(normalized())


def collateral_row(item):
    return next(
        row for row in item["holdings"] if row["security_lending"]["is_cash_collateral"] is True
    )


def cash_vehicle_row(item):
    return next(
        row
        for row in item["holdings"]
        if row["asset_category"] == "STIV"
        and row["issuer_category"] == "RF"
        and row["security_lending"]["is_cash_collateral"] is not True
    )


def titled(item, title):
    return next(row for row in item["holdings"] if row["title"] == title)


def test_default_view_is_as_filed_and_keeps_reported_collateral(filing):
    default = sec.ranked_snapshot(filing, SHA)
    assert default == sec.ranked_snapshot(filing, SHA, exclude_collateral=False)
    assert [row["source_row"] for row in default["top10"]] == list(range(1, 11))
    assert any(row["security_lending"]["is_cash_collateral"] is True for row in default["top10"])
    assert "Includes securities-lending cash collateral" in " ".join(default["quality_notes"])


def test_operating_view_excludes_only_the_affirmative_collateral_flag(filing):
    flagged = collateral_row(filing)
    filtered = sec.ranked_snapshot(filing, SHA, exclude_collateral=True)
    surviving = [row["source_row"] for row in filtered["top10"]]
    assert flagged["source_row"] not in surviving
    assert all(
        row["security_lending"]["is_cash_collateral"] is not True for row in filtered["top10"]
    )
    assert "Excludes securities-lending cash collateral" in " ".join(filtered["quality_notes"])


@pytest.mark.parametrize("unflagged", [False, None])
def test_only_true_is_affirmative_so_false_and_unreported_stay_eligible(filing, unflagged):
    flagged = collateral_row(filing)
    flagged["security_lending"]["is_cash_collateral"] = unflagged
    flagged["security_lending"]["cash_collateral_value_usd"] = None
    filtered = sec.ranked_snapshot(filing, SHA, exclude_collateral=True)
    assert flagged["source_row"] in [row["source_row"] for row in filtered["top10"]]
    assert filtered["top10"] == sec.ranked_snapshot(filing, SHA)["top10"]


def test_cash_vehicle_twin_is_not_excluded_by_name_cusip_or_category(filing):
    """The real filings pair one flagged and one unflagged BlackRock cash row, sometimes on
    the same CUSIP and title, so only the reported flag may decide eligibility."""
    flagged = collateral_row(filing)
    twin = cash_vehicle_row(filing)
    twin["name"] = flagged["name"]
    twin["title"] = flagged["title"]
    twin["cusip"] = flagged["cusip"]
    twin["weight_pct"] = "9.900000000000"
    twin["market_value_usd"] = "99000.00000000"
    filtered = sec.ranked_snapshot(filing, SHA, exclude_collateral=True)
    promoted = filtered["top10"][0]
    assert promoted["source_row"] == twin["source_row"]
    assert (promoted["cusip"], promoted["title"]) == (flagged["cusip"], flagged["title"])
    assert promoted["asset_category"] == "STIV" and promoted["issuer_category"] == "RF"
    assert flagged["source_row"] not in [row["source_row"] for row in filtered["top10"]]


def test_reranking_uses_unrounded_weights_not_display_rounding(filing):
    """Two candidates that tie at display precision, where the rounded tiebreak on reported
    value would pick the lower unrounded weight."""
    keeper, loser = (
        titled(filing, "Synthetic Test Security 10"),
        titled(filing, "Synthetic Test Security 11"),
    )
    keeper["weight_pct"], keeper["market_value_usd"] = "4.004000000000", "1.00000000"
    loser["weight_pct"], loser["market_value_usd"] = "4.001000000000", "999999.00000000"
    rounded = [Decimal(row["weight_pct"]).quantize(Decimal("0.01")) for row in (keeper, loser)]
    assert rounded[0] == rounded[1] == Decimal("4.00")
    assert Decimal(loser["market_value_usd"]) > Decimal(keeper["market_value_usd"])

    as_filed = sec.ranked_snapshot(filing, SHA)["top10"]
    assert keeper["source_row"] not in [row["source_row"] for row in as_filed]

    filtered = sec.ranked_snapshot(filing, SHA, exclude_collateral=True)["top10"]
    assert filtered[-1]["source_row"] == keeper["source_row"]
    assert loser["source_row"] not in [row["source_row"] for row in filtered]
    assert [Decimal(row["weight_pct"]) for row in filtered] == sorted(
        (Decimal(row["weight_pct"]) for row in filtered), reverse=True
    )


def test_operating_view_never_renormalizes_or_redistributes_omitted_weight(filing):
    as_filed = sec.ranked_snapshot(filing, SHA)
    filtered = sec.ranked_snapshot(filing, SHA, exclude_collateral=True)
    reported = {row["source_row"]: row["weight_pct"] for row in filing["holdings"]}
    assert all(row["weight_pct"] == reported[row["source_row"]] for row in filtered["top10"])
    omitted = Decimal(collateral_row(filing)["weight_pct"])
    assert (
        Decimal(filtered["portfolio_weight_pct"])
        == Decimal(as_filed["portfolio_weight_pct"]) - omitted
    )
    assert Decimal(filtered["top10_weight_pct"]) == sum(
        Decimal(row["weight_pct"]) for row in filtered["top10"]
    )
    assert Decimal(filtered["top10_weight_pct"]) != Decimal(100)
    assert Decimal(filtered["portfolio_weight_pct"]) != Decimal(100)
    assert "not redistributed" in " ".join(filtered["quality_notes"])


def test_operating_view_refuses_an_incomplete_top_ten(filing):
    flagged = collateral_row(filing)
    ordered = sorted(filing["holdings"], key=lambda row: row["source_row"] != flagged["source_row"])
    filing["holdings"] = ordered[:10]
    assert len(sec.ranked_snapshot(filing, SHA)["top10"]) == 10
    with pytest.raises(sec.DataError, match="ten eligible reported positions"):
        sec.ranked_snapshot(filing, SHA, exclude_collateral=True)


def test_security_groups_follow_the_active_view_and_stay_keyed_on_cusip(archive, tmp_path):
    report = app.build_site(archive, tmp_path / "site", test_only=True, now=TEST_NOW)
    flagged = {
        row["cusip"]
        for item in report["snapshots"]
        for row in item["top10"]
        if row["security_lending"]["is_cash_collateral"] is True
    }
    as_filed = {group["cusip"] for group in report["securities"]}
    operating = {group["cusip"] for group in report["securities_without_collateral"]}
    assert flagged and flagged <= as_filed
    assert not flagged & operating
    assert len(as_filed) == len(report["securities"])
    assert len(operating) == len(report["securities_without_collateral"])
    for groups in (report["securities"], report["securities_without_collateral"]):
        assert all(group["cusip"] in group["search_terms"] for group in groups)
        assert groups == sorted(
            groups, key=lambda group: (group["label"].casefold(), group["cusip"])
        )


def test_full_holdings_csv_reproduces_both_rankings(archive, tmp_path):
    site = tmp_path / "site"
    report = app.build_site(archive, site, test_only=True, now=TEST_NOW)
    rows = list(
        csv.DictReader(
            io.StringIO((site / report["downloads"]["all"]).read_text(encoding="utf-8-sig"))
        )
    )
    assert len(rows) == report["full_position_count"] > 200
    by_quarter: dict[str, list[dict]] = {}
    for row in rows:
        by_quarter.setdefault(row["reported_as_of"], []).append(row)

    def top_ten(pool, *, exclude_collateral):
        eligible = [
            row for row in pool if not (exclude_collateral and row["is_cash_collateral"] == "true")
        ]
        return sorted(
            eligible,
            key=lambda row: (
                Decimal(row["weight_pct"]),
                Decimal(row["market_value_usd"]),
                -int(row["source_row"]),
            ),
            reverse=True,
        )[:10]

    def fingerprint(holdings):
        return [(row["title"], row["source_row"], row["weight_pct"]) for row in holdings]

    for item in report["snapshots"]:
        pool = by_quarter[item["reported_as_of"]]
        assert any(row["is_cash_collateral"] == "true" for row in pool)
        assert [row["rank"] for row in top_ten(pool, exclude_collateral=False)] == [
            str(rank) for rank in range(1, 11)
        ]
        for holdings, reproduced in [
            (item["top10"], top_ten(pool, exclude_collateral=False)),
            (item["without_collateral"]["top10"], top_ten(pool, exclude_collateral=True)),
        ]:
            assert fingerprint(reproduced) == [
                (row["title"], str(row["source_row"]), row["weight_pct"]) for row in holdings
            ]


def test_the_two_top_ten_csvs_stay_as_filed_in_either_view(archive, tmp_path):
    site = tmp_path / "site"
    report = app.build_site(archive, site, test_only=True, now=TEST_NOW)
    core = app.export_csvs(report)
    assert set(core) == {"long", "wide"}
    for kind in ("long", "wide"):
        stored = (site / report["downloads"][kind]).read_bytes().decode("utf-8-sig")
        assert core[kind] == stored
    long_rows = list(csv.DictReader(io.StringIO(core["long"])))
    as_filed = [row for item in report["snapshots"] for row in item["top10"]]
    assert [row["weight_pct"] for row in long_rows] == [row["weight_pct"] for row in as_filed]
    assert sum(row["is_cash_collateral"] == "true" for row in long_rows) == sum(
        row["security_lending"]["is_cash_collateral"] is True for row in as_filed
    )


def test_built_page_defaults_to_as_filed_without_javascript(archive, tmp_path):
    site = tmp_path / "site"
    report = app.build_site(archive, site, test_only=True, now=TEST_NOW)
    page = (site / "index.html").read_text(encoding="utf-8")
    assert '<div class="panel controls" id="view-controls" hidden>' in page
    assert page.index('<option value="as-filed">') < page.index('<option value="operating">')
    assert '<label for="holdings-view">Holdings view<select id="holdings-view"' in page
    assert 'id="active-view-name">As filed (SEC)<' in page
    assert '<ul class="quarter-legend" id="quarter-legend"' in page
    assert '<div class="grouped-chart" id="quarter-detail"' in page
    assert 'id="quarter-chart-scroll" tabindex="0" role="region"' in page
    assert 'id="bar-scale-note"' in page
    assert "Quarter groups run left to right." in page
    assert "present holdings run from highest to lowest exact percentage" in page
    assert '"Not in top 10" gaps come last' in page
    assert "const orderedRowsFor = entry => selectedRows" in page
    assert "return left.match.rank - right.match.rank || left.row.rank - right.row.rank;" in page
    assert "followed by missing selected-universe slots" in page
    assert ".quarter-group {" in page
    assert ".group-meta {" not in page
    assert 'const metadata = make("div", "group-meta");' not in page
    assert "accessibleIdentifiers(identity)" in page
    assert ".security-group {" not in page
    assert "holding-card" not in page
    assert "holding-bar" not in page
    assert page.count('id="view-table-caption">As filed (SEC).') == 1
    for kind in ("wide", "long", "all"):
        assert f'href="{report["downloads"][kind]}" download' in page
        assert (site / report["downloads"][kind]).is_file()
    table = app.static_table(report)
    flagged = {
        row["title"]
        for item in report["snapshots"]
        for row in item["top10"]
        if row["security_lending"]["is_cash_collateral"] is True
    }
    assert flagged and all(title in table for title in flagged)
    assert table.count("Securities-lending collateral") == len(report["snapshots"])
