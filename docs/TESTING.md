# Testing

Audience: a human or agent proving a change — which layer's test is the proof,
what the 100% floor requires, and what a review rejects.

The operator's standing testing and coding conventions own the rules. This file
states their consequences for this repository. It does not restate layer
ownership, picture ownership, or how `main` is protected.

## Which layer proves what

A test sits at the layer that owns the behaviour
([ADR 0001](decisions/0001-enforced-layers.md)). A lower layer does not stand
in for a higher one.

| Layer | Proves | How |
| --- | --- | --- |
| `contracts`, `application` | Pure logic, including the run state machine | Direct calls. Fakes at the ports. No key, no browser, no subprocess. |
| `ports` | A protocol | It is a protocol; it is not proven by a port suite. Application tests against a fake of that port prove it. Tests do not invent the port. |
| `adapters` | The real dependency, in a temporary form | A tmp SQLite file; a tmp bare git repository; a `pnpm` and a `docker` the test wrote, on PATH, for the build adapter. A fixture sits next to the test that reads it. |
| `api` | Routes and wire schemas | FastAPI `TestClient` driving the real routes with the real application and fake or tmp adapters. |
| Template | Lobby HTML as the surface of a route | A ruled person-sentence is proven by a delegated agent driving the real interface. A route or fragment is FastAPI `TestClient` on the real route. A direct Jinja unit test is not the proof. |
| `host` | Composition | Once: start the app with a test configuration. |
| Slidev addon | That Slidev resolves this package; later, components | Vitest driving Slidev's own resolver, as `frontend/tests/addon-package.test.ts` already does. Component tests once components exist. |
| Surface | A person does X at the lobby | A delegated agent driving the real interface at the widths the dispatch brief names, taken from the picture at [mockups/README.md](mockups/README.md). One flow per ruled sentence. A unit test is not this proof. |

`gitmirror` is proven the same way as an adapter: against a tmp bare repository,
with no presentation code in the loop.

Core tests import no adapter. The first adapter import moves that module to the
integration suite. A remaining exception is one registered module marker, never
a path allowlist.

## The 100% floor

Every line and every branch is reached. The floor is owned by `fail_under` in
`pyproject.toml` and by Vitest coverage thresholds. It is met only by useful
tests, each pinning one observable behaviour.

A hard line — one that talks to a process, a file, a network, or a composition
choice — is reached in one of three ways:

1. A fake at the port, for contract and application tests.
2. A failing or succeeding dependency in a temporary form, for adapter tests.
3. A test configuration for the composition root.

An unreachable line is removed.

## Naming and shape

The test name states the behaviour (`the composition root refuses to run while
it is empty`, not `test_main`). One behaviour per test. A family that differs
only in data is one parametrized test. Builders and fixtures own arrangement.
Tests reuse production contracts; they do not re-implement construction,
serialization, or path logic to calculate the expected result.

Assert outcomes: the return value, the effect on state, or the error raised. No
private fields, no mock call counts, no pinned log strings, unless the
interaction itself is the contract. No sleeps, no timing races, no order
dependence, no process-global state another test can observe.

## Per-behaviour cost

Watch the cost per behaviour, not a ratio of test lines to source lines.
Parametrize and fixture rather than copy. A review rejects:

- a test that exists to touch a line
- a test that re-implements production logic to check it
- a unit test standing in for a surface proof

## Local runs and the CI gate

Local is targeted. CI is the full run and the gate. Never a full suite, a
coverage run, or a Playwright suite on this machine unless the operator asked.

Python, named modules or a behaviour:

```sh
uv run --locked pytest tests/test_host.py -q --tb=short
uv run --locked pytest -k "composition root refuses" -q --tb=short
```

Frontend, a named file, from `frontend/`:

```sh
pnpm exec vitest run tests/addon-package.test.ts
```

Never `pnpm test` of the whole tree locally.

CI on the pull request is the gate. What `main` requires is
[OPERATIONS.md](OPERATIONS.md). What the workflow runs is
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml). Coverage collection
is `--cov=src` for Python and Vitest `--coverage` for the frontend.
The floor those reports must meet is the configuration above, not a number
restated here.

## Probe stack and browser proofs

Every UI change is proven by a delegated agent driving the real interface at
the widths the brief names. The picture those widths are taken from is
[mockups/README.md](mockups/README.md).

Targeted browser proofs, live Docker probe stacks, and targeted E2E take
`/tmp/probe-stack.lock` (`flock`, shared across repositories). Wait for the
lock; do not skip the proof, and never point at the operator's live stack.
Drive only the flow of the slice, at the named widths. A test that starts a
server or process owns its end; remove the worktree only after those processes
stop.
