"""Agents block: keep the `## Agents` section of a pull request body up to date
from the agents' marked comments and the process statuses.

Rows already in the table stay as written; a role and model seen in a marked
comment and missing from the table gets a row. The summary line under the
table is rewritten on every run."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from common import (
    PROCESS_STATUSES, api, api_list, env, graphql, latest_statuses, log, open_prs_for_sha,
)

HEADING = "## Agents"
TABLE_HEADER = "| Role | Model | Effort | Tool | Thread |\n| --- | --- | --- | --- | --- |"
SUMMARY_MARK = "<!-- agents-block:summary -->"
# > 🤖 **GPT-6.1 Sol (high), design reviewer, on behalf of someone**
MARK = re.compile(
    r"^>\s*🤖\s*\*\*(?P<model>[^*(]+?)(?:\s*\((?P<effort>[^)]+)\))?,\s*(?P<role>[^*,]+?),\s*on behalf of [^*]+\*\*",
    re.MULTILINE,
)
COMMITS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      commits(last: 100) {
        nodes { commit { status { contexts { context description } } } }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class Agent:
    role: str
    model: str
    effort: str | None
    url: str


def parse_mark(body: str, url: str) -> Agent | None:
    head = "\n".join(body.strip().splitlines()[:4])
    match = MARK.search(head)
    if not match:
        return None
    return Agent(
        role=match["role"].strip(),
        model=match["model"].strip(),
        effort=(match["effort"] or "").strip() or None,
        url=url,
    )


def row_key(role: str, model: str) -> tuple[str, str]:
    return role.strip().lower(), model.strip().lower()


def cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def agent_row(agent: Agent) -> str:
    role = agent.role[:1].upper() + agent.role[1:]
    return f"| {role} | {agent.model} | {agent.effort or '—'} | — | [comment]({agent.url}) |"


def summary_line(statuses: dict[str, dict], rounds: int) -> str:
    parts = [f"Review rounds: {rounds}"] if rounds else []
    for context in PROCESS_STATUSES:
        status = statuses.get(context)
        if status:
            text = f"{context} {status['state']}"
            if status.get("description"):
                text += f" ({status['description']})"
            parts.append(text)
    return " · ".join(parts)


def _trim(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _drop_summary(lines: list[str]) -> list[str]:
    """Remove the previous summary: the marker and the line after it."""
    kept, skip = [], False
    for line in lines:
        if skip:
            skip = False
        elif line.strip() == SUMMARY_MARK:
            skip = True
        else:
            kept.append(line)
    return kept


def rebuild(body: str, agents: list[Agent], statuses: dict[str, dict], rounds: int) -> str:
    """The body with its Agents section updated; other sections untouched."""
    text = (body or "").replace("\r\n", "\n")
    lines = text.split("\n")
    status_line = summary_line(statuses, rounds)
    start = next((i for i, line in enumerate(lines) if line.strip() == HEADING), None)
    if start is None:
        if not agents and not status_line:
            return body
        lines = (_trim(lines) + [""] if text.strip() else []) + [HEADING]
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    section = _trim(_drop_summary(lines[start + 1:end]))

    table = [i for i, line in enumerate(section) if line.lstrip().startswith("|")]
    known = {row_key(*cells(section[i])[:2]) for i in table[2:] if len(cells(section[i])) >= 2}
    new_rows = []
    for agent in agents:
        key = row_key(agent.role, agent.model)
        if key not in known:
            known.add(key)
            new_rows.append(agent_row(agent))

    if table:
        pos = table[-1] + 1
        section[pos:pos] = new_rows
        pos += len(new_rows)
    else:
        if new_rows:
            section += ([""] if section else []) + TABLE_HEADER.split("\n") + new_rows
        pos = len(section)
    if status_line:
        block = ([""] if pos else []) + [SUMMARY_MARK, status_line]
        if section[pos:] and section[pos].strip():
            block.append("")
        section[pos:pos] = block

    tail = lines[end:]
    out = lines[:start + 1] + [""] + _trim(section) + ([""] + tail if tail else [])
    return "\n".join(out)


def collect_agents(repo: str, number: int) -> list[Agent]:
    items = []
    for comment in api_list(f"repos/{repo}/issues/{number}/comments"):
        items.append((comment["created_at"], comment.get("body") or "", comment["html_url"]))
    for review in api_list(f"repos/{repo}/pulls/{number}/reviews"):
        if review.get("submitted_at"):
            items.append((review["submitted_at"], review.get("body") or "", review["html_url"]))
    agents = []
    for _, body, url in sorted(items):
        agent = parse_mark(body, url)
        if agent:
            agents.append(agent)
    return agents


def count_rounds(repo: str, number: int) -> int:
    """Commits that received a review of their own (not a carried one)."""
    owner, name = repo.split("/")
    data = graphql(COMMITS_QUERY, owner=owner, name=name, number=number)
    rounds = 0
    for node in data["repository"]["pullRequest"]["commits"]["nodes"]:
        contexts = ((node["commit"].get("status") or {}).get("contexts")) or []
        if any(c["context"].startswith("review/")
               and not (c.get("description") or "").startswith(("carried from", "exempt:"))
               for c in contexts):
            rounds += 1
    return rounds


def update(repo: str, number: int) -> str:
    pr = api(f"repos/{repo}/pulls/{number}")
    body = pr.get("body") or ""
    new_body = rebuild(body, collect_agents(repo, number),
                       latest_statuses(repo, pr["head"]["sha"]), count_rounds(repo, number))
    if new_body == body:
        return f"#{number}: Agents section already up to date"
    api(f"repos/{repo}/pulls/{number}", method="PATCH", body={"body": new_body})
    return f"#{number}: Agents section updated"


def main() -> None:
    repo = env("REPO")
    number = os.environ.get("PR_NUMBER")
    numbers = [int(number)] if number else [pr["number"] for pr in open_prs_for_sha(repo, env("HEAD_SHA"))]
    for n in numbers:
        log(update(repo, n))
    if not numbers:
        log("No open pull request for this event.")


if __name__ == "__main__":
    main()
