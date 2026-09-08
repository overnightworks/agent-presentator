# Operations

Audience: whoever administers this repository and the machines it runs on.

## Running an instance

One value is required: `PRESENTATOR_SECRET_KEY`, at least 32 bytes. `webauth`
signs the session cookie with it, and, through a derivation of its own,
encrypts what a source's row holds ([ADR 0013](decisions/0013-secrets-at-rest-and-credential-delivery.md));
without it the process refuses to start. In development it lives in a gitignored
`.env` at the repository root; on the server it comes from the process
environment. A refusal names the setting and what is wrong with it, never the
value it was given, so a mistyped secret does not land in the startup output.
Five refused logins for one name inside five minutes, and five failures from
one address inside five minutes regardless of name, are throttled; the page
says the same sentence it says for a wrong password. A session lasts twelve
idle hours and slides forward on every request; logging out deletes the row.

A deck is code, so an instance builds every deck in a container of its own and
refuses to start where it cannot ("Building the decks"). A run from a checkout,
on a machine whose decks are all your own, is the one place that says otherwise:

```sh
export PRESENTATOR_SECRET_KEY="$(openssl rand -base64 48)"
export PRESENTATOR_BUILD_RUNNER=host
uv run agent-presentator
```

The rest carries defaults and varies by deployment. Every value in brackets
below is what a direct run uses; the container image replaces some of them, and
"Running it as a container" lists which. `PRESENTATOR_DATABASE` (the
SQLite file, `presentator.sqlite3`), `PRESENTATOR_MIRRORS` (where the bare
mirrors of the deck sources live, `mirrors`), `PRESENTATOR_HTTPS` (marks the
session cookie `Secure`, off), `PRESENTATOR_HOST` (`127.0.0.1`),
`PRESENTATOR_PORT` (`8000`), and `PRESENTATOR_TRUSTED_PROXIES` (empty: a
comma-separated list of addresses or networks). An empty instance offers
`/setup` once, to create the admin; from then on that page is closed.

The address budget keys on the ASGI peer. Empty `PRESENTATOR_TRUSTED_PROXIES`
is correct for a direct run. Behind the tunnel of
[ADR 0007](decisions/0007-browser-client-behind-tunnel.md) the tunnel client
connects from localhost, so that peer is `127.0.0.1` and every login through
the tunnel shares one budget. Set `PRESENTATOR_TRUSTED_PROXIES` to that peer
(`127.0.0.1`) so the library reads `X-Forwarded-For` and `X-Forwarded-Proto`.
Without it no form is accepted at all: the browser sends `Origin: https://…`
while the app sees the tunnel connection as http, so the origins never match.
The list is required for the product to work behind the tunnel, not only for
a sharper budget. `127.0.0.1` is the answer for a direct run, where the tunnel
client and the server share one loopback; a container has a network of its own
and sees that same client as its gateway, which the section after this one
names.

Starting an instance creates the tables it needs, and changes in place what it
finds: a file written before a source was a row of its own keeps its decks and
gains the column naming the source they came from, one written before a source
could hold its own secret gains that column, and one written before a source
carried a webhook-secret hash gains that column. There is no migration tool
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

That variable is one of the two forms a source's row can anchor. The other is
the encrypted column the row itself carries, which the page for adding a source
fills; a row carrying it is read with the instance key at every pull, and
the environment variable is what a row that has none still names. Ciphertext
this instance's key cannot open is refused rather than handed to `git`: that
source's fetch fails and its decks stay listed. Keep
`PRESENTATOR_SECRET_KEY` with the database backup — the file alone restores no
working source.

That configuration is written into the instance's `sources` table: once at
every start and again at the beginning of every refresh, so an instance whose
admin is created after it started carries its source too. The URL identifies
the row, so this writes nothing after the first time. Changing
`PRESENTATOR_SOURCE_URL` therefore adds a second source rather than replacing
the first — removing one is not built yet — and a new URL under the name
another source already answers to is refused with a line in the log, because a
name is unique. Adding a source on the Settings page writes a new row and
does not rewrite the seeded source's credential.

An admin adds a source under Settings · Sources with a name, a Git URL, HTTPS
token access, and the read-only secret. The name is lowercase letters, digits
and hyphens, at most 64 characters, unique; the URL is unique too, and must
not carry a password in its userinfo — that belongs in the Secret field. The
access kind is derived from the URL scheme (`https://` is a token; `http://` is
refused; `git@` and `ssh://` are a deploy key, not yet offered on the form).
The secret is stored encrypted. SSH deploy keys are not offered yet. An HTTPS
source needs the image's CA certificates to verify the git host's TLS
certificate; the image carries them, and CI proves it with a live HTTPS fetch
on every build.

The server polls the source every `PRESENTATOR_SOURCE_POLL_SECONDS` on a task
beside the routes, and never twice at once; opening the deck list reads the
database and pulls nothing. A pull is bounded by
`PRESENTATOR_SOURCE_TIMEOUT_SECONDS`, after which that tick ends without the
source rather than holding the next one. `git` runs with a minimal
environment: terminal prompting off, the global and system git configuration
neutralised, any inherited credential helper cleared, and `ssh` in batch mode
with its own connect timeout — so nothing on the machine can turn a pull into a
wait for an answer nobody will give.

Replacing the secret of an environment-configured source means changing the
environment of the running process, so today it takes a restart. A rotation
path without one is open work on
[ADR 0010](decisions/0010-git-sources-mirror.md), together with the fetch log.

## Running it as a container

`Dockerfile` builds two images, and the first is a layer of the second: the
Slidev toolchain with the Chromium it exports a PDF with, which is what a
deck's own build runs in, and the whole instance on top of it. `compose.yaml`
starts the instance and builds both:

```sh
docker compose --profile images build
```

Without that profile only the instance is built, and the sandbox image stays as
it was; "Building the decks" says what it is for. Five of the settings this
file names are not a deployment's choice inside a container but a fact of the
image's own filesystem and network, so the image sets them and their defaults
elsewhere here do not apply, and compose adds the two the sandbox is:

| Setting | In this image |
| --- | --- |
| `PRESENTATOR_DATABASE` | `/data/database/presentator.sqlite3` |
| `PRESENTATOR_MIRRORS` | `/data/mirrors` |
| `PRESENTATOR_BUILDS` | `/data/builds` |
| `PRESENTATOR_TOOLCHAIN` | `/app/frontend` |
| `PRESENTATOR_HOST` | `0.0.0.0`, offered by compose at `127.0.0.1:8000` |
| `PRESENTATOR_BUILD_IMAGE` | `agent-presentator-build`, set by `compose.yaml` |
| `PRESENTATOR_BUILD_VOLUME` | the project's own `builds` volume, derived by `compose.yaml` |

Overriding one of the three paths in `.env` moves that state out of its volume,
which is how an instance loses what it writes; every other setting is the
operator's as before.

`PRESENTATOR_SECRET_KEY` and `PRESENTATOR_DOCKER_GROUP` are the values a fresh
instance must be given, and compose refuses to start the service without either
of them, naming it. The second is the numeric id of this machine's `docker`
group — `getent group docker | cut -d: -f3` — because the server asks the
daemon for a container for every build and a group id is not a name.
Everything else is optional and reaches the container through `.env` beside
`compose.yaml` — a
file this repository never writes and git never sees, and the only channel that
carries the variable `PRESENTATOR_SOURCE_CREDENTIAL` names, because only the
operator knows what it is called. A value exported in the shell reaches compose
itself, but the container is handed nothing but that file and the key.

The second value this deployment needs is the trusted proxy. `cloudflared`
([ADR 0007](decisions/0007-browser-client-behind-tunnel.md)) runs on this
machine and enters through the published port, so the container sees it as the
gateway of its own network, never as `127.0.0.1`. `compose.yaml` fixes that
network's subnet so the address is one to write down rather than one Docker
numbered by chance — which also means one machine runs one such instance, since
a second project from this file would ask for the same subnet:

```sh
printf 'PRESENTATOR_SECRET_KEY=%s\n' "$(openssl rand -base64 48)" >> .env
printf 'PRESENTATOR_TRUSTED_PROXIES=%s\n' "172.31.255.1" >> .env
printf 'PRESENTATOR_SOURCE_URL=%s\n' "https://git.example/decks.git" >> .env
docker compose up -d
```

Without that line the instance answers every request against the connection it
has — http, and one login budget for the whole tunnel — so a browser's
`Origin: https://…` matches nothing and no form is accepted. After a change to
the subnet, what the running container's gateway really is:

```sh
docker compose ps -q presentator | xargs docker inspect \
  --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
```

An instance without a source runs and lists nothing; the settings above say
what each further variable does and what a refused one costs. The first start
offers `/setup` at `http://127.0.0.1:8000/setup` once, to create the admin.

Three named volumes hold what has to survive the container: `database`,
`mirrors` and `builds`, each under the name of the project — the directory
compose runs in, unless `COMPOSE_PROJECT_NAME` says otherwise. The server is
handed that same derived name for the builds volume, because it gives it to the
daemon for every build, so one machine can carry a second instance without the
two building into each other.
`docker compose down` keeps them, and the next `up` finds the accounts, the
sources, the decks and the talks that were built, with nothing built again.
`docker compose down -v` deletes them, which is the one command that loses an
instance.

An upgrade is the new tree, the image again, and the service again; a start
changes the tables it finds in place, as above:

```sh
git pull && docker compose --profile images build && docker compose up -d
```

A backup is the database volume and the builds volume: a deck standing at the
commit its talk was built from is never built again, so an instance restored
without the built talks answers not-found for each of them until every deck is
pushed anew. The mirrors are clones and cost a first pull. Both archives are
taken through compose, which knows the project's volumes — naming them by hand
archives an empty volume of a project that does not exist, and says it went
well — and out of the mount points `/data/database` and `/data/builds`, which
are where those volumes stand whatever `.env` says the settings are. Neither
archive is believed until it has been read back:

```sh
set -euo pipefail
docker compose stop
for volume in database builds; do
  docker compose run --rm --no-TTY presentator tar cz -C "/data/${volume}" . \
    > "${volume}.tar.gz"
done
databases="$(tar tzf database.tar.gz | grep -c 'presentator\.sqlite3$' || true)"
talks="$(tar tzf builds.tar.gz | grep -c 'talk/index\.html$' || true)"
if [ "$databases" -lt 1 ] || [ "$talks" -lt 1 ]; then
  echo "$databases databases and $talks talks in the archives:" \
       "keep the backup before this one" >&2
  exit 1
fi
docker compose start
```

Those files restore nothing by themselves. `PRESENTATOR_SECRET_KEY` belongs
with them, because the sources' stored secrets are encrypted with a derivation
of it, and so does every value an environment-configured source names through
`PRESENTATOR_SOURCE_CREDENTIAL` — a restored instance whose `.env` is gone
lists its decks and can pull none of them.

## Building the decks

Every refresh builds the decks whose commit moved. That needs a Node toolchain
on the machine: `pnpm` on `PATH`, and a project whose dependencies are
installed carrying Slidev — this repository's `frontend/`, installed with
`pnpm install --frozen-lockfile` under the Node version its `.nvmrc` names.
A deck may name the official themes that project already carries — `default`,
`seriph`, `apple-basic`, `bricks`, and `shibainu`; any other theme is a
decision for the toolchain, not the deck.
`PRESENTATOR_TOOLCHAIN` says where that project is (`frontend`), and
`PRESENTATOR_BUILDS` where the built talks and their PDFs are kept (`builds`).
Each step of a build is bounded by `PRESENTATOR_BUILD_TIMEOUT_SECONDS`
(`300`). The PDF export drives a browser: `playwright-chromium` is one of that
project's dependencies, and the browser itself is fetched once with `pnpm exec
playwright install chromium`, which keeps it under the home directory of the
user the server runs as — the two things the container image does for itself.
Without that browser a deck builds and its export fails, which leaves the
previously delivered talk standing.

Without a toolchain the instance still runs: every build fails, the deck pages
say no talk has been built, and the failure is in the server log.

Builds are kept per deck and per run, and none is ever deleted, so
`PRESENTATOR_BUILDS` grows with every push until the cleanup this defers lands
([#8](https://github.com/overnightworks/agent-presentator/issues/8), line 20).

**A deck is code, and its build runs in a container of its own.** That is what
an instance does, and it takes two names to do it: `PRESENTATOR_BUILD_IMAGE`,
the image the toolchain stands in, and `PRESENTATOR_BUILD_VOLUME`, the volume
`PRESENTATOR_BUILDS` is a directory of. Compose sets both. An instance missing
either of them does not start, and says which. The one way to build on this
machine instead is `PRESENTATOR_BUILD_RUNNER=host`, where a deck's Vue
components execute as the server process, with its rights over every file and
the network: that is a development run on a machine whose decks are all your
own, and `compose.yaml` never sets it.

Docker Engine 26 or newer is required (API 1.45), because giving a container
one directory of a volume is what keeps a build out of every other deck's
talk. An instance whose daemon is older refuses to start and names the version;
the client is pinned to it besides, so a daemon swapped underneath a running
instance refuses the call rather than mounting more than it was asked for.

Every step of a build is then a container the server asks this machine's daemon
for: no network at all, every capability dropped, no new privilege from a
setuid program, and none of this server's environment — no key, no source
secret, not even the variables it was started with. What the build can reach of
this machine's filesystem is two directories of the builds volume: the deck's
own tree, read-only, and the directory that run writes its talk and its PDF
into, neither of them named after anything a deck's author chose. Everything
else it writes is the container's own and goes with it. It runs as the image's
unprivileged user. A deck that reads a file it does not carry, or opens a
connection, fails the build with the toolchain's own words on the deck page,
and the talk that stood before it keeps standing. No seccomp or AppArmor
profile of this repository's own stands behind that: with no network, no
capability and nothing of this host mounted, Docker's default profiles already
deny what there is to deny. The container's own root is left writable because
Slidev's PDF export writes its dependency cache inside the toolchain it runs
from; that filesystem is the image's and lives as long as the one build.

What one build may take is bounded on every side it has:

| Bound | Setting | What happens at it |
| --- | --- | --- |
| Time, per step | `PRESENTATOR_BUILD_TIMEOUT_SECONDS` (`300`) | The step is killed and its container taken down; the page says the build was given up on |
| Memory | `PRESENTATOR_BUILD_MEMORY` (`4g`) | The kernel ends the build, and the toolchain's own words say so |
| Processes | 512, a constant | The container may spawn no more |
| Its own filesystem | `PRESENTATOR_BUILD_DISK` (`8g`) | Writing past it fails inside the build |
| The talk it leaves | `PRESENTATOR_BUILD_OUTPUT_MEGABYTES` (`300`) | The step still running is stopped there, in this server's own words on the page, and what it wrote is taken away |
| The files that talk holds | 100 000, a constant | The same, in the same words |

The talk is added up while the build runs, not only when it ends, because a
machine is filled long before a build is over; a step that has written past
either bound does not get to finish. Nothing in a run's own directory is
followed while that happens: a link a build leaves counts as the name it is,
never as what it points at, and adding up stops at the first entry this server
cannot read rather than passing over it.

The filesystem bound is Docker's `--storage-opt size`, which some storage
drivers take and others refuse — overlay2 takes one over xfs with project
quotas and refuses it over ext4. Which this machine is is not guessed from a
name: at every start the server runs one bounded container that does nothing,
and if the daemon refuses it the instance refuses to start, naming the driver.
On such a machine — overlay2 over ext4 is the common one — the way to run is to
say so, by leaving `PRESENTATOR_BUILD_DISK` empty in `.env`:

```sh
printf 'PRESENTATOR_BUILD_DISK=\n' >> .env
```

Then nothing but the time a step may take bounds what a build writes beside its
talk, the log says so at every start, and the talk bound above is what stands
between a deck and this machine. The same start also refuses a daemon that does
not carry the build image at all, so an instance that could build no deck says
so before it serves one.

The price is the socket. `compose.yaml` mounts `/var/run/docker.sock` into the
instance and puts the server in the `docker` group, so the server may ask the
daemon for any container this machine could run — an authority wider than the
one it uses, and the reason `PRESENTATOR_DOCKER_GROUP` exists. A machine whose
decks are all the operator's own can trade it the other way with
`PRESENTATOR_BUILD_RUNNER=host`, which keeps the socket out of the container
and runs every deck's code as the server; an instance serving a repository
somebody else can push to does not.

**A talk authored for another Slidev setup needs work before it builds here.**
Proven by pushing the operator's own March 2026 talk through a deployed
instance ([#71](https://github.com/overnightworks/agent-presentator/issues/71)):
a deck folder carries no dependencies of its own, so a theme, addon, or
plugin the talk's original `package.json` installed builds only if
`frontend/` already carries it too — the operator's talk named
`@slidev/theme-seriph`, one of the official themes above, so once that theme
landed the build reached the deck's own content. A slide deck's own Vue
components and global layers (`global-bottom.vue` and its kind) run again in
this build, so a component wired to a service the deck does not bring with
it — the operator's talk carried an AI overlay calling a chat backend from
the tool it was written for — has to be stripped from the deck folder before
pushing, because this instance has nowhere for it to call. A remote asset
named in the frontmatter, such as a background image fetched by URL at build
time, has to become a local file committed into the deck folder and named by
a relative path instead — this instance's build never gets network, sandbox
or not. With the theme resolved, the overlay stripped and the background
made local, the operator's talk built, served all sixteen slides in the
projector and presenter view at desktop and mobile widths, and exported a
sixteen-page PDF.

## The fetch-now hook

Adding a source creates `POST /sources/<name>/fetch` for that source and shows
its webhook secret exactly once. The secret is generated, shown on that
screen, stored only as a SHA-256 hash, and never shown again. The call carries
the secret as `Authorization: Bearer …` or as `X-Gitlab-Token`, and nothing
else: no payload is read, which is what lets any host or a `post-receive` hook
call it ([ADR 0010](decisions/0010-git-sources-mirror.md)). A wrong secret, a
missing one, an unknown source, a trailing slash, and any other path under
`/sources/` all answer `404` with an empty body. Only that `POST` is open:
every other method there leads to the login like any other address. A source
that still comes from the environment has no webhook secret until it is added
on the page; the poll still fetches it.

```sh
curl -X POST -H "Authorization: Bearer <the-secret-shown-once>" \
  https://<your-address>/sources/decks/fetch
```

`PRESENTATOR_SOURCE_HOOK_SECRET` is still read at start — an empty, blank, or
shorter-than-32-character value refuses to start — but it no longer opens an
address; the per-source secret the created screen shows does.

That path needs a Cloudflare Access bypass policy for `/sources/*` — a git host
has no browser to pass the outer door with — while every other address stays
behind Access ([ADR 0007](decisions/0007-browser-client-behind-tunnel.md)); the
secret is what guards it instead. Do not bypass `/settings/sources`.

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
          {"context": "Container image"},
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
