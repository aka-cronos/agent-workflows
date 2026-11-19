# agent-workflows

Reusable GitHub workflows for a pull request process where coding agents implement, verify, review and grade changes. None of them runs a model: they do the mechanical part of the line, so agents and people never repeat it by hand.

| Workflow | What it does | Triggered by |
| --- | --- | --- |
| [`zones.yml`](.github/workflows/zones.yml) | Labels the PR with the sensitive zones and UI it touches; marks dependency updates and docs-only PRs as exempt | `pull_request` |
| [`carry.yml`](.github/workflows/carry.yml) | Carries process statuses to a new commit after a clean rebase or a design-only round | `workflow_run` of CI |
| [`low-risk-merge.yml`](.github/workflows/low-risk-merge.yml) | Squash-merges PRs graded low risk, once the project turns its lane on | `status`, `workflow_run` |
| [`agents-block.yml`](.github/workflows/agents-block.yml) | Keeps the `## Agents` section of the PR body up to date from marked comments and statuses | `issue_comment`, `pull_request_review`, `status` |

## Use it in a project

1. Add `.agents/process.json` to the project (below).
2. Copy the caller files from [`examples/`](examples/) into the project's `.github/workflows/`. In `agent-carry.yml` and `agent-low-risk-merge.yml`, list the project's CI workflows by their `name:`.
3. Add `agent-low-risk-merge.yml` only when you turn the lane on: until then every run would end without merging.

Callers pin a major version (`@v1`). Each workflow checks out its own scripts at the ref in its `workflows-ref` input, which defaults to `v1`; when you pin anything else, pass the same ref there.

## Project data: `.agents/process.json`

Read from the PR's base branch (the default branch for the merge), so a PR cannot change the rules it is judged by.

```json
{
  "version": 1,
  "uiFiles": ["src/routes/**", "src/components/**", "src/**/*.css"],
  "sensitiveZones": {
    "money": [],
    "access": ["src/server/auth/**"],
    "migrations": ["migrations/"]
  },
  "docsFiles": [],
  "lowRiskLane": false
}
```

| Field | Used by | Meaning |
| --- | --- | --- |
| `uiFiles` | zones, carry | Globs whose change the owner must see on screen |
| `sensitiveZones` | zones | Globs per zone: `money`, `access`, `migrations`. An empty list means the project has none |
| `docsFiles` | zones | Optional. Globs added to the default docs list |
| `lowRiskLane` | low-risk merge | Nothing merges unless it is `true` |

Globs use gitignore syntax and are matched by `git check-ignore`, so `**`, a trailing `/` and `!` negation behave exactly as in `.gitignore`.

A project without `.agents/process.json` is left alone.

## Statuses and labels

The process uses these commit statuses. Agents set them; these workflows read them and, in the cases below, set them.

| Context | Set by | Meaning |
| --- | --- | --- |
| `verify` | The verifier | Every acceptance criterion proven on this commit |
| `review/spec`, `review/design` | The reviewers | `success` approves this commit, `failure` asks for changes |
| `review/interrogate` | The interrogator | Only on PRs that touch a sensitive zone |
| `risk` | The risk grader | `success` once graded; the description starts with the grade: `low`, `medium` or `high` |

Labels set by `zones.yml`, created on first use:

| Label | When |
| --- | --- |
| `zone:money`, `zone:access`, `zone:migrations` | A code change in that zone |
| `zone:ui` | A change in `uiFiles` |
| `exempt` | A dependency update or a docs-only PR |

Labels change only when the result changes.

### Zones

A file counts toward a zone only when it changes code. Docs files never count, and neither does a modified file whose changed lines are all comments or blank lines in a language the script knows (C-style, `#`-style and SQL comments). A new, deleted, renamed or binary file always counts.

### Exempt pull requests

- **Dependency update**: opened by `dependabot[bot]` and every commit is the bot's (merge commits that bring in the base branch are allowed).
- **Docs only**: every changed file matches the docs list: `*.md`, `*.mdx`, `docs/**`, `LICENSE*`, `.github/ISSUE_TEMPLATE/**`, `.github/PULL_REQUEST_TEMPLATE*`, plus `docsFiles`. Agent instructions are never docs: `AGENTS.md`, `CLAUDE.md`, `docs/agents/**`, `.agents/**`, `.claude/**`.

An exempt PR gets the `exempt` label and `verify`, `review/spec`, `review/design` and `risk` set to `success` with the description `exempt: dependency update` or `exempt: docs only`, so a ruleset that requires them still lets it merge after CI. Nobody needs to bypass the ruleset.

### Carry

Runs when a workflow listed in the caller finishes, and acts only when every check run on the PR's head commit is green.

- **Clean rebase**: the last force push replaced commit A with the head, and the PR's own change (its diff against the base, compared with `git patch-id --stable`) is identical. Every process status A had and the head lacks is copied, described as `carried from <sha>: clean rebase`.
- **Design-only round**: a normal push after commit A, the latest commit with a `review/spec` status. When A had `verify` and `review/spec` in `success` and the commits since A touch no `uiFiles`, `verify` is copied as `carried from <sha>: design-only round, e2e green`. The reviews run again on the new commit.

Anything else carries nothing: the agents run again.

### Low-risk merge

On every run it checks each open PR against the default branch and merges (squash, pinned to the head commit) only when all of these hold:

- `lowRiskLane` is `true`.
- The PR is not a draft and has no `zone:*` or `exempt` label.
- Every check run on the head commit is green.
- `verify`, `review/spec`, `review/design` and `risk` are `success`, and `review/interrogate`, when present, too.
- The `risk` description starts with `low`.

### Agents block

Agents mark every comment they post with a first line like:

```md
> [!NOTE]
> 🤖 **Model name (effort), role, on behalf of owner**
```

The workflow reads those marks from PR comments and reviews. Rows already in the `## Agents` table stay as written; a role and model that has no row yet gets one, linking to its first comment. Under the table it rewrites one summary line, after an HTML comment marker: review rounds (commits with a review of their own), then each process status on the head commit with its description. A PR without an `## Agents` section gets one at the end of its body.

## Limits

- Statuses and labels set with `GITHUB_TOKEN` do not start other workflows. That is why the merge listens to `workflow_run` of the carry too, and why a merge made with `GITHUB_TOKEN` starts no workflow on the merge commit; pass `merge_token` to change that.
- Dependabot runs get a read-only token unless the caller grants permissions, as the examples do. PRs from forks get a read-only token: zones cannot label them.
- `carry` looks at the last 100 commits of a PR; a diff too large for the GitHub API carries nothing.

## License

[MIT](LICENSE)
