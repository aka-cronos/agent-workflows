"""Low-risk merge: squash-merge an open pull request when the project's lane
switch is on and the head commit is green and graded low.

Checks every open pull request, so any trigger (a status, a finished workflow)
re-evaluates them all and none waits for an event of its own."""

from __future__ import annotations

import sys

from common import (
    EXEMPT_LABEL, GhError, REQUIRED_STATUSES, ZONE_LABELS, api, api_list, checks_state, env, gh,
    latest_statuses, load_process, log, summary,
)


def risk_grade(description: str | None) -> str:
    """The grade at the start of a `risk` description, past a carry prefix
    (`carried from <sha>: <reason> · low: ...`)."""
    text = (description or "").strip()
    if text.startswith("carried from"):
        text = text.partition(" · ")[2]
    return text.split(":", 1)[0].strip().lower()


def blockers(pr: dict, statuses: dict[str, dict], checks: str) -> list[str]:
    """Why this pull request may not merge on its own; empty means it may."""
    reasons = []
    if pr.get("draft"):
        reasons.append("draft")
    labels = {label["name"] for label in pr.get("labels", [])}
    flagged = sorted(labels & (set(ZONE_LABELS) | {EXEMPT_LABEL[0]}))
    if flagged:
        reasons.append(f"labelled {', '.join(flagged)}")
    if checks != "green":
        reasons.append(f"checks {checks}")
    for context in REQUIRED_STATUSES:
        state = statuses.get(context, {}).get("state")
        if state != "success":
            reasons.append(f"{context} {state or 'missing'}")
    interrogate = statuses.get("review/interrogate")
    if interrogate and interrogate["state"] != "success":
        reasons.append(f"review/interrogate {interrogate['state']}")
    if risk_grade(statuses.get("risk", {}).get("description")) != "low":
        reasons.append("risk is not low")
    return reasons


def main() -> None:
    repo = env("REPO")
    default_branch = api(f"repos/{repo}")["default_branch"]
    process = load_process(repo, default_branch)
    if not process or process.get("lowRiskLane") is not True:
        log("lowRiskLane is off: nothing merges.")
        summary(["### Low-risk merge", "", "- lowRiskLane is off: nothing merges."])
        return

    lines, failed = [], False
    for pr in api_list(f"repos/{repo}/pulls?state=open&base={default_branch}"):
        number, sha = pr["number"], pr["head"]["sha"]
        reasons = blockers(pr, latest_statuses(repo, sha), checks_state(repo, sha))
        if reasons:
            lines.append(f"#{number}: waits ({'; '.join(reasons)})")
            continue
        try:
            gh("pr", "merge", str(number), "--repo", repo, "--squash", "--match-head-commit", sha)
            lines.append(f"#{number}: merged at {sha[:7]}")
        except GhError as error:
            failed = True
            lines.append(f"#{number}: merge failed: {error}")
    for line in lines or ["No open pull requests."]:
        log(line)
    summary(["### Low-risk merge", ""] + [f"- {line}" for line in lines or ["No open pull requests."]])
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
