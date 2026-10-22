# About trust boundaries

Audience: an engineer or operator asking why the instance guards what it
guards, and what each guard costs.

## A deck is code

A deck's Vue components and global layers run in its build, so the author of a
deck can run code. An instance therefore builds every deck in a container of
its own: no network, every capability dropped, no log kept by Docker, none of
the server's environment, only the deck's tree and its own output directory
([ADR 0005](../decisions/0005-deck-folder-and-slidev.md); the exact bounds are
[builds](../reference/builds.md)). No seccomp or AppArmor profile of this
repository's own stands behind that: with no network, no capability and
nothing of this host mounted, Docker's default profiles already deny what there
is to deny.

The price is the socket. `compose.yaml` mounts `/var/run/docker.sock` into the
instance and puts the server in the `docker` group, so the server may ask the
daemon for any container this machine could run: an authority wider than the
one it uses, and the reason `PRESENTATOR_DOCKER_GROUP` exists. A machine whose
decks are all the operator's own can trade it the other way with
`PRESENTATOR_BUILD_RUNNER=host`, which keeps the socket out of the container
and runs every deck's code as the server, with its rights over every file and
the network. An instance serving a repository somebody else can push to does
not.

## A source on this box is trusted as the operator

A folder under the local-sources mount is read with the operator's trust: its
repositories, their metadata (a `gitdir` file, `objects/info/alternates`, a
symlink) and everyone who can write there. It is the door for the operator's
own folder, not for a repository somebody else controls.

## The co-presenter shares the operator's authority

The co-presenter's socket has no authentication layer of its own; filesystem
access under the shared UID is its only authority. The Presentator container,
every host process under that operator UID, and root can therefore spend the
operator's Claude login and card. Untrusted workloads do not belong under that
authority. Only readiness, question, PCM, transcript and answer events cross
the private boundary; browser identity and credentials do not.

## The webhook has a secret, not a login

A git host has no browser to sign in with, so the sessionless
`POST /sources/<name>/fetch` is guarded by a per-source secret and reads no
payload ([ADR 0015](../decisions/0015-public-like-songmaker-own-login.md)).

## The admin routes take a token, not a login

`/api/admin/` is for hal, the operator's tool, and takes an access token of
Keycloak alone: a session cookie counts for nothing there, so a stolen or
forged browser session cannot erase a person. Which clients may call it is
configuration, and an instance that names none serves no admin route at all.
The server keeps no access log, because the path of such a call carries the
person's Keycloak subject. See [people](../reference/people.md).
