from datetime import UTC, date, datetime

import pytest

import igv_snapshot as app


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2031-01-01T04:59:59+00:00", "2030-11"),
        ("2031-01-01T05:00:00+00:00", "2030-12"),
        ("2031-07-01T03:59:59+00:00", "2031-05"),
        ("2031-07-01T04:00:00+00:00", "2031-06"),
        ("2024-03-01T05:00:00+00:00", "2024-02"),
    ],
)
def test_new_york_rollover(instant, expected):
    assert app.completed_month(datetime.fromisoformat(instant)) == expected


@pytest.mark.parametrize(
    ("month", "expected"),
    [
        ("2024-02", "2024-02-29"),
        ("2023-02", "2023-02-28"),
        ("2024-03", "2024-03-28"),
        ("2021-05", "2021-05-28"),
        ("2022-12", "2022-12-30"),
    ],
)
def test_exchange_month_ends(month, expected):
    assert app.snapshot_date(month).isoformat() == expected


def test_exceptional_closures():
    sandy = app.trading_days(date(2012, 10, 26), date(2012, 10, 31))
    assert sandy == [date(2012, 10, 26), date(2012, 10, 31)]
    mourning = app.trading_days(date(2025, 1, 8), date(2025, 1, 10))
    assert date(2025, 1, 9) not in mourning
    september = app.trading_days(date(2001, 9, 10), date(2001, 9, 17))
    assert september == [date(2001, 9, 10), date(2001, 9, 17)]


def test_complete_window_and_baseline():
    assert app.display_months("2031-05") == app.month_range("2026-06", "2031-05")
    assert len(app.display_months("2031-05")) == 60
    assert app.price_bounds("2031-05")[0] == app.snapshot_date("2026-05")
    assert app.completed_month(datetime(2031, 6, 30, tzinfo=UTC)) == "2031-05"


def test_invalid_dates_are_explicit():
    for month in ["2031-13", "31-01", "2031-1", None]:
        with pytest.raises(app.DataError):
            app.month_start(month)
    with pytest.raises(app.DataError, match="timezone"):
        app.completed_month(datetime(2031, 6, 1))
    with pytest.raises(app.DataError, match="Reversed"):
        app.month_range("2031-05", "2031-04")
