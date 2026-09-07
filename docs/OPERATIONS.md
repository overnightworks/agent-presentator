# Operations

Audience: whoever administers this repository and the machines it runs on.

## Running an instance

One value is required: `PRESENTATOR_SECRET_KEY`, at least 32 bytes. It signs
the session cookie, and without it the process refuses to start. In development
it lives in a gitignored `.env` at the repository root; on the server it comes
from the process environment. A refusal names the setting and what is wrong
with it, never the value it was given, so a mistyped secret does not land in
the startup output.

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

Starting an instance creates the tables it needs, and changes in place what it
finds: a file written before a source was a row of its own keeps its decks and
gains the column naming the source they came from. There is no migration tool
beyond what a start does itself, so an older shape a start cannot upgrade is
still a file to delete and set up again.

The deck source is `PRESENTATOR_SOURCE_URL`, with `PRESENTATOR_SOURCE_REF`
(`main`), `PRESENTATOR_SOURCE_NAME` (`decks`, its name and the name in its hook
address),
`PRESENTATOR_SOURCE_POLL_SECONDS` (`300`) and
`PRESENTATOR_SOURCE_TIMEOUT_SECONDS` (`20`). Without a URL the
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

That configuration is written into the instance's `sources` table: once at
every start and again at the beginning of every refresh, so an instance whose
admin is created after it started carries its source too. The URL identifies
the row, so this writes nothing after the first time. Changing
`PRESENTATOR_SOURCE_URL` therefore adds a second source rather than replacing
the first — removing one is not built yet — and a new URL under the name
another source already answers to is refused with a line in the log, because a
name is unique. There is no page to add or remove a source at yet.

The server polls the source every `PRESENTATOR_SOURCE_POLL_SECONDS` on a task
beside the routes, and never twice at once; opening the deck list reads the
database and pulls nothing. A pull is bounded by
`PRESENTATOR_SOURCE_TIMEOUT_SECONDS`, after which that tick ends without the
source rather than holding the next one. `git` runs with a minimal
environment: terminal prompting off, the global and system git configuration
neutralised, any inherited credential helper cleared, and `ssh` in batch mode
with its own connect timeout — so nothing on the machine can turn a pull into a
wait for an answer nobody will give.

Replacing the secret means changing the environment of the running process, so
today it takes a restart. A rotation path without one is open work on
[ADR 0010](decisions/0010-git-sources-mirror.md), together with the fetch log.

## Building the decks

Every refresh builds the decks whose commit moved. That needs a Node toolchain
on the machine: `pnpm` on `PATH`, and a project whose dependencies are
installed carrying Slidev — this repository's `frontend/`, installed with
`pnpm install --frozen-lockfile` under the Node version its `.nvmrc` names.
`PRESENTATOR_TOOLCHAIN` says where that project is (`frontend`), and
`PRESENTATOR_BUILDS` where the built talks and their PDFs are kept (`builds`).
Each step of a build is bounded by `PRESENTATOR_BUILD_TIMEOUT_SECONDS`
(`300`). The PDF export drives a browser, so the toolchain also needs
`playwright-chromium` installed beside Slidev; without it a deck builds and its
export fails, which leaves the previously delivered talk standing.

Without a toolchain the instance still runs: every build fails, the deck pages
say no talk has been built, and the failure is in the server log.

Builds are kept per deck and per run, and none is ever deleted, so
`PRESENTATOR_BUILDS` grows with every push until the cleanup this defers lands
([#8](https://github.com/overnightworks/agent-presentator/issues/8), line 20).

**A deck is code, and the build is not sandboxed yet.** A deck's own Vue
components run on this machine during the build, as the user the server runs
as. The build's environment carries nothing but `PATH` and `HOME`, so no secret
of this instance is in reach through the environment; anything else that user
can read or reach, a deck's build can too. Until the sandbox lands
([#8](https://github.com/overnightworks/agent-presentator/issues/8), line 14a),
configure only deck sources you would run code from.

## The fetch-now hook

Setting `PRESENTATOR_SOURCE_HOOK_SECRET` opens
`POST /hooks/<PRESENTATOR_SOURCE_NAME>` for that source. That secret is the
only guard on the address, so it is at least 32 characters that are not blank,
generated rather than typed; an empty, blank, or shorter value refuses to
start rather than opening a hook anybody could call:

```sh
export PRESENTATOR_SOURCE_HOOK_SECRET="$(openssl rand -base64 32)"
```

The call carries the secret as `Authorization: Bearer …`, and nothing else: no
payload is read, which is what lets any host or a `post-receive` hook call it
([ADR 0010](decisions/0010-git-sources-mirror.md)). A wrong secret, a missing
one, an unknown source, and any other path under `/hooks/` all answer `404`
with an empty body. Only that one `POST` is open: every other method there
leads to the login like any other address. Without the variable the instance
has no hook address at all, only the poll.

```sh
curl -X POST -H "Authorization: Bearer $PRESENTATOR_SOURCE_HOOK_SECRET" \
  https://<your-address>/hooks/decks
```

That path needs a Cloudflare Access bypass policy — a git host has no browser to
pass the outer door with — while every other address stays behind Access
([ADR 0007](decisions/0007-browser-client-behind-tunnel.md)); the secret is what
guards it instead.

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
