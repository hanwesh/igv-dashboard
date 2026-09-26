import importlib.util
import json
import re

import pytest
import yaml

import igv_snapshot as app
import sec_nport as sec

WORKFLOW = app.ROOT / ".github/workflows/update-and-deploy.yml"
SPEC = importlib.util.spec_from_file_location(
    "pipeline_issue", app.ROOT / ".github/scripts/pipeline_issue.py"
)
issues = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(issues)


@pytest.mark.parametrize(
    "event", ["pull_request", "pull_request_target", "push", "schedule", "workflow_dispatch"]
)
@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/feature", "refs/pull/1/merge"])
@pytest.mark.parametrize("approval", ["false", "", "TRUE", "true"])
def test_live_context_gate_matrix(event, ref, approval):
    allowed = (
        approval == "true"
        and ref == "refs/heads/main"
        and event in {"push", "schedule", "workflow_dispatch"}
    )
    env = {
        "SEC_PUBLICATION_APPROVED": approval,
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REF": ref,
        "GITHUB_REPOSITORY": sec.REPOSITORY,
    }
    assert sec.publication_allowed(env) == allowed
    env["GITHUB_REPOSITORY"] = "untrusted/fork"
    assert not sec.publication_allowed(env)


def test_workflow_scope_and_off_gate():
    text = WORKFLOW.read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"pull_request", "push", "workflow_dispatch", "schedule"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["on"]["schedule"] == [{"cron": "17 7 * * *"}]
    assert workflow["permissions"] == {}
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    jobs = workflow["jobs"]
    assert set(jobs) == {"checks", "refresh", "deploy", "notify"}
    assert jobs["checks"]["permissions"] == {"contents": "read"}
    assert "allow-network" not in json.dumps(jobs["checks"])
    assert not any("upload" in step.get("uses", "") for step in jobs["checks"]["steps"])
    assert "tests.browser_smoke" in json.dumps(jobs["checks"])
    for name in ("refresh", "deploy", "notify"):
        condition = jobs[name]["if"]
        assert "github.repository == 'hanwesh/igv-dashboard'" in condition
        assert "github.ref == 'refs/heads/main'" in condition
        assert '["push","schedule","workflow_dispatch"]' in condition
        assert "vars.SEC_PUBLICATION_APPROVED == 'true'" in condition
    assert jobs["refresh"]["permissions"] == {"contents": "write"}
    assert jobs["refresh"]["steps"][0]["with"]["ref"] == "${{ github.sha }}"
    assert jobs["deploy"]["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert jobs["notify"]["permissions"] == {"contents": "read", "issues": "write"}
    assert jobs["deploy"]["needs"] == ["checks", "refresh"]
    assert "needs.refresh.outputs.data_sha" in json.dumps(jobs["deploy"])
    actions = [step for job in jobs.values() for step in job["steps"] if "uses" in step]
    assert all(re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"]) for step in actions)
    configure = next(step for step in actions if "actions/configure-pages@" in step["uses"])
    assert configure["with"]["enablement"] == "false"
    for step in actions:
        if "actions/upload-pages-artifact@" in step["uses"]:
            assert step["with"]["path"] == "site"
    assert "git push origin HEAD:main" in text
    assert "--force" not in text and "pull_request_target" not in text
    assert text.count("secrets.") == 1
    assert "SEC_USER_AGENT: ${{ secrets.SEC_USER_AGENT }}" in text
    assert "secrets.SEC_USER_AGENT" not in json.dumps(jobs["checks"])
    assert "secrets.SEC_USER_AGENT" not in json.dumps(jobs["deploy"])
    assert "github.token" in text
    assert "Co-authored-by: Copilot App" in text
    refresh_steps = json.dumps(jobs["refresh"]["steps"])
    assert refresh_steps.index("validate --allow-stale") < refresh_steps.index(
        "refresh --allow-network"
    )
    assert "bootstrap" not in refresh_steps
    assert "data/manifest.json data/objects" in refresh_steps
    assert "git add ." not in text


def test_normalized_sec_schema_matches_runtime_contract():
    schema = json.loads((app.ROOT / "data/schema.json").read_text())
    assert "Normalized SEC N-PORT factual archive" in schema["description"]
    assert schema["properties"]["source"]["const"] == sec.SOURCE
    assert set(schema["required"]) == app.MANIFEST_KEYS
    assert set(schema["$defs"]["provenance"]["required"]) == sec.PROVENANCE_KEYS
    assert set(schema["$defs"]["holding"]["required"]) == sec.ROW_KEYS
    assert set(schema["$defs"]["normalized_filing"]["required"]) == sec.SNAPSHOT_KEYS
    assert set(schema["$defs"]["identity"]["required"]) == sec.IDENTITY_KEYS


def test_legacy_approval_and_provider_code_cannot_authorize_new_source(monkeypatch):
    assert not sec.publication_allowed({"DATA_PUBLICATION_APPROVED": "true"})
    code = (app.ROOT / "igv_snapshot.py").read_text() + (app.ROOT / "sec_nport.py").read_text()
    for obsolete in [
        "query1.finance.yahoo.com",
        "www.blackrock.com",
        "www.ishares.com",
        "exchange_calendars",
        "parse_prices",
        "trading_days",
        "PRICE_API",
    ]:
        assert obsolete not in code
    monkeypatch.setenv("DATA_PUBLICATION_APPROVED", "true")
    with pytest.raises(SystemExit, match="BLOCKED"):
        issues.main()


def test_actions_cannot_bootstrap_even_on_gated_main(tmp_path, monkeypatch):
    for key, value in {
        "SEC_PUBLICATION_APPROVED": "true",
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": sec.REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
    }.items():
        monkeypatch.setenv(key, value)
    assert app.main(["bootstrap", "--allow-network", "--data-dir", str(tmp_path)]) == 2
    with pytest.raises(sec.BlockedError, match="cannot bootstrap"):
        app.refresh_archive(tmp_path, lambda _: pytest.fail("no network"), bootstrap=True)


def incident(number=7, state="open"):
    return {
        "number": number,
        "state": state,
        "title": issues.TITLE,
        "body": issues.MARKER,
        "user": {"login": "github-actions[bot]"},
    }


def test_single_incident_failure_update_reopen_and_recovery():
    run = "https://github.com/hanwesh/igv-dashboard/actions/runs/123"
    assert issues.desired_issue([], "success", run) is None
    number, fields = issues.desired_issue([], "failure", run)
    assert number is None and fields["state"] == "open"
    assert issues.MARKER in fields["body"] and run in fields["body"]
    existing = {**incident(), **fields}
    assert issues.desired_issue([existing], "failure", run) is None
    number, fields = issues.desired_issue([incident(state="closed")], "failure", run)
    assert number == 7 and fields["state"] == "open"
    number, fields = issues.desired_issue([incident()], "success", run)
    assert number == 7 and fields["state"] == "closed"
    assert issues.desired_issue([incident(state="closed")], "success", run) is None
    unrelated = {**incident(), "user": {"login": "someone-else"}}
    assert issues.desired_issue([unrelated], "failure", run)[0] is None
    with pytest.raises(ValueError, match="Multiple"):
        issues.desired_issue([incident(7), incident(8)], "failure", run)


def test_issue_writer_blocked_even_with_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "synthetic-not-a-token")
    with pytest.raises(SystemExit, match="BLOCKED"):
        issues.main()
