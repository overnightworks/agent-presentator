# Runbook: back up an instance

Reader: the operator of a containerised instance.

A backup is the database volume and the builds volume. A deck standing at the
commit its talk was built from is never built again, so an instance restored
without the built talks answers not-found for each of them until every deck is
pushed anew. The mirrors are clones and cost a first pull.

## Before you start

Both archives are taken through compose, which knows the project's volumes.
Naming them by hand archives an empty volume of a project that does not exist,
and says it went well. They are taken out of the mount points `/data/database`
and `/data/builds`, which are where those volumes stand whatever `.env` says
the settings are.

## Steps

Neither archive is believed until it has been read back:

```sh
set -euo pipefail
docker compose stop
for volume in database builds; do
  docker compose run --rm --no-TTY presentator tar cz -C "/data/${volume}" . \
    > "${volume}.tar.gz"
done
databases="$(tar tzf database.tar.gz | grep -c 'presentator\.sqlite3$' || true)"
talks="$(tar tzf builds.tar.gz | grep -c 'talk/index\.html$' || true)"
if [ "$databases" -lt 1 ] || [ "$talks" -lt 1 ]; then
  echo "$databases databases and $talks talks in the archives:" \
       "keep the backup before this one" >&2
  exit 1
fi
docker compose start
```

Keep `PRESENTATOR_SECRET_KEY` with the archives. The sources' stored secrets
are encrypted with a derivation of it, so a restored instance without that key
lists its decks and can pull none of them. Environment variables do not
restore a source's secret; [renew it](renew-a-source-secret.md) if it is
unreadable.

## Check

The script ends without its error line and `docker compose start` ran.

Related: [start over from an old database](start-over-from-an-old-database.md).
