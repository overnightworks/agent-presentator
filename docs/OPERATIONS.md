# Operations

Audience: whoever administers this repository and the machines it runs on.

## Running an instance

One value is required: `PRESENTATOR_SECRET_KEY`, at least 32 bytes. It signs
the session cookie, and without it the process refuses to start. In development
it lives in a gitignored `.env` at the repository root; on the server it comes
from the process environment.

```sh
export PRESENTATOR_SECRET_KEY="$(openssl rand -base64 48)"
uv run agent-presentator
```

The rest carries defaults and varies by deployment: `PRESENTATOR_DATABASE` (the
SQLite file, `presentator.sqlite3`), `PRESENTATOR_MIRRORS` (where the bare
mirrors of the deck sources live, `mirrors`), `PRESENTATOR_HTTPS` (marks the
session cookie `Secure`, off), `PRESENTATOR_HOST` (`127.0.0.1`) and
`PRESENTATOR_PORT` (`8000`). An empty instance offers `/setup` once, to create
the admin; from then on that page is closed.

Starting an instance creates the tables it needs. There is no migration path
yet, so a database file written by an older version of the code is deleted and
the instance set up again rather than upgraded.

The deck source is `PRESENTATOR_SOURCE_URL`, with `PRESENTATOR_SOURCE_REF`
(`main`) and `PRESENTATOR_SOURCE_TIMEOUT_SECONDS` (`20`). Without a URL the
instance runs and its deck list stays empty. A private remote adds
`PRESENTATOR_SOURCE_CREDENTIAL`, which holds the *name* of the environment
variable carrying the read-only secret, never the secret:

```sh
export PRESENTATOR_SOURCE_URL="https://token-user@git.example/decks.git"
export PRESENTATOR_SOURCE_CREDENTIAL="DECKS_TOKEN"
export DECKS_TOKEN="…"
```

The user name belongs in the URL, because only the operator knows which name
the host expects beside a token.

Opening the deck list pulls the source, so a pull runs while someone waits. It
is bounded by `PRESENTATOR_SOURCE_TIMEOUT_SECONDS`, after which the list renders
without that source rather than holding the request. `git` runs with a minimal
environment: terminal prompting off, the global and system git configuration
neutralised, any inherited credential helper cleared, and `ssh` in batch mode
with its own connect timeout — so nothing on the machine can turn a pull into a
wait for an answer nobody will give.

Replacing the secret means changing the environment of the running process, so
today it takes a restart. A rotation path without one is open work on
[ADR 0010](decisions/0010-git-sources-mirror.md), together with polling, the
webhook, and the fetch log.

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

The project runs SonarCloud's built-in "Sonar way" gate. A custom
`presentator` gate exists in the organisation but is not associated, because
the bootstrap token lacks the right to associate one and because a custom
gate would buy nothing here — its coverage condition (90% of new code) is
strictly weaker than this repository's own floor, and its other conditions
are identical to the built-in gate's. The coverage floor and what meets it
are owned by [TESTING.md](TESTING.md).
