"""Zones: label a pull request with the sensitive zones and UI it touches, and
mark dependency updates and docs-only changes as exempt from agent review."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from urllib.parse import quote

from common import (
    EXEMPT_LABEL, REQUIRED_STATUSES, ZONE_LABELS, api, api_list, ensure_label, env,
    latest_statuses, load_process, log, match_paths, set_status, summary,
)

DOCS_DEFAULTS = [
    "*.md",
    "*.mdx",
    "docs/**",
    "LICENSE*",
    ".github/ISSUE_TEMPLATE/**",
    ".github/PULL_REQUEST_TEMPLATE*",
]
# Instructions for agents change how work is done: never "docs only".
NEVER_DOCS = [
    "AGENTS.md",
    "CLAUDE.md",
    "docs/agents/**",
    ".agents/**",
    ".claude/**",
]
DEPENDENCY_BOT = "dependabot[bot]"

C_STYLE = ("//", "/*", "*", "*/")
HASH_STYLE = ("#",)
COMMENT_PREFIXES = {
    **{ext: C_STYLE for ext in (
        ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".go", ".java",
        ".kt", ".kts", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rs",
        ".scss", ".less", ".php", ".dart", ".scala",
    )},
    ".css": ("/*", "*", "*/"),
    **{ext: HASH_STYLE for ext in (
        ".py", ".sh", ".bash", ".zsh", ".rb", ".yml", ".yaml", ".toml", ".pl", ".r",
    )},
    ".sql": ("--", "/*", "*", "*/"),
}


@dataclass
class ChangedFile:
    path: str
    status: str
    previous_path: str | None = None
    patch: str | None = None


@dataclass
class Result:
    zones: list[str] = field(default_factory=list)
    exempt: str | None = None

    @property
    def labels(self) -> set[str]:
        labels = {f"zone:{zone}" for zone in self.zones}
        if self.exempt:
            labels.add(EXEMPT_LABEL[0])
        return labels


def comment_only(file: ChangedFile) -> bool:
    """True when a modified file's change touches only comments or blank lines.
    Anything unknown (new, deleted or renamed file, binary, no patch, a
    language not listed) counts as a code change."""
    if file.status != "modified" or not file.patch:
        return False
    prefixes = COMMENT_PREFIXES.get(posixpath.splitext(file.path)[1].lower())
    if not prefixes:
        return False
    for line in file.patch.splitlines():
        if line.startswith("@@") or not line[:1] in ("+", "-"):
            continue
        text = line[1:].strip()
        if text and not text.startswith(prefixes):
            return False
    return True


def classify(process: dict, files: list[ChangedFile], author: str,
             commits_by_bot_only: bool) -> Result:
    paths = [f.path for f in files] + [f.previous_path for f in files if f.previous_path]
    docs_patterns = DOCS_DEFAULTS + list(process.get("docsFiles") or [])
    never_docs = match_paths(NEVER_DOCS, paths)
    docs = match_paths(docs_patterns, paths) - never_docs

    result = Result()
    if author == DEPENDENCY_BOT and commits_by_bot_only:
        result.exempt = "dependency update"
    elif files and all(f.path in docs and (not f.previous_path or f.previous_path in docs)
                       for f in files):
        result.exempt = "docs only"

    code_paths = set()
    for f in files:
        if f.path in docs or comment_only(f):
            continue
        code_paths.add(f.path)
        if f.previous_path:
            code_paths.add(f.previous_path)

    zones = dict(process.get("sensitiveZones") or {})
    zones["ui"] = process.get("uiFiles") or []
    for name in ("money", "access", "migrations", "ui"):
        if match_paths(zones.get(name) or [], code_paths):
            result.zones.append(name)
    return result


def fetch_files(repo: str, number: int) -> list[ChangedFile]:
    return [
        ChangedFile(
            path=item["filename"],
            status=item["status"],
            previous_path=item.get("previous_filename"),
            patch=item.get("patch"),
        )
        for item in api_list(f"repos/{repo}/pulls/{number}/files")
    ]


def only_bot_commits(repo: str, number: int) -> bool:
    """Every commit is the bot's, or a merge commit that brings in the base."""
    for commit in api_list(f"repos/{repo}/pulls/{number}/commits"):
        login = (commit.get("author") or {}).get("login")
        if login != DEPENDENCY_BOT and len(commit.get("parents", [])) < 2:
            return False
    return True


def sync_labels(repo: str, number: int, current: set[str], wanted: set[str]) -> None:
    definitions = {**ZONE_LABELS, EXEMPT_LABEL[0]: EXEMPT_LABEL[1:]}
    for name in sorted(wanted - current):
        ensure_label(repo, name, *definitions[name])
    if wanted - current:
        api(f"repos/{repo}/issues/{number}/labels", method="POST",
            body={"labels": sorted(wanted - current)})
    for name in sorted((current & set(definitions)) - wanted):
        api(f"repos/{repo}/issues/{number}/labels/{quote(name, safe='')}", method="DELETE")


def mark_exempt(repo: str, sha: str, reason: str) -> None:
    description = f"exempt: {reason}"
    existing = latest_statuses(repo, sha)
    for context in REQUIRED_STATUSES:
        status = existing.get(context)
        if status and status["state"] == "success" and status["description"] == description:
            continue
        set_status(repo, sha, context, "success", description)


def main() -> None:
    repo = env("REPO")
    number = int(env("PR_NUMBER"))
    pr = api(f"repos/{repo}/pulls/{number}")
    process = load_process(repo, pr["base"]["ref"])
    if process is None:
        log(f"No .agents/process.json on {pr['base']['ref']}: nothing to do.")
        return

    files = fetch_files(repo, number)
    author = pr["user"]["login"]
    bot_only = author == DEPENDENCY_BOT and only_bot_commits(repo, number)
    result = classify(process, files, author, bot_only)

    current = {label["name"] for label in pr["labels"]}
    sync_labels(repo, number, current, result.labels)
    if result.exempt:
        mark_exempt(repo, pr["head"]["sha"], result.exempt)

    zones = ", ".join(result.zones) or "none"
    log(f"zones: {zones}; exempt: {result.exempt or 'no'}")
    summary([
        f"### Zones for #{number}",
        "",
        f"- Zones: {zones}",
        f"- Exempt: {result.exempt or 'no'}",
        f"- Files: {len(files)}",
    ])


if __name__ == "__main__":
    main()
