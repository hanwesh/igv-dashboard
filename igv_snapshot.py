#!/usr/bin/env python3
"""SEC-only quarterly IGV archive and native dashboard. No local price feed."""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from sec_nport import (
    FUND_NAME,
    MAPPING_URL,
    PRODUCTION,
    SOURCE,
    SYMBOL,
    SYNTHETIC,
    BlockedError,
    DataError,
    SECClient,
    check_hash,
    decimal,
    decode_json,
    digest,
    discover_filings,
    filing_url,
    index_metadata,
    json_bytes,
    month_end,
    month_shift,
    normalize_filing,
    parse_date,
    parse_utc,
    ranked_snapshot,
    require_publication,
    resolve_identity,
    utc_now,
    utc_text,
    validate_identity,
    validate_snapshot,
)

ROOT = Path(__file__).resolve().parent
QUARTER_COUNT = 20
PUBLICATION_LAG_DAYS = 60
PUBLICATION_GRACE_DAYS = 7
MANIFEST_KEYS = {
    "schema_version",
    "data_kind",
    "source",
    "identity",
    "discovery_start_date",
    "last_successful_data_refresh_utc",
    "data_through",
    "filings",
    "quarters",
}


def object_path(root: Path, entry: dict) -> Path:
    if not isinstance(entry, dict) or set(entry) != {"object", "sha256"}:
        raise DataError("Invalid normalized object reference")
    digest_value = entry["sha256"]
    check_hash(digest_value)
    relative = f"objects/{digest_value}.json"
    if entry["object"] != relative:
        raise DataError("Invalid immutable object path")
    path = root / relative
    if path.is_symlink() or path.parent.resolve() != root.resolve() / "objects":
        raise DataError("Archive objects must not be symlinks or external paths")
    return path


def read_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if path.is_symlink():
        raise DataError("The manifest must not be a symlink")
    if not path.is_file():
        raise BlockedError(
            "No verified SEC factual archive. Build/deployment is blocked; "
            "synthetic fixtures are never a production fallback."
        )
    return decode_json(path.read_bytes())


def active_quarters(snapshots: dict[str, dict]) -> dict[str, str]:
    grouped: dict[str, list[dict]] = {}
    for item in snapshots.values():
        if item["fiscal_quarter"] is not None:
            grouped.setdefault(item["reported_as_of"][:7], []).append(item)
    selected = {}
    for period, candidates in sorted(grouped.items()):
        dates = {item["reported_as_of"] for item in candidates}
        fiscal_ends = {item["fiscal_year_end"] for item in candidates}
        if len(dates) != 1 or len(fiscal_ends) != 1:
            raise DataError("Conflicting reported/fiscal dates for one quarter require review")
        ordered = sorted(
            candidates,
            key=lambda item: (
                parse_utc(item["provenance"]["accepted_at_utc"]),
                item["provenance"]["accession"],
            ),
        )
        originals = [item for item in ordered if item["provenance"]["form"] == "NPORT-P"]
        if len(originals) != 1:
            raise DataError("Quarter needs one original NPORT-P, with any subsequent amendments")
        if ordered[0] is not originals[0]:
            raise DataError("An amendment predates the quarter's original filing")
        selected[period] = ordered[-1]["provenance"]["accession"]
    return selected


def stale_after(reported_as_of: str) -> str:
    next_quarter_end = month_end(month_shift(parse_date(reported_as_of), 3))
    return (
        next_quarter_end + timedelta(days=PUBLICATION_LAG_DAYS + PUBLICATION_GRACE_DAYS)
    ).isoformat()


def validate_manifest(
    manifest: dict,
    root: Path,
    *,
    now: datetime | None = None,
    kind: str = PRODUCTION,
    require_current: bool = True,
    pending: dict[str, bytes] | None = None,
) -> dict:
    now = now or utc_now()
    if now.tzinfo is None:
        raise DataError("Validation requires a timezone-aware clock")
    if kind not in {PRODUCTION, SYNTHETIC}:
        raise DataError("Unrecognized archive kind")
    pending = pending or {}
    if (
        not isinstance(manifest, dict)
        or set(manifest) != MANIFEST_KEYS
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 2
        or manifest["data_kind"] != kind
        or manifest["source"] != SOURCE
    ):
        raise DataError("SEC-only manifest schema/source/data kind mismatch; legacy data rejected")
    identity = manifest["identity"]
    validate_identity(identity, now, kind)
    refreshed = parse_utc(manifest["last_successful_data_refresh_utc"])
    if refreshed > now or parse_utc(identity["resolved_at_utc"]) > refreshed:
        raise DataError("Invalid successful data refresh timestamp")
    start = parse_date(manifest["discovery_start_date"])
    if start > refreshed.date():
        raise DataError("Invalid discovery start date")
    entries = manifest["filings"]
    if not isinstance(entries, dict) or not entries:
        raise DataError("Missing normalized SEC filings")
    snapshots, hashes = {}, {}
    for accession, entry in entries.items():
        path = object_path(root, entry)
        try:
            raw = pending[entry["sha256"]] if entry["sha256"] in pending else path.read_bytes()
        except FileNotFoundError as error:
            raise DataError("Missing normalized SEC object") from error
        if digest(raw) != entry["sha256"]:
            raise DataError("Normalized object SHA256 mismatch")
        item = decode_json(raw)
        if json_bytes(item) != raw:
            raise DataError("SEC facts must use the canonical normalized JSON format")
        validate_snapshot(item, identity, refreshed, kind)
        if item["provenance"]["accession"] != accession:
            raise DataError("Manifest accession does not match normalized source provenance")
        if parse_date(item["provenance"]["filing_date"]) < start:
            raise DataError("Filing precedes the declared discovery range")
        snapshots[accession], hashes[accession] = item, entry["sha256"]
    quarters = active_quarters(snapshots)
    if quarters != manifest["quarters"]:
        raise DataError("Active quarters do not select the latest appropriate SEC amendments")
    if len(quarters) < QUARTER_COUNT:
        raise DataError(
            f"Need {QUARTER_COUNT} reported fiscal quarters; only {len(quarters)} available"
        )
    periods = sorted(quarters)[-QUARTER_COUNT:]
    for previous, current in zip(periods, periods[1:]):
        if month_shift(parse_date(previous + "-01"), 3).strftime("%Y-%m") != current:
            raise DataError("Missing fiscal quarter in the rolling display; no forward filling")
    displayed = [snapshots[quarters[period]] for period in periods]
    if len({item["fiscal_year_end"][5:7] for item in displayed}) != 1:
        raise DataError("Fiscal-year convention changed; manual cadence review required")
    latest = displayed[-1]["reported_as_of"]
    if manifest["data_through"] != latest:
        raise DataError("Data-through date differs from the latest reported quarter")
    if require_current and now.date() > parse_date(stale_after(latest)):
        raise DataError(
            "SEC holdings are overdue beyond the normal filing lag and grace period. "
            "Last-good archive/site retained; investigate missing public filings."
        )
    return {
        "snapshots": [
            {
                **ranked_snapshot(item, hashes[item["provenance"]["accession"]]),
                "revision_count": sum(
                    other["reported_as_of"] == item["reported_as_of"]
                    for other in snapshots.values()
                ),
            }
            for item in displayed
        ],
        "all_filings": snapshots,
        "quarter_count": len(quarters),
    }


def promote_archive(root: Path, manifest: dict, pending: dict[str, bytes]) -> None:
    if root.is_symlink() or (root / "manifest.json").is_symlink():
        raise DataError("Archive promotion refuses symlinks")
    root.mkdir(parents=True, exist_ok=True)
    if (root / "objects").is_symlink():
        raise DataError("Archive object directory must not be a symlink")
    added = []
    committed = False
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=root) as temporary:
        stage = Path(temporary)
        for hash_value, raw in pending.items():
            check_hash(hash_value)
            if digest(raw) != hash_value:
                raise DataError("Pending normalized object SHA256 mismatch")
            (stage / f"{hash_value}.json").write_bytes(raw)
        staged_manifest = stage / "manifest.json"
        staged_manifest.write_bytes(json_bytes(manifest))
        (root / "objects").mkdir(exist_ok=True)
        try:
            for hash_value, raw in pending.items():
                entry = {"object": f"objects/{hash_value}.json", "sha256": hash_value}
                target = object_path(root, entry)
                if target.exists():
                    if target.read_bytes() != raw:
                        raise DataError("Immutable normalized object was modified")
                    continue
                os.replace(stage / f"{hash_value}.json", target)
                added.append(target)
            os.replace(staged_manifest, root / "manifest.json")
            committed = True
        finally:
            if not committed:
                for path in added:
                    path.unlink()


def refresh_archive(
    root: Path,
    fetcher: Callable[[str], bytes],
    *,
    kind: str = PRODUCTION,
    clock: Callable[[], datetime] = utc_now,
    bootstrap: bool = False,
) -> bool:
    if kind == PRODUCTION:
        require_publication(os.environ)
        if bootstrap and os.environ.get("GITHUB_ACTIONS") == "true":
            raise BlockedError("Actions cannot bootstrap a production archive")
    elif kind != SYNTHETIC:
        raise DataError("Unrecognized archive kind")
    now = clock()
    existing = read_manifest(root) if (root / "manifest.json").exists() else None
    if existing is None and not bootstrap:
        raise BlockedError(
            "No verified SEC seed. Scheduled refresh cannot bootstrap a production dataset; "
            "an operator must acquire, audit and commit a factual seed before activation."
        )
    previous = None
    if existing is not None:
        previous = validate_manifest(existing, root, now=now, kind=kind, require_current=False)
        manifest = decode_json(json_bytes(existing))
    else:
        mapping_url = (
            "https://example.invalid/synthetic/company_tickers_mf.json"
            if kind == SYNTHETIC
            else MAPPING_URL
        )
        mapping = fetcher(mapping_url)
        identity = resolve_identity(mapping, clock(), kind)
        manifest = {
            "schema_version": 2,
            "data_kind": kind,
            "source": SOURCE,
            "identity": identity,
            "discovery_start_date": month_shift(now.date().replace(day=1), -72).isoformat(),
            "last_successful_data_refresh_utc": utc_text(now),
            "data_through": "",
            "filings": {},
            "quarters": {},
        }
    identity = manifest["identity"]
    filings = discover_filings(
        fetcher, identity, manifest["discovery_start_date"], now.date().isoformat(), kind
    )
    discovered = {filing.accession for filing in filings}
    if not set(manifest["filings"]) <= discovered:
        raise DataError("Previously archived SEC accessions disappeared from discovery")
    if previous:
        for filing in filings:
            if filing.accession not in previous["all_filings"]:
                continue
            archived = previous["all_filings"][filing.accession]
            provenance = archived["provenance"]
            if (
                filing.form != provenance["form"]
                or filing.filing_date != provenance["filing_date"]
                or filing.reported_date_hint != archived["reported_as_of"]
                or filing.document_url(kind) != provenance["source_url"]
            ):
                raise DataError("SEC metadata changed for an immutable archived accession")
    new_filings = [filing for filing in filings if filing.accession not in manifest["filings"]]
    if existing is not None and not new_filings:
        validate_manifest(existing, root, now=now, kind=kind)
        return False
    pending = {}
    snapshots = previous["all_filings"] if previous else {}
    for filing in new_filings:
        index = fetcher(filing_url(filing.accession, kind))
        metadata = index_metadata(index, filing, kind)
        raw = fetcher(filing.document_url(kind))
        normalized = normalize_filing(raw, filing, identity, metadata, clock(), kind)
        content = json_bytes(normalized)
        hash_value = digest(content)
        pending[hash_value] = content
        manifest["filings"][filing.accession] = {
            "object": f"objects/{hash_value}.json",
            "sha256": hash_value,
        }
        snapshots[filing.accession] = normalized
    quarters = active_quarters(snapshots)
    if not quarters:
        raise DataError("No publicly reported fiscal-quarter portfolios found")
    manifest["quarters"] = quarters
    manifest["data_through"] = snapshots[quarters[max(quarters)]]["reported_as_of"]
    manifest["last_successful_data_refresh_utc"] = utc_text(clock())
    validate_manifest(manifest, root, now=clock(), kind=kind, pending=pending)
    promote_archive(root, manifest, pending)
    return True


def report_payload(manifest: dict, validated: dict) -> dict:
    snapshots, kind = validated["snapshots"], manifest["data_kind"]
    first, last = snapshots[0]["reported_as_of"], snapshots[-1]["reported_as_of"]
    prefix = "SYNTHETIC_TEST_ONLY" if kind == SYNTHETIC else SYMBOL
    stem = f"{prefix}_sec_nport_top10_quarterly_{first}_to_{last}"
    next_period = month_end(month_shift(parse_date(last), 3))
    report = {
        "data_kind": kind,
        "ticker": "TEST ONLY" if kind == SYNTHETIC else SYMBOL,
        "fund_name": "Synthetic test fund - not IGV" if kind == SYNTHETIC else FUND_NAME,
        "identity": manifest["identity"],
        "period_start": first,
        "period_end": last,
        "data_through": manifest["data_through"],
        "last_successful_data_refresh_utc": manifest["last_successful_data_refresh_utc"],
        "stale_after": stale_after(last),
        "next_fiscal_quarter_month": next_period.strftime("%Y-%m"),
        "expected_publication_around": (
            next_period + timedelta(days=PUBLICATION_LAG_DAYS)
        ).isoformat(),
        "retained_quarters": validated["quarter_count"],
        "retained_filings": len(validated["all_filings"]),
        "snapshots": snapshots,
        "methodology": (
            "The latest 20 consecutive reported fiscal-quarter-end portfolios, not calendar "
            "quarters or monthly estimates. Dates are N-PORT A.3(b) (repPdDate); A.3(a) "
            "(repPdEnd) supplies fiscal year end. Quarter-end months follow that fiscal year. "
            "Actual reported days are retained, including weekends; no exchange calendar is "
            "imposed. Rankings use the exact reported C.2(d) percentage of net assets (pctVal), "
            "then reported USD value and source row order, before display rounding. "
            "All public row types remain eligible, including cash-like positions and derivatives. "
            "Zero/negative values are retained. No inferred historical tickers, "
            "normalization, interpolation or current-holdings substitutions."
        ),
    }
    report["downloads"] = {
        name: f"{stem}_{digest(content.encode())[:16]}_{name}.csv"
        for name, content in export_csvs(report).items()
    }
    return report


def csv_text(headers: list[str], rows: list[list]) -> str:
    def safe(value):
        if isinstance(value, str) and (
            value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))
        ):
            return "'" + value
        return value

    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows([safe(value) for value in row] for row in rows)
    return stream.getvalue()


def export_csvs(report: dict) -> dict[str, str]:
    snapshots, kind, identity = report["snapshots"], report["data_kind"], report["identity"]
    provenance_fields = [
        "accession",
        "form",
        "filing_date",
        "accepted_at_utc",
        "retrieved_at_utc",
        "source_url",
        "filing_url",
        "source_sha256",
        "filing_index_sha256",
    ]
    leading = ["reported_as_of", "fiscal_year_end", "fiscal_quarter"]
    trailing = [
        *provenance_fields,
        "normalized_object_sha256",
        "registrant_cik",
        "series_id",
        "data_kind",
    ]

    def start(item):
        return [item[name] for name in leading]

    def finish(item):
        return [
            *[item["provenance"][name] for name in provenance_fields],
            item["object_sha256"],
            identity["registrant_cik"],
            identity["series_id"],
            kind,
        ]

    long_fields = [
        "rank",
        "ticker",
        "name",
        "title",
        "cusip",
        "isin",
        "asset_category",
        "payoff_profile",
        "weight_pct",
        "market_value_usd",
        "balance",
        "units",
        "currency",
        "source_row",
    ]
    long_rows, wide_rows = [], []
    for item in snapshots:
        for row in item["top10"]:
            values = {
                **row,
                "ticker": "; ".join(row["ticker"]),
                "isin": "; ".join(row["isin"]),
                **{key: decimal(row[key]) for key in ("weight_pct", "market_value_usd", "balance")},
            }
            long_rows.append([*start(item), *[values[key] for key in long_fields], *finish(item)])
        wide_rows.append(
            [
                *start(item),
                *[
                    f"{'; '.join(row['ticker']) or row['name']} {row['weight_pct']}%"
                    for row in item["top10"]
                ],
                decimal(item["top10_weight_pct"]),
                *finish(item),
            ]
        )
    return {
        "long": csv_text([*leading, *long_fields, *trailing], long_rows),
        "wide": csv_text(
            [
                *leading,
                *[f"rank_{rank}_reported_identifier_and_weight_pct" for rank in range(1, 11)],
                "top10_weight_pct",
                *trailing,
            ],
            wide_rows,
        ),
    }


def static_table(report: dict) -> str:
    rows = []
    for item in report["snapshots"]:
        reported = item["reported_as_of"]
        label = f"FY {item['fiscal_year_end'][:4]} Q{item['fiscal_quarter']}"
        cells = []
        for holding in item["top10"]:
            identifier = "; ".join(holding["ticker"]) or "Ticker not reported"
            name = html.escape(holding["name"], quote=True)
            cells.append(
                f'<td><div class="cell" title="{name}"><span class="symbol">'
                f'{html.escape(identifier)}</span><span class="holding-name">{name}</span>'
                f'<span class="pct">{decimal(holding["weight_pct"]):.2f}%</span></div></td>'
            )
        rows.append(
            f'<tr data-period="{reported}"><th scope="row"><button type="button" '
            f'class="quarter-button" data-period="{reported}">{label}</button>'
            f'<span class="date">{reported}</span></th>{"".join(cells)}'
            f"<td>{decimal(item['top10_weight_pct']):.2f}%</td></tr>"
        )
    return "\n".join(rows)


def build_site(
    root: Path, output: Path, *, test_only: bool = False, now: datetime | None = None
) -> dict:
    if test_only:
        if output.resolve() == ROOT.resolve() or ROOT.resolve() in output.resolve().parents:
            raise BlockedError(
                "Synthetic sites must stay in temporary directories outside the repo"
            )
    else:
        require_publication(os.environ)
    if output.is_symlink() or (
        output.resolve() == root.resolve()
        or root.resolve() in output.resolve().parents
        or output.resolve() in root.resolve().parents
    ):
        raise DataError("Site output must be separate from the archive and cannot be a symlink")
    manifest = read_manifest(root)
    validated = validate_manifest(
        manifest, root, now=now, kind=SYNTHETIC if test_only else PRODUCTION
    )
    report = report_payload(manifest, validated)
    template = (ROOT / "igv_snapshot_template.html").read_text(encoding="utf-8")
    title = "SYNTHETIC TEST ONLY" if test_only else SYMBOL
    replacements = {
        "__TEST_VISIBILITY__": "" if test_only else "hidden",
        "__IGV_DATA__": json.dumps(report, ensure_ascii=True, allow_nan=False).replace(
            "<", "\\u003c"
        ),
        "__IGV_TITLE__": html.escape(
            f"{title} | Quarterly SEC holdings | {report['period_start']} - {report['period_end']}"
        ),
        "__PERIOD__": f"{report['period_start']} - {report['period_end']}",
        "__DATA_THROUGH__": report["data_through"],
        "__LAST_REFRESH__": report["last_successful_data_refresh_utc"],
        "__STATIC_TABLE__": static_table(report),
        **{f"__CSV_{key.upper()}__": value for key, value in report["downloads"].items()},
    }
    for marker, replacement in replacements.items():
        if template.count(marker) != 1:
            raise DataError(f"HTML template needs exactly one {marker} placeholder")
        template = template.replace(marker, replacement)
    if re.search(r"__[A-Z_]+__", template):
        raise DataError("Unresolved HTML template placeholder")
    if output.exists():
        for path in output.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or (
                    path.name not in {"index.html", ".nojekyll"}
                    and not re.fullmatch(
                        r"(?:IGV|SYNTHETIC_TEST_ONLY)_sec_nport_top10_quarterly_"
                        r"\d{4}-\d{2}-\d{2}_to_\d{4}-\d{2}-\d{2}_[a-f0-9]{16}_(?:wide|long)\.csv",
                        path.name,
                    )
                )
            ):
                raise DataError(
                    "Output contains non-SEC or unknown files; use a clean site directory"
                )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".site-stage-", dir=output.parent) as temporary:
        stage = Path(temporary)
        for name, content in export_csvs(report).items():
            (stage / report["downloads"][name]).write_text(
                content, encoding="utf-8-sig", newline=""
            )
        (stage / ".nojekyll").write_text("", encoding="utf-8")
        (stage / "index.html").write_text(template, encoding="utf-8")
        output.mkdir(parents=True, exist_ok=True)
        for filename in [*report["downloads"].values(), ".nojekyll", "index.html"]:
            if (output / filename).is_symlink():
                raise DataError("Site target files must not be symlinks")
        for filename in [*report["downloads"].values(), ".nojekyll"]:
            target = output / filename
            if target.exists() and target.read_bytes() != (stage / filename).read_bytes():
                raise DataError("An immutable holdings download was modified")
        added = []
        committed = False
        try:
            for filename in [*report["downloads"].values(), ".nojekyll"]:
                target = output / filename
                if not target.exists():
                    os.replace(stage / filename, target)
                    added.append(target)
            # Amendments use new content-hashed CSV names, so the old page stays coherent.
            os.replace(stage / "index.html", output / "index.html")
            committed = True
        finally:
            if not committed:
                for path in added:
                    path.unlink()
    return report


def print_summary(manifest: dict, validated: dict) -> None:
    first = validated["snapshots"][0]["reported_as_of"]
    print(
        f"{manifest['data_kind']}: {first} through {manifest['data_through']}; "
        f"20 reported fiscal quarters / 200 ranked positions. "
        f"Last successful data refresh: {manifest['last_successful_data_refresh_utc']}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("bootstrap", "refresh", "validate", "build"))
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=ROOT / "site")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--test-only", action="store_true")
    parser.add_argument(
        "--allow-stale", action="store_true", help="Offline validation only; never build/deploy"
    )
    args = parser.parse_args(argv)
    try:
        if args.allow_stale and args.command != "validate":
            raise BlockedError("--allow-stale is only valid for offline archive validation")
        if args.command in {"refresh", "bootstrap"}:
            if args.test_only:
                raise BlockedError("Synthetic refresh is injected offline by tests only")
            require_publication(os.environ)
            if not args.allow_network:
                raise BlockedError("SEC collection requires explicit --allow-network")
            if args.command == "bootstrap" and os.environ.get("GITHUB_ACTIONS") == "true":
                raise BlockedError(
                    "Bootstrap is operator-local only; Actions requires an audited seed"
                )
            if args.command == "refresh":
                read_manifest(args.data_dir)
            client = SECClient(os.environ.get("SEC_USER_AGENT", ""))
            changed = refresh_archive(args.data_dir, client, bootstrap=args.command == "bootstrap")
            print(
                "Normalized SEC archive updated."
                if changed
                else "No new SEC filings/amendments; no data or timestamp changes."
            )
        elif args.allow_network:
            raise BlockedError("--allow-network is only valid for SEC acquisition")
        if args.command == "build":
            build_site(args.data_dir, args.output, test_only=args.test_only)
            print("Built quarterly holdings with two relative holdings-only CSV downloads.")
        manifest = read_manifest(args.data_dir)
        validated = validate_manifest(
            manifest,
            args.data_dir,
            kind=SYNTHETIC if args.test_only else PRODUCTION,
            require_current=not args.allow_stale,
        )
        print_summary(manifest, validated)
        return 0
    except (DataError, OSError) as error:
        label = "BLOCKED" if isinstance(error, BlockedError) else "FAILED"
        print(f"{label}: {error}", file=sys.stderr)
        return 2 if isinstance(error, BlockedError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
