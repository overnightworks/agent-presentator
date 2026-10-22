# Runbook: clear leftover co-presenter turn homes

Reader: the operator of the machine that runs the co-presenter.

Each turn's home lives below `COPRESENTER_CLI_WORKING_ROOT`; the service tries
to remove it when the turn ends. When removal fails, for example because the
log says a turn's process group is not confirmed gone, it tries again when the
service stops in an orderly way. If that fails too, or after a crash, SIGKILL
or OOM, the home stays.

## Steps

1. Stop `presentator-copresenter.service`; systemd's cgroup kill ends the old
   processes.
2. Remove what is left under `COPRESENTER_CLI_WORKING_ROOT`.
3. Start the service.

## Check

`COPRESENTER_CLI_WORKING_ROOT` holds no turn home, and a question on stage is
answered.
