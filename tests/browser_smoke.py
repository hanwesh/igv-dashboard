"""Repeatable offline UI smoke check. Temporary synthetic site, never a Pages artifact."""

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


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


def smoke(page, origin, output, report):
    errors, outside = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def local_only(route):
        if route.request.url.startswith(origin + "/"):
            route.continue_()
        else:
            outside.append(route.request.url)
            route.abort()

    page.route("**/*", local_only)
    page.clock.install(time=TEST_NOW)
    response = page.goto(origin + "/igv-dashboard/", wait_until="networkidle")
    assert response.status == 200
    expect(page).to_have_title(
        "SYNTHETIC TEST ONLY | Performance and monthly holdings | 2026-06 - 2031-05"
    )
    expect(page.locator("#test-banner")).to_be_visible()
    expect(page.locator("#stale-warning")).to_be_hidden()
    expect(page.locator("#coverage")).to_have_text("60 / 60")
    expect(page.locator("#monthly-body tr")).to_have_count(60)
    expect(page.locator("#monthly-body .cell")).to_have_count(600)
    expect(page.locator("#month-select option")).to_have_count(60)
    expect(page.locator(".return-bar")).to_have_count(60)
    expect(page.locator(".volume-bar")).to_have_count(60)
    assert page.locator('.return-bar[fill="#169477"]').count() > 0
    assert page.locator('.return-bar[fill="#db5b64"]').count() > 0
    expect(page.locator("#source-notes")).to_contain_text("102%")
    expect(page.locator("#source-notes")).to_contain_text("without normalization")
    expect(page.locator("#price-methodology")).to_contain_text(
        report["performance"]["baseline_date"]
    )
    expect(page.locator("#price-methodology")).to_contain_text("not total returns")
    expect(page.locator("#data-through")).to_have_text("May 2031")
    expect(page.locator("#last-refresh")).to_contain_text("2031-06-15")
    assert (
        page.locator("#product-link").get_attribute("href").startswith("https://example.invalid/")
    )
    initial_month = page.locator("#month-select").input_value()
    first = page.locator(".price-point").first
    first.hover()
    expect(page.locator("#chart-inspector")).to_contain_text("Jun 2026")
    assert page.locator("#month-select").input_value() == initial_month
    first.click()
    expect(page.locator("#month-select")).to_have_value("2026-06")
    expect(page.locator(".holding-card")).to_have_count(10)
    expect(first).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#month-note")).to_contain_text("Source as of")
    expect(page.locator("#month-note")).to_contain_text("SHA256")
    first.focus()
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.dataset.priceMonth") == "2026-07"
    page.keyboard.press("Enter")
    expect(page.locator("#month-select")).to_have_value("2026-07")
    page.keyboard.press("End")
    page.keyboard.press("Space")
    expect(page.locator("#month-select")).to_have_value(TEST_END)
    page.keyboard.press("Home")
    page.keyboard.press("Enter")
    expect(page.locator("#month-select")).to_have_value("2026-06")
    for count in [12, 36, 60]:
        page.locator(f'[data-range="{count}"]').click()
        expect(page.locator(".price-point")).to_have_count(count)
        expect(page.locator('.price-point[tabindex="0"]')).to_have_count(1)
        expect(page.locator("#range-return")).to_contain_text(f"{count // 12}Y price return")
        series = report["performance"]["series"][-count:]
        expected_return = (series[-1]["close"] / series[0]["previous_close"] - 1) * 100
        expect(page.locator("#range-return")).to_contain_text(f"{expected_return:.2f}%")
    page.locator("#chart-view").select_option("ohlc")
    expect(page.locator(".ohlc-bar")).to_have_count(60)
    expect(page.locator(".return-bar")).to_have_count(0)
    expect(page.locator(".volume-bar")).to_have_count(60)
    page.locator("#chart-view").select_option("returns")
    page.locator("#year-select").select_option("2028")
    expect(page.locator("#monthly-body tr")).to_have_count(12)
    page.locator("#ticker-search").fill("TEST00")
    expect(page.locator("#monthly-body .match")).to_have_count(12)
    page.locator("#order-select").select_option("desc")
    expect(page.locator("#monthly-body tr").first).to_have_attribute("data-month", "2028-12")
    page.locator("#monthly-body .month-button").first.click()
    expect(page.locator("#month-select")).to_have_value("2028-12")
    expect(page.locator('[data-price-month="2028-12"]')).to_have_attribute("aria-pressed", "true")
    page.locator("#month-select").select_option("2029-01")
    expect(page.locator('[data-price-month="2029-01"]')).to_have_attribute("aria-pressed", "true")
    for kind, row_count in [("wide", 60), ("long", 600), ("performance", 60)]:
        link = page.locator(f"#download-{kind}")
        href = link.get_attribute("href")
        assert href == report["downloads"][kind] and not href.startswith("/")
        with page.expect_download() as download_event:
            link.click()
        download = download_event.value
        assert download.suggested_filename == href
        assert "SYNTHETIC_TEST_ONLY" in download.suggested_filename
        text = Path(download.path()).read_text(encoding="utf-8-sig")
        assert text == (output / href).read_text(encoding="utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        assert len(rows) == row_count
        assert {row["data_kind"] for row in rows} == {app.SYNTHETIC}
    page.set_viewport_size({"width": 375, "height": 812})
    expect(page.locator(".holding-card")).to_have_count(10)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator(".table-scroll").evaluate("el => el.scrollWidth > el.clientWidth")
    assert page.locator("#chart-scroll").evaluate("el => el.scrollWidth > el.clientWidth")
    page.locator(".table-scroll").evaluate("el => el.scrollLeft = 300")
    assert page.locator(".table-scroll").evaluate("el => el.scrollLeft") > 0
    # Freshness is computed locally; no Actions, server timestamp or fetch is needed.
    page.clock.set_fixed_time(datetime(2031, 7, 1, 3, 59, tzinfo=UTC))
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#stale-warning")).to_be_hidden()
    page.clock.set_fixed_time(datetime(2031, 7, 1, 4, 0, tzinfo=UTC))
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#stale-warning")).to_be_visible()
    expect(page.locator("#stale-warning")).to_contain_text("Jun 2031")
    expect(page.locator("#data-through")).to_have_text("May 2031")
    assert not errors, errors
    assert not outside, outside


def main():
    with tempfile.TemporaryDirectory(prefix="igv-synthetic-smoke-") as temporary:
        root = Path(temporary)
        data, output = root / "data", root / "igv-dashboard"
        seed(data, provider=Provider(warning_month=TEST_END))
        report = app.build_site(data, output, test_only=True, now=TEST_NOW)
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(origin + "/igv-dashboard/", timeout=5) as response:
                assert response.status == 200
                assert b"SYNTHETIC TEST ONLY" in response.read()
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
                    offline = browser.new_context(java_script_enabled=False, accept_downloads=True)
                    try:
                        page = offline.new_page()
                        page.route(
                            "**/*",
                            lambda route: (
                                route.continue_()
                                if route.request.url.startswith(origin + "/")
                                else route.abort()
                            ),
                        )
                        page.goto(origin + "/igv-dashboard/", wait_until="networkidle")
                        expect(page.locator("#test-banner")).to_be_visible()
                        expect(page.locator("noscript p")).to_contain_text("three CSV links")
                        with page.expect_download() as event:
                            page.locator("#download-long").click()
                        assert event.value.suggested_filename == report["downloads"]["long"]
                    finally:
                        offline.close()
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print(
        "PASS: synthetic browser smoke - 60/600, returns/OHLC/volume, 1Y/3Y/5Y, "
        "hover/keyboard/holdings sync, filters, 3 CSVs, mobile, subpath and stale clock. "
        "Temporary site and browser profile removed; no financial requests."
    )


if __name__ == "__main__":
    main()
