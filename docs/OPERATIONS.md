# Operations

Audience: whoever administers this repository and the machines it runs on.

## Branch protection

`main` takes a change only through a pull request whose gates are green. A
repository ruleset owns that — not the legacy per-branch settings — and this is
the command that created it. It is reproducible: `POST` refuses a second
ruleset of the same name, so an update sends the same body to
`PUT repos/overnightworks/agent-presentator/rulesets/<id>`, with the id that
`gh api repos/overnightworks/agent-presentator/rulesets` reports.

```sh
gh api --method POST repos/overnightworks/agent-presentator/rulesets --input - <<'JSON'
{
  "name": "main-protection",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {
    "ref_name": {
      "include": ["refs/heads/main"],
      "exclude": []
    }
  },
  "rules": [
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": false,
        "required_reviewers": [],
        "require_code_owner_review": false,
        "dismissal_restriction": {
          "enabled": false,
          "allowed_actors": []
        },
        "require_last_push_approval": false,
        "required_review_thread_resolution": false,
        "require_extra_approval_for_unattributed_changes": true,
        "allowed_merge_methods": ["squash", "rebase"]
      }
    },
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "required_linear_history"},
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          {"context": "Python: architecture, lint, types"},
          {"context": "Python: tests"},
          {"context": "Frontend: lint, types, tests, deck build"},
          {"context": "Secret scan"},
          {"context": "pr-check"},
          {"context": "SonarCloud scan"}
        ]
      }
    }
  ]
}
JSON
```

No bypass actor exists: an administrator is subject to the same gates, and
`main` cannot be deleted at all. Approvals are set to zero on purpose — this
repository is reviewed by agents before the pull request opens, not through
GitHub review requests. Only `squash` and `rebase` are offered because a merge
commit would violate the linear history the same ruleset requires.

`pr-check` runs from its own workflow on `opened`, `synchronize`,
`reopened`, and `edited`, so a body change after the first run still has
to pass the classification gate. A merge-queue candidate still reports
that check: the workflow answers `merge_group` by holding the name
green, because the classification already ran on the pull request that
armed the candidate.

## SonarCloud

The SonarCloud project is created by dispatching the one-off bootstrap
workflow, which also switches Automatic Analysis off:

```sh
gh workflow run sonar-bootstrap.yml --ref main
```

Running it again is harmless; it converges on the same state. Automatic
Analysis and the CI scan are mutually exclusive: while Automatic Analysis is
on, SonarCloud refuses the report the `SonarCloud scan` job uploads.

SonarCloud runs its built-in "Sonar way" gate (80% on new code; custom gates
are a paid feature). The coverage floor and what meets it are owned by
[TESTING.md](TESTING.md).
