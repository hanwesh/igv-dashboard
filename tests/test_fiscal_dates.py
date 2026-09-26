from datetime import UTC, datetime

import pytest

import igv_snapshot as app
import sec_nport as sec
from tests.synthetic import TEST_NOW, filing, holdings, identity, index


@pytest.mark.parametrize(
    ("reported", "year_end", "quarter"),
    [
        ("2031-01-31", "2031-10-31", 1),
        ("2031-04-30", "2031-10-31", 2),
        ("2031-07-31", "2031-10-31", 3),
        ("2031-10-31", "2031-10-31", 4),
        ("2031-03-31", "2031-10-31", None),
        ("2027-02-26", "2027-11-30", 1),
        ("2027-02-28", "2027-11-30", 1),
        ("2030-12-31", "2031-09-30", 1),
        ("2028-02-29", "2028-11-30", 1),
    ],
)
def test_reported_fiscal_not_calendar_or_exchange_dates(reported, year_end, quarter):
    assert sec.fiscal_quarter(reported, year_end) == quarter


@pytest.mark.parametrize(
    ("reported", "year_end"),
    [("2031-07-31", "2030-10-31"), ("2031-07-31", "2032-10-31"), ("2031-02-30", "2031-10-31")],
)
def test_inconsistent_fiscal_dates_rejected(reported, year_end):
    with pytest.raises(sec.DataError):
        sec.fiscal_quarter(reported, year_end)


@pytest.mark.parametrize("value", ["2031-1-31", "2031-13-01", "", None, 20310131, True])
def test_bad_dates(value):
    with pytest.raises(sec.DataError):
        sec.parse_date(value)


def test_publication_lag_is_not_a_demand_for_current_quarter():
    assert app.stale_after("2031-07-31") == "2032-01-06"
    assert app.stale_after("2027-02-26") == "2027-08-06"
    assert app.stale_after("2027-02-28") == "2027-08-06"


def test_year_end_is_not_holdings_date():
    item = filing("2031-07-31")
    normalized = sec.normalize_filing(
        holdings(item),
        item,
        identity(),
        sec.index_metadata(index(item), item, sec.SYNTHETIC),
        TEST_NOW,
        sec.SYNTHETIC,
    )
    assert normalized["reported_as_of"] == "2031-07-31"
    assert normalized["fiscal_year_end"] == "2031-10-31"
    assert normalized["fiscal_quarter"] == 3


@pytest.mark.parametrize("value", ["2031-01-01T00:00:00", "2031-01-01T00:00:00+10:00", None])
def test_utc_provenance_not_filesystem_or_local_time(value):
    with pytest.raises(sec.DataError):
        sec.parse_utc(value)


def test_utc_round_trip_and_naive_clock_rejected():
    assert sec.parse_utc(sec.utc_text(TEST_NOW)) == TEST_NOW
    with pytest.raises(sec.DataError):
        sec.utc_text(datetime(2031, 1, 1))
    assert sec.utc_text(datetime(2031, 1, 1, tzinfo=UTC)).endswith("Z")
