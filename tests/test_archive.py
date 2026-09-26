import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime

import pytest

import igv_snapshot as app
from tests.synthetic import TEST_END, TEST_NOW, Provider, seed


def tree_bytes(root):
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_manifest_persists_checkout_provenance_and_contract(archive, tmp_path):
    before = app.read_manifest(archive)
    validated = app.validate_manifest(before, archive, now=TEST_NOW, kind=app.SYNTHETIC)
    assert len(validated["snapshots"]) == len(validated["series"]) == 60
    assert sum(len(item["top10"]) for item in validated["snapshots"]) == 600
    for path in archive.rglob("*.json"):
        os.utime(path, (1, 1))
    after = app.read_manifest(archive)
    assert before == after
    assert app.validate_manifest(after, archive, now=TEST_NOW, kind=app.SYNTHETIC) == validated
    for entry in [*after["holdings"].values(), *after["prices"].values()]:
        assert entry["retrieved_at_utc"] == app.utc_text(TEST_NOW)
        assert (
            hashlib.sha256((archive / entry["object"]).read_bytes()).hexdigest() == entry["sha256"]
        )


def test_provenance_survives_actual_local_git_checkout(archive, tmp_path):
    repository, checkout = tmp_path / "fixture-repo", tmp_path / "fixture-checkout"
    repository.mkdir()
    shutil.copytree(archive, repository / "data")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    prefix = [
        "git",
        "-C",
        str(repository),
        "-c",
        "user.name=Synthetic Test",
        "-c",
        "user.email=synthetic@example.invalid",
        "-c",
        "commit.gpgsign=false",
    ]
    for arguments in [
        ["init", "--quiet"],
        ["add", "data"],
        [
            "commit",
            "--quiet",
            "-m",
            "Synthetic fixture checkout test\n\n"
            "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
        ],
        ["clone", "--quiet", "--no-hardlinks", str(repository), str(checkout)],
    ]:
        subprocess.run([*prefix, *arguments], env=env, check=True, capture_output=True)
    cloned = checkout / "data"
    manifest = app.read_manifest(cloned)
    assert manifest == app.read_manifest(archive)
    assert app.validate_manifest(manifest, cloned, now=TEST_NOW, kind=app.SYNTHETIC)
    assert manifest["last_successful_data_refresh_utc"] == app.utc_text(TEST_NOW)
    assert (cloned / "manifest.json").stat().st_mtime != TEST_NOW.timestamp()


def test_idempotent_no_new_month_has_no_network_or_timestamp_change(archive):
    before = tree_bytes(archive)

    def unexpected(_):
        pytest.fail("A no-new-month refresh must not fetch anything")

    assert not app.refresh_archive(
        archive,
        unexpected,
        kind=app.SYNTHETIC,
        clock=lambda: TEST_NOW.replace(day=20),
        sleep=lambda _: None,
    )
    assert tree_bytes(archive) == before


def test_catchup_fetches_only_missing_and_preserves_history(archive):
    before = app.read_manifest(archive)
    now = datetime(2031, 9, 15, tzinfo=UTC)
    provider = Provider("2031-08")
    assert app.refresh_archive(
        archive, provider, kind=app.SYNTHETIC, clock=lambda: now, sleep=lambda _: None
    )
    assert provider.calls == [
        *[app.source_url(month, app.SYNTHETIC) for month in ["2031-06", "2031-07", "2031-08"]],
        app.price_url("2031-08", app.SYNTHETIC),
    ]
    after = app.read_manifest(archive)
    assert len(after["holdings"]) == 63
    assert before["holdings"].items() <= after["holdings"].items()
    assert before["prices"].items() <= after["prices"].items()
    validated = app.validate_manifest(after, archive, now=now, kind=app.SYNTHETIC)
    assert validated["snapshots"][0]["month"] == "2026-09"
    assert validated["snapshots"][-1]["month"] == "2031-08"


def test_long_outage_catchup_does_not_leave_daily_history_gap(archive):
    now = datetime(2036, 9, 15, tzinfo=UTC)
    provider = Provider("2036-08")
    seed(archive, now=now, provider=provider)
    manifest = app.read_manifest(archive)
    assert set(manifest["prices"]) == {TEST_END, "2036-05", "2036-08"}
    assert len(manifest["holdings"]) == 123
    app.validate_manifest(manifest, archive, now=now, kind=app.SYNTHETIC)


def test_partial_delayed_response_never_promotes(archive):
    before = tree_bytes(archive)
    provider = Provider("2031-07")
    now = datetime(2031, 8, 15, tzinfo=UTC)
    failed_url = app.source_url("2031-07", app.SYNTHETIC)
    attempts = []

    def delayed(url):
        attempts.append(url)
        if url == failed_url:
            return Provider()(app.source_url(TEST_END, app.SYNTHETIC))
        return provider(url)

    with pytest.raises(app.DataError, match="Last-good archive retained"):
        app.refresh_archive(
            archive, delayed, kind=app.SYNTHETIC, clock=lambda: now, sleep=lambda _: None
        )
    assert attempts.count(failed_url) == 3
    assert tree_bytes(archive) == before
    assert not list(archive.glob(".stage-*"))
    seed(archive, now=now, provider=provider)
    assert app.read_manifest(archive)["data_through"] == "2031-07"


def test_failed_initial_seed_leaves_no_active_archive(tmp_path):
    root = tmp_path / "empty"
    with pytest.raises(app.DataError):
        app.refresh_archive(
            root,
            lambda _: b"{}",
            kind=app.SYNTHETIC,
            clock=lambda: TEST_NOW,
            sleep=lambda _: None,
        )
    assert not (root / "manifest.json").exists()


def test_manifest_promotion_failure_rolls_back_objects(archive, monkeypatch):
    before = tree_bytes(archive)
    replace = app.os.replace

    def fail_pointer(source, destination):
        if destination.name == "manifest.json":
            raise OSError("synthetic disk failure")
        replace(source, destination)

    monkeypatch.setattr(app.os, "replace", fail_pointer)
    with pytest.raises(OSError, match="synthetic disk failure"):
        seed(archive, now=datetime(2031, 7, 15, tzinfo=UTC), provider=Provider("2031-06"))
    assert tree_bytes(archive) == before
    assert not list(archive.glob(".stage-*"))


@pytest.mark.parametrize(
    "case",
    [
        "hash",
        "missing_month",
        "date",
        "url",
        "future_retrieval",
        "naive_retrieval",
        "wrong_product",
        "wrong_kind",
        "path",
        "missing_price",
        "invalid_json",
        "invalid_calendar",
        "old_retrieval",
    ],
)
def test_bad_archives_rejected(archive, case):
    manifest = app.read_manifest(archive)
    month = app.display_months(TEST_END)[0]
    entry = manifest["holdings"][month]
    if case == "hash":
        (archive / entry["object"]).write_bytes(b"{}")
    elif case == "missing_month":
        del manifest["holdings"][app.shift_month(month, 2)]
    elif case == "date":
        entry["as_of_date"] = "2031-01-01"
    elif case == "url":
        entry["source_url"] = "https://example.invalid/latest"
    elif case == "future_retrieval":
        entry["retrieved_at_utc"] = "2099-01-01T00:00:00Z"
    elif case == "naive_retrieval":
        entry["retrieved_at_utc"] = "2031-01-01"
    elif case == "wrong_product":
        manifest["product_id"] = 1
    elif case == "wrong_kind":
        manifest["data_kind"] = "production"
    elif case == "path":
        entry["object"] = "../outside.json"
    elif case == "missing_price":
        del manifest["prices"][TEST_END]
    elif case == "invalid_calendar":
        manifest["calendar"] = None
    elif case == "old_retrieval":
        entry["retrieved_at_utc"] = "2000-01-01T00:00:00Z"
    elif case == "invalid_json":
        (archive / "manifest.json").write_bytes(b"broken")
        with pytest.raises(app.DataError):
            app.read_manifest(archive)
        return
    with pytest.raises(app.DataError):
        app.validate_manifest(manifest, archive, now=TEST_NOW, kind=app.SYNTHETIC)


def test_stale_or_absent_archive_never_replaces_site(archive, tmp_path):
    output = tmp_path / "site"
    app.build_site(archive, output, test_only=True, now=TEST_NOW)
    before = tree_bytes(output)
    with pytest.raises(app.DataError, match="Stale archive"):
        app.build_site(archive, output, test_only=True, now=datetime(2031, 8, 1, 12, tzinfo=UTC))
    with pytest.raises(app.BlockedError, match="No complete authorized archive"):
        app.build_site(tmp_path / "absent", output, test_only=True, now=TEST_NOW)
    assert tree_bytes(output) == before


def test_build_reproducible_dynamic_escaped_and_relative(archive, tmp_path):
    output = tmp_path / "igv-dashboard"
    report = app.build_site(archive, output, test_only=True, now=TEST_NOW)
    before = tree_bytes(output)
    app.build_site(archive, output, test_only=True, now=TEST_NOW.replace(day=20))
    assert tree_bytes(output) == before
    page = (output / "index.html").read_text()
    assert "SYNTHETIC TEST ONLY" in page
    assert "2026-06 - 2031-05" in page
    assert str(archive) not in page and str(app.ROOT) not in page
    for kind, count in [("wide", 60), ("long", 600), ("performance", 60)]:
        path = output / report["downloads"][kind]
        assert not report["downloads"][kind].startswith("/")
        assert f'href="{path.name}"' in page
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
        assert len(rows) == count
        assert all(row["data_kind"] == app.SYNTHETIC for row in rows)
    assert set(before) == {"index.html", ".nojekyll", *report["downloads"].values()}


def test_unsafe_text_cannot_break_script_or_csv(archive, tmp_path):
    manifest = app.read_manifest(archive)
    month = app.display_months(TEST_END)[0]
    entry = manifest["holdings"][month]
    data = json.loads((archive / entry["object"]).read_bytes())
    points = data["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"][
        "dataPointsByNameMap"
    ]
    points["issueName"]["value"][-1] = "</script><script>window.testLeak=true</script>"
    points["ticker"]["value"][-1] = "=SYNTHETIC_FORMULA"
    raw = app.json_bytes(data)
    new_entry = {
        **entry,
        **app.provenance(
            raw,
            entry["source_url"],
            TEST_NOW,
            requested_date=entry["requested_date"],
            as_of_date=entry["as_of_date"],
        ),
    }
    manifest["holdings"][month] = new_entry
    app.promote_archive(archive, manifest, {new_entry["sha256"]: raw})
    report = app.build_site(archive, tmp_path / "site", test_only=True, now=TEST_NOW)
    page = (tmp_path / "site" / "index.html").read_text()
    assert "</script><script>window.testLeak" not in page
    assert "\\u003c/script>" in page
    csv_body = (tmp_path / "site" / report["downloads"]["long"]).read_text(encoding="utf-8-sig")
    assert "'=SYNTHETIC_FORMULA" in csv_body


def test_cli_and_live_gate_are_off_by_default(tmp_path, capsys, monkeypatch):
    assert app.main(["refresh", "--data-dir", str(tmp_path / "data")]) == 2
    assert app.main(["refresh", "--allow-network", "--data-dir", str(tmp_path / "data")]) == 2
    assert app.main(["build", "--data-dir", str(tmp_path / "data")]) == 2
    assert app.main(["validate", "--data-dir", str(tmp_path / "data")]) == 2
    with pytest.raises(app.BlockedError):
        app.download(app.price_url(TEST_END), allow_network=True)
    assert "BLOCKED" in capsys.readouterr().err
    monkeypatch.setenv("DATA_PUBLICATION_APPROVED", "true")
    with pytest.raises(app.BlockedError, match="explicit"):
        app.download(app.price_url(TEST_END))
    assert not (tmp_path / "data").exists()


def test_synthetic_build_cannot_target_publish_directory(archive):
    with pytest.raises(app.BlockedError, match="outside the repo"):
        app.build_site(archive, app.ROOT / "site", test_only=True, now=TEST_NOW)
