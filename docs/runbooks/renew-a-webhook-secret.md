# Runbook: renew a source's webhook secret

Reader: an admin of the instance. Use it when the secret a Git host sends to
the [fetch-now hook](call-the-fetch-now-hook.md) is leaked or lost.

## Before you start

- The source exists.
- You are signed in as an admin of the instance.
- You can update the caller of the hook (the Git host's webhook settings or the
  `post-receive` hook).

Renewing invalidates the old secret at once: the caller fails with `404` until
it holds the new value.

## Steps

1. Open `/settings/sources/<name>` and choose the webhook's own Renew. The new
   value is shown exactly once, to the session that renewed it. The old value
   stops working.
2. Put the new value into the caller of the hook (the Git host's webhook or
   `post-receive` hook).

## Check

Call the hook with the new secret as described in
[call the fetch-now hook](call-the-fetch-now-hook.md): the source's page shows
a new run. The old secret answers `404`.

Related: [renew a source's Git access secret](renew-a-source-secret.md).
