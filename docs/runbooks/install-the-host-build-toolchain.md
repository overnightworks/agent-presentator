# Runbook: install the build toolchain on the host

Reader: an operator whose instance builds on the host
(`PRESENTATOR_BUILD_RUNNER=host`, see
[run from a checkout](run-from-a-checkout.md)). The container image carries
its own toolchain; skip this there.

## Before you start

- `pnpm` on `PATH`, and the Node version `frontend/.nvmrc` names.
- `PRESENTATOR_TOOLCHAIN` points at the project carrying Slidev (default
  `frontend`).

## Steps

1. In `frontend/`, run `pnpm install --frozen-lockfile`.
2. Fetch the PDF export's browser once, as the user the server runs as:
   `pnpm exec playwright install chromium`. It stays under that user's home
   directory.

## Check

Push a deck change and open its page: the build ends ready. Without the
toolchain every build fails and the deck pages say no talk has been built; the
failure is in the server log. Without the browser a deck builds and its export
fails, which leaves the previously delivered talk standing.

Related: [what a deck may build with](../reference/builds.md).
