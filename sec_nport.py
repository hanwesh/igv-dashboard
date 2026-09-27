"""Official SEC discovery and normalized N-PORT facts. Raw documents are never archived."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from xml.etree.ElementTree import ParseError, canonicalize, tostring
from zoneinfo import ZoneInfo

import certifi
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

REPOSITORY = "hanwesh/igv-dashboard"
CIK = "0001100663"
SYMBOL = "IGV"
FUND_NAME = "iShares Expanded Tech-Software Sector ETF"
PRODUCTION = "sec-nport-public-facts"
SYNTHETIC = "synthetic-test-only"
SOURCE = "sec-edgar-nport"
NAMESPACE = "http://www.sec.gov/edgar/nport"
FIELD_NAMESPACES = {
    NAMESPACE,
    "http://www.sec.gov/edgar/nportcommon",
    "http://www.sec.gov/edgar/common",
}
MAPPING_URL = "https://www.sec.gov/files/company_tickers_mf.json"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES_URL = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}"
FORMS = {"NPORT-P", "NPORT-P/A"}
NY = ZoneInfo("America/New_York")
MAX_BYTES = 8 * 1024 * 1024
MAX_SEARCH_RESULTS = 500
MAX_RETRY_DELAY = 120
HASH_PATTERN = r"[a-f0-9]{64}"
ACCESSION_PATTERN = r"\d{10}-\d{2}-\d{6}"
DECIMAL_PATTERN = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
NULL_IDENTIFIERS = {"", "N/A", "NA", "NONE", "NOT AVAILABLE", "000000000", "000000000000"}


class DataError(ValueError):
    """A source or archive cannot be used without guessing."""


class BlockedError(DataError):
    """A required authorization, source or verified archive is unavailable."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise DataError("Timestamps must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise DataError("Missing UTC provenance timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DataError("Invalid UTC provenance timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise DataError("Provenance timestamps must be UTC")
    return parsed


def parse_date(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DataError("Dates must have YYYY-MM-DD form")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise DataError("Invalid reported date") from error


def month_shift(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    try:
        return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))
    except ValueError as error:
        raise DataError("Fiscal date outside the supported range") from error


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def fiscal_quarter(reported: str, fiscal_end: str) -> int | None:
    as_of, year_end = parse_date(reported), parse_date(fiscal_end)
    months = (year_end.year - as_of.year) * 12 + year_end.month - as_of.month
    if not 0 <= months <= 11:
        raise DataError("Reported date is outside its stated fiscal year")
    # A.3(a) is fiscal year end; A.3(b) is the actual reported date, not an exchange date.
    return 4 - months // 3 if months % 3 == 0 else None


def decimal(value: object, label: str = "numeric field") -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(DECIMAL_PATTERN, value):
        raise DataError(f"Invalid decimal {label}")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise DataError(f"Invalid decimal {label}") from error
    if not result.is_finite() or len(result.as_tuple().digits) > 36:
        raise DataError(f"Nonfinite or oversized {label}")
    if result.as_tuple().exponent < -12:
        raise DataError(f"Unsupported precision in {label}")
    return result


def decimal_sum(values) -> str:
    with localcontext() as context:
        context.prec = 60
        return format(sum((decimal(value) for value in values), Decimal(0)), "f")


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()


def decode_json(raw: bytes) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DataError("Duplicate JSON object key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeDecodeError) as error:
        raise DataError("Malformed JSON document") from error
    if not isinstance(value, dict):
        raise DataError("Expected a JSON object")
    return value


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def check_hash(value: object) -> None:
    if not isinstance(value, str) or not re.fullmatch(HASH_PATTERN, value):
        raise DataError("Invalid SHA256 provenance")


def text(value: object, label: str, *, nullable: bool = False, limit: int = 300) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise DataError(f"Missing {label}")
    value = value.strip()
    if nullable and value.upper() in NULL_IDENTIFIERS:
        return None
    if not value or len(value) > limit or any(ord(char) < 32 for char in value):
        raise DataError(f"Invalid {label}")
    return value


def publication_allowed(env: Mapping[str, str]) -> bool:
    if env.get("SEC_PUBLICATION_APPROVED") != "true":
        return False
    if env.get("GITHUB_ACTIONS") != "true":
        return True
    return (
        env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") in {"push", "schedule", "workflow_dispatch"}
    )


def require_publication(env: Mapping[str, str]) -> None:
    if not publication_allowed(env):
        raise BlockedError(
            "SEC-only production processing requires SEC_PUBLICATION_APPROVED=true "
            "and, in Actions, trusted repository/main/event context. "
            "DATA_PUBLICATION_APPROVED does not authorize this source."
        )


def source_url(accession: str, filename: str, kind: str = PRODUCTION) -> str:
    if not re.fullmatch(ACCESSION_PATTERN, accession):
        raise DataError("Invalid SEC accession")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.xml", filename):
        raise DataError("Invalid SEC XML document filename")
    base = "https://example.invalid/synthetic/Archives" if kind == SYNTHETIC else ARCHIVES_URL
    return f"{base}/{accession.replace('-', '')}/{filename}"


def filing_url(accession: str, kind: str = PRODUCTION) -> str:
    base = source_url(accession, "primary_doc.xml", kind).rsplit("/", 1)[0]
    return f"{base}/{accession}-index.html"


def allowed_url(url: str) -> bool:
    if url == MAPPING_URL:
        return True
    parts = urlparse(url)
    if parts.username or parts.password or parts.fragment:
        return False
    if parts.scheme == "https" and parts.netloc == "efts.sec.gov":
        return parts.path == "/LATEST/search-index" and bool(parts.query)
    return bool(
        re.fullmatch(
            re.escape(ARCHIVES_URL) + r"/\d{18}/[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:xml|html)",
            url,
        )
    )


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise DataError("Unexpected SEC redirect; no alternate host or source will be used")


class SECClient:
    """One request/second, bounded reads/retries; never echo the operator's identification."""

    def __init__(
        self,
        user_agent: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = utc_now,
        open_url=None,
    ):
        if (
            not isinstance(user_agent, str)
            or "igv-dashboard" not in user_agent
            or not 20 <= len(user_agent) <= 500
            or any(ord(char) < 32 for char in user_agent)
            or not re.search(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", user_agent)
        ):
            raise BlockedError(
                "Set secret SEC_USER_AGENT to truthful igv-dashboard/operator contact "
                "identification. It is not recorded in the archive."
            )
        self.user_agent = user_agent
        self.sleep, self.monotonic, self.clock = sleep, monotonic, clock
        self.open_url = (
            open_url
            or build_opener(
                NoRedirects(),
                HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
            ).open
        )
        self.last_request: float | None = None

    def __call__(self, url: str) -> bytes:
        if not allowed_url(url):
            raise DataError("Refusing a URL outside the official SEC source contract")
        for attempt in range(3):
            if self.last_request is not None:
                self.sleep(max(0, 1.0 - (self.monotonic() - self.last_request)))
            self.last_request = self.monotonic()
            request = Request(
                url,
                headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"},
            )
            delay = 2 ** (attempt + 1)
            try:
                with self.open_url(request, timeout=45) as response:
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise DataError("SEC document exceeds the bounded response limit")
                return raw
            except HTTPError as error:
                if error.code in {401, 403}:
                    raise BlockedError(
                        f"SEC denied access (HTTP {error.code}). Check declared operator "
                        "contact/access with SEC; do not change identity or bypass the block."
                    ) from None
                if error.code not in {408, 429, 500, 502, 503, 504}:
                    raise DataError(f"SEC HTTP {error.code}; no substitute source used") from None
                retry_after = error.headers.get("Retry-After") if error.headers else None
                if retry_after:
                    try:
                        delay = max(
                            delay,
                            float(retry_after)
                            if re.fullmatch(r"\d+", retry_after)
                            else (
                                parsedate_to_datetime(retry_after) - self.clock()
                            ).total_seconds(),
                        )
                    except (TypeError, ValueError, OverflowError):
                        raise DataError("Invalid SEC Retry-After; defer to a later run") from None
                    if delay > MAX_RETRY_DELAY:
                        raise DataError(
                            "SEC Retry-After exceeds this run's wait budget; retry later"
                        )
            except URLError as error:
                if isinstance(error.reason, ssl.SSLCertVerificationError):
                    raise DataError(
                        "SEC TLS verification failed; verify the CA bundle, never disable TLS"
                    ) from None
            except (TimeoutError, ConnectionError):
                pass
            if attempt == 2:
                break
            self.sleep(delay)
        raise DataError("SEC unavailable after three bounded attempts; last-good archive retained")


def resolve_identity(raw: bytes, retrieved: datetime, kind: str = PRODUCTION) -> dict:
    value = decode_json(raw)
    if (value.get("_synthetic_test_only") is True) != (kind == SYNTHETIC):
        raise DataError("Synthetic/production SEC identity data cannot be mixed")
    fields, rows = value.get("fields"), value.get("data")
    if fields != ["cik", "seriesId", "classId", "symbol"] or not isinstance(rows, list):
        raise DataError("SEC fund ticker mapping schema changed")
    matches = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 4:
            raise DataError("Malformed SEC fund ticker mapping row")
        if row[3] == SYMBOL:
            matches.append(row)
    if len(matches) != 1:
        raise DataError("IGV does not resolve to exactly one SEC fund class")
    cik, series, share_class, symbol = matches[0]
    if str(cik).zfill(10) != CIK:
        raise DataError("IGV ticker mapping registrant mismatch")
    if not isinstance(series, str) or not re.fullmatch(r"S\d{9}", series):
        raise DataError("Invalid SEC series identity")
    if not isinstance(share_class, str) or not re.fullmatch(r"C\d{9}", share_class):
        raise DataError("Invalid SEC class identity")
    return {
        "registrant_cik": CIK,
        "series_id": series,
        "class_id": share_class,
        "ticker": symbol,
        "series_name": FUND_NAME,
        "mapping_url": (
            "https://example.invalid/synthetic/company_tickers_mf.json"
            if kind == SYNTHETIC
            else MAPPING_URL
        ),
        "mapping_source_sha256": digest(raw),
        "resolved_at_utc": utc_text(retrieved),
    }


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    filing_date: str
    filename: str
    reported_date_hint: str

    def document_url(self, kind: str = PRODUCTION) -> str:
        return source_url(self.accession, self.filename, kind)


def search_url(series_id: str, start: str, end: str, offset: int, kind=PRODUCTION) -> str:
    base = "https://example.invalid/synthetic/search-index" if kind == SYNTHETIC else SEARCH_URL
    return (
        base
        + "?"
        + urlencode(
            {
                "q": f'"{series_id}"',
                "ciks": CIK,
                # EFTS uses the base form to include amendments; adding /A narrows to amendments.
                "forms": "NPORT-P",
                "dateRange": "custom",
                "startdt": start,
                "enddt": end,
                "from": offset,
                "size": 100,
            }
        )
    )


def discover_filings(
    fetcher: Callable[[str], bytes], identity: dict, start: str, end: str, kind=PRODUCTION
) -> list[Filing]:
    if parse_date(start) > parse_date(end):
        raise DataError("Reversed SEC discovery range")
    filings: dict[str, Filing] = {}
    offset, expected_total = 0, None
    while True:
        result = decode_json(fetcher(search_url(identity["series_id"], start, end, offset, kind)))
        hits = result.get("hits")
        if result.get("timed_out") is not False or not isinstance(hits, dict):
            raise DataError("Incomplete SEC filing search")
        total, items = hits.get("total"), hits.get("hits")
        if (
            not isinstance(total, dict)
            or total.get("relation") != "eq"
            or type(total.get("value")) is not int
            or not 0 <= total["value"] <= MAX_SEARCH_RESULTS
            or not isinstance(items, list)
        ):
            raise DataError("SEC search is malformed, truncated or too broad")
        if expected_total is None:
            expected_total = total["value"]
        elif expected_total != total["value"]:
            raise DataError("SEC search changed during pagination; retry on the next run")
        if len(items) != min(100, expected_total - offset):
            raise DataError("Missing SEC search results")
        for hit in items:
            if not isinstance(hit, dict) or not isinstance(hit.get("_source"), dict):
                raise DataError("Malformed SEC filing search hit")
            item = hit["_source"]
            accession = item.get("adsh")
            form = item.get("file_type")
            ciks = item.get("ciks")
            if (
                not isinstance(accession, str)
                or not re.fullmatch(ACCESSION_PATTERN, accession)
                or form not in FORMS
                or not isinstance(ciks, list)
                or CIK not in [str(cik).zfill(10) for cik in ciks]
            ):
                raise DataError("SEC search fund, form or accession mismatch")
            identifier = hit.get("_id")
            if not isinstance(identifier, str) or not identifier.startswith(accession + ":"):
                raise DataError("SEC search document identity missing")
            filename = identifier.removeprefix(accession + ":")
            source_url(accession, filename, kind)
            filed = parse_date(item.get("file_date")).isoformat()
            reported = parse_date(item.get("period_ending")).isoformat()
            if not start <= filed <= end or reported > filed:
                raise DataError("SEC filing dates are out of range")
            filing = Filing(accession, form, filed, filename, reported)
            if accession in filings:
                raise DataError("Duplicate SEC accession in search results")
            filings[accession] = filing
        offset += len(items)
        if offset == expected_total:
            break
    return sorted(filings.values(), key=lambda item: (item.filing_date, item.accession))


class FilingIndex(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, list[str]] = {}
        self.links: list[str] = []
        self.current_class = None
        self.current_text: list[str] = []
        self.label = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        if tag == "div" and attributes.get("class") in {"infoHead", "info"}:
            self.current_class = attributes["class"]
            self.current_text = []

    def handle_data(self, data):
        if self.current_class:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag != "div" or not self.current_class:
            return
        value = "".join(self.current_text).strip()
        if self.current_class == "infoHead":
            self.label = value
        elif self.label:
            self.fields.setdefault(self.label, []).append(value)
            self.label = None
        self.current_class = None


def index_metadata(raw: bytes, filing: Filing, kind: str = PRODUCTION) -> dict:
    parser = FilingIndex()
    try:
        parser.feed(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise DataError("Malformed SEC filing index") from error
    if parser.fields.get("Filing Date") != [filing.filing_date]:
        raise DataError("SEC index filing date mismatch")
    if parser.fields.get("Period of Report") != [filing.reported_date_hint]:
        raise DataError("SEC index period mismatch")
    accepted = parser.fields.get("Accepted")
    if not accepted or len(accepted) != 1:
        raise DataError("Missing SEC acceptance timestamp")
    try:
        local = datetime.strptime(accepted[0], "%Y-%m-%d %H:%M:%S")
    except ValueError as error:
        raise DataError("Invalid SEC acceptance timestamp") from error
    if local.date().isoformat() > filing.filing_date:
        raise DataError("SEC acceptance/filing date mismatch")
    raw_path = urlparse(filing.document_url(kind)).path
    folder, filename = raw_path.rsplit("/", 1)
    if not any(
        link == raw_path
        or link == filing.document_url(kind)
        or re.fullmatch(
            re.escape(folder) + r"/xslFormNPORT-P_[A-Za-z0-9]+/" + re.escape(filename), link
        )
        for link in parser.links
    ):
        raise DataError("SEC index does not link to the discovered N-PORT XML")
    return {
        "accession": filing.accession,
        "form": filing.form,
        "filing_date": filing.filing_date,
        "accepted_at_utc": utc_text(local.replace(tzinfo=NY)),
        "source_url": filing.document_url(kind),
        "filing_url": filing_url(filing.accession, kind),
        "filing_index_sha256": digest(raw),
    }


def local_name(element) -> str:
    namespace, separator, name = element.tag.removeprefix("{").partition("}")
    if not separator or namespace not in FIELD_NAMESPACES:
        raise DataError("Unrecognized namespace in N-PORT factual fields")
    return name


def child(parent, name: str, *, optional=False):
    matches = [element for element in parent if local_name(element) == name]
    if len(matches) != 1:
        if optional and not matches:
            return None
        raise DataError(f"N-PORT needs exactly one {name} element")
    return matches[0]


def field(parent, name: str, *, nullable=False) -> str | None:
    node = child(parent, name)
    if len(node):
        raise DataError(f"Unexpected nested N-PORT {name}")
    return text(node.text, name, nullable=nullable)


def choice_code(parent, name: str, conditional: str) -> str:
    simple = child(parent, name, optional=True)
    alternate = child(parent, conditional, optional=True)
    if (simple is None) == (alternate is None):
        raise DataError(f"N-PORT needs exactly one {name} choice")
    value = simple.text if simple is not None else alternate.attrib.get(name)
    return text(value, name)


def yes_no(value: object, label: str) -> bool:
    if value not in {"Y", "N"}:
        raise DataError(f"Invalid N-PORT {label}")
    return value == "Y"


LENDING_KEYS = {
    "is_cash_collateral",
    "cash_collateral_value_usd",
    "is_non_cash_collateral",
    "non_cash_collateral_value_usd",
    "is_loan_by_fund",
    "loan_value_usd",
}


def lending_facts(element) -> dict:
    facts = dict.fromkeys(sorted(LENDING_KEYS))
    block = child(element, "securityLending", optional=True)
    if block is None:
        return facts
    fields = (
        (
            "isCashCollateral",
            "cashCollateralCondition",
            "cashCollateralVal",
            "is_cash_collateral",
            "cash_collateral_value_usd",
        ),
        (
            "isNonCashCollateral",
            "nonCashCollateralCondition",
            "nonCashCollateralVal",
            "is_non_cash_collateral",
            "non_cash_collateral_value_usd",
        ),
        ("isLoanByFund", "loanByFundCondition", "loanVal", "is_loan_by_fund", "loan_value_usd"),
    )
    if any(
        local_name(node) not in {name for field in fields for name in field[:2]} for node in block
    ):
        raise DataError("Unrecognized N-PORT securities-lending field")
    for flag, conditional, amount, key, amount_key in fields:
        simple, alternate = (
            child(block, flag, optional=True),
            child(block, conditional, optional=True),
        )
        if simple is not None and alternate is not None:
            raise DataError("Ambiguous N-PORT securities-lending status")
        if simple is not None:
            facts[key] = yes_no(simple.text, flag)
        elif alternate is not None:
            facts[key] = yes_no(alternate.attrib.get(flag), flag)
            value = alternate.attrib.get(amount)
            decimal(value, amount)
            facts[amount_key] = value
    return facts


def holding_row(element, number: int) -> dict:
    if child(element, "notDissem", optional=True) is not None:
        raise DataError("N-PORT contains nondisseminated Part D rows; not a public portfolio")
    identifiers = child(element, "identifiers")
    securities: dict[str, list] = {"isin": [], "ticker": [], "other": []}
    for identifier in identifiers:
        name = local_name(identifier)
        if name not in securities:
            raise DataError("Unrecognized N-PORT security identifier")
        value = text(identifier.attrib.get("value"), name, nullable=True)
        if value is None:
            continue
        if name == "other":
            value = {
                "value": value,
                "type": text(identifier.attrib.get("otherDesc"), "identifier type"),
            }
        if value in securities[name]:
            raise DataError("Duplicate N-PORT security identifier")
        securities[name].append(value)
    row_xml = canonicalize(tostring(element, encoding="unicode"), strip_text=True)
    row = {
        "source_row": number,
        "source_row_sha256": digest(row_xml.encode()),
        "name": field(element, "name"),
        "title": field(element, "title"),
        "cusip": field(element, "cusip", nullable=True),
        "lei": field(element, "lei", nullable=True),
        **securities,
        "asset_category": choice_code(element, "assetCat", "assetConditional"),
        "issuer_category": choice_code(element, "issuerCat", "issuerConditional"),
        "payoff_profile": field(element, "payoffProfile"),
        "units": field(element, "units"),
        "currency": choice_code(element, "curCd", "currencyConditional"),
        "balance": field(element, "balance"),
        "market_value_usd": field(element, "valUSD"),
        "weight_pct": field(element, "pctVal"),
        "security_lending": lending_facts(element),
    }
    for name in ("balance", "market_value_usd", "weight_pct"):
        decimal(row[name], name)
    return row


def normalize_filing(
    raw: bytes,
    filing: Filing,
    identity: dict,
    metadata: dict,
    retrieved: datetime,
    kind: str = PRODUCTION,
) -> dict:
    if len(raw) > MAX_BYTES:
        raise DataError("N-PORT XML exceeds the response limit")
    try:
        document = fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ParseError, DefusedXmlException, ValueError) as error:
        raise DataError("Malformed or unsafe N-PORT XML") from error
    if document.tag != f"{{{NAMESPACE}}}edgarSubmission":
        raise DataError("Unrecognized N-PORT XML root/namespace")
    if (document.attrib.get("synthetic-test-only") == "true") != (kind == SYNTHETIC):
        raise DataError("Synthetic/production XML cannot be mixed")
    header = child(document, "headerData")
    if field(header, "submissionType") != filing.form:
        raise DataError("N-PORT form mismatch")
    form = child(document, "formData")
    general, fund = child(form, "genInfo"), child(form, "fundInfo")
    name = field(general, "seriesName")
    if (
        str(field(general, "regCik")).zfill(10) != identity["registrant_cik"]
        or field(general, "seriesId") != identity["series_id"]
        or name.casefold() != identity["series_name"].casefold()
    ):
        raise DataError("N-PORT does not match IGV's SEC registrant, series and name")
    if field(general, "isFinalFiling") != "N":
        raise DataError("Final N-PORT filing requires manual fund/cadence review")
    reported = field(general, "repPdDate")
    fiscal_end = field(general, "repPdEnd")
    if reported != filing.reported_date_hint:
        raise DataError("N-PORT A.3(b) reported date disagrees with the SEC filing index")
    quarter = fiscal_quarter(reported, fiscal_end)
    schedule = child(form, "invstOrSecs")
    if any(local_name(item) != "invstOrSec" for item in schedule):
        raise DataError("Unrecognized N-PORT portfolio row")
    rows = [holding_row(item, number) for number, item in enumerate(schedule, 1)]
    snapshot = {
        "schema_version": 2,
        "data_kind": kind,
        "source": SOURCE,
        "registrant_cik": identity["registrant_cik"],
        "series_id": identity["series_id"],
        "series_name": name,
        "reported_as_of": reported,
        "fiscal_year_end": fiscal_end,
        "fiscal_quarter": quarter,
        "net_assets_usd": field(fund, "netAssets"),
        "source_row_count": len(rows),
        "holdings": rows,
        "provenance": {
            **metadata,
            "retrieved_at_utc": utc_text(retrieved),
            "source_sha256": digest(raw),
        },
    }
    validate_snapshot(snapshot, identity, retrieved, kind)
    return snapshot


ROW_KEYS = {
    "source_row",
    "source_row_sha256",
    "name",
    "title",
    "cusip",
    "lei",
    "isin",
    "ticker",
    "other",
    "asset_category",
    "issuer_category",
    "payoff_profile",
    "units",
    "currency",
    "balance",
    "market_value_usd",
    "weight_pct",
    "security_lending",
}
SNAPSHOT_KEYS = {
    "schema_version",
    "data_kind",
    "source",
    "registrant_cik",
    "series_id",
    "series_name",
    "reported_as_of",
    "fiscal_year_end",
    "fiscal_quarter",
    "net_assets_usd",
    "source_row_count",
    "holdings",
    "provenance",
}
PROVENANCE_KEYS = {
    "accession",
    "form",
    "filing_date",
    "accepted_at_utc",
    "source_url",
    "filing_url",
    "filing_index_sha256",
    "retrieved_at_utc",
    "source_sha256",
}
IDENTITY_KEYS = {
    "registrant_cik",
    "series_id",
    "class_id",
    "ticker",
    "series_name",
    "mapping_url",
    "mapping_source_sha256",
    "resolved_at_utc",
}


def validate_identity(identity: dict, now: datetime, kind: str) -> None:
    if (
        not isinstance(identity, dict)
        or set(identity) != IDENTITY_KEYS
        or identity["registrant_cik"] != CIK
        or identity["ticker"] != SYMBOL
        or identity["series_name"] != FUND_NAME
        or not re.fullmatch(r"S\d{9}", str(identity["series_id"]))
        or not re.fullmatch(r"C\d{9}", str(identity["class_id"]))
        or identity["mapping_url"]
        != (
            "https://example.invalid/synthetic/company_tickers_mf.json"
            if kind == SYNTHETIC
            else MAPPING_URL
        )
    ):
        raise DataError("SEC fund identity/provenance mismatch")
    check_hash(identity["mapping_source_sha256"])
    if parse_utc(identity["resolved_at_utc"]) > now:
        raise DataError("Fund identity resolution is in the future")


def validate_snapshot(snapshot: dict, identity: dict, now: datetime, kind: str) -> None:
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != SNAPSHOT_KEYS
        or type(snapshot["schema_version"]) is not int
        or snapshot["schema_version"] != 2
        or snapshot["source"] != SOURCE
        or snapshot["data_kind"] != kind
        or snapshot["registrant_cik"] != identity["registrant_cik"]
        or snapshot["series_id"] != identity["series_id"]
        or not isinstance(snapshot["series_name"], str)
        or snapshot["series_name"].casefold() != identity["series_name"].casefold()
    ):
        raise DataError("Normalized SEC facts schema/source/fund mismatch")
    quarter = fiscal_quarter(snapshot["reported_as_of"], snapshot["fiscal_year_end"])
    if snapshot["fiscal_quarter"] != quarter or isinstance(snapshot["fiscal_quarter"], bool):
        raise DataError("Incorrect fiscal-quarter classification")
    if decimal(snapshot["net_assets_usd"], "net assets") <= 0:
        raise DataError("Nonpositive fund net assets require manual review")
    provenance = snapshot["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != PROVENANCE_KEYS:
        raise DataError("Unexpected or incomplete SEC source provenance")
    accession = provenance["accession"]
    if not isinstance(accession, str) or not re.fullmatch(ACCESSION_PATTERN, accession):
        raise DataError("Invalid SEC accession")
    if provenance["form"] not in FORMS:
        raise DataError("Only public NPORT-P filings/amendments may be published")
    url = provenance["source_url"]
    if not isinstance(url, str) or url != source_url(accession, url.rsplit("/", 1)[-1], kind):
        raise DataError("SEC XML source URL mismatch")
    if provenance["filing_url"] != filing_url(accession, kind):
        raise DataError("SEC filing index URL mismatch")
    filed = parse_date(provenance["filing_date"])
    accepted = parse_utc(provenance["accepted_at_utc"])
    retrieved = parse_utc(provenance["retrieved_at_utc"])
    if (
        parse_date(snapshot["reported_as_of"]) > filed
        or accepted.astimezone(NY).date() > filed
        or filed > retrieved.date()
        or parse_date(snapshot["reported_as_of"]) > accepted.astimezone(NY).date()
        or accepted > retrieved
        or retrieved > now
    ):
        raise DataError("Future or inconsistent SEC reported/filing/acceptance/retrieval dates")
    for name in ("source_sha256", "filing_index_sha256"):
        check_hash(provenance[name])
    rows = snapshot["holdings"]
    if (
        not isinstance(rows, list)
        or not 10 <= len(rows) <= 10000
        or type(snapshot["source_row_count"]) is not int
        or snapshot["source_row_count"] != len(rows)
    ):
        raise DataError("Incomplete N-PORT portfolio/row count")
    row_hashes = set()
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            raise DataError("Unexpected normalized holdings fields")
        if type(row["source_row"]) is not int or row["source_row"] != number:
            raise DataError("Duplicate or missing source row")
        check_hash(row["source_row_sha256"])
        if row["source_row_sha256"] in row_hashes:
            raise DataError("Duplicate complete N-PORT source row")
        row_hashes.add(row["source_row_sha256"])
        for name in (
            "name",
            "title",
            "asset_category",
            "issuer_category",
            "payoff_profile",
            "units",
            "currency",
        ):
            text(row[name], name)
        for name in ("cusip", "lei"):
            text(row[name], name, nullable=True)
        for name in ("isin", "ticker", "other"):
            if not isinstance(row[name], list):
                raise DataError("Invalid normalized identifiers")
            for identifier in row[name]:
                if name == "other":
                    if not isinstance(identifier, dict) or set(identifier) != {"value", "type"}:
                        raise DataError("Invalid normalized other identifier")
                    text(identifier["value"], "other identifier")
                    text(identifier["type"], "other identifier type")
                else:
                    text(identifier, name)
        for name in ("balance", "market_value_usd", "weight_pct"):
            decimal(row[name], name)
        lending = row["security_lending"]
        if not isinstance(lending, dict) or set(lending) != LENDING_KEYS:
            raise DataError("Invalid normalized securities-lending facts")
        for flag, amount in [
            ("is_cash_collateral", "cash_collateral_value_usd"),
            ("is_non_cash_collateral", "non_cash_collateral_value_usd"),
            ("is_loan_by_fund", "loan_value_usd"),
        ]:
            if lending[flag] is not None and type(lending[flag]) is not bool:
                raise DataError("Invalid normalized securities-lending flag")
            if lending[amount] is not None:
                decimal(lending[amount], amount)
                if lending[flag] is not True:
                    raise DataError("Lending amount without an affirmative reported flag")
        cash_vehicle = row["asset_category"] == "STIV" and row["issuer_category"] == "RF"
        collateral = lending["is_cash_collateral"]
        if collateral is True and not cash_vehicle:
            raise DataError("IGV cash-collateral status and asset/issuer classification disagree")


def rank_holdings(holdings: list[dict]) -> list[dict]:
    rows = sorted(
        holdings,
        key=lambda row: (
            decimal(row["weight_pct"]),
            decimal(row["market_value_usd"]),
            -row["source_row"],
        ),
        reverse=True,
    )
    return [{**row, "rank": number} for number, row in enumerate(rows, 1)]


def ranked_snapshot(
    snapshot: dict, object_sha256: str, *, exclude_collateral: bool = False
) -> dict:
    rows = snapshot["holdings"]
    eligible = (
        [row for row in rows if row["security_lending"]["is_cash_collateral"] is not True]
        if exclude_collateral
        else rows
    )
    if len(eligible) < 10:
        raise DataError("A complete top ten needs at least ten eligible reported positions")
    top = rank_holdings(eligible)[:10]
    total = decimal_sum(row["weight_pct"] for row in eligible)
    notes = []
    if exclude_collateral:
        notes.append(
            f"Reported investment weights excluding collateral total {total}% of net assets. "
            "The omitted weights are not redistributed; neither view is normalized to 100%."
        )
    elif abs(Decimal(total) - 100) > Decimal("0.005"):
        notes.append(
            f"Reported investment weights total {total}% of net assets. "
            "The investment schedule need not total 100% (cash, liabilities and derivatives "
            "can differ); values are retained without normalization."
        )
    missing = sum(not row["ticker"] for row in top)
    identifier_note = ""
    if not any(row["ticker"] for row in rows):
        identifier_note = (
            "This SEC N-PORT filing supplies no exchange tickers. Positions are identified "
            "by security title, issuer name, CUSIP and available ISIN; no tickers are inferred."
        )
    elif missing:
        notes.append(
            f"{missing} top-ten positions have no reported ticker. "
            "SEC names and available CUSIP/ISIN identifiers are shown; no current tickers inferred."
        )
    collateral_rows = [row for row in rows if row["security_lending"]["is_cash_collateral"] is True]
    if collateral_rows:
        action = "Excludes" if exclude_collateral else "Includes"
        notes.append(
            f"{action} securities-lending cash collateral: "
            f"{decimal_sum(row['weight_pct'] for row in collateral_rows)}% of net assets. "
            "Collateral is a reported investment with an offsetting obligation elsewhere; "
            "it can lift total weights above 100% and change the as-filed top ten."
        )
    return {
        key: value
        for key, value in {
            **snapshot,
            "top10": top,
            "top10_weight_pct": decimal_sum(row["weight_pct"] for row in top),
            "portfolio_weight_pct": total,
            "quality_notes": notes,
            "identifier_note": identifier_note,
            "object_sha256": object_sha256,
        }.items()
        if key != "holdings"
    }
