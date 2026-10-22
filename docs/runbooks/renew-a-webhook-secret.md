# Runbook: renew a source's webhook secret

Reader: an admin of the instance. Use it when the secret a Git host sends to
the [fetch-now hook](call-the-fetch-now-hook.md) is leaked or lost.

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
