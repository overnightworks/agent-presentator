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

```sh
export PRESENTATOR_SECRET_KEY="$(openssl rand -base64 48)"
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

An instance starts with no source. An admin adds one under Settings · Sources
with a name, a Git URL, HTTPS token access, and the read-only secret. The name
is lowercase letters, digits and hyphens, at most 64 characters, unique; the
URL is unique too, and must not carry a password in its userinfo — that belongs
in the Secret field. The access kind is derived from the URL scheme (`https://`
is a token; `http://` is refused; `git@` and `ssh://` are a deploy key, not yet
offered on the form). An HTTPS source needs the image's CA certificates to
verify the git host's TLS certificate; the image carries them, and CI proves
it with a live HTTPS fetch on every build. The secret is stored encrypted in
the source's row and read with the instance key at every pull. Ciphertext
this instance's key cannot open is refused rather than handed to `git`: that
source's fetch fails and its decks stay listed. Keep `PRESENTATOR_SECRET_KEY`
with the database backup — the file alone restores no working source.

A source that still names only an environment variable — a row written before
this instance stored secrets itself — stays listed and keeps its decks. Fetch
fails until the operator opens that source's page and presses *Renew secret*.
`PRESENTATOR_SOURCE_URL`, `_REF`, `_NAME`, `_CREDENTIAL` and `_HOOK_SECRET` are
not settings; leaving them in the environment does nothing.

The source's own page is `/settings/sources/<name>`: state and fetched age,
Fetch now, the access secret as a fixed run of dots with Renew, the webhook
address with Copy and its own Renew, the newest three runs with the commit as
evidence, and the decks that come from here. Renewing the access secret takes a
new value and shows nothing back. Renewing the webhook secret shows the new
value exactly once, to the session that renewed it.

The server polls every source every `PRESENTATOR_SOURCE_POLL_SECONDS` (`300`)
on a task beside the routes, and never twice at once; opening the deck list
reads the database and pulls nothing. A pull is bounded by
`PRESENTATOR_SOURCE_TIMEOUT_SECONDS` (`20`), after which that tick ends without
the source rather than holding the next one. `git` runs with a minimal
environment: terminal prompting off, the global and system git configuration
neutralised, any inherited credential helper cleared, and `ssh` in batch mode
with its own connect timeout — so nothing on the machine can turn a pull into a
wait for an answer nobody will give.

## Running it as a container

`Dockerfile` builds one image: the packaged server, the Slidev toolchain it
spawns, and the Chromium that toolchain exports a PDF with. `compose.yaml`
starts it. Five of the settings this file names are not a deployment's choice
inside a container but a fact of the image's own filesystem and network, so the
image sets them and their defaults elsewhere here do not apply:

| Setting | In this image |
| --- | --- |
| `PRESENTATOR_DATABASE` | `/data/database/presentator.sqlite3` |
| `PRESENTATOR_MIRRORS` | `/data/mirrors` |
| `PRESENTATOR_BUILDS` | `/data/builds` |
| `PRESENTATOR_TOOLCHAIN` | `/app/frontend` |
| `PRESENTATOR_HOST` | `0.0.0.0`, offered by compose at `127.0.0.1:8000` |

Overriding one of the three paths in `.env` moves that state out of its volume,
which is how an instance loses what it writes; every other setting is the
operator's as before.

`PRESENTATOR_SECRET_KEY` is the only value a fresh instance must be given, and
compose refuses to start the service without it, naming it. Everything else is
optional and reaches the container through `.env` beside `compose.yaml` — a
file this repository never writes and git never sees.
`PRESENTATOR_SOURCE_CREDENTIAL` is not a setting, so a line that names it does
nothing in `.env` or in the shell. A value exported in the shell reaches
compose itself, but the container is handed nothing but that file and the key.

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
`mirrors` and `builds`, under the name of the directory compose runs in.
`docker compose down` keeps them, and the next `up` finds the accounts, the
sources, the decks and the talks that were built, with nothing built again.
`docker compose down -v` deletes them, which is the one command that loses an
instance.

An upgrade is the new tree, the image again, and the service again; a start
changes the tables it finds in place, as above:

```sh
git pull && docker compose build && docker compose up -d
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
of it — a restored instance without that key lists its decks and can pull none
of them. A row that still names an environment variable is not restored by
`.env`; it needs *Renew secret* on the source page.

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

**A deck is code, and the build is not sandboxed yet.** A deck's own Vue
components run on this machine during the build, as the user the server runs
as. The build's environment carries nothing but `PATH` and `HOME`, so no secret
of this instance is in reach through the environment; anything else that user
can read or reach, a deck's build can too. Until the sandbox lands
([#8](https://github.com/overnightworks/agent-presentator/issues/8), line 14a),
configure only deck sources you would run code from.

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

## The co-presenter on stage

The voice that answers beside a talk is two processes on the machine that holds
the GPU, not part of the instance: the speech service on `127.0.0.1:8090`
(`speech/`, never exposed) and the co-presenter on `127.0.0.1:3040`
(`copresenter/`), which proxies hearing and asks Claude through the installed
`claude` executable with the operator's own login. Speech starts first and is
ready when `GET /health` says both models are; the co-presenter is pointed at
the deck folder it should know, at that speech address, and at the one origin it
answers. Both run as `systemctl --user` units so they outlive the shell that
started them, which needs `loginctl show-user <user> -p Linger` to say
`Linger=yes`.

```sh
# speech/, then copresenter/, each as a user unit with these values
SPEECH_HOST=127.0.0.1 SPEECH_PORT=8090            uv run presentator-speech
COPRESENTER_HOST=127.0.0.1 COPRESENTER_PORT=3040 \
COPRESENTER_SPEECH_URL=http://127.0.0.1:8090 \
COPRESENTER_DECK=<the deck folder> \
COPRESENTER_ALLOWED_ORIGIN=https://presentator.hallucinai.de \
  uv run copresenter
```

The overlay rides in the deck: `global-bottom.vue` next to `slides.md` mounts
`components/CoPresenter.vue` and sets `window.COPRESENTER_URL` to the address
the browser can reach, `https://` there giving `wss://` for the hearing socket.
The deck is the only place that names it, so a shared talk URL cannot point the
overlay — and with it the microphone — at another host. The service refuses to
start unless `COPRESENTER_ALLOWED_ORIGIN` is one canonical origin, and every
route and the hearing socket refuse a call whose `Origin` is missing or
different, `/who` included. That address is public through the same tunnel as
the instance, one more ingress entry above the `http_status:404` catch-all, and
one proxied DNS record:

```
  - hostname: copresenter.hallucinai.de
    service: http://localhost:3040
```

`Origin` is a guard against other web pages, not authentication: a browser
cannot forge it, but any native client can, so the gate keeps a second site out
and nothing else. The co-presenter has no login of its own, so whoever reaches
that name spends the operator's Claude session and the card. A Cloudflare Access
policy on the hostname is what prevents that, and the ingress entry comes out
again after the talk.

## The fetch-now hook

Adding a source creates `POST /sources/<name>/fetch` for that source and shows
its webhook secret exactly once, to the session that created it. The secret is
generated, shown on that screen, stored only as a SHA-256 hash, and never
shown again until Renew mints another. The call carries the secret as `Authorization: Bearer …` or as
`X-Gitlab-Token`, and nothing else: no payload is read, which is what lets any
host or a `post-receive` hook call it
([ADR 0010](decisions/0010-git-sources-mirror.md)). A wrong secret, a missing
one, an unknown source, a trailing slash, and any other path under `/sources/`
all answer `404` with an empty body. Only that `POST` is open: every other
method there leads to the login like any other address.

```sh
curl -X POST -H "Authorization: Bearer <the-secret-shown-once>" \
  https://<your-address>/sources/decks/fetch
```

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

The `Container image` job that proves the image still builds is not in that
list yet. Adding it is a ruleset change of its own, sent as the same body
through the `PUT` above; until then a red image build does not hold a merge.

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
