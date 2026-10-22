# Runbook: create or renew the shared Claude token

Reader: the operator of the machine that runs the co-presenter.

The shared token file belongs to every app on the machine, not to the
co-presenter. The unit needs no `UnsetEnvironment=` line for the login, and old
CLI copies under `~/.claude` can be purged.

## Before you start

The directory `~/.secrets/agent-service/claude/` exists with mode `0700`.

## Steps

1. Create it once: run `claude setup-token` and write the one line to `token`
   in that directory, file mode `0600`.
2. To renew, write `token.new` in that directory and run
   `mv -T token.new token`.

## Check

Ask one question on stage; the answer arrives. Only the admin role may use the
co-presenter.
