# ADR 0017: A build worker runs each step sealed, one life per step

Audience: humans and agents working on how a deck is built, on the Docker
socket the instance holds today, or on the worker's container.

- Status: ACCEPTED 2026-10-22
- Date: 2026-10-22
- Decision authority: the design of
  [#243](https://github.com/overnightworks/presentator/issues/243) (design B)
  and the slice [#431](https://github.com/overnightworks/presentator/issues/431)
- Neighbours: [ADR 0014](0014-toolchain-owns-build-dependencies.md) owns what
  the toolchain installs; [trust boundaries](../explanation/trust-boundaries.md)
  owns why a deck is code

## Context

A deck is code, and today the instance builds it by asking the host's Docker
daemon for a container. The socket that allows this is root on the host, and
the instance faces the internet.

## Decision

A separate service, the build worker, runs the steps instead. It runs the
instance image with the command `presentator-worker`, and it is sealed: root
with the runtime user's group as PID 1, `cap_drop: ALL` with only `SETUID`,
`SETGID` and `KILL` added, `no-new-privileges`, a read-only root, no network,
a private IPC namespace, a process bound, a memory bound whose swap equals it,
no `env_file`, no `init`, no healthcheck and `restart: always`. Its writable
places are tmpfs mounts. The image is the one the instance service builds; the
worker never pulls and has no `build:` of its own.

The controller (PID 1) parses nothing. For each connection it starts a wire
child as `presentator-wire` (10002) and, for a step, the build as
`presentator-build` (10001), each with no groups and no capabilities. A life
serves one step and ends; Docker restarts the service, and the next life
replaces the stale socket.

- **The socket volume** is a tmpfs that starts `0750` owned by root, so that a
  container mounting it cannot plant an entry before the controller tightens
  the directory itself. It is mounted `nocopy`. It outlives the worker's
  restarts only while another container holds it.
- **`stop_signal: SIGKILL`.** PID 1 receives only the signals it has a handler
  for ([pid_namespaces(7)](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)),
  and the controller installs none, so `docker stop`'s SIGTERM would do nothing
  until the kill timeout. The controller keeps no state worth flushing, and the
  caller maps a reply without END to a failure.
- **The image's entrypoint is cleared (`entrypoint: []`).** The base image's
  entrypoint is a shell that exports variables of its own into the controller's
  environment, which the self-check refuses. The environment predicate admits
  the keys the image sets and two that are not the image's: `HOSTNAME`, which
  the runtime sets, and `LC_CTYPE`, which Python sets itself when it coerces
  the C locale.
- **Proof from the sealed deployment.** The worker serves only when its
  self-check finds it sealed, so the CI job "Build worker: sealed step" starts
  the service from the built image and drives it over the real socket as the
  runtime user, through the codec's client half.
- **Assumptions about the host.** The default Docker seccomp profile and
  AppArmor profile are in force (the job reads both), and the image's glibc is
  2.36 or later, because the controller reaches `pidfd_open` through ctypes.

## Consequences

- Nothing calls the worker until the instance is wired to its socket. A
  `docker compose up -d` on a host starts an idle worker; it opens no port and
  reaches nothing.
- The instance keeps the Docker socket until it is wired to the worker.
