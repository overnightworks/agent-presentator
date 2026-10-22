# Runbook: renew a source's secret

Reader: an admin of the instance. Use it when a Git access token or deploy key
is leaked, expired, or unreadable (for example after a restore without
`PRESENTATOR_SECRET_KEY`). The webhook secret has its own
[runbook](renew-a-webhook-secret.md).

## Before you start

Open `/settings/sources/<name>`. Ciphertext this instance's key cannot open is
refused rather than handed to `git`: that source's fetch fails and its decks
stay listed.

## Steps

- HTTPS token: choose Renew and enter the new value. Nothing is shown back.
- SSH deploy key: choose Renew. It immediately replaces the local encrypted
  pair and shows the replacement public key. Register it at the Git host
  before the next fetch; renewal neither contacts that host nor keeps an
  old-key overlap.

## Check

Choose *Fetch now*: the source's state reads reachable.

Related: [add a Git source](add-a-source.md).
