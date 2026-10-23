# Runbook: start an instance in a container

Reader: the operator of this machine's deployment.

## Before you start

- Docker Engine 26 or newer (API 1.45). An older daemon refuses the start and
  names the version.
- A Keycloak client ([configure Keycloak](configure-keycloak.md)).
- Private directories you own for the two sockets, for example under
  `/run/user/<uid>`.
- One machine runs one such instance: `compose.yaml` fixes the network subnet
  so the trusted proxy is an address to write down, and a second project from
  this file would ask for the same subnet.

## Steps

1. Build both images: `docker compose --profile images build`. Without the
   profile only the instance is built.
2. Write `.env` beside `compose.yaml` (a file this repository never writes and
   git never sees). Compose refuses to start the service without these six and
   names each one missing:
   - `PRESENTATOR_SECRET_KEY`: a fresh random value of at least 32 bytes.
   - `PRESENTATOR_PUBLIC_ORIGIN`: the canonical https origin.
   - `PRESENTATOR_TALK_ORIGIN`: the talk host's canonical https origin, another
     hostname that reaches the same listener.
   - `PRESENTATOR_DOCKER_GROUP`: the numeric id of this machine's `docker`
     group, `getent group docker | cut -d: -f3`, because the server asks the
     daemon for a container for every build and a group id is not a name.
   - `PRESENTATOR_RUNTIME_UID`: the operator's uid.
   - `SPEECH_SOCKET_DIRECTORY`: the private directory of the speech service's
     socket, which you own.

   The `hal_agent_presentator` network must exist: the edge creates it, and the instance
   joins it to reach the agent service. The instance also needs the sign-in settings
   ([settings](../reference/configuration.md)) and, in this deployment:

   - `PRESENTATOR_TRUSTED_PROXIES=172.31.255.1`: `cloudflared`
     ([ADR 0007](../decisions/0007-browser-client-behind-tunnel.md)) enters
     through the published port, so the container sees it as the gateway of
     its own network, never as `127.0.0.1`.
3. If the daemon's storage driver refuses a disk bound, also
   [run builds without a disk bound](run-builds-without-a-disk-bound.md).
4. Start it: `docker compose up -d`. Only `127.0.0.1:8000` is published.

Everything else in `.env` is optional. A value exported in the shell reaches
compose itself, but the container is handed nothing but that file and the key.
Overriding `PRESENTATOR_DATABASE`, `PRESENTATOR_MIRRORS` or `PRESENTATOR_BUILDS`
in `.env` moves that state out of its volume, which is how an instance loses
what it writes.

## Check

After any change to the subnet, read what the running container's gateway
really is; it must equal `PRESENTATOR_TRUSTED_PROXIES`:

```sh
docker compose ps -q presentator | xargs docker inspect \
  --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
```

Without the trusted proxy the instance answers every request against the
connection it has: http, and one client address for the whole tunnel. The form
guard still passes a real browser, which sends `Sec-Fetch-Site` regardless of
the proxy. An instance without a source runs and lists nothing.

Three named volumes hold what has to survive, `database`, `mirrors` and
`builds`, each under the project's name (the directory compose runs in, unless
`COMPOSE_PROJECT_NAME` says otherwise). `docker compose down` keeps them and
the next `up` finds the accounts, sources, decks and built talks, with nothing
built again. `docker compose down -v` deletes them: the one command that
loses an instance.

Related: [upgrade](upgrade-an-instance.md), [back up](back-up-an-instance.md).
