# Runbook: add a folder on this box as a source

Reader: the operator of a containerised instance whose talks live on the same
machine.

A folder becomes a source by being a bare git repository under a host directory
mounted read-only into the instance at the fixed path `/data/local-sources`.
Everything under the mount is trusted as the operator: its repositories, their
metadata (a `gitdir` file, `objects/info/alternates`, a symlink), and everyone
who can write there. This is not a door for a repository somebody else
controls; a stranger's repository still needs pushing over SSH to this box.

## Before you start

`compose.yaml` cannot name the mount cleanly for an operator who wants none of
it (an unset variable would bind the whole checkout in its place), so it stays
out of the checked-in file.

## Steps

1. Beside `compose.yaml`, create `compose.override.yaml`, which
   `docker compose up` merges on its own:

   ```yaml
   services:
     presentator:
       volumes:
         - ${PRESENTATOR_LOCAL_SOURCES}:/data/local-sources:ro
   ```

2. Name the host directory in `.env` as `PRESENTATOR_LOCAL_SOURCES`.
3. Make the folder a bare repository under it:

   ```sh
   git init --bare --initial-branch main "$PRESENTATOR_LOCAL_SOURCES/talks.git"
   git -C ~/talks init --initial-branch main
   git -C ~/talks remote add box "$PRESENTATOR_LOCAL_SOURCES/talks.git"
   ```

4. The mirror runs as `presentator`, uid 1001, with its global and system git
   configuration neutralised, so it can never read a `safe.directory` exception
   from a file: git refuses a repository not owned by that uid outright as
   "dubious ownership", whatever its mode bits say. Hand the repository to uid
   1001 and your own group, so both keep write access, and tell your own git it
   may open a directory it does not own. Do this once per repository, after
   `git init --bare` made it, never before:

   ```sh
   sudo chown -R 1001:"$(id -g)" "$PRESENTATOR_LOCAL_SOURCES/talks.git"
   sudo chmod -R u+rwX,g+rwX "$PRESENTATOR_LOCAL_SOURCES/talks.git"
   git config --global --add safe.directory "$PRESENTATOR_LOCAL_SOURCES/talks.git"
   ```

5. Restart the service (`docker compose up -d`), then add the source under
   Settings · Sources with `/data/local-sources/talks.git` as its file address,
   "on this box" as the access kind, and no secret; one is refused there.

## Check

*Check connection* answers reachable. An address resolved and decoded the way
git itself opens one (following a symlink, an encoded or literal `..`) must
still stand under the mount, or it is refused the same way, when it is added
and at every fetch after. A later change reaches the instance as for every
other source: `git -C ~/talks push box main`, then the next poll or *Fetch now*
takes in what moved.
