import copy
import csv
import io
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

import igv_snapshot as app
import sec_nport as sec
from tests.synthetic import TEST_END, TEST_NOW, Provider, filing, seed


def tree_bytes(root):
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def validate(root, *, now=TEST_NOW, **kwargs):
    return app.validate_manifest(
        app.read_manifest(root), root, now=now, kind=sec.SYNTHETIC, **kwargs
    )


def replace_object(root, manifest, accession, change):
    entry = manifest["filings"][accession]
    item = sec.decode_json(app.object_path(root, entry).read_bytes())
    change(item)
    raw = sec.json_bytes(item)
    checksum = sec.digest(raw)
    entry.update(object=f"objects/{checksum}.json", sha256=checksum)
    (root / entry["object"]).write_bytes(raw)
    (root / "manifest.json").write_bytes(sec.json_bytes(manifest))


def test_archive_contains_normalized_facts_only_and_rolls_20_quarters(archive):
    manifest = app.read_manifest(archive)
    validated = validate(archive)
    assert manifest["schema_version"] == 2
    assert manifest["source"] == sec.SOURCE
    assert manifest["identity"]["registrant_cik"] == sec.CIK
    assert manifest["data_through"] == TEST_END
    assert manifest["last_successful_data_refresh_utc"] == sec.utc_text(TEST_NOW)
    assert len(manifest["quarters"]) == 23
    assert len(validated["snapshots"]) == 20
    assert sum(len(item["top10"]) for item in validated["snapshots"]) == 200
    for content in tree_bytes(archive).values():
        assert b"SYNTHETIC NARRATIVE" not in content
        assert b"<edgarSubmission" not in content
        assert b"prices" not in content


def test_checkout_does_not_rewrite_provenance(archive, tmp_path):
    repository = tmp_path / "git-source"
    repository.mkdir()
    source = repository / "data"
    source.mkdir()
    for relative, raw in tree_bytes(archive).items():
        path = source / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
    commands = [
        ["git", "init", "--quiet", str(repository)],
        ["git", "-C", str(repository), "config", "user.name", "Synthetic Test"],
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        ["git", "-C", str(repository), "add", "data"],
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--quiet",
            "-m",
            "Synthetic provenance fixture\n\n"
            "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
        ],
        ["git", "clone", "--quiet", "--no-hardlinks", str(repository), str(tmp_path / "checkout")],
    ]
    for command in commands:
        subprocess.run(command, check=True, capture_output=True)
    checkout = tmp_path / "checkout/data"
    for path in checkout.rglob("*.json"):
        os.utime(path, (1, 1))
    assert tree_bytes(checkout) == tree_bytes(archive)
    assert validate(checkout)["snapshots"] == validate(archive)["snapshots"]


def test_unchanged_daily_check_has_one_discovery_call_and_no_timestamp_commit(archive):
    before = tree_bytes(archive)
    provider = Provider(warning_period=TEST_END)
    changed = app.refresh_archive(
        archive, provider, kind=sec.SYNTHETIC, clock=lambda: TEST_NOW + timedelta(days=1)
    )
    assert not changed
    assert len(provider.calls) == 1 and "/search-index?" in provider.calls[0]
    assert tree_bytes(archive) == before


def test_refresh_catches_up_and_retains_old_normalized_objects(archive):
    before = app.read_manifest(archive)
    provider = Provider("2032-10-31", count=28)
    now = datetime(2032, 12, 16, 12, tzinfo=UTC)
    assert app.refresh_archive(archive, provider, kind=sec.SYNTHETIC, clock=lambda: now)
    after = app.read_manifest(archive)
    assert len(after["quarters"]) == 28
    assert set(before["filings"]) < set(after["filings"])
    assert all(after["filings"][key] == value for key, value in before["filings"].items())
    assert len([url for url in provider.calls if url.endswith(".xml")]) == 5
    assert len(provider.calls) == 11
    validated = validate(archive, now=now)
    assert validated["snapshots"][-1]["reported_as_of"] == "2032-10-31"
    assert len(validated["snapshots"]) == 20


def test_latest_legitimate_amendment_selected_and_originals_retained(archive):
    old = app.read_manifest(archive)
    original = old["quarters"][TEST_END[:7]]
    provider = Provider()
    first = provider.add(TEST_END, amendment=1, filed="2031-09-15", allocation_delta="3")
    second = provider.add(TEST_END, amendment=2, filed="2031-09-15", allocation_delta="4")
    now = TEST_NOW + timedelta(days=1)
    assert app.refresh_archive(archive, provider, kind=sec.SYNTHETIC, clock=lambda: now)
    manifest = app.read_manifest(archive)
    assert manifest["quarters"][TEST_END[:7]] == second.accession
    assert manifest["filings"][original] == old["filings"][original]
    assert {original, first.accession, second.accession} <= set(manifest["filings"])
    last = validate(archive, now=now)["snapshots"][-1]
    assert last["revision_count"] == 3 and last["provenance"]["form"] == "NPORT-P/A"


def test_old_quarter_amendments_are_checked_beyond_display_window(archive):
    provider = Provider()
    amendment = provider.add("2026-01-31", amendment=1, filed="2031-09-15")
    now = TEST_NOW + timedelta(days=1)
    app.refresh_archive(archive, provider, kind=sec.SYNTHETIC, clock=lambda: now)
    manifest = app.read_manifest(archive)
    assert manifest["quarters"]["2026-01"] == amendment.accession
    assert all(
        item["reported_as_of"] != "2026-01-31" for item in validate(archive, now=now)["snapshots"]
    )


def test_extra_public_months_are_archived_but_not_counted_as_quarters(archive):
    provider = Provider(extra_month=True)
    app.refresh_archive(archive, provider, kind=sec.SYNTHETIC, clock=lambda: TEST_NOW)
    manifest = app.read_manifest(archive)
    assert len(manifest["filings"]) == 24
    assert len(manifest["quarters"]) == 23
    assert "2031-06" not in manifest["quarters"]
    assert all(item["fiscal_quarter"] is not None for item in validate(archive)["snapshots"])


def test_late_source_failure_does_not_promote_partial_acquisition(archive):
    before = tree_bytes(archive)
    provider = Provider("2032-01-31", count=25)
    provider.records[filing("2032-01-31").accession]["xml"] = b"<html>unavailable</html>"
    with pytest.raises(sec.DataError):
        app.refresh_archive(
            archive,
            provider,
            kind=sec.SYNTHETIC,
            clock=lambda: datetime(2032, 3, 18, 12, tzinfo=UTC),
        )
    assert tree_bytes(archive) == before
    assert not list(archive.glob(".stage-*"))


def test_no_data_or_gaps_do_not_create_a_real_looking_archive(tmp_path):
    empty = tmp_path / "empty"
    with pytest.raises(sec.DataError, match="No publicly reported"):
        seed(empty, provider=Provider(count=0))
    assert not (empty / "manifest.json").exists()
    provider = Provider()
    provider.records.pop(filing("2029-01-31").accession)
    with pytest.raises(sec.DataError, match="Missing fiscal quarter"):
        seed(tmp_path / "gap", provider=provider)
    with pytest.raises(sec.DataError, match="only 19"):
        seed(tmp_path / "short", provider=Provider(count=19))


def test_archive_promotion_failure_rolls_back_new_objects(archive, monkeypatch):
    before = tree_bytes(archive)
    original = os.replace

    def fail_manifest(source, target):
        if str(target).endswith("manifest.json"):
            raise OSError("Synthetic promotion failure")
        return original(source, target)

    monkeypatch.setattr(app.os, "replace", fail_manifest)
    with pytest.raises(OSError, match="promotion"):
        app.refresh_archive(
            archive,
            Provider("2031-10-31", count=24),
            kind=sec.SYNTHETIC,
            clock=lambda: datetime(2031, 12, 16, 12, tzinfo=UTC),
        )
    assert tree_bytes(archive) == before
    assert not list(archive.glob(".stage-*"))


def test_missing_or_changed_known_search_metadata_is_not_silently_ignored(archive):
    before = tree_bytes(archive)
    provider = Provider()
    provider.records.pop(filing("2026-01-31").accession)
    with pytest.raises(sec.DataError, match="disappeared"):
        app.refresh_archive(archive, provider, kind=sec.SYNTHETIC, clock=lambda: TEST_NOW)
    provider = Provider()

    def changed(url):
        result = json.loads(provider(url))
        result["hits"]["hits"][0]["_source"]["period_ending"] = "2025-10-31"
        return sec.json_bytes(result)

    with pytest.raises(sec.DataError, match="metadata changed"):
        app.refresh_archive(archive, changed, kind=sec.SYNTHETIC, clock=lambda: TEST_NOW)
    assert tree_bytes(archive) == before


@pytest.mark.parametrize(
    "case",
    [
        "wrong-source",
        "wrong-kind",
        "old-schema",
        "extra-prose",
        "wrong-through",
        "wrong-selection",
        "future-refresh",
    ],
)
def test_invalid_manifest_is_rejected(archive, case):
    manifest = app.read_manifest(archive)
    if case == "wrong-source":
        manifest["source"] = "legacy-source"
    elif case == "wrong-kind":
        manifest["data_kind"] = sec.PRODUCTION
    elif case == "old-schema":
        manifest["schema_version"] = 1
    elif case == "extra-prose":
        manifest["narrative"] = "not normalized facts"
    elif case == "wrong-through":
        manifest["data_through"] = "2031-10-31"
    elif case == "wrong-selection":
        manifest["quarters"]["2031-07"] = manifest["quarters"]["2031-04"]
    else:
        manifest["last_successful_data_refresh_utc"] = "2099-01-01T00:00:00Z"
    with pytest.raises(sec.DataError):
        app.validate_manifest(manifest, archive, now=TEST_NOW, kind=sec.SYNTHETIC)


@pytest.mark.parametrize(
    "case", ["checksum", "missing", "path", "symlink", "prose", "source", "count", "rank-kind"]
)
def test_bad_immutable_objects_fail_closed(archive, tmp_path, case):
    manifest = app.read_manifest(archive)
    accession = manifest["quarters"]["2031-07"]
    entry = manifest["filings"][accession]
    path = app.object_path(archive, entry)
    if case == "checksum":
        path.write_bytes(path.read_bytes() + b" ")
    elif case == "missing":
        path.unlink()
    elif case == "path":
        entry["object"] = "../outside.json"
        (archive / "manifest.json").write_bytes(sec.json_bytes(manifest))
    elif case == "symlink":
        other = tmp_path / "outside.json"
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
    else:

        def mutate(item):
            if case == "prose":
                item["footnotes"] = "Source narrative"
            elif case == "source":
                item["provenance"]["source_url"] = "https://example.invalid/unapproved.xml"
            elif case == "count":
                item["source_row_count"] += 1
            else:
                item["data_kind"] = sec.PRODUCTION

        replace_object(archive, manifest, accession, mutate)
    with pytest.raises(sec.DataError):
        validate(archive)


def test_conflicting_amendment_dates_or_missing_originals_are_not_guessed(archive):
    snapshots = validate(archive)["all_filings"]
    item = copy.deepcopy(snapshots[filing().accession])
    item["provenance"]["accession"] = filing(amendment=1).accession
    item["provenance"]["form"] = "NPORT-P/A"
    with pytest.raises(sec.DataError, match="one original"):
        app.active_quarters({item["provenance"]["accession"]: item})
    item["reported_as_of"] = "2031-07-30"
    snapshots[item["provenance"]["accession"]] = item
    with pytest.raises(sec.DataError, match="Conflicting"):
        app.active_quarters(snapshots)


def test_stale_or_failed_refresh_preserves_last_good_site(archive, tmp_path):
    output = tmp_path / "site"
    app.build_site(archive, output, test_only=True, now=TEST_NOW)
    before_data, before_site = tree_bytes(archive), tree_bytes(output)
    now = datetime(2032, 1, 7, 12, tzinfo=UTC)
    with pytest.raises(sec.DataError, match="overdue"):
        app.build_site(archive, output, test_only=True, now=now)
    with pytest.raises(sec.DataError, match="overdue"):
        app.refresh_archive(archive, Provider(), kind=sec.SYNTHETIC, clock=lambda: now)
    assert tree_bytes(archive) == before_data
    assert tree_bytes(output) == before_site
    validate(archive, now=now, require_current=False)


def test_site_and_csvs_are_exact_reproducible_relative_and_holdings_only(archive, tmp_path):
    output = tmp_path / "igv-dashboard"
    report = app.build_site(archive, output, test_only=True, now=TEST_NOW)
    before = tree_bytes(output)
    app.build_site(archive, output, test_only=True, now=TEST_NOW + timedelta(days=1))
    assert tree_bytes(output) == before
    page = (output / "index.html").read_text()
    assert page.count('data-period="') == 40
    assert "__STATIC_TABLE__" not in page and "monthly-table" not in page
    assert "SYNTHETIC TEST ONLY" in page
    assert "priceByMonth" not in page and "download-performance" not in page
    assert set(report["downloads"]) == {"long", "wide"}
    for kind, count in [("wide", 20), ("long", 200)]:
        filename = report["downloads"][kind]
        assert not filename.startswith("/")
        rows = list(
            csv.DictReader(io.StringIO((output / filename).read_text(encoding="utf-8-sig")))
        )
        assert len(rows) == count
        assert {row["data_kind"] for row in rows} == {sec.SYNTHETIC}
        assert {row["reported_as_of"] for row in rows} == {
            item["reported_as_of"] for item in report["snapshots"]
        }
        assert "close" not in rows[0] and "volume" not in rows[0]
        if kind == "long":
            expected = [row for item in report["snapshots"] for row in item["top10"]]
            assert [row["weight_pct"] for row in rows] == [row["weight_pct"] for row in expected]
            assert [row["rank"] for row in rows] == [str(row["rank"]) for row in expected]
            assert rows[1]["ticker"] == ""


def test_amendment_site_promotion_failure_preserves_original_html_and_downloads(
    archive, tmp_path, monkeypatch
):
    output = tmp_path / "site"
    app.build_site(archive, output, test_only=True, now=TEST_NOW)
    before = tree_bytes(output)
    provider = Provider()
    provider.add(TEST_END, amendment=1, filed="2031-09-15", allocation_delta="3")
    now = TEST_NOW + timedelta(days=1)
    app.refresh_archive(archive, provider, kind=sec.SYNTHETIC, clock=lambda: now)
    original = os.replace

    def fail_page(source, target):
        if str(target).endswith("index.html"):
            raise OSError("Synthetic page promotion failure")
        return original(source, target)

    monkeypatch.setattr(app.os, "replace", fail_page)
    with pytest.raises(OSError, match="promotion"):
        app.build_site(archive, output, test_only=True, now=now)
    assert tree_bytes(output) == before
    assert not list(tmp_path.glob(".site-stage-*"))


def test_html_json_and_csv_formula_safety(archive, tmp_path):
    manifest = app.read_manifest(archive)
    accession = manifest["quarters"]["2031-07"]
    hostile = '=HYPERLINK("bad")</script><script>window.bad=1</script>'

    def mutate(item):
        item["holdings"][0]["name"] = hostile
        item["holdings"][0]["title"] = hostile
        item["holdings"][0]["ticker"] = ["+TEST"]
        item["holdings"][0]["cusip"] = "@TEST"

    replace_object(archive, manifest, accession, mutate)
    output = tmp_path / "site"
    report = app.build_site(archive, output, test_only=True, now=TEST_NOW)
    page = (output / "index.html").read_text()
    assert "</script><script>window.bad" not in page
    assert "&lt;/script&gt;" in page and "\\u003c/script>" in page
    rows = list(
        csv.DictReader(
            io.StringIO((output / report["downloads"]["long"]).read_text(encoding="utf-8-sig"))
        )
    )
    assert rows[-10]["name"].startswith("'=")
    assert rows[-10]["ticker"] == "'+TEST"
    assert rows[-10]["cusip"] == "'@TEST"
    text = app.csv_text(["text", "number"], [["\t=1", sec.decimal("-0.25")]])
    assert "'\t=1" in text and ",-0.25" in text


def test_unknown_or_legacy_site_files_are_not_republished(archive, tmp_path):
    output = tmp_path / "site"
    output.mkdir()
    (output / "legacy_performance.csv").write_text("not provider data; synthetic sentinel")
    before = tree_bytes(output)
    with pytest.raises(sec.DataError, match="non-SEC"):
        app.build_site(archive, output, test_only=True, now=TEST_NOW)
    assert tree_bytes(output) == before


def test_cli_and_no_data_gates_do_not_fetch_or_create_output(tmp_path, monkeypatch, capsys):
    root, output = tmp_path / "data", tmp_path / "site"
    for command in ["refresh", "bootstrap", "build"]:
        assert app.main([command, "--data-dir", str(root), "--output", str(output)]) == 2
    monkeypatch.setenv("DATA_PUBLICATION_APPROVED", "true")
    assert app.main(["build", "--data-dir", str(root)]) == 2
    monkeypatch.setenv("SEC_PUBLICATION_APPROVED", "true")
    provider = Provider()
    with pytest.raises(sec.BlockedError, match="No verified SEC seed"):
        app.refresh_archive(root, provider)
    assert provider.calls == []
    assert app.main(["refresh", "--allow-network", "--data-dir", str(root)]) == 2
    assert not root.exists() and not output.exists()
    assert "BLOCKED" in capsys.readouterr().err


def test_synthetic_and_unsafe_output_routes_are_blocked(archive, tmp_path):
    with pytest.raises(sec.BlockedError):
        app.build_site(archive, app.ROOT / "site", test_only=True, now=TEST_NOW)
    for output in [archive, archive / "site", archive.parent]:
        with pytest.raises(sec.DataError):
            app.build_site(archive, output, test_only=True, now=TEST_NOW)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "other")
    with pytest.raises(sec.DataError):
        app.build_site(archive, link, test_only=True, now=TEST_NOW)
