# Sources reference

Audience: an engineer or operator looking up what a source is, what is
accepted, and how it is kept current. Tasks: [add a Git
source](../runbooks/add-a-source.md), [renew a secret](../runbooks/renew-a-source-secret.md),
[add a folder on this box](../runbooks/add-a-folder-on-this-box.md),
[call the fetch-now hook](../runbooks/call-the-fetch-now-hook.md).

## The row

A source is a row: an id, a name and a URL that are each unique, the ref it
follows, and the account that owns it. An instance starts with no source; an
admin adds one under Settings · Sources.

- The name is `[a-z0-9][a-z0-9-]{0,63}` and unique. The URL is unique and must
  not carry a password in its userinfo (that belongs in the Secret field). A
  user name in the URL is optional: when the URL names one the mirror keeps it,
  and when it does not the mirror answers a user name of its own so a host that
  insists on one still reaches the token.
- Of remote addresses only `https://[user@]host[:port]/…`,
  `ssh://[user@]host[:port]/…` and `user@host:path` are an access: the host a
  plain name or an IPv6 literal in brackets, an `ssh://` path free of `%`, `?`,
  `#`, `[` and `]`, an scp path free of `[` and `]`. Every other remote
  spelling, `http://` and any control character included, is refused before
  any secret is used.
- The access kind is derived from the URL scheme: `https://` is a token;
  `git@` and `ssh://` are a deploy key. An unknown radio value, a URL that
  names no access, or a mismatch with the chosen radio is refused. A fourth
  kind reads no secret: a bare repository under `/data/local-sources`, added
  with a `file://` or bare-path address, "on this box" as the access kind and
  no secret (one is refused). An address resolved the way git opens one
  (symlink, encoded or literal `..`) must stay under the mount, when added and
  at every fetch.
- HTTPS needs a read-only token and the image's CA certificates. SSH uses the
  owned deploy-key draft Add shows, for registration at the Git host.
  *Check connection* probes that exact token or draft (or path) before
  anything is stored, and *Create* is refused unless that exact check
  succeeded.
- The secret is stored encrypted in the source's row, the only place it stands,
  opened with a key derived from `PRESENTATOR_SECRET_KEY` and read at every
  pull ([ADR 0013](../decisions/0013-secrets-at-rest-and-credential-delivery.md)).
  Ciphertext this instance's key cannot open is refused rather than handed to
  `git`: that source's fetch fails and its decks stay listed. A source row
  always carries its encrypted secret; private halves and HTTPS tokens are
  never shown.
- Opening Add with the SSH radio mints this admin an ed25519 deploy key, or
  shows the one already minted for them today, as a `source_key_drafts` row
  carrying the public half in clear and the private half encrypted the same
  way; *Create* binds it to the new source and deletes the draft, and a draft
  nobody creates a source from is swept a day after it was minted.

## Keeping it current

`gitmirror` keeps a bare mirror of each source's repository under
`PRESENTATOR_MIRRORS` by driving `git` as a subprocess
([ADR 0010](../decisions/0010-git-sources-mirror.md)); nothing is ever checked
out, and the tree is read at one commit. A pull runs with a minimal
environment: terminal prompting off, the global and system git configuration
neutralised, any inherited credential helper cleared, and `ssh` in batch mode
with its own connect timeout, so nothing on the machine can turn a pull into a
wait for an answer nobody will give. An SSH fetch writes the private half to a
0600 file in a 0700 directory it removes in a `finally`, and runs `ssh`
against it alone (`IdentitiesOnly=yes`), with the host key pinned on first
contact (`StrictHostKeyChecking=accept-new`) and refused if it ever changes.
The mirror of a local folder runs as `presentator`, uid 1001, so the
repository it opens must be owned by that uid outright, or git refuses it as
"dubious ownership".

The server polls every source every `PRESENTATOR_SOURCE_POLL_SECONDS` on a
task beside the routes, never twice at once, and starts the next tick after the
bound ends a slow one. A pull is bounded by
`PRESENTATOR_SOURCE_TIMEOUT_SECONDS`, so an unreachable source costs one poll
that bound and no more. Opening the deck list reads the database and pulls
nothing. A refresh walks the sources one after another, taking each one in and
building its decks before it reads the next.

`POST /sources/<name>/fetch` with the source's own secret does the same at
once. It reads no payload, so every git host and a `post-receive` hook are the
same caller. The secret is accepted from `Authorization: Bearer` or
`X-Gitlab-Token`, stored only as a SHA-256 hash, and compared in constant
time. A wrong secret, a missing one, an unknown source, a trailing slash, and
any other path under `/sources/` answer alike (404, empty body). Only that POST
is open; GET and every other method under `/sources/` stay behind the login. A
flood of calls collapses into the one refresh that runs at a time.

## Runs and state

Every poll of a source, reachable or not, records one run: the moment, whether
it reached the source, and either the commit it found or a typed reason it did
not, never the raw error a git command left behind. The table keeps the newest
three runs per source. The Sources list shows the newest run's state: never
fetched; reachable; refused when the host turns the login down; failed when
the host answered with anything else; unreachable only when the host never
answers; or Error when a credential could not be resolved, never the internal
reason why.

## The source's page

`/settings/sources/<name>` shows state and fetched age, *Fetch now* (which
refreshes that one source), the HTTPS secret as fixed dots with one Renew, or
the SSH deploy-key public half with Copy and direct Renew, the webhook address
with Copy and its own Renew, the newest three runs, and its decks. SSH Renew
replaces the local pair immediately, leaves the replacement public half ready
to register, and neither updates the Git host nor keeps an old-key overlap;
HTTPS renewal takes a new value and shows nothing back; webhook renewal shows
its new value exactly once to the session that created or renewed it.

Remove, at the bottom, asks once for a confirm naming the source's decks and
runs, then deletes the source row, its runs, its decks, its mirror, and every
built talk those decks left on disk, so the same URL can be added again
straight after as a new source with a fresh secret. Once a talk was pointed
at, only this takes it off disk.
