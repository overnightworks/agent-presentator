# People

Audience: whoever calls or operates the people route. The contract (the body,
the token rules, the order hal follows) is owned by edge's `docs/people.md`;
this page states only what Presentator does with it.

## Erase a person

`DELETE /api/admin/people/{subject}` erases the person whose Keycloak subject
is `{subject}`: their account, sessions, preferences and key draft, and every
source they own with its decks, runs, mirror and builds. `?dry_run=true`
counts and changes nothing. Both answer the `people_erasure` v1 body with
`kinds`, counted by [kind](../../src/presentator/contracts/people.py); the
counts are what was removed in a real run. `affects_others` is always empty.

| Answer | When |
| --- | --- |
| 200 | Erased, previewed, or the subject is unknown (zero counts, no row made). |
| 401 | No token, or any verification failure. |
| 403 | A valid token without the admin group or a group below it. |
| 409 | A refresh or build holds the lock for longer than 60 seconds; nothing was deleted. `Retry-After` is the wait in whole seconds, and `busy` lists the running work. hal's timeout must exceed 60 seconds. |
| 422 | `dry_run` is not a boolean; nothing was deleted. |
| 500 | A mirror or build could not be removed; the person remains and a repeat finishes it. The body's `leftover` counts the source, never names it. |

Every answer carries `Cache-Control: no-store`. A request never creates an
account.

## Export a person

`GET /api/admin/people/{subject}/export` answers a ZIP
(`application/zip`, a file name that names no subject) whose root holds
`export.json` and nothing else. It is the `people_export` v1 body: `app`,
`subject`, `exported_at` (UTC, `Z`, whole seconds), `data`, `files` and
`withheld`. `data` holds every row keyed by the person (account, sessions,
preferences, key draft, sources with their decks and runs) as the
[export records](../../src/presentator/contracts/people.py) show them; `files`
is always empty, because builds and mirrors derive from git.

The secrets are never read into it. `withheld` counts each kind left out under
its own word: `source-credential`, `hook-secret-hash`, `key-draft-private-key`
and `refresh-token`, each with `reason` `secret`. A source address is exported
as stored: the guarantee is that its authority carries no credential part; a
secret typed into a path or query is exported as typed.

An unknown subject, and a person who owns no row, answers 200 with `data`
exactly `{}`, `files` `[]` and `withheld` `[]`; no account is made. The answers
401 and 403 are the erase route's, and every answer carries
`Cache-Control: no-store`. The log line holds the withheld counts and names
nobody.

## Who may call

Only an access token of the instance's Keycloak client, with that client as
its one audience, type `at+jwt`, an `azp` listed in
`PRESENTATOR_ADMIN_TOKEN_CLIENTS` and the admin group or a subgroup. `webauth`
checks it; the cookie counts for nothing. With the setting empty (the
default) neither the gate nor the route exists.

## What is logged

One line per call: the operation, the outcome and the counts. It names neither
the caller nor the person, and the server writes no access log.

## Known gap

Builds that a newer build superseded stay on disk until the deletion of
superseded builds exists (#243), so an erasure does not yet reach them. The
live instance keeps the setting empty until then.
