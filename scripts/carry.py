"""Status carry: move process statuses to a new commit when the new commit
does not need them run again.

- Clean rebase: the pull request's own change is identical before and after
  (`git patch-id`), so every process status carries over.
- Design-only round: the previous reviewed commit had `verify` and
  `review/spec` in success and the round touched no UI file, so `verify`
  carries over. The new commit's reviews still run again.

Both need every check on the new commit green first, e2e included."""

from __future__ import annotations

import subprocess

from common import (
    PROCESS_STATUSES, api, api_list, checks_state, env, graphql, latest_statuses,
    load_process, log, match_paths, open_prs_for_sha, set_status, summary,
)

HISTORY_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      timelineItems(itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT], last: 1) {
        nodes { ... on HeadRefForcePushedEvent { beforeCommit { oid } afterCommit { oid } } }
      }
      commits(last: 100) { nodes { commit { oid } } }
    }
  }
}
"""
LOOKBACK = 30


def patch_id(diff: str) -> str | None:
    """Stable patch id of a whole diff; None for an empty diff."""
    if not diff.strip():
        return None
    result = subprocess.run(["git", "patch-id", "--stable"], input=diff,
                            capture_output=True, text=True, check=True)
    ids = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    return ids[0] if len(ids) == 1 else None


def statuses_to_carry(previous: dict[str, dict], current: dict[str, dict],
                      contexts=PROCESS_STATUSES) -> list[dict]:
    """Statuses the previous commit has and the new one lacks."""
    return [previous[c] for c in contexts if c in previous and c not in current]


def design_only_carry(previous: dict[str, dict], current: dict[str, dict],
                      ui_touched: bool) -> bool:
    return (
        not ui_touched
        and "verify" not in current
        and previous.get("verify", {}).get("state") == "success"
        and previous.get("review/spec", {}).get("state") == "success"
    )


def carried_description(sha: str, reason: str, original: str | None) -> str:
    text = f"carried from {sha[:7]}: {reason}"
    if original and not original.startswith("carried from"):
        text += f" · {original}"
    return text[:140]


def history(repo: str, number: int) -> tuple[dict | None, list[str]]:
    owner, name = repo.split("/")
    pr = graphql(HISTORY_QUERY, owner=owner, name=name, number=number)["repository"]["pullRequest"]
    pushes = pr["timelineItems"]["nodes"]
    commits = [node["commit"]["oid"] for node in pr["commits"]["nodes"]]
    return (pushes[-1] if pushes else None), commits


def compare_diff(repo: str, base: str, head: str) -> str | None:
    try:
        return api(f"repos/{repo}/compare/{base}...{head}", accept="application/vnd.github.diff")
    except Exception as error:  # noqa: BLE001 - a diff too large to fetch means no carry
        log(f"Could not fetch the diff {base}...{head}: {error}")
        return None


def carry(repo: str, sha: str, previous_sha: str, reason: str, statuses: list[dict]) -> list[str]:
    for status in statuses:
        set_status(repo, sha, status["context"], status["state"],
                   carried_description(previous_sha, reason, status.get("description")),
                   status.get("target_url"))
    return [s["context"] for s in statuses]


def handle(repo: str, pr: dict) -> str:
    number, sha = pr["number"], pr["head"]["sha"]
    state = checks_state(repo, sha)
    if state != "green":
        return f"#{number}: checks are {state} on {sha[:7]}; nothing carried"

    current = latest_statuses(repo, sha)
    last_push, commits = history(repo, number)

    if last_push and (last_push.get("afterCommit") or {}).get("oid") == sha:
        before = (last_push.get("beforeCommit") or {}).get("oid")
        if not before:
            return f"#{number}: force push without a previous commit; nothing carried"
        base = pr["base"]["ref"]
        old, new = compare_diff(repo, base, before), compare_diff(repo, base, sha)
        if old is None or new is None or patch_id(old) is None or patch_id(old) != patch_id(new):
            return f"#{number}: the change differs from {before[:7]}: a new round, nothing carried"
        moved = carry(repo, sha, before, "clean rebase",
                      statuses_to_carry(latest_statuses(repo, before), current))
        return f"#{number}: clean rebase from {before[:7]}; carried {', '.join(moved) or 'nothing'}"

    if sha not in commits:
        return f"#{number}: {sha[:7]} not among the last 100 commits; nothing carried"
    index = commits.index(sha)
    for previous_sha in reversed(commits[max(0, index - LOOKBACK):index]):
        previous = latest_statuses(repo, previous_sha)
        if "review/spec" not in previous:
            continue
        process = load_process(repo, pr["base"]["ref"]) or {}
        files = [f["filename"] for f in api_list(f"repos/{repo}/compare/{previous_sha}...{sha}", key="files")]
        ui_touched = bool(match_paths(process.get("uiFiles") or [], files))
        if not design_only_carry(previous, current, ui_touched):
            return f"#{number}: round since {previous_sha[:7]} is not design-only; nothing carried"
        carry(repo, sha, previous_sha, "design-only round, e2e green", [previous["verify"]])
        return f"#{number}: design-only round since {previous_sha[:7]}; carried verify"
    return f"#{number}: no earlier reviewed commit; nothing carried"


def main() -> None:
    repo = env("REPO")
    sha = env("HEAD_SHA")
    results = [handle(repo, pr) for pr in open_prs_for_sha(repo, sha)] or [f"No open pull request at {sha[:7]}"]
    for line in results:
        log(line)
    summary(["### Status carry", ""] + [f"- {line}" for line in results])


if __name__ == "__main__":
    main()
