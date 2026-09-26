import importlib.util
import json
from urllib.error import URLError

import pytest

import igv_snapshot as app
from tests.synthetic import mapping

SPEC = importlib.util.spec_from_file_location(
    "sec_diagnostic", app.ROOT / ".github/scripts/sec_access_diagnostic.py"
)
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)
ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": "hanwesh/igv-dashboard",
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_REF": "refs/heads/diagnostic-review",
    "SEC_ACCESS_DIAGNOSTIC": "true",
}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_ACTIONS", ""),
        ("GITHUB_REPOSITORY", "other/repo"),
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/pull/2/merge"),
        ("SEC_ACCESS_DIAGNOSTIC", "false"),
        ("SEC_ACCESS_DIAGNOSTIC", ""),
    ],
)
def test_no_diagnostic_requests_without_explicit_trusted_dispatch(key, value):
    assert (
        diagnostic.main({**ENV, key: value}, reader=lambda _: pytest.fail("No network permitted"))
        == 2
    )


@pytest.mark.parametrize(
    ("body", "category"),
    [
        (b"<title>Request Rate Threshold Exceeded</title>", "rate-threshold"),
        (b"<title>Your Request Originates from an Undeclared Automated Tool</title>", "undeclared"),
        (b"<body>PRIVATE RAW RESPONSE MUST NOT BE LOGGED</body>", "unavailable"),
    ],
)
def test_diagnostic_stops_on_denial_without_response_logs_or_retries(body, category, capsys):
    calls = []

    def read(url):
        calls.append(url)
        return 403, body

    assert diagnostic.main(ENV, reader=read, sleep=lambda _: pytest.fail("No retry")) == 1
    assert calls == [diagnostic.ENDPOINTS[0]]
    output = capsys.readouterr().out
    assert "HTTP 403" in output and category in output
    assert "<title>" not in output and "PRIVATE RAW RESPONSE" not in output


def test_success_logs_only_exact_mapping_and_status(capsys):
    calls, waits = [], []
    replies = iter(
        [
            (200, mapping()),
            (200, json.dumps({"cik": "1100663", "private_test_marker": "DO_NOT_LOG"}).encode()),
        ]
    )

    def read(url):
        calls.append(url)
        return next(replies)

    assert diagnostic.main(ENV, reader=read, sleep=waits.append) == 0
    assert calls == list(diagnostic.ENDPOINTS) and waits == [1]
    output = capsys.readouterr().out
    assert "CIK=0001100663 series=S000999999 class=C000999999" in output
    assert "TEST-NOT-IGV" not in output and "DO_NOT_LOG" not in output


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"{}", b"x" * (diagnostic.MAX_BYTES + 1)])
def test_diagnostic_bad_success_response_is_not_treated_as_access_verification(body, capsys):
    assert diagnostic.main(ENV, reader=lambda _: (200, body)) == 1
    assert "CIK=" not in capsys.readouterr().out


def test_diagnostic_no_redirect_host_fallback_or_unbounded_transport_retry(capsys):
    def read(_):
        raise URLError("Potentially sensitive detail")

    assert diagnostic.main(ENV, reader=read) == 1
    assert "Potentially sensitive detail" not in capsys.readouterr().out
    assert (
        diagnostic.NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.invalid")
        is None
    )
    with pytest.raises(ValueError, match="fixed"):
        diagnostic.read_metadata("https://example.invalid")
    assert diagnostic.USER_AGENT == (
        "igv-dashboard SEC holdings research; "
        "contact https://github.com/hanwesh/igv-dashboard/issues"
    )
