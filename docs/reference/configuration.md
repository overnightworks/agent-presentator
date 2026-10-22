# Configuration reference

Audience: an operator or engineer looking up a setting. Settings are
environment variables with the prefix `PRESENTATOR_`, read from the process
environment and, in development, from a gitignored `.env` at the repository
root. A refusal at start names the setting and what is wrong with it, never
the value it was given, so a mistyped secret does not land in the startup
output.

## Every run names

| Setting | Meaning |
| --- | --- |
| `PRESENTATOR_SECRET_KEY` | At least 32 bytes. `webauth` signs the lobby's session cookie with it, signs the talk host's through a derivation of its own, and, through another, encrypts what a source's row holds ([ADR 0013](../decisions/0013-secrets-at-rest-and-credential-delivery.md)). Without it the process refuses to start. |
| `PRESENTATOR_PUBLIC_ORIGIN` | The one canonical https origin. An `http://` origin refuses the start, because a browser does not send a `Secure` cookie back over plain http. |
| `PRESENTATOR_TALK_ORIGIN` | The talk host's canonical https origin, on another hostname than `PRESENTATOR_PUBLIC_ORIGIN`; talks are served and signed in there ([ADR 0016](../decisions/0016-a-talk-host-signs-in-by-itself.md)). |
| `PRESENTATOR_COPRESENTER_SOCKET`, `PRESENTATOR_SPEECH_SOCKET` | Absolute private Unix socket paths of the co-presenter and the speech service. |
| `PRESENTATOR_OIDC_ISSUER` | The https URL of the Keycloak realm, without a trailing slash; exactly the `issuer` of its discovery document. |
| `PRESENTATOR_OIDC_CLIENT_ID` | The Keycloak client id. |
| `PRESENTATOR_OIDC_CLIENT_SECRET_FILE` | Absolute path of a file holding the client secret, read once at start, minus one trailing newline. |

There is no `/setup` and no first admin: accounts arrive with the Keycloak
sign-in ([sign-in and session](sign-in-and-session.md)).

## Defaults

The value in brackets is what a direct run uses; the container image replaces
some of them.

| Setting | Default | Meaning |
| --- | --- | --- |
| `PRESENTATOR_DATABASE` | `presentator.sqlite3` | The SQLite file. |
| `PRESENTATOR_MIRRORS` | `mirrors` | Where the bare mirrors of the deck sources live. |
| `PRESENTATOR_HOST` | `127.0.0.1` | Listen address. |
| `PRESENTATOR_PORT` | `8000` | Listen port. |
| `PRESENTATOR_TRUSTED_PROXIES` | empty | Comma-separated addresses or networks; see below. |
| `PRESENTATOR_SOURCE_POLL_SECONDS` | `300` | Interval of the source poll. |
| `PRESENTATOR_SOURCE_TIMEOUT_SECONDS` | `20` | Bound of one pull. |
| `PRESENTATOR_TOOLCHAIN` | `frontend` | The project carrying Slidev. |
| `PRESENTATOR_BUILDS` | `builds` | Where built talks and PDFs are kept. |
| `PRESENTATOR_BUILD_TIMEOUT_SECONDS` | `300` | Bound of each build step. |
| `PRESENTATOR_BUILD_MEMORY` | `4g` | Memory bound of a build container. |
| `PRESENTATOR_BUILD_DISK` | `8g` | Filesystem bound of a build container; `none` removes it ([runbook](../runbooks/run-builds-without-a-disk-bound.md)). |
| `PRESENTATOR_BUILD_OUTPUT_MEGABYTES` | `300` | Size bound of the talk a build leaves. |
| `PRESENTATOR_BUILD_RUNNER` | container | `host` builds on this machine instead ([trust boundaries](../explanation/trust-boundaries.md)); `compose.yaml` never sets it. |
| `PRESENTATOR_BUILD_IMAGE`, `PRESENTATOR_BUILD_VOLUME` | none | The image the toolchain stands in and the volume `PRESENTATOR_BUILDS` is a directory of. Compose sets both; an instance missing either does not start and says which. |

`PRESENTATOR_SOURCE_URL`, `_REF`, `_NAME`, `_CREDENTIAL` and `_HOOK_SECRET` are
not settings; leaving them in the environment does nothing, and a line naming
`PRESENTATOR_SOURCE_CREDENTIAL` does nothing in `.env` or in the shell.

## Trusted proxies and the client address

The client address is the ASGI peer. Empty `PRESENTATOR_TRUSTED_PROXIES` is
correct for a direct run. Behind the tunnel of
[ADR 0007](../decisions/0007-browser-client-behind-tunnel.md) the tunnel client
connects from localhost, so that peer is `127.0.0.1` and every request through
the tunnel shares one address. Setting the list to that peer (`127.0.0.1` where
the tunnel client and the server share one loopback) makes the library read `X-Forwarded-For` and `X-Forwarded-Proto`: the per-address
rate limit counts the real client instead of the tunnel, and the fetch-now hook
address the Sources page shows names the https the tunnel client saw, not the
plain http the app itself is served over. A container has a network of its own
and sees that client as its gateway (`172.31.255.1`, a subnet `compose.yaml`
fixes). The guard against another site's form does not depend on this list
([sign-in and session](sign-in-and-session.md)).

## In the container image

The first six rows are a fact of the image's filesystem and network, so the
image sets them and the defaults above do not apply; compose sets the rest.

| Setting | In this image |
| --- | --- |
| `PRESENTATOR_DATABASE` | `/data/database/presentator.sqlite3` |
| `PRESENTATOR_MIRRORS` | `/data/mirrors` |
| `PRESENTATOR_BUILDS` | `/data/builds` |
| `PRESENTATOR_LOCAL_SOURCES_MOUNT` | `/data/local-sources` |
| `PRESENTATOR_TOOLCHAIN` | `/app/frontend` |
| `PRESENTATOR_HOST` | `0.0.0.0`, offered by compose at `127.0.0.1:8000` |
| `PRESENTATOR_BUILD_IMAGE` | `presentator-build`, set by `compose.yaml` |
| `PRESENTATOR_BUILD_VOLUME` | the project's own `builds` volume, derived by `compose.yaml` |
| `PRESENTATOR_COPRESENTER_SOCKET` | `/run/presentator-copresenter/copresenter.sock`, set by `compose.yaml` |
| `PRESENTATOR_SPEECH_SOCKET` | `/run/presentator-speech/speech.sock`, set by `compose.yaml` |

`Dockerfile` builds two images, the first a layer of the second: the Slidev
toolchain with the Chromium its PDF export drives (what a deck's own build
runs in), and the whole instance on top of it. The image carries the CA
certificates an HTTPS source needs to verify the git host's TLS certificate;
CI proves it with a live HTTPS fetch on every build.

Values the compose file needs rather than the process: `PRESENTATOR_DOCKER_GROUP`
(numeric id of the `docker` group), `PRESENTATOR_RUNTIME_UID`,
`COPRESENTER_SOCKET_DIRECTORY`, `SPEECH_SOCKET_DIRECTORY`,
`PRESENTATOR_LOCAL_SOURCES` (only with the override of
[add a folder on this box](../runbooks/add-a-folder-on-this-box.md)), and
`COMPOSE_PROJECT_NAME`, which names the volumes. Compose refuses to start the
service without the secret key, the public origin and the first four of these
values.

## Co-presenter and speech (host processes)

`COPRESENTER_*` and `SPEECH_*` settings of the host processes are named in
[run the speech service and the co-presenter](../runbooks/run-the-co-presenter-on-the-host.md).
