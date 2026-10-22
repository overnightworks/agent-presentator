# Runbook: run an instance from a checkout

Reader: a developer on a machine whose decks are all their own.

A deck is code, so an instance normally builds every deck in a container of
its own and refuses to start where it cannot
([trust boundaries](../explanation/trust-boundaries.md)). A run from a checkout
is the one place that says otherwise: `PRESENTATOR_BUILD_RUNNER=host` executes
a deck's Vue components as the server process, with its rights over every file
and the network. `compose.yaml` never sets it. Do not use it for a repository
somebody else can push to.

## Before you start

- An https public origin and a front that terminates it. Served over plain
  http the instance shows the signed-out pages only, because a browser does
  not send a `Secure` cookie back over http, so a local sign-in needs an https
  front.
- A Keycloak client set up as in
  [configure Keycloak](configure-keycloak.md), and its secret in a file.
- The build toolchain on this machine
  ([install it](install-the-host-build-toolchain.md)).
- A gitignored `.env` at the repository root may carry the values instead of
  the shell.

## Steps

```sh
export PRESENTATOR_SECRET_KEY="$(openssl rand -base64 48)"
export PRESENTATOR_PUBLIC_ORIGIN=https://presentator.example.test
export PRESENTATOR_OIDC_ISSUER=https://auth.example.test/realms/example
export PRESENTATOR_OIDC_CLIENT_ID=presentator
export PRESENTATOR_OIDC_CLIENT_SECRET_FILE=/run/user/"$(id -u)"/agent-presentator/oidc-client-secret
export PRESENTATOR_COPRESENTER_SOCKET=/run/user/"$(id -u)"/agent-presentator/copresenter.sock
export PRESENTATOR_BUILD_RUNNER=host
uv run agent-presentator
```

## Check

Open the public origin: a signed-out visitor sees `/login`. A refusal at start
names the setting and what is wrong with it, never the value.

Related: [settings](../reference/configuration.md).
