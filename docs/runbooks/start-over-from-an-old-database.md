# Runbook: start over from an old database

Reader: the operator whose instance refuses to start with an
`OldDatabaseError`.

A database file written by an older version (`login_attempts`, or a `users` or
`sessions` table whose columns differ from the current ones, such as a
`sessions` table without `created_at` or without `refresh_token`) is refused at
start, before the file or its `-wal` is written to; a `-shm` may appear. The
same holds for a database from before sources stored secrets themselves, which
also holds the password-era tables.

## Before you start

The order matters. The backup is what lets you return to the old version.

## Steps

1. Stop presentator.
2. Take the backup as [back up an instance](back-up-an-instance.md)
   describes, but leave out the script's final `docker compose start`: the
   service must stay stopped.
3. Move the complete database state aside: the file and its `-wal` and `-shm`
   companions (after an unclean shutdown committed transactions can still sit
   in `-wal`), or the whole database volume.
4. Start the new version. It creates a fresh database.

## Check

The instance starts, creating the tables it needs, and `/login` answers. Decks and sources are re-added in
the fresh database, and the fresh start gets fresh builds and mirrors volumes.
