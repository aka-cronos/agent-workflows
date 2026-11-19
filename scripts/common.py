"""Helpers shared by the workflow scripts: GitHub calls through `gh`, the
project's process file, gitignore-style matching and step output."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from typing import Iterable
from urllib.parse import quote

PROCESS_FILE = ".agents/process.json"
PROCESS_STATUSES = ("verify", "review/spec", "review/design", "review/interrogate", "risk")
REQUIRED_STATUSES = ("verify", "review/spec", "review/design", "risk")
GREEN_CONCLUSIONS = {"success", "neutral", "skipped"}

ZONE_LABELS = {
    "zone:money": ("b60205", "Touches code that moves money"),
    "zone:access": ("d93f0b", "Touches auth, permissions or tokens"),
    "zone:migrations": ("5319e7", "Touches the database schema or migrations"),
    "zone:ui": ("1d76db", "Changes what the user sees"),
}
EXEMPT_LABEL = ("exempt", "c2e0c6", "Dependency update or docs only: no agent review")


class GhError(RuntimeError):
    def __init__(self, args: list[str], code: int, stderr: str):
        super().__init__(f"gh {' '.join(args)} failed ({code}): {stderr.strip()}")
        self.stderr = stderr

    @property
    def not_found(self) -> bool:
        return "HTTP 404" in self.stderr

    @property
    def unprocessable(self) -> bool:
        return "HTTP 422" in self.stderr


def gh(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)
    if result.returncode != 0:
        raise GhError(list(args), result.returncode, result.stderr)
    return result.stdout


def api(path: str, method: str = "GET", body: dict | None = None, accept: str | None = None):
    args = ["api", path, "-X", method]
    if accept:
        args += ["-H", f"Accept: {accept}"]
    if body is not None:
        args += ["--input", "-"]
    out = gh(*args, stdin=json.dumps(body) if body is not None else None)
    if accept and "diff" in accept:
        return out
    return json.loads(out) if out.strip() else None


def api_list(path: str, key: str | None = None) -> list:
    """Every item of a paginated list endpoint. `key` names the array when the
    endpoint wraps it in an object (check runs, compare files)."""
    sep = "&" if "?" in path else "?"
    pages = json.loads(gh("api", f"{path}{sep}per_page=100", "--paginate", "--slurp"))
    items: list = []
    for page in pages:
        items.extend(page[key] if key else page)
    return items


def graphql(query: str, **variables) -> dict:
    args = ["api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{name}={value}"]
    return json.loads(gh(*args))["data"]


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"missing environment variable {name}")
    return value


def load_process(repo: str, ref: str) -> dict | None:
    """The project's process file at `ref`, or None when the project has none."""
    try:
        data = api(f"repos/{repo}/contents/{PROCESS_FILE}?ref={quote(ref, safe='')}")
    except GhError as error:
        if error.not_found:
            return None
        raise
    return json.loads(base64.b64decode(data["content"]))


def match_paths(patterns: Iterable[str], paths: Iterable[str]) -> set[str]:
    """Paths matched by gitignore-style patterns, decided by git itself so the
    syntax (`**`, trailing `/`, `!` negation) is exactly gitignore's."""
    patterns = [p for p in patterns if p and p.strip()]
    paths = [p for p in paths if p]
    if not patterns or not paths:
        return set()
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", tmp], check=True)
        pattern_file = os.path.join(tmp, "patterns")
        with open(pattern_file, "w", encoding="utf-8") as handle:
            handle.write("\n".join(patterns) + "\n")
        result = subprocess.run(
            ["git", "-C", tmp, "-c", f"core.excludesFile={pattern_file}",
             "check-ignore", "--no-index", "--stdin", "-z"],
            input="\0".join(paths) + "\0",
            capture_output=True,
            text=True,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(f"git check-ignore failed: {result.stderr.strip()}")
        return {p for p in result.stdout.split("\0") if p}


def latest_statuses(repo: str, sha: str) -> dict[str, dict]:
    """The newest commit status per context on `sha`."""
    latest: dict[str, dict] = {}
    for status in api_list(f"repos/{repo}/commits/{sha}/statuses"):
        latest.setdefault(status["context"], status)
    return latest


def set_status(repo: str, sha: str, context: str, state: str, description: str,
               target_url: str | None = None) -> None:
    body = {"state": state, "context": context, "description": description[:140]}
    if target_url:
        body["target_url"] = target_url
    api(f"repos/{repo}/statuses/{sha}", method="POST", body=body)


def checks_state(repo: str, sha: str) -> str:
    """`green` when every check run on `sha` finished well, `pending` while one
    still runs, `red` otherwise (also when there is none)."""
    runs = api_list(f"repos/{repo}/commits/{sha}/check-runs", key="check_runs")
    return summarize_checks(runs)


def summarize_checks(runs: list[dict]) -> str:
    if not runs:
        return "red"
    if any(run["status"] != "completed" for run in runs):
        return "pending"
    if all(run["conclusion"] in GREEN_CONCLUSIONS for run in runs):
        return "green"
    return "red"


def open_prs_for_sha(repo: str, sha: str) -> list[dict]:
    pulls = api(f"repos/{repo}/commits/{sha}/pulls") or []
    return [p for p in pulls if p["state"] == "open" and p["head"]["sha"] == sha]


def ensure_label(repo: str, name: str, color: str, description: str) -> None:
    try:
        api(f"repos/{repo}/labels", method="POST",
            body={"name": name, "color": color, "description": description})
    except GhError as error:
        if not error.unprocessable:
            raise


def log(message: str) -> None:
    print(message, flush=True)


def summary(lines: Iterable[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text)
