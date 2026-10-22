# Runbook: call the fetch-now hook from a Git host

Reader: an admin wiring a Git host's webhook or `post-receive` hook to an
instance.

Adding a source creates `POST /sources/<name>/fetch` for it. No login stands in
front of a `POST` under `/sources/`, because a git host has no browser to sign
in with; the secret guards it instead
([ADR 0015](../decisions/0015-public-like-songmaker-own-login.md)).

## Before you start

The source's webhook secret. It is generated, shown exactly once to the session
that created the source, stored only as a SHA-256 hash, and never shown again
until [Renew](renew-a-source-secret.md) mints another.

## Steps

Send the secret as `Authorization: Bearer …` or as `X-Gitlab-Token`, and
nothing else; no payload is read, which is what lets any host or a
`post-receive` hook call it
([ADR 0010](../decisions/0010-git-sources-mirror.md)):

```sh
curl -X POST -H "Authorization: Bearer <the-secret-shown-once>" \
  https://<your-address>/sources/decks/fetch
```

## Check

The source's page shows a new run. A wrong secret, a missing one, an unknown
source, a trailing slash, and any other path under `/sources/` all answer
`404` with an empty body. Only that `POST` is open: every other method there
leads to the login like any other address.
