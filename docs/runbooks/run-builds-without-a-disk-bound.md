# Runbook: run builds without a disk bound

Reader: the operator whose instance refuses to start naming the storage
driver.

The disk bound of a build is Docker's `--storage-opt size`, which some storage
drivers take and others refuse: overlay2 takes it over xfs with project quotas
and refuses it over ext4. At every start the server makes and starts one
container of its own, bounded like a build's and running nothing, and takes it
down; if the daemon refuses that container the instance refuses to start,
naming the driver.

## Before you start

Accept the cost: with `none`, nothing but the time a step may take bounds what
a build writes beside its talk, and the talk bound
([build limits](../reference/builds.md)) is what stands between a deck and this
machine.

## Steps

Say so in as many words:

```sh
printf 'PRESENTATOR_BUILD_DISK=none\n' >> .env
```

Only that word does it. A value that came out blank is refused at the start,
because an interpolation nobody watched is not a decision. Then
`docker compose up -d`.

## Check

The log says at every start that nothing but the time a step may take bounds
what a build writes beside its talk. The same start also refuses a daemon that
does not carry the build image at all, so an instance that could build no deck
says so before it serves one.
