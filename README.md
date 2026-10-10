# IGV quarterly SEC holdings dashboard

A static, free dashboard of **20 reported fiscal quarters / 200 top-ten
positions**, with a separately hosted TradingView price chart. Holdings are
normalized factual fields from official SEC N-PORT filings, not an iShares
product-data download. There is no local price feed, paid service, API key,
backend or frontend framework.

**Publication requires owner activation.** This migration does not enable Pages,
merge itself or change repository variables. The intended address is
`https://hanwesh.github.io/igv-dashboard/`; it is not a claim of a live deployment.
Keep the legacy `DATA_PUBLICATION_APPROVED=false`. Only the new, default-off
`SEC_PUBLICATION_APPROVED` gate can authorize the SEC path. The removed
Yahoo/iShares ingestion and price-export code cannot be reactivated by that gate.
The prior local monthly report and its provider records are not imported.

## Verified source and coverage

The SEC's [fund ticker mapping](https://www.sec.gov/files/company_tickers_mf.json)
and the filings identify **iShares Expanded Tech-Software Sector ETF (IGV)**:
registrant **CIK 0001100663**, series **S000004355**, class **C000012085**.
Other funds in iShares Trust are not accepted as IGV.

The initial audited seed contains **29 filings / 28 consecutive quarters /
3,410 full investment rows**, from **2019-09-30 through 2026-06-30**. It retains
all older quarters and the superseded original. Its rolling display is
**2021-09-30 through 2026-06-30**. No earlier or intervening monthly portfolio
is invented or carried forward.

The latest quarter's [SEC filing index](https://www.sec.gov/Archives/edgar/data/1100663/000207169126019778/0002071691-26-019778-index.html)
identifies accession `0002071691-26-019778`, filed **2026-08-25**. For
2025-09-30, amendment `0002071691-26-013333`, filed 2026-06-18, supersedes
`0002071691-25-007647`, filed 2025-11-26. Both normalized versions remain in Git.
Seed regression tests check retained historical accessions; future quarters
extend the archive rather than replacing this evidence.

### Reported dates, fiscal quarters and publication lag

In [Form N-PORT](https://www.sec.gov/files/formn-port.pdf), A.3(b)
**`repPdDate` is the reported portfolio date**. A.3(a) **`repPdEnd` is the fiscal
year end**, not the holdings date. IGV's seed uses a March 31 fiscal year end.
For example, reported **2026-06-30** with fiscal year end **2027-03-31** is
**FY 2027 Q1**, not fiscal Q2. Quarter-end months are derived from the stated
fiscal year, not assumed calendar quarters.

Actual reported days are preserved, including calendar/business month ends
and weekends. There is no NYSE-calendar adjustment. Additional public reporting
months, if discovered under future reporting rules, are retained but do not
count toward the 20 quarterly portfolios.

These are delayed reports, not current holdings. Original filings in the seed
usually arrive approximately **55-60 days after the reported quarter**
(the seed's original-filing range is 53-62 days); amendments can arrive much
later. The stale threshold is the next nominal
fiscal-quarter month end plus **60 days and a 7-day monitoring grace period**.
This is an operational freshness check, not a legal-deadline calculation.
After that threshold the browser warns, and production refresh/build fails
closed rather than presenting a stale archive as a successful update.

The reported-as-of date, filing/acceptance dates, original retrieval UTC and
**last successful data refresh UTC** are distinct. That last timestamp advances
only after new filings or amendments validate and are promoted. An unchanged
daily check, checkout, rebuild or deployment never resets it. Check Actions for
the most recent check outcome; the refresh timestamp is not a scheduler heartbeat.

### Weights, identifiers and securities-lending collateral

`pctVal` is the source's **percentage of fund net assets** (C.2(d)), not a ratio
to multiply by 100. Original decimal strings are retained. Ranking uses exact,
unrounded weights, then reported USD value, then source row order. Display
rounding to two decimals does not change ranking, concentration totals or CSV
precision. All source row types remain eligible in the default as-filed view,
including cash vehicles and derivatives. Legitimate zero/negative rows and
null identifiers are retained.

The full investment weights in the seed total **103.228492046366% to
113.100937298738%**, roughly **103-113%**, and are **never renormalized**.
Securities-lending cash collateral is an investment with an offsetting
obligation reported elsewhere. It can increase that investment total above
100% and enter the top ten. In the latest quarter the collateral position is
rank 6 at **5.868089075690%**; it ranked first in 2019-12 and 2020-03.

Collateral classification requires an affirmative cash-collateral flag,
cross-checked against asset category `STIV` and issuer category `RF`.
Those categories alone are insufficient: an ordinary cash-management vehicle
with the collateral flag `N` is not collateral. Both types remain visible.
This **as-filed investment top ten** can differ from an iShares published
holdings list because of the collateral inclusion, scope and reporting date;
it is not an attempt to reproduce that product list. Weight changes also
reflect prices, fund flows and corporate actions, not just purchases or sales.

The keyboard-accessible **Holdings view** selector offers two rankings over the
same immutable SEC records:

- **As filed (SEC)** is the default, including collateral. With JavaScript
  disabled this is the only displayed view.
- **Operating companies only** excludes positions only when
  `security_lending.is_cash_collateral` is explicitly `true`, then takes the
  highest ten unrounded weights from the **full remaining portfolio**, including
  positions below the original top ten. Names, CUSIPs and asset/issuer categories
  alone never drive this exclusion. Despite the short label, this is a
  collateral-exclusion view, not a general equity-only classification: ordinary
  cash management, derivatives and unknown flags remain eligible.

Both views retain the exact source percentage-of-net-assets weights. Excluding
collateral lowers the selected total when it occupied a top-ten slot; the
omitted weight is **not redistributed or normalized to 100%**. The collateral
note remains visible with the same exact percentage, described as included or
excluded as appropriate. The selected-quarter grouped vertical chart, table,
concentration totals, CUSIP filters and appearance counts all follow the
selected view. The chart places the selected report and up to three preceding
reports as the outer x-axis groups, using one labelled color per quarter and a
non-color outline/text marker for the selected quarter. The comparison universe
remains the selected quarter's ten CUSIP-keyed securities, but each quarter
orders its present members from highest to lowest exact active-view rank, then
places labelled `Not in top 10` gaps last in selected-quarter rank order. Exact
weight ties therefore follow the ranking's reported-USD-value and source-row
tiebreakers, while missing slots remain deterministic. Present bars use that
quarter's reported title, active-view rank and exact source weight; a gap means
the selected-quarter security was outside that report's top ten, never zero.
Visible chart labels keep percentage, rank, title and meaningful category text
without repeating ticker/CUSIP lines; ticker, CUSIP and ISIN remain in chart
accessibility text, the table, filters and downloads. Heights are relative to
the largest present weight in the comparison and are never renormalized. The
independently hosted chart does not change.

None of the IGV seed filings supply exchange tickers. This is a property of
the source, not a per-position data-quality failure. Security titles and issuer
names are shown as filed; CUSIP identifies every displayed top-ten position.
ISIN is available for **109 of the latest 111 rows**, and CUSIP for **108/111**.
The two reported index-derivative rows lack both identifiers; the non-top Elastic
position has ISIN but no reported CUSIP. Both cash funds have CUSIP and ISIN.
These legitimate non-top nulls remain intact. Historical tickers are never
inferred from current identifiers.

Cross-quarter filtering, historical-name search and top-ten appearance counts
use **exact CUSIP**, never name spelling or fuzzy matching. A group's label is
its most recent as-filed security title; individual rows keep their original
titles. Search can resolve any historical title, issuer name, reported ticker
or ISIN to that same CUSIP. A row without CUSIP is still displayed/searchable,
but is not guessed into a cross-quarter group. The initial 20-quarter
**as-filed** window has **17 CUSIPs**; Salesforce, Microsoft, Oracle and
ServiceNow each appear in **20/20** quarters. A CUSIP filter is retained across
view changes when present, otherwise visibly reset to all securities.

## Hosted chart and holdings downloads

The native quarterly table, date/order/security controls, identifier search,
selected-quarter details and provenance remain separate from the chart.
The TradingView advanced-chart widget loads **only after a button click**,
which connects the visitor's browser to TradingView. Its visible attribution
and external IGV help link remain in place; blocked scripts and empty frames
have explanatory fallback text, never substitute prices.

The independently verified embed uses `AMEX:IGV`, `range: "60M"`,
`interval: "W"`, OHLC style and volume, in `America/New_York`. The **60-month
range forces weekly resolution**, rather than monthly bars. TradingView's
displayed **delayed Cboe One feed** is not a primary-exchange closing-price
series. Availability and any later feed changes are controlled by TradingView.
The fixed-height wrapper preserves widget sizing on desktop and mobile.

The chart is a hosted widget, **not a downloadable price API**. Nothing scrapes,
stores, calculates or exports its prices, returns, OHLC or volume. There is no
native performance CSV or price-return calculation, and no chart-to-holdings
synchronization. A chart quote does not imply fresh SEC holdings. TradingView
branding and [widget attribution](https://www.tradingview.com/widget-docs/widgets/charts/advanced-chart/)
must not be removed or obscured.

The original **two as-filed CSVs** remain unchanged: wide (20 rows) and long
(200 ranked positions). Neither changes when the view or interactive filters
change. The long file already has an explicit `is_cash_collateral` column.
A third **full holdings CSV**, using the same long-format exporter, contains
every source investment for those same 20 quarters (**2,420 rows in the initial
window**), including the positions needed to refill the filtered top ten.
Its `rank` is the full as-filed rank. To reproduce either view, group by reported
date, optionally exclude only `is_cash_collateral=true`, sort by unrounded
`weight_pct` descending, `market_value_usd` descending and `source_row` ascending,
then take and re-rank the first ten. False or blank collateral flags are not
excluded; percentages are never rescaled.

All exports retain source-reported precision, filing URLs/accessions, fiscal
dates, checksums and provenance; the long and full files include identifiers
and lending flags. Blank tickers remain blank in CSV. Spreadsheet-formula-like
text is escaped with an apostrophe; genuine numeric negatives remain numbers.

CSV filenames include the window and a content hash, so amended data cannot
overwrite a download referenced by an older page. All download URLs are
relative and work under `/igv-dashboard/`. The complete table, source links,
reported/refresh dates and all downloads work without JavaScript. The alternate
view, interactive controls, hosted chart and browser-clock stale updates require
JavaScript.

## Normalized archive and refresh behavior

```text
data/
  schema.json
  manifest.json
  objects/<sha256>.json        # immutable normalized SEC facts only
site/                         # ignored generated output
  index.html
  IGV_sec_nport_top10_quarterly_<window>_<hash>_wide.csv
  IGV_sec_nport_top10_quarterly_<window>_<hash>_long.csv
  IGV_sec_nport_all_holdings_quarterly_<window>_<hash>_all.csv
```

Each normalized object records the exact series, reported/fiscal dates, all
investment rows and factual metadata. Provenance includes source/index URLs,
accession/form, filing date, acceptance and retrieval UTC, original XML SHA256,
filing-index SHA256, and per-row source checksum. The manifest records the
normalized object's SHA256 and the original identity-mapping checksum.
Validation rechecks these hashes, canonical serialization, fund identity,
decimal precision, dates, required rows, duplicate records and amendment order.
Filesystem timestamps are not provenance.

**Only normalized factual holdings and metadata are published.** Raw XML,
filing-index HTML, narrative notes and signatures are not retained in the
archive, site, logs or artifacts. SEC publication is not a blanket public-domain
designation or a third-party data license. Source links identify the original
documents; this project does not grant rights to their prose or other
third-party materials.

Discovery uses official SEC fund mapping and a targeted EDGAR full-text
NPORT-P series query from 2019-04-01, not bulk multi-gigabyte downloads.
The base-form query also finds amendments. Daily checks revisit metadata for
the full retained range so old-quarter amendments are not missed, and fetch
index/XML documents only for new accessions. Changes or disappearance of known
source metadata fail explicitly.

One original filing per reported quarter is required. The latest appropriate
amendment wins by acceptance time, then accession; originals and previous
amendments stay immutable. Conflicting dates, missing quarters, unknown schemas,
future records or insufficient coverage are not repaired by guessing.
Candidates validate before atomic manifest promotion. Failed promotion removes
new partial objects and keeps the last-good manifest. Abrupt process termination
can leave unreferenced objects, which are not active archive facts.

No new filings means no data, retrieval-time or refresh-time change and no
timestamp-only commit. A no-op check may still rebuild/deploy the same persisted
data to recover a prior deployment failure. Site promotion writes content-hashed
CSVs first and HTML last; failure preserves coherent last-good output. Unknown
or legacy files in the output directory are rejected rather than uploaded.

## Setup and offline verification

Use Python 3.13. Runtime dependencies are pinned `defusedxml` for guarded XML
parsing and `certifi` for verified HTTPS CA roots; the old exchange-calendar
dependency is removed. Direct dependencies are pinned, not fully transitively
locked.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest
python -m playwright install chromium
python -m tests.browser_smoke
python igv_snapshot.py validate --allow-stale
```

On Linux, use `python -m playwright install --with-deps chromium` if required.
`.venv/`, generated `site/` and test/browser outputs are ignored.

Tests use independently invented, conspicuously marked synthetic fixtures and
offline regression checks of the committed SEC seed. Ordinary tests make no
financial requests. Chromium serves a temporary synthetic site on loopback
under `/igv-dashboard/`, intercepts the widget with a mock, blocks all other
external requests, and removes the site/profile afterward. It checks exact
20/200 shapes in both views, exact collateral exclusion and replacement ranks,
CUSIP/name-variant continuity, per-quarter descending/tie/missing-last chart
ordering, visually decluttered chart labels with identifiers retained for
accessibility, keyboard controls, source notes, unchanged as-filed exports,
full-portfolio reconstruction, desktop/mobile including 390px, no-JS behavior,
stale rollover and widget success/error/timeout.
Real TradingView availability is independently verified, not inferred from this
mock. Synthetic sites stay outside the repository and are never uploaded.

`validate` is offline and needs no contact secret or publication approval.
`--allow-stale` permits only an offline historical audit; it cannot bypass
freshness on build or deployment. `--test-only` is exclusively for synthetic
archives and cannot build inside the repository.

## Owner activation, daily automation and recovery

Only the owner performs activation after reviewing and merging this migration:

1. Confirm the normalized seed/provenance and green checks. Keep repository
   variable **`DATA_PUBLICATION_APPROVED=false`**.
2. Create or confirm Actions secret **`SEC_USER_AGENT`**: truthful project
   identification containing `igv-dashboard` and a real operator contact email.
   It is request identification, not a paid API key. Keep the complete value
   private; never commit, print or paste it into docs, fixtures, issues or data.
3. Select **Settings > Pages > Source: GitHub Actions** and restrict the
   `github-pages` environment to main, with reviewer approval if desired.
   The workflow uses `configure-pages` with `enablement: false` and expects
   an already-enabled Pages site.
4. Explicitly set **`SEC_PUBLICATION_APPROVED=true`**, then dispatch the workflow
   from **main**. Verify the deployment URL, real chart/attribution, reported
   dates, both holdings views and all downloads. This source-specific gate is not a license
   attestation for other providers. No legacy pipeline is enabled.

For owner-authorized local operation, first supply `SEC_USER_AGENT` privately
in the environment. These scoped flags do not change repository variables:

```sh
SEC_PUBLICATION_APPROVED=true python igv_snapshot.py refresh --allow-network
python igv_snapshot.py validate
SEC_PUBLICATION_APPROVED=true python igv_snapshot.py build
```

`--data-dir` and `--output` select local paths; never point at an old provider
archive. The committed audited seed is used for normal refresh. Explicit
`bootstrap --allow-network` is operator-local only, for a separate empty archive
followed by source verification/review; it is forbidden in Actions. Missing seed
blocks production, not a reason to publish synthetic or current-only fallback.

The SEC client follows [official EDGAR access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data):
one request/second, verified HTTPS, 8 MiB response bounds, at most three
transient attempts and bounded `Retry-After` handling. HTTP 401/403 stops
immediately. A requested wait exceeding 120 seconds defers to a later run.
No contact rotation, TLS bypass, proxy, unofficial mirror or alternate provider
is used to circumvent a denial.

The workflow checks PRs/main pushes plus manual dispatch and **07:17 UTC daily**.
Checks have only `contents: read`, no financial acquisition and no site upload.
Every live job requires the exact repository, `refs/heads/main`, a trusted
push/schedule/manual event and `SEC_PUBLICATION_APPROVED == 'true'`.
The new gate defaults off; `pull_request_target` is not used.

Global permissions are empty. The archive job alone has `contents: write`;
deployment explicitly declares **`contents: read`, `pages: write` and
`id-token: write`**, even when the repository default is read-only. Incident
maintenance has only `contents: read` and `issues: write`. They use the built-in
`GITHUB_TOKEN`, no PAT. Official actions are pinned to immutable commits.
Branch protection must permit the intended bot data commit; no bypass or force
push is attempted.

Main runs are serialized without cancelling an active run. Validation and a
build precede a narrow data-only commit and ordinary fast-forward push.
The same run deploys the **exact persisted data commit**, not a hypothetical
bot-push retrigger. A check against current main rejects a superseded revision;
a main change after that check can still race the external Pages service, with
the next main run converging it. Pending runs can be replaced by GitHub
concurrency; discovery catches up from retained data.

Failures retain the last-good site and maintain one bot-owned, marker-tagged
incident issue. Repeated failures update/reopen it; only a successful deployment
closes it. Notification errors fail explicitly. Watch Actions and subscribe to
that issue; body edits do not guarantee another email. Raw responses and
operator contact never belong in incident text.

To pause live work, set **`SEC_PUBLICATION_APPROVED=false`** or disable the
workflow; offline checks can continue. Keep the legacy flag false. A gate change
is not a kill switch for a job already running; cancel in-flight work separately
when needed. Pausing does not unpublish a site or erase public Git history.
Investigate access/schema/date/amendment/concurrent-main failures, restore the
appropriate configuration, then dispatch from main. Do not delete validated
history or weaken checks to silence a failure.

GitHub can delay scheduled runs or disable an inactive public-repository
schedule after 60 days. The owner must re-enable it when necessary. The browser's
lag-aware warning remains useful while automation is paused. This dashboard is
not affiliated with SEC, the fund or TradingView and is not investment advice.
