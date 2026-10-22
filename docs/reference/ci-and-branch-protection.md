# CI and branch protection reference

Audience: a repository administrator looking up what `main` requires and what
the quality gate checks.

## Branch protection

`main` takes a change only through a pull request whose gates are green. A
repository ruleset named `main-protection` owns that, not the legacy per-branch
settings; `gh api repos/overnightworks/agent-presentator/rulesets` reads it,
and GitHub is its one owner. It requires a pull request (squash or rebase
only), linear history, no deletion and no force-push, and required status
checks that include `pr-check` and `quality / verdict`. No bypass actor exists:
an administrator is subject to the same gates. Approvals are set to zero on
purpose: this repository is reviewed by agents before the pull request opens,
not through GitHub review requests.

`pr-check` runs from its own workflow on `opened`, `synchronize`, `reopened`
and `edited`, so a body change after the first run still has to pass the
classification gate. A merge-queue candidate still reports that check: the
workflow answers `merge_group` by holding the name green, because the
classification already ran on the pull request that armed the candidate.

## The quality gate

The `quality` job of `ci.yml` calls the shared gate workflow of
`overnightworks/quality`, pinned by commit SHA. Its `hygiene` job scans the
history for secrets and the workflows for unsafe patterns, its `sonar` job runs
the SonarCloud scan, and its `verdict` job is the one required check
`quality / verdict`: green only when every job named in `quality/config.toml`
(`mandatory_jobs`, the display names of this workflow's jobs) and the gate's
own jobs are green. The `tests` job hands its coverage report to the scan as
the artifact `coverage`. `SONAR_TOKEN` is passed to the gate by name, never
`inherit`. The `quality_gate` property of this repository in the fleet registry
records that `quality / verdict` is expected on `main`; it does not edit the
ruleset.

## SonarCloud

The project runs SonarCloud's built-in "Sonar way" gate. A custom `presentator`
gate exists in the organisation but is not associated, because the bootstrap
token lacks the right to associate one and because a custom gate would buy
nothing here: its coverage condition (90% of new code) is strictly weaker than
this repository's own floor, and its other conditions are identical to the
built-in gate's. The coverage floor and what meets it are owned by
[TESTING.md](../TESTING.md). Setup and repair is
[bootstrap SonarCloud](../runbooks/bootstrap-sonarcloud.md).
