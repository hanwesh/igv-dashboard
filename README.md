# IGV dashboard - CODE ONLY

**Source data collection and GitHub Pages publication are OFF.**
`DATA_PUBLICATION_APPROVED` is `false`; Pages has not been enabled. This public
repository contains code, a data-free schema and independently generated test
fixtures, **not holdings records, market prices or a live investment dashboard**.
The existing local report and its data archive are not imported or modified.

**Unresolved permission blocker:** the official IGV product page links to
[BlackRock's terms](https://www.blackrock.com/corporate/compliance/terms-and-conditions).
Their content-use restrictions require appropriate permission for public reuse
and distribution; written permission has not been established. Yahoo price-data
redistribution rights are also unresolved. Public endpoints are not a license.
Do not enable collection, check in provider responses, or publish a report until
the appropriate rights are obtained. No third-party data license is granted here.

Expected future Pages address: `https://hanwesh.github.io/igv-dashboard/`
**(NOT LIVE; this implementation does not enable it).**

## What the code does

Python builds a self-contained native HTML/CSS/JavaScript/SVG dashboard: monthly
top ten, green/red price-return bars or OHLC, monthly volume, 1Y/3Y/5Y ranges,
hover and keyboard inspection, chart/holdings selection, year and ticker filters,
and three CSV downloads. There is no frontend framework, backend or database.

A complete display is exactly **60 completed months / 600 ranked holding
records / 60 monthly performance bars**. Titles, concentration labels, dates,
baseline, methodology and download names follow the active window. The stable
entry point is `site/index.html`; CSV URLs are relative, including under the
repository subpath. Downloads retain all 60 months regardless of UI filters.
The long and performance CSVs retain numeric precision; the wide view rounds
the combined ticker/weight cells to two decimals. Spreadsheet-formula-like text
gets an apostrophe prefix in CSV exports so it remains text, not executable formulas.

**Data through** is the last complete archive month. **Last successful data
refresh** is the persisted UTC retrieval/validation completion time, not build,
checkout, deployment or file-modification time. A browser-local New York clock
displays a stale warning if the archive trails the latest completed month, even
if Actions is disabled or offline. No missing-data or error path produces a
real-looking fallback site.

## Setup and offline verification

Use Python 3.13 and a virtual environment. The two dependency manifests pin
direct dependencies; `exchange-calendars` supplies the maintained XNYS calendar.
Transitive packages are resolved by pip, so this is not a full dependency lock.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest
python -m playwright install chromium
python -m tests.browser_smoke
```

On Linux, Playwright may need `python -m playwright install --with-deps chromium`.
For a repository-local browser cache, set
`PLAYWRIGHT_BROWSERS_PATH="$PWD/.venv/playwright-browsers"` for both installation
and the smoke command. `.venv/`, `site/` and browser/test outputs are ignored.

Fixtures in `tests/synthetic.py` are mathematical, independently invented test
data, marked `synthetic-test-only`, with `TEST` security identifiers and
`example.invalid` provenance. They are not observations of IGV or any security.
Unit tests block real provider network calls. The headless smoke check creates a
temporary archive and conspicuously marked test site **outside this repository**,
serves it on loopback under `/igv-dashboard/`, permits only local browser requests,
and removes the site and browser profile afterward. It checks the 60/600 shape,
both chart views, volume, all ranges, source notes, keyboard/hover selection,
filters, all CSVs, mobile overflow containment and stale-clock rollover.
Synthetic output is never uploaded by CI or used as a production fallback.

## Archive and methodology

`data/schema.json` documents the manifest; **there is no production manifest yet**.
An authorized refresh would create:

```text
data/
  schema.json
  manifest.json
  objects/<sha256>.json
site/                         # ignored generated output
  index.html
  <dynamic-window>_wide.csv
  <dynamic-window>_long.csv
  <dynamic-window>_performance.csv
```

Objects are immutable, content-addressed original responses. The manifest stores
product ID `239771`, symbol `IGV`, data kind, source URL, requested/as-of dates,
retrieval UTC, SHA256, calendar version and last successful refresh. Historical
holdings and superseded coherent price windows remain archived; display rolls
forward without deleting history. Validation rechecks hashes after checkout.
Filesystem mtimes are never provenance.

The latest completed calendar month is determined in `America/New_York`.
Each holdings snapshot must report the exact final XNYS session of its month,
including holidays and exceptional closures from `exchange-calendars`; no
weekday shortcuts or hardcoded fund-specific date exceptions are used.

Holdings use the iShares/BlackRock dated product-data schema linked from the
[official IGV product page](https://www.ishares.com/us/products/239771/ishares-north-american-techsoftware-etf).
All required arrays must be present, nonempty and aligned. Product ID, reported
date, finite numeric values, usable top-ten identifiers and duplicate securities
are checked. Ranking uses **unrounded weight**, then market value and ticker;
cash and other asset classes remain eligible. All weights are retained, including
zero/negative non-top positions. Full-list allocation discrepancies greater than
0.005 percentage points generate visible warnings, **never normalization**;
smaller differences are treated as source rounding noise. Display rounding does
not change stored weights or top-ten concentration calculations.

Prices use the Yahoo Finance daily chart schema for `IGV` in USD. Every expected
exchange session from the preceding month-end baseline to the latest month-end
must be present, ordered, unique, finite and consistent with OHLC invariants.
No missing session is interpolated or forward-filled. Daily quotes are aggregated
as first open, highest high, lowest low and last close; volume sums the supplied
daily field, without estimation or rescaling.

**Returns are price returns, not total returns.** Each month-end close is divided
by the preceding month-end close, minus one; range returns use the close before
the first displayed month. Cash distributions are excluded. The chart uses
`quote.close`, not dividend-adjusted `adjclose`. The adapter relies on the source's
split-adjusted quote convention, records split events and rejects unexplained
overlapping OHLC revisions or mixed adjustment bases rather than silently joining
incompatible histories. It also checks adjusted/quote ratios for discontinuities
outside recorded cash-distribution dates (with a small floating-point tolerance).
A simultaneous split and distribution is blocked for manual verification, not
estimated. Initial provider semantics cannot be independently proven
from one response; this is not an independent price audit.

Source APIs may change, deny automated access or publish month-end holdings late.
Neither endpoint is an availability contract. Schema changes, missing old months,
stale/latest-date fallbacks, revisions and unexplained corporate actions fail
closed for investigation; they do not trigger substitute-source estimates.
Keep the exchange-calendar dependency current for newly announced closures.

## Future authorized operation - do not activate now

Only after obtaining appropriate permission for **both** holdings and price
collection/public redistribution (including public Git history and downloads):

1. Record the scope of the permission/licensed source with the repository owner.
   If another source is licensed, adapt and revalidate the provider contract
   first; toggling a variable does not grant permission.
2. The owner manually selects **Settings > Pages > Source: GitHub Actions** and
   restricts the `github-pages` environment to main, adding reviewer approval if
   desired. The workflow uses `configure-pages` with `enablement: false`; it cannot
   bootstrap Pages using a PAT, and no PAT or additional secret is needed.
3. The owner explicitly sets repository variable `DATA_PUBLICATION_APPROVED` to
   `true`. Only then manually dispatch the workflow from main. Verify the first
   complete archive, source warnings and deployment. Branch-protection rules must
   permit the built-in bot's scoped data commit; this code never bypasses them.

For **future authorized local use**, the owner must deliberately set the same
approval environment variable before production refresh/build. The commands
below do not set it and currently fail with an explicit blocked state:

```sh
python igv_snapshot.py refresh --allow-network
python igv_snapshot.py validate
python igv_snapshot.py build
```

`--data-dir` and `--output` select local paths; do not point them at an unapproved
existing archive. The first refresh is the seed operation: it requests the full
60-month holdings window and daily prices including the prior baseline. No
unsafe import-from-arbitrary-directory shortcut is provided. An authorized
archive with original objects and intact provenance can later be validated
offline using `validate`; synthetic validation requires `--test-only`.
Production `build` requires approval and a current complete production archive.
Synthetic `build --test-only` rejects outputs anywhere inside this repository.

Subsequent refreshes fetch only missing holdings, catch up every missing month,
and replace the entire active daily price window on a consistent adjustment
basis. An outage longer than a window also fetches intermediate overlapping
windows, so retained daily history has no gap. Retries use the **same requested dates**
(up to three attempts with bounded
backoff); permanent access failures are not retried as another date. A month-end
publication delay fails the run and retries on a later scheduled/manual run.
All candidates validate before a manifest is atomically promoted. Partial
responses never overwrite good archives. Orphan immutable objects after an
external process termination are not active unless referenced by the manifest.

A repeated run with no new completed month performs no provider calls and
does not alter retrieval times or create timestamp-only data commits. It can
still rebuild/deploy the persisted archive, for example to recover a previous
deployment failure. It intentionally does not poll intramonth revisions.
If a provider correction changes historical OHLC beyond the recorded split
basis, stop and investigate instead of deleting the archive to silence the check.

## GitHub Actions, pausing and recovery

`.github/workflows/update-and-deploy.yml` runs synthetic validation on pull
requests and main pushes, plus manual dispatch and **07:17 UTC daily**. Global
permissions are empty; the synthetic job has only `contents: read` and disables
checkout credential persistence. No `pull_request_target` trigger is used.

Every mutation job requires the exact repository, `refs/heads/main`, a trusted
push/schedule/manual event and `vars.DATA_PUBLICATION_APPROVED == 'true'`.
With the flag false, CI explains that live work is disabled and does not create
failure issues, commit archives, configure/upload Pages artifacts or deploy.

After approval, separate jobs use `contents: write` only for archive persistence;
`contents: read`, `pages: write`, `id-token: write` only for deployment; and
`contents: read`, `issues: write` only for incident maintenance. They use the
built-in `GITHUB_TOKEN`, no secrets/PATs, no account upgrades and no extra Copilot
automation. Official checkout/Python/Pages actions are pinned to immutable
commits; verify the upstream release before updating a pin.

Main runs share a non-cancelling concurrency group. Data is validated and built
before a narrow data-only commit; push is ordinary fast-forward, never forced.
A concurrent user push causes failure, not a rebase or overwrite. Deployment
checks the persisted commit against main and occurs in the **same run** from
that exact commit, after successful persistence. It does not depend on a
`GITHUB_TOKEN` bot push retriggering workflows. A main change after that last check
can still race the external Pages service; the next main run converges it.
GitHub concurrency can replace an older pending run; refresh catches up from
the archive rather than assuming every scheduled run executed.

Authorized failures leave the last-good site in place and maintain **one**
bot-owned marker-tagged issue, updating/reopening it for failures and closing it
only after a successful deployment. Notification failure makes its job fail
explicitly. Watch Actions and subscribe to the incident issue for notifications;
GitHub notification settings control delivery, and issue-body edits do not
guarantee another email. No raw provider response is put in issue text.

To pause future live work, set `DATA_PUBLICATION_APPROVED=false` (synthetic checks
continue), or disable the workflow. Gate changes are evaluated per run, not as a
kill switch for in-flight work: cancel an already-running job separately if
needed. Pausing does not remove a previously published site or erase public Git
history. GitHub may delay scheduled runs and disables inactive public-repository
schedules after 60 days; an owner must re-enable the workflow and can manually
dispatch it from main. The client stale warning remains useful during inactivity.

Excluded from this implementation: original local raw data or reports, downloaded
provider fixtures, public synthetic investment reports, secret credentials,
paid subscriptions, permission attestations, automatic Pages enrollment and
automatic merging. Source permission and live provider compatibility remain
unverified until an explicitly authorized future run.
