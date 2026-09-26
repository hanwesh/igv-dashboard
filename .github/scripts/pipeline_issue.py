"""Maintain only the authorized pipeline's own incident issue; no source data in issues."""

import json
import os
import subprocess
import sys

MARKER = "<!-- igv-dashboard:authorized-pipeline -->"
REPOSITORY = "hanwesh/igv-dashboard"
TITLE = "[automation] Authorized IGV refresh/publication failure"


def desired_issue(issues: list[dict], result: str, run_url: str) -> tuple[int | None, dict] | None:
    matches = [
        issue
        for issue in issues
        if MARKER in (issue.get("body") or "")
        and issue.get("user", {}).get("login") == "github-actions[bot]"
        and "pull_request" not in issue
    ]
    if len(matches) > 1:
        raise ValueError("Multiple pipeline incident issues; resolve the duplicate manually")
    existing = matches[0] if matches else None
    if result == "success":
        if existing is None or existing["state"] == "closed":
            return None
        body = (
            f"{MARKER}\n\nRecovered: the authorized archive and Pages deployment succeeded.\n\n"
            f"Successful run: {run_url}\n\nNo source records are included in this issue."
        )
        return existing["number"], {"body": body, "state": "closed", "state_reason": "completed"}
    if result != "failure":
        raise ValueError("Unknown pipeline outcome")
    body = (
        f"{MARKER}\n\nThe authorized pipeline failed. Any last-good deployed site remains in "
        "place unless the deployment itself was externally changed. No partial response "
        "replaces the active archive.\n\n"
        f"Latest failed run: {run_url}\n\n"
        "Inspect the failed step. Delayed dated holdings should be retried on the next schedule "
        "or by manual dispatch; never substitute a latest-date response. Fix source, calendar, "
        "permission or concurrent-main errors before retrying. Setting "
        "`DATA_PUBLICATION_APPROVED=false` pauses future live jobs.\n\n"
        "This single issue is updated on failure, reopened if needed, and closed only after a "
        "successful authorized deployment. No source records are included."
    )
    fields = {"title": TITLE, "body": body, "state": "open"}
    if existing is not None and all(existing.get(key) == value for key, value in fields.items()):
        return None
    return (existing["number"] if existing else None), fields


def api(endpoint: str, *, method: str = "GET", fields: dict | None = None):
    command = ["gh", "api", endpoint, "--method", method]
    if fields is None:
        command += ["--paginate", "--slurp"]
    else:
        command += ["--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(fields) if fields is not None else None,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def main() -> None:
    if not (
        os.environ.get("DATA_PUBLICATION_APPROVED") == "true"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("GITHUB_EVENT_NAME") in {"push", "schedule", "workflow_dispatch"}
    ):
        raise SystemExit("BLOCKED: issue writes require approval and trusted main context")
    run_id = os.environ["GITHUB_RUN_ID"]
    if not run_id.isdigit():
        raise ValueError("Invalid workflow run ID")
    run_url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
    endpoint = f"repos/{REPOSITORY}/issues"
    pages = api(endpoint + "?state=all&creator=github-actions%5Bbot%5D&per_page=100")
    mutation = desired_issue(
        [issue for page in pages for issue in page], os.environ["PIPELINE_RESULT"], run_url
    )
    if mutation is None:
        print("No incident issue change needed.")
        return
    number, fields = mutation
    if number is None:
        fields.pop("state", None)
    api(
        endpoint if number is None else f"{endpoint}/{number}",
        method="POST" if number is None else "PATCH",
        fields=fields,
    )
    print("Pipeline incident issue updated.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"FAILED: incident notification: {error}", file=sys.stderr)
        raise SystemExit(1) from error
