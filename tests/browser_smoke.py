"""Offline native-UI/browser check: synthetic holdings and an intercepted widget, never prices."""

import csv
import io
import tempfile
import threading
from datetime import UTC, datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

import igv_snapshot as app
from tests.synthetic import TEST_END, TEST_NOW, Provider, seed

WIDGET_URL = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js"
WIDGET_CONFIG = {
    "autosize": True,
    "symbol": "AMEX:IGV",
    "interval": "W",
    "range": "60M",
    "timezone": "America/New_York",
    "theme": "light",
    "style": "0",
    "locale": "en",
    "allow_symbol_change": False,
    "withdateranges": True,
    "hide_volume": False,
    "support_host": "https://www.tradingview.com",
}
MOCK_WIDGET = """
const current = document.currentScript;
window.mockWidgetConfig = JSON.parse(current.textContent);
const container = current.parentElement;
container.style.height = "100%";
const iframe = document.createElement("iframe");
iframe.title = "Synthetic hosted chart frame";
iframe.style.cssText = "width:100%;height:100%;border:0;display:block";
iframe.srcdoc = "<!doctype html><html><body>MOCK WIDGET: no financial data</body></html>";
container.querySelector(".tradingview-widget-container__widget").append(iframe);
"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


def route_offline(page, origin, *, widget="mock"):
    outside, widgets, errors = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route_request(route):
        url = route.request.url
        if url.startswith(origin + "/"):
            route.continue_()
        elif url == WIDGET_URL:
            widgets.append(url)
            if widget == "error":
                route.abort()
            else:
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body=MOCK_WIDGET if widget == "mock" else "/* No frame: synthetic timeout */",
                )
        else:
            outside.append(url)
            route.abort()

    page.route("**/*", route_request)
    return outside, widgets, errors


def check_download(page, output, report, kind, expected_count):
    link = page.locator(f"#download-{kind}")
    href = link.get_attribute("href")
    assert href == report["downloads"][kind] and not href.startswith("/")
    with page.expect_download() as event:
        link.click()
    download = event.value
    assert download.suggested_filename == href
    text = Path(download.path()).read_text(encoding="utf-8-sig")
    assert text == (output / href).read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == expected_count
    assert {row["data_kind"] for row in rows} == {app.SYNTHETIC}


def check_holdings_views(page, output, report):
    selector = page.get_by_role("combobox", name="Holdings view", exact=True)
    expect(selector).to_have_value("as-filed")
    original_titles = page.locator("#quarterly-body .symbol").all_text_contents()
    original_downloads = {
        kind: page.locator("#download-" + kind).get_attribute("href")
        for kind in report["downloads"]
    }
    page.locator("#security-select").select_option("TEST00006")
    selector.focus()
    # Native select typeahead works without an OS popup in headless Chromium.
    page.keyboard.press("o")
    page.keyboard.press("Tab")
    expect(selector).to_have_value("operating")
    expect(page.locator("#active-view-name")).to_have_text("Operating companies only")
    expect(page.locator("#view-description")).to_contain_text("not normalized to 100%")
    expect(page.locator("#view-description")).to_contain_text("filter has reset")
    expect(page.locator("#security-select")).to_have_value("")
    expect(page.locator('#security-select option[value="TEST00006"]')).to_have_count(0)
    expect(page.locator('#security-select option[value="TEST00010"]')).to_have_count(1)
    expected_rows = [
        row for item in report["snapshots"] for row in item["without_collateral"]["top10"]
    ]
    assert page.locator("#quarterly-body .symbol").all_text_contents() == [
        row["title"] for row in expected_rows
    ]
    assert page.locator("#quarterly-body .pct").all_text_contents() == [
        f"{app.decimal(row['weight_pct']):.2f}%" for row in expected_rows
    ]
    last = report["snapshots"][-1]
    selected_rows = last["without_collateral"]["top10"]
    comparison = report["snapshots"][-4:]
    chart_label = (
        "Operating companies only grouped top-ten holdings comparison ending "
        + last["reported_as_of"]
        + ". Ten security groups follow the selected quarter's rank order. "
        + "Each group compares up to four reported quarters; exact percentages "
        + "and missing top-ten appearances are shown as text."
    )
    expect(page.locator("#quarter-detail")).to_have_attribute(
        "aria-label",
        chart_label,
    )
    expect(page.locator("#quarter-legend li")).to_have_count(4)
    assert page.locator("#quarter-legend li").all_text_contents() == [
        f"FY {item['fiscal_year_end'][:4]} Q{item['fiscal_quarter']} / "
        + item["reported_as_of"]
        + (" (selected)" if item is last else "")
        for item in comparison
    ]
    colors = page.locator("#quarter-legend .series-swatch").evaluate_all(
        "swatches => swatches.map(swatch => getComputedStyle(swatch).backgroundColor)"
    )
    assert len(set(colors)) == 4
    expect(page.locator("#quarter-legend .selected-quarter")).to_have_count(1)
    expect(page.locator("#quarter-detail .security-group")).to_have_count(10)
    expect(page.locator("#quarter-detail .quarter-bar-slot")).to_have_count(40)
    expect(page.locator("#quarter-detail .quarter-bar-slot.selected-quarter")).to_have_count(10)
    expected_values, expected_missing = [], 0
    maximum = app.decimal("0")
    for selected in selected_rows:
        for snapshot in comparison:
            match = next(
                (
                    row
                    for row in snapshot["without_collateral"]["top10"]
                    if row["cusip"] == selected["cusip"]
                ),
                None,
            )
            if match:
                maximum = max(maximum, app.decimal(match["weight_pct"]))
                expected_values.append(f"{app.decimal(match['weight_pct']):.2f}%")
            else:
                expected_values.append("—")
                expected_missing += 1
    assert page.locator("#quarter-detail .quarter-bar-value").all_text_contents() == (
        expected_values
    )
    expect(page.locator("#quarter-detail .bar-missing")).to_have_count(expected_missing)
    expect(page.locator('#quarter-detail [data-present="false"]')).to_have_count(expected_missing)
    expect(page.locator("#quarter-detail")).not_to_contain_text("0.00%")
    ratios = page.locator("#quarter-detail .quarter-bar-slot[data-present=true]").evaluate_all(
        """slots => slots.map(slot => {
            const plot = slot.querySelector(".quarter-bar-plot").getBoundingClientRect().height;
            const bar = slot.querySelector(".vertical-bar").getBoundingClientRect().height;
            return {ratio: bar / plot, weight: slot.dataset.weightPct};
        })"""
    )
    for actual in ratios:
        expected = float(app.decimal(actual["weight"]) / maximum)
        assert abs(actual["ratio"] - expected) < 0.01
    expect(page.locator("#bar-scale-note")).to_contain_text("not normalized")
    expect(page.locator("#bar-scale-note")).to_contain_text("not zero")
    expect(page.locator("#last-total")).to_have_text(
        f"{app.decimal(last['without_collateral']['top10_weight_pct']):.2f}%"
    )
    collateral = next(
        note
        for note in last["without_collateral"]["quality_notes"]
        if note.startswith("Excludes securities-lending")
    )
    expect(page.locator("#quarter-note")).to_contain_text(collateral)
    expect(page.locator("#source-notes")).to_contain_text(collateral)
    assert page.locator("#quarter-detail .rank").all_text_contents() == [
        f"#{row['rank']} / {row['asset_category']}" for row in last["without_collateral"]["top10"]
    ]
    page.locator("#security-select").select_option("TEST00010")
    expect(page.locator("#quarterly-body .match")).to_have_count(20)
    page.locator("#quarter-select").select_option("2029-01-31")
    expect(page.locator("#selected-date")).to_contain_text("2029-01-31")
    expect(page.locator("#quarter-detail .security-title").last).to_have_text(
        "Synthetic Test Security 10"
    )
    expect(page.locator("#quarter-legend li")).to_have_count(4)
    expect(page.locator("#quarter-legend li").last).to_contain_text("2029-01-31 (selected)")
    for kind, original in original_downloads.items():
        assert page.locator("#download-" + kind).get_attribute("href") == original
    for kind, count in [("wide", 20), ("long", 200), ("all", report["full_position_count"])]:
        check_download(page, output, report, kind, count)
    selector.focus()
    page.keyboard.press("a")
    page.keyboard.press("Tab")
    expect(selector).to_have_value("as-filed")
    expect(page.locator("#view-description")).to_contain_text("filter has reset")
    expect(page.locator("#security-select")).to_have_value("")
    expect(page.locator("#quarter-select")).to_have_value("2029-01-31")
    assert page.locator("#quarterly-body .symbol").all_text_contents() == original_titles
    early = report["snapshots"][2]
    page.locator("#quarter-select").select_option(early["reported_as_of"])
    expect(page.locator("#quarter-legend li")).to_have_count(3)
    expect(page.locator("#quarter-legend li").last).to_contain_text(
        early["reported_as_of"] + " (selected)"
    )
    expect(page.locator("#quarter-detail .quarter-bar-slot")).to_have_count(30)
    page.locator("#quarter-select").select_option(TEST_END)


def smoke(page, origin, output, report):
    outside, widgets, errors = route_offline(page, origin)
    page.clock.install(time=TEST_NOW)
    response = page.goto(origin + "/igv-dashboard/", wait_until="networkidle")
    assert response.status == 200
    expect(page).to_have_title(
        "SYNTHETIC TEST ONLY | Quarterly SEC holdings | 2026-10-31 - 2031-07-31"
    )
    expect(page.locator("#test-banner")).to_be_visible()
    expect(page.locator("#stale-warning")).to_be_hidden()
    expect(page.locator("#coverage")).to_have_text("20 / 20")
    expect(page.locator("#quarterly-body tr")).to_have_count(20)
    expect(page.locator("#quarterly-body .cell")).to_have_count(200)
    expect(page.locator("#quarter-select option")).to_have_count(20)
    expect(page.locator("#data-through")).to_have_text(TEST_END)
    expect(page.locator("#last-refresh")).to_have_text("2031-09-15T12:00:00Z")
    expect(page.locator("#selected-date")).to_contain_text("Fiscal year end 2031-10-31")
    expect(page.locator("#selected-filing")).to_contain_text("Filed 2031-09-14")
    expect(page.locator("#quarter-detail")).to_contain_text("Ticker not reported")
    expect(page.locator("#quarter-detail")).to_contain_text("ISIN: TEST00000001")
    expect(page.locator("#quarter-note")).to_contain_text("Original XML SHA256")
    expect(page.locator("#quarter-note")).to_contain_text("Normalized immutable object SHA256")
    expect(page.locator("#source-notes")).to_contain_text("without normalization")
    expect(page.locator("#source-notes")).to_contain_text("no reported ticker")
    expect(page.locator("#collateral-note")).to_contain_text("securities-lending collateral")
    expect(page.locator("#quarter-note")).to_contain_text("All reported investments:")
    expect(page.locator("#quarter-detail .category")).to_contain_text(
        "Securities-lending collateral"
    )
    expect(page.locator("#publication-lag")).to_contain_text("60 days")
    expect(page.locator("#chart-viewport")).to_be_hidden()
    assert widgets == []
    assert page.locator("#download-performance").count() == 0
    check_holdings_views(page, output, report)
    assert widgets == [], "Changing holdings view must not request the independent chart"
    expect(page.locator("#security-select option")).to_have_count(11)
    group = next(group for group in report["securities"] if group["cusip"] == "TEST00001")
    expect(page.locator('#security-select option[value="TEST00001"]')).to_have_text(
        f"{group['label']} / TEST00001 (20/20 quarters)"
    )
    page.locator("#holding-search").fill("Synthetic Historical Alias")
    expect(page.locator("#quarterly-body .match")).to_have_count(20)
    expect(page.locator("#quarterly-body .match").last).to_contain_text(group["label"])
    page.locator("#holding-search").fill("")
    page.locator("#security-select").select_option("TEST00001")
    expect(page.locator("#quarterly-body .match")).to_have_count(20)
    page.locator("#security-select").select_option("")
    page.locator("#year-select").select_option("2028")
    expect(page.locator("#quarterly-body tr")).to_have_count(4)
    page.locator("#holding-search").fill("TEST02")
    expect(page.locator("#quarterly-body .match")).to_have_count(4)
    page.locator("#holding-search").fill("TEST00000001")
    expect(page.locator("#quarterly-body .match")).to_have_count(4)
    page.locator("#order-select").select_option("desc")
    expect(page.locator("#quarterly-body tr").first).to_have_attribute("data-period", "2028-10-31")
    page.locator("#quarterly-body .quarter-button").first.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#quarter-select")).to_have_value("2028-10-31")
    selected = next(item for item in report["snapshots"] if item["reported_as_of"] == "2028-10-31")
    expect(page.locator("#quarter-detail .security-title").first).to_have_text(
        selected["top10"][0]["title"]
    )
    expect(page.locator("#quarter-legend li").last).to_contain_text("2028-10-31 (selected)")
    page.locator("#quarter-select").select_option("2029-01-31")
    expect(page.locator("#selected-date")).to_contain_text("FY 2029 Q1")
    expect(page.locator(".security-group")).to_have_count(10)
    expect(page.locator("#row-count")).to_contain_text("downloads always include all 20")
    for kind, count in [("wide", 20), ("long", 200), ("all", report["full_position_count"])]:
        check_download(page, output, report, kind, count)

    page.locator("#load-chart").click()
    expect(page.locator("#chart-viewport iframe")).to_have_count(1)
    assert page.evaluate("window.mockWidgetConfig") == WIDGET_CONFIG
    assert widgets == [WIDGET_URL]
    expect(page.locator(".tradingview-widget-copyright")).to_be_visible()
    expect(page.locator(".tradingview-widget-copyright")).to_contain_text(
        "IGV chart by TradingView"
    )
    assert page.locator("#chart-viewport iframe").bounding_box()["height"] >= 480
    page.locator("#quarter-select").select_option(TEST_END)
    assert widgets == [WIDGET_URL], "Holdings selection must not reload or synchronize the chart"
    expect(page.locator("#chart-help")).to_be_visible()

    for width in (390, 375):
        page.set_viewport_size({"width": width, "height": 812})
        for mode in ("operating", "as-filed"):
            page.locator("#holdings-view").select_option(mode)
            expect(page.locator(".security-group")).to_have_count(10)
            expect(page.locator("#quarter-legend li")).to_have_count(4)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert page.locator("#quarter-chart-scroll").evaluate(
                "el => el.scrollWidth > el.clientWidth"
            )
            assert page.locator(".table-scroll").evaluate("el => el.scrollWidth > el.clientWidth")
            expect(page.locator("#holdings-view")).to_be_visible()
    assert widgets == [WIDGET_URL], "Holdings view changes must not reload the hosted chart"
    page.locator(".table-scroll").evaluate("el => el.scrollLeft = 300")
    assert page.locator(".table-scroll").evaluate("el => el.scrollLeft") > 0
    assert page.locator("#chart-viewport iframe").bounding_box()["height"] >= 390
    expect(page.locator(".tradingview-widget-copyright")).to_be_visible()
    page.clock.set_fixed_time(datetime(2032, 1, 6, 23, 59, tzinfo=UTC))
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#stale-warning")).to_be_hidden()
    page.clock.set_fixed_time(datetime(2032, 1, 7, tzinfo=UTC))
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#stale-warning")).to_be_visible()
    expect(page.locator("#stale-warning")).to_contain_text("normal publication lag")
    expect(page.locator("#data-through")).to_have_text(TEST_END)
    assert not errors, errors
    assert not outside, outside


def widget_failure(browser, origin, mode):
    context = browser.new_context()
    try:
        page = context.new_page()
        outside, widgets, errors = route_offline(page, origin, widget=mode)
        page.clock.install(time=TEST_NOW)
        page.goto(origin + "/igv-dashboard/", wait_until="networkidle")
        page.locator("#load-chart").click()
        if mode == "error":
            expect(page.locator("#chart-status")).to_contain_text("could not load")
        else:
            expect(page.locator("#chart-status")).to_contain_text("frame loading does not verify")
            page.clock.run_for(16000)
            expect(page.locator("#chart-status")).to_contain_text("may be blocked")
        expect(page.locator("#chart-help")).to_be_visible()
        expect(page.locator("#quarterly-body tr")).to_have_count(20)
        assert widgets == [WIDGET_URL] and not outside and not errors
    finally:
        context.close()


def main():
    with tempfile.TemporaryDirectory(prefix="igv-synthetic-smoke-") as temporary:
        root = Path(temporary)
        data, output = root / "data", root / "igv-dashboard"
        provider = Provider(warning_period=TEST_END, extra_month=True)
        for record in provider.records.values():
            if record["filing"].reported_date_hint < "2030-01-01":
                record["xml"] = record["xml"].replace(
                    b"Synthetic Test Company 01", b"Synthetic Historical Alias"
                )
        seed(data, provider=provider)
        report = app.build_site(data, output, test_only=True, now=TEST_NOW)
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(origin + "/igv-dashboard/", timeout=5) as response:
                assert response.status == 200 and b"SYNTHETIC TEST ONLY" in response.read()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1000}, accept_downloads=True
                    )
                    try:
                        smoke(context.new_page(), origin, output, report)
                    finally:
                        context.close()
                    for mode in ("error", "empty"):
                        widget_failure(browser, origin, mode)
                    offline = browser.new_context(java_script_enabled=False, accept_downloads=True)
                    try:
                        page = offline.new_page()
                        outside, widgets, errors = route_offline(page, origin)
                        page.goto(origin + "/igv-dashboard/", wait_until="networkidle")
                        expect(page.locator("#test-banner")).to_be_visible()
                        expect(page.locator("noscript p")).to_contain_text("default As filed (SEC)")
                        expect(page.locator("#view-controls")).to_be_hidden()
                        expect(page.locator("#quarterly-body tr")).to_have_count(20)
                        expect(page.locator("#quarterly-body .cell")).to_have_count(200)
                        expect(page.locator("#data-through")).to_have_text(TEST_END)
                        assert page.locator("#quarterly-body .symbol").all_text_contents() == [
                            row["title"] for item in report["snapshots"] for row in item["top10"]
                        ]
                        for kind, count in [
                            ("wide", 20),
                            ("long", 200),
                            ("all", report["full_position_count"]),
                        ]:
                            check_download(page, output, report, kind, count)
                        assert not outside and not widgets and not errors
                    finally:
                        offline.close()
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print(
        "PASS: synthetic Chromium smoke - 20 fiscal quarters / 200 positions, historical "
        "identifiers, keyboard-operated collateral views, quarterly controls/search, "
        "three exact CSVs, subpath, desktop/390px/375px mobile, no-JS "
        "table/downloads, filing-lag rollover and mocked hosted-chart success/error/timeout. "
        "No financial network requests; temporary site/browser profiles removed."
    )


if __name__ == "__main__":
    main()
