"""One explicitly dispatched, read-only check of SEC metadata access in Actions."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = "hanwesh/igv-dashboard"
USER_AGENT = (
    "igv-dashboard SEC holdings research; contact https://github.com/hanwesh/igv-dashboard/issues"
)
ENDPOINTS = (
    "https://www.sec.gov/files/company_tickers_mf.json",
    "https://data.sec.gov/submissions/CIK0001100663.json",
)
MAX_BYTES = 4 * 1024 * 1024


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def read_metadata(url: str) -> tuple[int, bytes]:
    if url not in ENDPOINTS:
        raise ValueError("Only the two fixed official metadata endpoints are permitted")
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with build_opener(NoRedirects()).open(request, timeout=30) as response:
            return response.status, response.read(MAX_BYTES + 1)
    except HTTPError as error:
        return error.code, error.read(MAX_BYTES + 1)


def denial_category(raw: bytes) -> str:
    lower = raw.lower()
    if b"request rate threshold exceeded" in lower:
        return "SEC rate-threshold denial"
    if b"undeclared automated tool" in lower:
        return "SEC undeclared-automated-tool denial"
    return "SEC access unavailable"


def main(
    env: Mapping[str, str] | None = None,
    *,
    reader: Callable[[str], tuple[int, bytes]] = read_metadata,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    env = os.environ if env is None else env
    if not (
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and env.get("SEC_ACCESS_DIAGNOSTIC") == "true"
        and env.get("GITHUB_REF", "").startswith("refs/heads/")
    ):
        print("BLOCKED: diagnostic requires explicit dispatch in this repository")
        return 2
    for number, url in enumerate(ENDPOINTS):
        if number:
            sleep(1)
        try:
            status, raw = reader(url)
        except (URLError, TimeoutError, ConnectionError, OSError):
            print(f"Endpoint {number + 1}: transport failure; no retries")
            return 1
        if status != 200:
            print(f"Endpoint {number + 1}: HTTP {status}; {denial_category(raw)}; no retries")
            return 1
        if len(raw) > MAX_BYTES:
            print(f"Endpoint {number + 1}: HTTP 200; response exceeds diagnostic size bound")
            return 1
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            print(f"Endpoint {number + 1}: HTTP 200; invalid metadata JSON")
            return 1
        if not isinstance(data, dict):
            print(f"Endpoint {number + 1}: HTTP 200; invalid metadata schema")
            return 1
        if number == 0:
            rows = data.get("data")
            if (
                data.get("fields") != ["cik", "seriesId", "classId", "symbol"]
                or not isinstance(rows, list)
                or any(not isinstance(row, list) or len(row) != 4 for row in rows)
            ):
                print("Endpoint 1: HTTP 200; fund ticker mapping schema mismatch")
                return 1
            matches = [row for row in rows if row[3] == "IGV"]
            if len(matches) != 1:
                print("Endpoint 1: HTTP 200; IGV does not have one unique mapping")
                return 1
            cik, series, share_class, _ = matches[0]
            if (
                str(cik).zfill(10) != "0001100663"
                or not isinstance(series, str)
                or not re.fullmatch(r"S\d{9}", series)
                or not isinstance(share_class, str)
                or not re.fullmatch(r"C\d{9}", share_class)
            ):
                print("Endpoint 1: HTTP 200; IGV identity does not match the expected contract")
                return 1
            print(
                f"Endpoint 1: HTTP 200; IGV CIK={int(cik):010d} series={series} class={share_class}"
            )
        else:
            if str(data.get("cik")).zfill(10) != "0001100663":
                print("Endpoint 2: HTTP 200; submissions CIK mismatch")
                return 1
            print("Endpoint 2: HTTP 200; SEC registrant submissions metadata available")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
